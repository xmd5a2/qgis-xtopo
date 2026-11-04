import hashlib
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import geofabrik, overpass_batch, osmium_tool, pbf_source
from .overpass_client import OverpassClient, build_query
from .resources import read_query


# Сколько запросов Overpass выполняем одновременно.
OVERPASS_WORKERS = 6

# Сколько простых запросов объединяем в один пакетный запрос.
OVERPASS_BATCH_SIZE = 40


class SourceError(RuntimeError):
    pass


class _CancelProxy:
    def __init__(self, *events):
        self.events = [event for event in events if event is not None]

    def is_set(self):
        return any(event.is_set() for event in self.events)


@dataclass
class SourceResult:
    files: dict = field(default_factory=dict)
    dataset: object = None
    warnings: list = field(default_factory=list)
    # Время, потраченное на сеть: скачивание карт, ответы Overpass.
    # Оно показывается в хронометраже отдельно от подготовки источника.
    download_seconds: float = 0.0


class _DownloadTimer:
    """Суммирует время сетевых запросов (загрузки карт и ответов Overpass)."""

    def __init__(self):
        self.total = 0.0
        self._lock = threading.Lock()

    def add(self, seconds):
        with self._lock:
            self.total += seconds

    @contextmanager
    def measure(self):
        started = time.monotonic()
        try:
            yield
        finally:
            self.add(time.monotonic() - started)


def _cache_dir(config, bbox):
    key = hashlib.sha1(bbox.as_csv().encode("utf-8")).hexdigest()[:12]
    path = config.cache_dir / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _query_signature(query_text):
    return hashlib.sha1((query_text or "").strip().encode("utf-8")).hexdigest()


def _cache_is_current(cache, name, query_text):
    """Кеш считается актуальным, если текст запроса не менялся.

    Иначе в проекте окажутся данные по старому запросу — например, если
    запрос отредактировали или добавили новый слой с тем же именем.
    """
    meta = Path(cache) / f"{name}.meta"
    try:
        stored = meta.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stored == _query_signature(query_text)


def _store_query_signature(cache, name, query_text):
    meta = Path(cache) / f"{name}.meta"
    try:
        meta.write_text(_query_signature(query_text), encoding="utf-8")
    except OSError:
        pass


def _validate_osm(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size < 100:
        return False
    with path.open("rb") as handle:
        head = handle.read(2048)
        handle.seek(max(0, path.stat().st_size - 4096))
        tail = handle.read()
    return b"<osm" in head and b"</osm>" in tail


def _fetch_local(config, query_name, query_text, destination, cancel):
    command = shlex.split(config.overpass_endpoint_local)
    if not command:
        raise SourceError("overpass_endpoint_local is empty")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Download cancelled")
    with destination.open("wb") as handle:
        process = subprocess.run(
            command,
            input=query_text.encode("utf-8"),
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"{query_name}: local Overpass failed: {message}")
    if not _validate_osm(destination):
        raise SourceError(f"{query_name}: local Overpass returned an empty or incomplete response")


def _fetch_ssh(config, query_name, query_text, destination, cancel):
    if not config.overpass_endpoint_ssh:
        raise SourceError("overpass_endpoint_ssh is empty")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Download cancelled")
    with destination.open("wb") as handle:
        process = subprocess.run(
            config.overpass_endpoint_ssh,
            input=query_text.encode("utf-8"),
            stdout=handle,
            stderr=subprocess.PIPE,
            shell=True,
            check=False,
        )
    if process.returncode != 0:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"{query_name}: SSH Overpass failed: {message}")
    if not _validate_osm(destination):
        raise SourceError(f"{query_name}: SSH Overpass returned an empty or incomplete response")


def _selected_maps(index, config, bbox):
    """Карты Geofabrik для подготовки данных.

    Пустой список в конфиге означает автоматический подбор по bbox, иначе
    используются карты, отмеченные пользователем в панели плагина.
    """
    chosen = [str(item).strip() for item in (config.geofabrik_maps or []) if str(item).strip()]
    if not chosen:
        return index.required(bbox)
    known = {item.id: item for item in index.maps}
    maps = []
    for identifier in chosen:
        item = known.get(identifier)
        if item is None:
            raise SourceError(f"Карта Geofabrik не найдена в индексе: {identifier}")
        if item not in maps:
            maps.append(item)
    return maps


