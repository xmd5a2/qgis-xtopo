import hashlib
import json
import os
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from qgis.core import QgsGeometry, QgsRectangle

from . import osmium_tool


INDEX_URL = "https://download.geofabrik.de/index-v1.json"
INDEX_TTL = 7 * 24 * 60 * 60
RETRY_TTL = 10 * 60
INDEX_TIMEOUT = 30
HEAD_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 30
CHECK_TTL = 10 * 60
CHUNK = 1024 * 1024
MAX_DOWNLOADS = 4
# Карты крупнее этого размера не предлагаются к скачиванию.
MAX_MAP_SIZE = 7 * 1024 * 1024 * 1024
USER_AGENT = "QGIS-xtopo/0.5 (+https://github.com/xmd5a2/qgis-xtopo)"
MIN_FREE_SPACE = 100 * 1024 * 1024


class GeofabrikError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeofabrikMap:
    id: str
    name: str
    parent: str
    url: str


@dataclass
class MapStatus:
    map: GeofabrikMap
    available: bool
    size: int = 0
    error: str = ""
    local: bool = False


@dataclass
class _Entry:
    map: GeofabrikMap
    geometry: object
    bounds: object
    area: float


@dataclass
class GeofabrikIndex:
    entries: list = field(default_factory=list)

    @property
    def maps(self):
        return [entry.map for entry in self.entries]

    def _by_id(self):
        return {entry.map.id: entry for entry in self.entries}

    def _ancestor_ids(self, entry, by_id):
        """Идентификаторы родительских выгрузок (europe -> france -> corse)."""
        ids = set()
        current = entry.map.parent
        while current and current in by_id:
            ids.add(current)
            current = by_id[current].map.parent
        return ids

    def _depth(self, entry, by_id):
        return len(self._ancestor_ids(entry, by_id))

    def _clip(self, bbox):
        rectangle = QgsRectangle(
            bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max
        )
        return rectangle, QgsGeometry.fromRect(rectangle)

    def _entries(self, bbox):
        rectangle, clip = self._clip(bbox)
        found = []
        for entry in self.entries:
            if entry.map.id == "europe":
                continue
            if not entry.bounds.intersects(rectangle):
                continue
            if not entry.geometry.intersects(clip):
                continue
            found.append(entry)
        return found

    def candidates(self, bbox):
        """Все выгрузки, пересекающие bbox, от маленьких к большим."""
        by_id = self._by_id()
        found = self._entries(bbox)
        found.sort(
            key=lambda entry: (entry.area, -self._depth(entry, by_id), entry.map.id)
        )
        return [entry.map for entry in found]

    def required(self, bbox, limit=1e-12):
        """Минимальный набор выгрузок, покрывающий bbox.

        Родительская выгрузка (например, "france" для "corse") содержит те же
        данные, что и дочерняя, поэтому она не добавляется, если дочерняя
        выбрана: иначе для небольшой области пришлось бы качать всю страну.
        """
        by_id = self._by_id()
        rectangle, clip = self._clip(bbox)
        candidates = self._entries(bbox)
        candidates.sort(
            key=lambda entry: (entry.area, -self._depth(entry, by_id), entry.map.id)
        )

        selected = []
        covered_parents = set()
        coverage = QgsGeometry()
        for entry in candidates:
            piece = entry.geometry.intersection(clip)
            if piece.isEmpty():
                continue
            if coverage.isEmpty():
                new = piece
            else:
                new = piece.difference(coverage)
            if new.isEmpty() or new.area() < limit:
                continue
            if entry.map.id in covered_parents:
                continue
            selected.append(entry.map)
            covered_parents |= self._ancestor_ids(entry, by_id)
            coverage = (
                piece if coverage.isEmpty() else QgsGeometry.unaryUnion([coverage, piece])
            )
        return sorted(selected, key=lambda item: item.id)


@dataclass
class IndexState:
    index: GeofabrikIndex
    fetched_at: float
    last_modified: str = ""
    warning: str = ""


@dataclass
class GeofabrikPlan:
    state: IndexState
    maps: list
    statuses: list
    extra: list = field(default_factory=list)
    extra_statuses: list = field(default_factory=list)
    parent_names: dict = field(default_factory=dict)
    # Карты, которые не предлагаются из-за размера: (карта, размер в байтах).
    skipped: list = field(default_factory=list)

    def items(self):
        """Все карты плана вместе со статусом: сначала выбранные, потом прочие."""
        return list(zip(self.maps, self.statuses)) + list(
            zip(self.extra, self.extra_statuses)
        )


