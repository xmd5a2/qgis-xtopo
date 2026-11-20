import hashlib
import os
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    disk_space, extracts, geofabrik, overpass_batch, osmium_tool, pbf_source,
    slice_estimate,
)
from .overpass_client import OverpassClient, build_query, valid_osm
from .resources import read_query


# Сколько запросов Overpass выполняем одновременно.
OVERPASS_WORKERS = 6

# Сколько простых запросов объединяем в один пакетный запрос.
# Пакет меньше: ответ держится в памяти целиком (узлы, линии,
# отношения), и на стране один пакет — сотни мегабайт.
OVERPASS_BATCH_SIZE = 20

# Сколько ждать локальный Overpass и ssh-туннель, секунды.
OVERPASS_PROCESS_TIMEOUT = 3600


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
    # Информационные заметки: ожидаемые особенности источника, не проблемы.
    notes: list = field(default_factory=list)
    # Время, потраченное на сеть: скачивание карт, ответы Overpass.
    # Оно показывается в хронометраже отдельно от подготовки источника.
    download_seconds: float = 0.0


def _dataset_notes(dataset):
    """Информационные заметки источника; у старых объектов их может не быть."""
    notes = getattr(dataset, "notes", None)
    return list(notes) if isinstance(notes, (list, tuple)) else []


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
    key = hashlib.sha1(bbox.as_key().encode("utf-8")).hexdigest()[:12]
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
    """Похож ли файл на ответ Overpass с данными.

    Ответ с ошибкой (``<remark> runtime error: ...``) данными не
    считается: иначе он попадёт в кэш, и слой навсегда останется
    пустым.
    """
    return valid_osm(path)


def _fetch_local(config, query_name, query_text, destination, cancel):
    command = shlex.split(config.overpass_endpoint_local)
    if not command:
        raise SourceError("overpass_endpoint_local is empty")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Download cancelled")
    temporary = destination.with_suffix(destination.suffix + ".part")
    with temporary.open("wb") as handle:
        try:
            process = subprocess.run(
                command,
                input=query_text.encode("utf-8"),
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
                timeout=OVERPASS_PROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            temporary.unlink(missing_ok=True)
            raise SourceError(
                f"{query_name}: локальный Overpass не ответил за "
                f"{OVERPASS_PROCESS_TIMEOUT} с"
            ) from exc
    if cancel is not None and cancel.is_set():
        temporary.unlink(missing_ok=True)
        raise InterruptedError("Download cancelled")
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"{query_name}: local Overpass failed: {message}")
    if not _validate_osm(temporary):
        temporary.unlink(missing_ok=True)
        raise SourceError(f"{query_name}: local Overpass returned an empty or incomplete response")
    os.replace(temporary, destination)


def _fetch_ssh(config, query_name, query_text, destination, cancel):
    if not config.overpass_endpoint_ssh:
        raise SourceError("overpass_endpoint_ssh is empty")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Download cancelled")
    temporary = destination.with_suffix(destination.suffix + ".part")
    with temporary.open("wb") as handle:
        try:
            process = subprocess.run(
                config.overpass_endpoint_ssh,
                input=query_text.encode("utf-8"),
                stdout=handle,
                stderr=subprocess.PIPE,
                shell=True,
                check=False,
                timeout=OVERPASS_PROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            temporary.unlink(missing_ok=True)
            raise SourceError(
                f"{query_name}: SSH Overpass не ответил за "
                f"{OVERPASS_PROCESS_TIMEOUT} с"
            ) from exc
    if cancel is not None and cancel.is_set():
        temporary.unlink(missing_ok=True)
        raise InterruptedError("Download cancelled")
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"{query_name}: SSH Overpass failed: {message}")
    if not _validate_osm(temporary):
        temporary.unlink(missing_ok=True)
        raise SourceError(f"{query_name}: SSH Overpass returned an empty or incomplete response")
    os.replace(temporary, destination)


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


def _extracted_paths(config, paths):
    """Вырезанные osmium файлы прошлого прохода.

    Имена те же, что у ``osmium_prefilter``.  После ошибки чтения они
    остаются на диске, и читать их вместо сырой выгрузки страны
    намного быстрее: часы вместо минут.
    """
    directory = Path(config.temp_dir) / "osmium"
    if not directory.is_dir():
        return []
    found = []
    for position, source in enumerate(paths, 1):
        stem = Path(source).stem.replace(".", "_")
        candidate = directory / f"{stem}_{position}_extract.osm.pbf"
        try:
            if candidate.stat().st_size > 0:
                found.append(candidate)
        except OSError:
            continue
    return found


def _prepare_file_maps(config, bbox, query_names, maps, timer, progress,
                        percent, cancel):
    """Скачать выбранные выгрузки и подготовить по ним слои.

    Общий путь для файловых источников: Geofabrik и другие провайдеры
    отдают файлы PBF одинаково, отличается только список выгрузок.
    """
    query_texts = {name: read_query(name) for name in query_names}
    warnings = []
    # С osmium выгрузки режутся и читаются по отдельности: склейка
    # нужна только для драйвера GDAL без osmium.
    osmium_ready = osmium_tool.find_osmium(config.temp_dir) is not None
    try:
        if not all(geofabrik.local_ready(config, item) for item in maps):
            with timer.measure():
                geofabrik.ensure_maps(
                    config, maps, progress=progress, percent=percent,
                    cancel=cancel,
                )
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
        # Склеенный osmium файл может не читаться драйвером GDAL:
        # пограничные объекты выгрузок имеют разные версии, и после
        # merge драйвер обрывает чтение.  Тогда карты читаются по
        # отдельности без предварительной склейки: объекты с
        # одинаковым id уйдут в один слой при разборе.  Даже если
        # paths уже отдельные (osmium склеил их внутри себя), повтор
        # с use_osmium=False обязателен — иначе откат не сработает.
        if osmium_ready:
            # Повтор читает вырезанные osmium файлы прошлого прохода:
            # сырую выгрузку страны читать часами.
            separate = _extracted_paths(config, paths)
            if not separate:
                separate = geofabrik.local_maps(config, maps)
        else:
            separate = geofabrik.local_maps(config, maps)
        if (not osmium_ready
                and [str(item) for item in separate]
                == [str(item) for item in paths]):
            raise
        warnings.append(
            "Склеенный файл не читается драйвером GDAL, "
            "карты обработаны по отдельности"
        )
        dataset = pbf_source.prepare_dataset(
            config, bbox, query_names, query_texts=query_texts,
            cancel=cancel, progress=progress, sources=separate,
            use_osmium=False,
        )
    return dataset, warnings