def prepare_sources(config, bbox, query_names, cancel=None, progress=None,
                    percent=None):
    if config.source_mode == "geofabrik":
        query_texts = {name: read_query(name) for name in query_names}
        timer = _DownloadTimer()
        with timer.measure():
            state = geofabrik.fetch_index(config, progress=progress, cancel=cancel)
        maps = _selected_maps(state.index, config, bbox)
        if not maps:
            raise SourceError("Для выбранного охвата карты Geofabrik не найдены")
        warnings = []
        # С osmium выгрузки обрабатываются по отдельности: склейка нужна
        # только для чтения карт целиком драйвером GDAL.
        osmium_ready = osmium_tool.find_osmium(config.temp_dir) is not None
        try:
            if not all(geofabrik.local_ready(config, item) for item in maps):
                with timer.measure():
                    downloaded = geofabrik.ensure_maps(
                        config, maps, progress=progress, percent=percent,
                        cancel=cancel,
                    )
                if not osmium_ready:
                    try:
                        geofabrik.merge_if_available(
                            config, maps, downloaded,
                            progress=progress, cancel=cancel,
                        )
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        warnings.append(f"Склейка osmium не удалась: {exc}")
            if osmium_ready:
                paths = geofabrik.source_paths(
                    config, maps, progress=progress, cancel=cancel,
                    prefer_separate=True,
                )
            else:
                paths = geofabrik.source_paths(
                    config, maps, progress=progress, cancel=cancel
                )
        except geofabrik.GeofabrikError as exc:
            raise SourceError(str(exc)) from exc
        try:
            dataset = pbf_source.prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=paths,
            )
        except RuntimeError:
            # Склеенный osmium файл может содержать повторяющиеся id
            # (пограничные объекты выгрузок выходят с разными версиями),
            # и на таких файлах драйвер GDAL обрывает чтение.  Тогда карты
            # читаются по отдельности: объекты с одинаковым id уйдут в один
            # слой при разборе (см. pbf_source.prepare_dataset).
            separate = geofabrik.local_maps(config, maps)
            if [str(item) for item in separate] == [str(item) for item in paths]:
                raise
            warnings.append(
                "Склеенный файл не читается драйвером GDAL, "
                "карты обработаны по отдельности"
            )
            dataset = pbf_source.prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=separate,
            )
        warnings.extend(dataset.warnings)
        if state.warning:
            warnings.append(state.warning)
        return SourceResult(
            dataset=dataset, warnings=warnings, download_seconds=timer.total
        )

    if config.source_mode == "pbf":
        query_texts = {name: read_query(name) for name in query_names}
        dataset = pbf_source.prepare_dataset(
            config, bbox, query_names, query_texts=query_texts,
            cancel=cancel, progress=progress,
        )
        return SourceResult(dataset=dataset, warnings=list(dataset.warnings))

    cache = _cache_dir(config, bbox)
    texts = {name: read_query(name) for name in query_names}
    files = {}
    timer = _DownloadTimer()

    if config.source_mode == "overpass":
        endpoints = []
        for endpoint in [config.overpass_endpoint] + list(config.overpass_endpoints):
            if endpoint and endpoint not in endpoints:
                endpoints.append(endpoint)

        pending = [
            name for name in query_names
            if not (
                _validate_osm(cache / f"{name}.osm")
                and _cache_is_current(cache, name, texts[name])
            )
        ]
        for name in query_names:
            if name not in pending:
                files[name] = cache / f"{name}.osm"
        if pending:
            tester = OverpassClient(endpoints)
            working = []
            errors = []
            for endpoint in endpoints:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Overpass check cancelled")
                if progress:
                    progress(f"Проверка Overpass: {endpoint}")
                try:
                    with timer.measure():
                        tester.check(endpoint, cancel=cancel)
                    working.append(endpoint)
                    if progress:
                        progress(f"Overpass доступен: {endpoint}")
                except InterruptedError:
                    raise
                except Exception as exc:
                    errors.append(str(exc))
                    if progress:
                        progress(f"Overpass недоступен: {endpoint}: {exc}")
            if not working:
                raise SourceError(
                    "Ни один Overpass-сервер не прошёл тестовый запрос. "
                    + "; ".join(errors)
                )
        else:
            working = endpoints
            if progress:
                progress("Все ответы Overpass уже в кэше, проверка сервера не требуется")

        client = OverpassClient(working)
        abort = threading.Event()
        cancel_proxy = _CancelProxy(cancel, abort)

        # Простые запросы (только фильтры по тегам) объединяем в пакеты:
        # один HTTP-запрос вместо десятков, ответ разбирается на клиенте.
        simple = {}
        complex_names = []
        for name in pending:
            selectors = overpass_batch.parse_selectors(texts[name])
            if selectors is None:
                complex_names.append(name)
            else:
                simple[name] = selectors
        batches = []
        batch_names = list(simple)
        for start in range(0, len(batch_names), OVERPASS_BATCH_SIZE):
            chunk = batch_names[start:start + OVERPASS_BATCH_SIZE]
            batches.append((chunk, overpass_batch.build_batch_query(
                [selector for name in chunk for selector in simple[name]], bbox
            )))

        def fetch(name):
            destination = cache / f"{name}.osm"
            with timer.measure():
                path = client.fetch(
                    name, texts[name], bbox, destination,
                    cancel=cancel_proxy, progress=progress,
                )
            _store_query_signature(cache, name, texts[name])
            return name, path

        def fetch_batch(index, chunk, query):
            key = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
            destination = cache / f"batch_{key}.osm"
            with timer.measure():
                path = client.fetch_raw(
                    f"batch {index}", query, destination,
                    cancel=cancel_proxy, progress=progress,
                )
            return chunk, path

        workers = min(OVERPASS_WORKERS, max(1, len(query_names)))
        pool = ThreadPoolExecutor(max_workers=workers)
        downloaded = []
        try:
            futures = [pool.submit(fetch, name) for name in complex_names]
            for index, (chunk, query) in enumerate(batches, start=1):
                futures.append(pool.submit(fetch_batch, index, chunk, query))
            for future in as_completed(futures):
                result = future.result()
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list):
                    downloaded.append(result)
                else:
                    name, path = result
                    files[name] = path
        except Exception:
            abort.set()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)

        # Разбор объединённых ответов по слоям.
        for chunk, batch_path in downloaded:
            layers = {name: simple[name] for name in chunk}
            try:
                split = overpass_batch.split_batch(
                    batch_path, layers, progress=progress, cancel=cancel,
                )
            except Exception as exc:
                if progress:
                    progress(f"WARNING: не удалось разделить ответ Overpass: {exc}")
                split = {}
            for name, (path, _count) in split.items():
                if not path.exists() or path.stat().st_size == 0:
                    continue
                files[name] = path
                _store_query_signature(cache, name, texts[name])
            # Слои, которые не удалось разобрать, запрашиваем обычным образом.
            missing = [name for name in chunk if name not in files]
            for name in missing:
                if progress:
                    progress(f"{name}: пакетный ответ не разобран, обычный запрос")
                try:
                    destination = cache / f"{name}.osm"
                    with timer.measure():
                        path = client.fetch(
                            name, texts[name], bbox, destination,
                            cancel=cancel_proxy, progress=progress,
                        )
                except Exception as exc:
                    if progress:
                        progress(f"WARNING: {name}: {exc}")
                    continue
                files[name] = path
                _store_query_signature(cache, name, texts[name])
        return SourceResult(files=files, download_seconds=timer.total)

    if config.source_mode == "local":
        for name in query_names:
            destination = cache / f"{name}.osm"
            if _validate_osm(destination) and _cache_is_current(
                cache, name, texts[name]
            ):
                files[name] = destination
                continue
            with timer.measure():
                _fetch_local(
                    config, name, build_query(texts[name], bbox), destination, cancel
                )
            _store_query_signature(cache, name, texts[name])
            files[name] = destination
        return SourceResult(files=files, download_seconds=timer.total)

    if config.source_mode == "ssh":
        for name in query_names:
            destination = cache / f"{name}.osm"
            if _validate_osm(destination) and _cache_is_current(
                cache, name, texts[name]
            ):
                files[name] = destination
                continue
            with timer.measure():
                _fetch_ssh(
                    config, name, build_query(texts[name], bbox), destination, cancel
                )
            _store_query_signature(cache, name, texts[name])
            files[name] = destination
        return SourceResult(files=files, download_seconds=timer.total)

    raise SourceError(f"Unsupported source mode: {config.source_mode}")