_urlopen = urllib.request.urlopen
_size_cache = {}
_index_cache = {}


def _qgs_geometry(payload):
    if not payload:
        return None
    try:
        from osgeo import ogr

        raw = ogr.CreateGeometryFromJson(json.dumps(payload))
        if raw is None or raw.IsEmpty():
            return None
        geometry = QgsGeometry.fromWkt(raw.ExportToWkt())
        if geometry.isNull() or geometry.isEmpty():
            return None
        return geometry
    except Exception:
        return None


def _parse_index(data):
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    entries = []
    for feature in data.get("features", []):
        properties = feature.get("properties") or {}
        urls = properties.get("urls") or {}
        url = urls.get("pbf")
        identifier = properties.get("id")
        if not identifier or not url:
            continue
        geometry = _qgs_geometry(feature.get("geometry"))
        if geometry is None:
            continue
        entries.append(
            _Entry(
                map=GeofabrikMap(
                    id=str(identifier),
                    name=str(properties.get("name") or identifier),
                    parent=str(properties.get("parent") or ""),
                    url=str(url),
                ),
                geometry=geometry,
                bounds=geometry.boundingBox(),
                area=geometry.area(),
            )
        )
    return GeofabrikIndex(entries)


def load_index(path):
    path = Path(path)
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _index_cache.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    index = _parse_index(path.read_text(encoding="utf-8"))
    _index_cache[str(path)] = (key, index)
    return index


def _cache_paths(config):
    directory = config.geofabrik_dir
    return directory, directory / "index-v1.json", directory / "index-meta.json"


def _read_meta(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(path, meta):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".part")
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def invalidate_index(config):
    """Пометить кэш индекса Geofabrik устаревшим.

    Следующий вызов ``fetch_index`` скачает индекс заново (сервер может
    ответить «не изменён» — тогда останется прежний индекс, но с новой
    датой проверки). Используется кнопкой «Обновить» в панели.
    """
    _directory, _index_path, meta_path = _cache_paths(config)
    meta = _read_meta(meta_path)
    if not meta:
        return
    meta["fetched_at"] = 0
    _write_meta(meta_path, meta)