def prepare_sources(config, bbox, query_names, cancel=None, progress=None,
                    percent=None):
    if config.source_mode == "geofabrik":
        timer = _DownloadTimer()
        with timer.measure():
            state = geofabrik.fetch_index(config, progress=progress, cancel=cancel)
        maps = _selected_maps(state.index, config, bbox)
        if not maps:
            raise SourceError("Для выбранного охвата карты Geofabrik не найдены")
        dataset, warnings = _prepare_file_maps(
            config, bbox, query_names, maps, timer, progress, percent, cancel,
        )
        warnings.extend(dataset.warnings)
        if state.warning:
            warnings.append(state.warning)
        return SourceResult(
            dataset=dataset,
            warnings=warnings,
            notes=_dataset_notes(dataset),
            download_seconds=timer.total,
        )

    if config.source_mode == "extracts":
        # Файловые выгрузки с других хостов: тот же Geofabrik-механизм,
        # но список карт и их границы берутся у другого провайдера.
        timer = _DownloadTimer()
        try:
            with timer.measure():
                index, catalogue = extracts.build_index(
                    config, config.extract_provider, bbox,
                    progress=progress, cancel=cancel,
                )
        except extracts.ExtractError as exc:
            raise SourceError(str(exc)) from exc
        except InterruptedError:
            raise
        maps = _selected_maps(index, config, bbox)
        if not maps:
            raise SourceError(
                "Для выбранного охвата выгрузки не найдены "
                f"({config.extract_provider})"
            )
        dataset, warnings = _prepare_file_maps(
            config, bbox, query_names, maps, timer, progress, percent, cancel,
        )
        warnings.extend(dataset.warnings)
        if catalogue.warning:
            warnings.append(catalogue.warning)
        return SourceResult(
            dataset=dataset,
            warnings=warnings,
            notes=_dataset_notes(dataset),
            download_seconds=timer.total,
        )

    if config.source_mode == "slice":
        # Вырезка по охвату через SliceOSM: сервис собирает файл PBF по
        # указанной области, данные свежее минуты.
        timer = _DownloadTimer()
        query_texts = {name: read_query(name) for name in query_names}
        client = extracts.SliceOsm()
        destination = slice_estimate.slice_cache_path(config, bbox)
        # Готовым считается только целый вырез: оборванный файл по
        # одному признаку «непустой» выглядит как скачанная карта.
        if not extracts.looks_like_pbf(destination):
            config.geofabrik_dir.mkdir(parents=True, exist_ok=True)
            try:
                with timer.measure():
                    # Сервис собирает вырезы по одному: очередь — это
                    # время ожидания до начала работы.
                    queue = client.service_status(cancel=cancel)
                    pending = 0
                    try:
                        pending = int(queue.get("QueueSize") or 0)
                    except (TypeError, ValueError):
                        pending = 0
                    if progress and pending:
                        progress(f"SliceOSM: в очереди {pending} заданий")
                    uuid = client.submit(bbox, cancel=cancel)
                    if progress:
                        progress("SliceOSM: вырез заказан")
                    payload = client.wait(
                        uuid, cancel=cancel, progress=progress,
                    )
                    # Размер сервис отдаёт только для готового выреза:
                    # до этого момента он неизвестен.
                    size = client.size_bytes(payload)
                    if progress:
                        progress(
                            "SliceOSM: вырез готов, "
                            f"{disk_space.human_size(size)}"
                        )
                        progress("SliceOSM: скачивание выреза")
                    client.download(
                        uuid, destination, progress=progress, cancel=cancel,
                        expected_size=size,
                    )
            except extracts.ExtractError as exc:
                raise SourceError(str(exc)) from exc
            except InterruptedError:
                raise
        try:
            dataset = pbf_source.prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=[destination],
            )
        except RuntimeError as exc:
            raise SourceError(f"Не удалось разобрать вырез SliceOSM: {exc}") from exc
        return SourceResult(
            dataset=dataset,
            warnings=list(dataset.warnings),
            notes=_dataset_notes(dataset),
            download_seconds=timer.total,
        )

    if config.source_mode == "pbf":
        query_texts = {name: read_query(name) for name in query_names}
        dataset = pbf_source.prepare_dataset(
            config, bbox, query_names, query_texts=query_texts,
            cancel=cancel, progress=progress,
        )
        return SourceResult(
            dataset=dataset,
            warnings=list(dataset.warnings),
            notes=_dataset_notes(dataset),
        )

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
            # Кэш берётся только пока текст запроса не менялся: иначе
            # старый ответ помечается актуальным, и слой навсегда
            # остаётся со старыми данными.
            current = _cache_is_current(cache, name, texts[name])
            with timer.measure():
                path = client.fetch(
                    name, texts[name], bbox, destination,
                    cancel=cancel_proxy, progress=progress,
                    use_cache=current,
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
                            use_cache=_cache_is_current(
                                cache, name, texts[name]
                            ),
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
