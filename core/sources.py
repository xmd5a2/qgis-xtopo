import hashlib
import shlex
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import geofabrik, pbf_source
from .overpass_client import OverpassClient, build_query
from .resources import read_query


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


def _cache_dir(config, bbox):
    key = hashlib.sha1(bbox.as_csv().encode("utf-8")).hexdigest()[:12]
    path = config.cache_dir / key
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        state = geofabrik.fetch_index(config, progress=progress, cancel=cancel)
        maps = _selected_maps(state.index, config, bbox)
        if not maps:
            raise SourceError("Для выбранного bbox карты Geofabrik не найдены")
        warnings = []
        try:
            if not all(geofabrik.local_ready(config, item) for item in maps):
                downloaded = geofabrik.ensure_maps(
                    config, maps, progress=progress, percent=percent,
                    cancel=cancel,
                )
                try:
                    geofabrik.merge_if_available(
                        config, maps, downloaded,
                        progress=progress, cancel=cancel,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    warnings.append(f"Склейка osmium не удалась: {exc}")
            paths = geofabrik.source_paths(
                config, maps, progress=progress, cancel=cancel
            )
        except geofabrik.GeofabrikError as exc:
            raise SourceError(str(exc)) from exc
        dataset = pbf_source.prepare_dataset(
            config, bbox, query_names, query_texts=query_texts,
            cancel=cancel, progress=progress, sources=paths,
        )
        warnings.extend(dataset.warnings)
        if state.warning:
            warnings.append(state.warning)
        return SourceResult(dataset=dataset, warnings=warnings)

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

    if config.source_mode == "overpass":
        endpoints = []
        for endpoint in [config.overpass_endpoint] + list(config.overpass_endpoints):
            if endpoint and endpoint not in endpoints:
                endpoints.append(endpoint)

        pending = [
            name for name in query_names
            if not _validate_osm(cache / f"{name}.osm")
        ]
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

        def fetch(name):
            destination = cache / f"{name}.osm"
            return name, client.fetch(
                name, texts[name], bbox, destination, cancel=cancel_proxy, progress=progress
            )

        workers = min(3, max(1, len(query_names)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [pool.submit(fetch, name) for name in query_names]
            for future in as_completed(futures):
                name, path = future.result()
                files[name] = path
        except Exception:
            abort.set()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        return SourceResult(files=files)

    if config.source_mode == "local":
        for name in query_names:
            destination = cache / f"{name}.osm"
            if _validate_osm(destination):
                files[name] = destination
                continue
            _fetch_local(config, name, build_query(texts[name], bbox), destination, cancel)
            files[name] = destination
        return SourceResult(files=files)

    if config.source_mode == "ssh":
        for name in query_names:
            destination = cache / f"{name}.osm"
            if _validate_osm(destination):
                files[name] = destination
                continue
            _fetch_ssh(config, name, build_query(texts[name], bbox), destination, cancel)
            files[name] = destination
        return SourceResult(files=files)

    raise SourceError(f"Unsupported source mode: {config.source_mode}")