def _fetch(url, headers=None, cancel=None, progress=None, label=""):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        response = _urlopen(request, timeout=INDEX_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", exc.headers
        raise
    with response:
        status = getattr(response, "status", 200)
        total = int(response.headers.get("Content-Length") or 0)
        chunks = []
        received = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Geofabrik index download cancelled")
            chunk = response.read(CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if progress and total and received != total:
                percent = received * 100 // total
                progress(f"{label}: {percent}% ({received / 1048576:.1f} МБ)")
        return status, b"".join(chunks), response.headers


def fetch_index(config, progress=None, cancel=None):
    directory, index_path, meta_path = _cache_paths(config)
    directory.mkdir(parents=True, exist_ok=True)
    meta = _read_meta(meta_path)
    now = time.time()
    cached = index_path.exists() and index_path.stat().st_size > 0

    if cached and meta.get("fetched_at", 0) + INDEX_TTL > now:
        return IndexState(
            load_index(index_path),
            float(meta.get("fetched_at", now)),
            str(meta.get("last_modified", "")),
            str(meta.get("warning", "")),
        )

    headers = {}
    if meta.get("etag"):
        headers["If-None-Match"] = str(meta["etag"])
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = str(meta["last_modified"])

    if progress:
        progress("Загрузка индекса Geofabrik (3.8 МБ)…")
    try:
        status, data, response_headers = _fetch(
            INDEX_URL,
            headers=headers,
            cancel=cancel,
            progress=progress,
            label="Индекс Geofabrik",
        )
    except InterruptedError:
        raise
    except Exception as exc:
        if cached:
            warning = f"Не удалось обновить индекс Geofabrik: {exc}"
            meta["fetched_at"] = now - INDEX_TTL + RETRY_TTL
            meta["warning"] = warning
            _write_meta(meta_path, meta)
            return IndexState(
                load_index(index_path),
                now,
                str(meta.get("last_modified", "")),
                warning,
            )
        raise GeofabrikError(f"Не удалось загрузить индекс Geofabrik: {exc}") from exc

    if status == 304:
        meta["fetched_at"] = now
        meta["warning"] = ""
        _write_meta(meta_path, meta)
        if progress:
            progress("Индекс Geofabrik не изменился")
        return IndexState(
            load_index(index_path),
            now,
            str(meta.get("last_modified", "")),
            "",
        )

    try:
        json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise GeofabrikError("Geofabrik вернул повреждённый индекс") from exc

    temp = index_path.with_suffix(index_path.suffix + ".part")
    temp.write_bytes(data)
    os.replace(temp, index_path)
    last_modified = response_headers.get("Last-Modified", "")
    etag = response_headers.get("ETag", "")
    meta = {
        "fetched_at": now,
        "last_modified": last_modified,
        "etag": etag,
        "warning": "",
    }
    _write_meta(meta_path, meta)
    if progress:
        progress("Индекс Geofabrik обновлён")
    return IndexState(load_index(index_path), now, last_modified, "")


def _head_size(url):
    try:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        with _urlopen(request, timeout=HEAD_TIMEOUT) as response:
            size = int(response.headers.get("Content-Length") or 0)
            return True, size, ""
    except Exception as exc:
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
            )
            with _urlopen(request, timeout=HEAD_TIMEOUT) as response:
                content_range = response.headers.get("Content-Range") or ""
                size = int(content_range.rsplit("/", 1)[-1]) if "/" in content_range else 0
                return True, size, ""
        except Exception:
            return False, 0, str(exc)


def _cached_head(url):
    cached = _size_cache.get(url)
    now = time.time()
    if cached is not None and now - cached[0] < CHECK_TTL:
        return cached[1:]
    result = _head_size(url)
    _size_cache[url] = (now, *result)
    return result


def clear_caches():
    _size_cache.clear()
    _index_cache.clear()


def _too_large(status):
    """Карта крупнее MAX_MAP_SIZE не предлагается к скачиванию."""
    return status is not None and status.size > MAX_MAP_SIZE


def check_maps(config, maps, cancel=None, progress=None, report=True):
    if not maps:
        return []
    statuses = {}
    with ThreadPoolExecutor(max_workers=min(MAX_DOWNLOADS, len(maps))) as pool:
        futures = {pool.submit(_cached_head, item.url): item for item in maps}
        for future in as_completed(futures):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Geofabrik check cancelled")
            item = futures[future]
            available, size, error = future.result()
            destination = local_path(config, item)
            statuses[item.id] = MapStatus(
                map=item,
                available=available,
                size=size,
                error=error,
                local=destination.exists()
                and destination.stat().st_size > 0
                and _looks_like_pbf(destination),
            )
    if progress and report:
        for item in maps:
            status = statuses[item.id]
            state = "доступна" if status.available else "недоступна"
            progress(f"Карта {item.name}: {state}")
    return [statuses[item.id] for item in maps]


def local_path(config, item):
    return config.geofabrik_dir / f"{item.id}.osm.pbf"


def local_ready(config, item):
    path = local_path(config, item)
    try:
        return path.stat().st_size > 0 and _looks_like_pbf(path)
    except OSError:
        return False


def local_maps(config, maps):
    return [local_path(config, item) for item in maps]


def _looks_like_pbf(path):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(4)
            if len(raw) < 4:
                return False
            size = struct.unpack(">I", raw)[0]
            if not 0 < size < 1000000:
                return False
            header = handle.read(size)
            return b"OSMHeader" in header[:200]
    except OSError:
        return False


class _Transfer:
    """Aggregated byte counter for parallel downloads.

    Reporting is global and throttled, so the progress text does not flicker
    between maps: one line shows the combined percentage for all downloads
    together with the current download speed.
    """

    def __init__(self, total, total_maps, percent=None):
        self.total = total
        self.total_maps = total_maps
        self.percent = percent
        self.done = 0
        self.maps = 0
        self.speed = 0.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._speed_done = 0
        self._speed_time = time.monotonic()

    def add(self, size):
        if size <= 0:
            return
        with self._lock:
            self.done += size
            now = time.monotonic()
            if now - self._last < 0.3:
                return
            self._last = now
            self._report()

    def map_done(self):
        with self._lock:
            self.maps += 1
            self._last = 0.0
            self._report()

    def finish(self):
        with self._lock:
            self.done = max(self.done, self.total)
            self.maps = self.total_maps
            self._last = 0.0
            self._report()

    def _update_speed(self):
        """Скорость считается по приросту байтов между отчётами."""
        now = time.monotonic()
        elapsed = now - self._speed_time
        if elapsed < 0.2:
            return
        current = (self.done - self._speed_done) / elapsed
        # Сглаживание, чтобы значение не скакало из-за пауз между чанками.
        self.speed = current if not self.speed else 0.5 * self.speed + 0.5 * current
        self._speed_done = self.done
        self._speed_time = now

    def _report(self):
        if not self.percent:
            return
        self._update_speed()
        if self.total:
            value = min(100, self.done * 100 // self.total)
            text = (
                f"Загрузка карт: {value}% "
                f"({self.done / 1048576:.1f}/{self.total / 1048576:.1f} МБ)"
            )
        else:
            value = -1
            text = f"Загрузка карт: {self.done / 1048576:.1f} МБ"
        parts = [text, f"{self.maps}/{self.total_maps}"]
        if self.speed > 0:
            parts.append(f"{self.speed / 1048576:.1f} МБ/с")
        self.percent(value, " · ".join(parts))


def _download_one(config, item, status, transfer, progress, cancel):
    destination = local_path(config, item)
    temporary = destination.with_suffix(destination.suffix + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    start = temporary.stat().st_size if temporary.exists() else 0
    remote = status.size if status is not None else 0
    if remote and start >= remote:
        start = 0
        temporary.unlink(missing_ok=True)

    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(item.url, headers=headers)
    with _urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        code = getattr(response, "status", 200)
        if code == 200 and start:
            start = 0
            temporary.unlink(missing_ok=True)
        elif start:
            transfer.add(start)
        mode = "ab" if start else "wb"
        with temporary.open(mode) as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Geofabrik download cancelled")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                transfer.add(len(chunk))
    if not _looks_like_pbf(temporary):
        temporary.unlink(missing_ok=True)
        raise GeofabrikError(f"Файл {item.id} не похож на PBF")
    os.replace(temporary, destination)
    transfer.map_done()
    if progress:
        size = destination.stat().st_size / 1048576
        progress(f"Скачана карта {item.name} ({size:.1f} МБ)")
    return destination


def ensure_maps(config, maps, statuses=None, progress=None, percent=None,
                cancel=None):
    if not maps:
        raise GeofabrikError("Для выбранного охвата карты Geofabrik не найдены")
    if statuses is None:
        statuses = check_maps(config, maps, cancel=cancel, progress=progress)
    oversized = [status.map.name for status in statuses if _too_large(status)]
    if oversized:
        raise GeofabrikError(
            "Слишком большая карта для скачивания: " + ", ".join(oversized)
        )
    by_id = {status.map.id: status for status in statuses}

    pending = []
    unavailable = []
    for item in maps:
        status = by_id.get(item.id)
        destination = local_path(config, item)
        local_size = destination.stat().st_size if destination.exists() else 0
        valid_local = local_size > 0 and _looks_like_pbf(destination)
        if valid_local and (not status or not status.available or not status.size or local_size == status.size):
            continue
        if status is not None and not status.available and not valid_local:
            unavailable.append(item.id)
            continue
        pending.append((item, status))

    if unavailable:
        raise GeofabrikError(
            "Карты Geofabrik недоступны: " + ", ".join(unavailable)
        )

    needed = sum(status.size for _, status in pending if status is not None and status.size)
    config.geofabrik_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(config.geofabrik_dir).free
    if needed and free < needed + MIN_FREE_SPACE:
        raise GeofabrikError(
            f"Недостаточно места: нужно {needed / 1073741824:.1f} ГБ, "
            f"свободно {free / 1073741824:.1f} ГБ"
        )

    if pending:
        total_maps = len(pending)
        known = all(status is not None and status.size for _, status in pending)
        total = sum(status.size for _, status in pending) if known else 0
        if progress:
            progress(
                f"Скачивание карт: {total_maps} шт."
                + (f", {total / 1048576:.1f} МБ" if total else "")
            )
        transfer = _Transfer(total, total_maps, percent)
        errors = []
        with ThreadPoolExecutor(max_workers=min(MAX_DOWNLOADS, total_maps)) as pool:
            futures = {
                pool.submit(
                    _download_one,
                    config,
                    item,
                    status,
                    transfer,
                    progress,
                    cancel,
                ): item
                for item, status in pending
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except InterruptedError:
                    raise
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise GeofabrikError("; ".join(errors))
        transfer.finish()

    return local_maps(config, maps)


def _merged_key(maps, paths):
    digest = hashlib.sha1()
    for item, path in zip(maps, paths):
        size = path.stat().st_size if Path(path).exists() else 0
        digest.update(f"{item.id}:{size}".encode("utf-8"))
    return digest.hexdigest()[:12]


def merged_path(config, maps, paths):
    return config.geofabrik_dir / f"merged_{_merged_key(maps, paths)}.osm.pbf"


def find_osmium():
    return osmium_tool.find_osmium()


def merge_if_available(config, maps, paths, progress=None, cancel=None):
    executable = find_osmium()
    if not executable:
        return ""
    destination = merged_path(config, maps, paths)
    if destination.exists() and _looks_like_pbf(destination):
        return str(destination)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if progress:
        progress(f"Склейка карт osmium ({len(paths)} шт.)…")
    command = [executable, "merge", "--overwrite", "-o", str(temporary)]
    command.extend(str(path) for path in paths)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                process.terminate()
                raise InterruptedError("osmium merge cancelled")
            time.sleep(0.5)
    finally:
        stdout, stderr = process.communicate()
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        message = stderr.decode("utf-8", "replace").strip()
        raise GeofabrikError(f"osmium merge failed: {message}")
    if not _looks_like_pbf(temporary):
        temporary.unlink(missing_ok=True)
        raise GeofabrikError("osmium создал файл, не похожий на PBF")
    os.replace(temporary, destination)
    if progress:
        progress("Карты склеены osmium")
    return str(destination)


def source_paths(config, maps, merge=False, progress=None, cancel=None,
                 prefer_separate=False):
    """Файлы выгрузок для чтения.

    ``prefer_separate`` — вернуть отдельные выгрузки, даже если есть
    склеенный файл: он нужен только для чтения карт целиком, а при
    вырезке области osmium каждая выгрузка обрабатывается сама по себе.
    """
    paths = local_maps(config, maps)
    missing = [path.name for path in paths if not path.exists()]
    if missing:
        raise GeofabrikError(
            "Карты не скачаны: " + ", ".join(missing)
            + ". Запустите «Подготовить данные» ещё раз."
        )
    if not merge and not prefer_separate:
        cached = merged_path(config, maps, paths)
        if cached.exists() and _looks_like_pbf(cached):
            return [cached]
    broken = [path.name for path in paths if not _looks_like_pbf(path)]
    if broken:
        raise GeofabrikError(
            "Повреждённые карты: " + ", ".join(broken)
            + ". Запустите «Подготовить данные» ещё раз."
        )
    if merge:
        merged = merge_if_available(config, maps, paths, progress=progress, cancel=cancel)
        if merged:
            return [Path(merged)]
    return paths


def analyse(config, bbox, cancel=None, progress=None):
    state = fetch_index(config, progress=progress, cancel=cancel)
    maps = state.index.required(bbox)
    chosen = {item.id for item in maps}
    extra = [
        item for item in state.index.candidates(bbox) if item.id not in chosen
    ]
    statuses = check_maps(
        config, maps + extra, cancel=cancel, progress=progress, report=False
    )
    by_id = {status.map.id: status for status in statuses}
    names = {entry.map.id: entry.map.name for entry in state.index.entries}
    parent_names = {
        entry.map.id: names.get(entry.map.parent, "")
        for entry in state.index.entries
        if entry.map.parent
    }

    # Слишком большие выгрузки не предлагаются к скачиванию.
    skipped = []
    allowed_maps = []
    allowed_statuses = []
    for item in maps:
        status = by_id[item.id]
        if _too_large(status):
            skipped.append((item, status.size))
        else:
            allowed_maps.append(item)
            allowed_statuses.append(status)
    allowed_extra = []
    allowed_extra_statuses = []
    for item in extra:
        status = by_id[item.id]
        if _too_large(status):
            skipped.append((item, status.size))
        else:
            allowed_extra.append(item)
            allowed_extra_statuses.append(status)

    return GeofabrikPlan(
        state=state,
        maps=allowed_maps,
        statuses=allowed_statuses,
        extra=allowed_extra,
        extra_statuses=allowed_extra_statuses,
        parent_names=parent_names,
        skipped=skipped,
    )
