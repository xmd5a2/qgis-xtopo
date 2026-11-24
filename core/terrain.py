import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .bbox import BBox
from . import dem_sources, disk_space, i18n, net, sonny, terrain_cache

CHUNK = 256 * 1024

# Сколько тайлов SRTM качаем одновременно.
DEM_DOWNLOAD_WORKERS = 4

# Запас вокруг области для растровых наборов: тайлы Copernicus сдвинуты
# относительно целых градусов, и без запаса у края остаётся полоса nodata.
COG_PADDING = 0.002

# Предел размера окна DEM (пикселей).  Для 3DEP 10 м на большой охват
# окно иначе занимает гигабайты; разрешение в этом случае немного
# снижается, а сообщение об этом выводится в журнал.
MAX_DEM_PIXELS = 120_000_000

# Сколько пикселей одного удалённого тайла разрешено скачать для
# параллельной подготовки мозаики.  Для 3DEP 10 м тайл 1°×1° — это
# сотни мегапикселей, такие окна остаются на прямом чтении /vsicurl.
MAX_COG_PREFETCH_PIXELS = 30_000_000

# Сколько окон COG качать одновременно.  Это сетевые загрузки, а не
# расчёт: число не зависит от количества ядер и ограничено каналом.
COG_PREFETCH_WORKERS = 6

# Сколько частей одного набора можно заранее положить на диск: каждая
# часть — это полная сетка мозаики, и без ограничения их число растёт
# вместе с числом тайлов набора.
MAX_COG_PREFETCH_PARTS = 8

# Сколько раз повторяется сборка растра после сетевого сбоя.  Обрыв
# одного HTTP-блока не должен валить рельеф: при повторе источники
# подписываются заново, потому что ссылки Planetary Computer живут час
# и за время долгой сборки успевают истечь.
WARP_ATTEMPTS = 3
WARP_RETRY_DELAY = 5.0

# Запас вокруг тайла при нарезке DEM на изолинии.  Алгоритм GDAL считает
# контур по соседним ячейкам, и без запаса линия у края тайла обрывается
# и загибается.  В результат попадает только ядро тайла, поэтому куски
# соседних тайлов не дублируются и стыкуются по границе.
CONTOUR_HALO = 32

# Начиная с этого числа пикселей DEM нарезка на тайлы даёт выигрыш:
# при меньшем размере нарезка стоит дороже самого расчёта.
CONTOUR_TILE_PIXELS = 4_000_000

# Сколько пикселей должно приходиться на один тайл: окно держится в
# памяти, а нахлёст тайла съедает часть выигрыша от параллельности.
CONTOUR_TILE_MAX_PIXELS = 6_000_000

# Сколько метров в одном градусе: столько метров в шаге растра
# географической СК.  Для проекционной СК масштаб берётся из неё самой.
GEOGRAPHIC_SCALE = 111120.0

# Сколько долей ядер можно занять расчётом контуров: больше потоков —
# быстрее, но дальше потолок, GEOS держит GIL, и лишние потоки мешают.
CONTOUR_TILE_WORKERS_RATIO = 0.75

# Оценка памяти одного процесса обработки тайла: окно DEM, контуры тайла
# и загруженные полигоны воды и ледников.
ISOLINE_PROCESS_BYTES = 1536 * 1024 * 1024

# Сколько линий тайла отдавать одному процессу разрезания по ледникам.
# Стоимость операции определяется числом полигонов-кандидатов, и в
# пределах тайла она распределена неравномерно: части по 4000 строк
# выравнивают самый тяжёлый тайл, не плодя лишних файлов.
ISOLINE_CHUNK = 4000

# Отладочный выключатель процессов: тесты и ручные прогоны.
NO_ISOLINE_PROCESSES_ENV = "QGIS_XTOPO_NO_ISOLINE_PROCESSES"

# Успешно найденный интерпретатор для процессов тайлов: проверка с
# импортом QGIS стоит около секунды, повторять её на каждой сборке незачем.
# ``None`` — ещё не проверяли; ``(None, env)`` — подходящего нет.
_WORKER_PYTHON = None


def _gdal():
    from osgeo import gdal, ogr, osr

    from .memory import gdal_cache_mb

    gdal.UseExceptions()
    ogr.UseExceptions()
    # Растры читаются по HTTP Range (/vsicurl): без коротких таймаутов
    # при недоступной сети GDAL ждёт ответа очень долго.
    gdal.SetConfigOption("CPL_VSIL_CURL_CONNECTTIMEOUT", "5")
    gdal.SetConfigOption("CPL_VSIL_CURL_TIMEOUT", "15")
    gdal.SetConfigOption("GDAL_HTTP_CONNECTTIMEOUT", "5")
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "15")
    # Повтор HTTP-запроса на уровне GDAL: без него любой обрыв чтения
    # блока /vsicurl возвращает ошибку всему gdal.Warp.
    gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "4")
    gdal.SetConfigOption("GDAL_HTTP_RETRY_DELAY", "2")
    # Кэш GDAL — часть общего бюджета памяти. На 8 ГБ он меньше, на
    # машинах с запасом — больше, но не выше 2 ГБ.
    gdal.SetConfigOption("GDAL_CACHEMAX", str(gdal_cache_mb()))
    return gdal, ogr, osr


def _numpy():
    import numpy

    return numpy


def srtm_tile_names(bbox):
    return dem_sources.tile_names(bbox)


def _tile_directory(tile):
    return tile[:3]


def _cached_tile(cache_dir, tile):
    if not cache_dir:
        return None
    return Path(cache_dir) / _tile_directory(tile) / f"{tile}.hgt"


def _hgt_size_ok(path):
    """Целый ли файл ``.hgt``: 1201 или 3601 точка на сторону.

    Обрезанный после аварийного завершения файл нельзя считать
    готовым: GDAL прочитает его как растр другого размера или с мусором
    на краю.
    """
    try:
        size = Path(path).stat().st_size
    except OSError:
        return False
    return size in (1201 * 1201 * 2, 3601 * 3601 * 2)


def _valid_raster(path):
    """Открывается ли растровый файл и есть ли в нём данные."""
    try:
        if Path(path).stat().st_size <= 0:
            return False
    except OSError:
        return False
    try:
        gdal, _, _ = _gdal()
        dataset = gdal.Open(str(path))
    except Exception:  # noqa: BLE001 - битый файл считаем отсутствующим
        return False
    if dataset is None:
        return False
    try:
        return (
            dataset.RasterCount > 0
            and dataset.RasterXSize > 0
            and dataset.RasterYSize > 0
        )
    finally:
        dataset = None


class TerrainUnavailable(RuntimeError):
    """DEM не собран; причина известна и должна попасть в журнал."""


def _connection_warning():
    """Понятное сообщение о сетевом сбое при скачивании рельефа."""
    reason = net.last_error().strip() or net.OFFLINE_REASON
    label = net.proxy_label()
    if label:
        return i18n.trf(
            "Рельеф: нет соединения с сервером — тайлы DEM не скачаны: {}. "
            "Проверьте настройки прокси ({}) и доступность сети.",
            reason, label,
        )
    return i18n.trf(
        "Рельеф: нет соединения с сервером — тайлы DEM не скачаны: {}. "
        "Проверьте доступность сети.",
        reason,
    )


class _Speed:
    """Скорость загрузки с усреднением по окну времени."""

    def __init__(self, window=0.5):
        self.window = window
        self.total = 0
        self.speed = 0.0
        self._mark = 0
        self._time = time.monotonic()
        self._lock = threading.Lock()

    def add(self, size):
        """Добавляет байты. True, если скорость пересчитана."""
        with self._lock:
            self.total += size
            now = time.monotonic()
            elapsed = now - self._time
            if elapsed < self.window:
                return False
            current = (self.total - self._mark) / elapsed
            self.speed = current if not self.speed else 0.5 * self.speed + 0.5 * current
            self._mark = self.total
            self._time = now
            return True


# Набор рельефа, который за минуту не отдал и 200 КБ/с, заменяется
# следующим по приоритету: иначе одна медленная выдача задерживает
# подготовку на десятки минут.  Порог умышленно низкий — у быстрых
# зеркал скорость в разы выше, у перегруженных в разы ниже.
SLOW_DOWNLOAD_RATE = 200 * 1024
SLOW_DOWNLOAD_WINDOW = 60.0


class SlowDownload(RuntimeError):
    """Набор рельефа отдаёт данные медленнее порога."""


class _SpeedGuard:
    """Сторож скорости скачивания одного набора рельефа.

    Скорость оценивается окнами по :data:`SLOW_DOWNLOAD_WINDOW` секунд.
    У Sonny байты приходят из читающего цикла (:meth:`add`); для
    растровых наборов GDAL не даёт отдельного счётчика для одного
    набора, а общий сетевой счётчик процесса смешал бы рельеф с
    параллельной загрузкой OSM.  Поэтому меряется прирост файлов слоя —
    тайлы, приведённые к общей сетке, пишутся в них по мере чтения;
    величина пропорциональна прочитанным по сети данным.

    Окно начинается с начала загрузки набора; подпись ссылок и проверка
    каталога в него не входят.  Сторож с ``measure`` стартует при
    создании, сторож без него — с первого байта в :meth:`add`.
    """

    def __init__(self, measure=None, window=SLOW_DOWNLOAD_WINDOW,
                 threshold=SLOW_DOWNLOAD_RATE):
        self._measure = measure
        self._window = window
        self._threshold = threshold
        self._lock = threading.Lock()
        self._extra = 0
        self._base = self._measure_bytes()
        self._mark_bytes = 0
        self._mark_time = time.monotonic() if measure is not None else None
        self.slow = False
        self.speed = 0.0

    def _measure_bytes(self):
        if self._measure is None:
            return 0
        try:
            return int(self._measure() or 0)
        except OSError:
            return 0

    def _total(self):
        return self._extra + max(0, self._measure_bytes() - self._base)

    def add(self, size):
        """Добавляет прочитанные байты; :class:`SlowDownload` — медленно."""
        with self._lock:
            if self._mark_time is None:
                self._mark_time = time.monotonic()
            self._extra += size
        self.check()
        if self.slow:
            raise SlowDownload(self.reason())

    def check(self):
        """True — последнее окно оказалось медленнее порога."""
        with self._lock:
            if self.slow:
                return True
            if self._mark_time is None:
                return False
            now = time.monotonic()
            elapsed = now - self._mark_time
            if elapsed <= 0 or elapsed < self._window:
                return False
            total = self._total()
            self.speed = max(0, total - self._mark_bytes) / elapsed
            self._mark_time = now
            self._mark_bytes = total
            if self.speed < self._threshold:
                self.slow = True
            return self.slow

    def reason(self):
        """Короткая причина для журнала: измеренная скорость и порог."""
        return (
            f"{self.speed / 1024:.0f} КБ/с < "
            f"{self._threshold / 1024:.0f} КБ/с"
        )


def _layer_bytes(root, prefix):
    """Байты, записанные слоем: сам растр и окна предзагрузки."""
    total = 0
    for entry in Path(root).glob(prefix + "*"):
        try:
            if entry.is_dir():
                total += sum(child.stat().st_size for child in entry.iterdir())
            else:
                total += entry.stat().st_size
        except OSError:
            # Файл мог исчезнуть между обходом каталога и stat.
            continue
    return total


def download_srtm(bbox, destination, progress=None, cancel=None, cache_dir=None,
                  status=None, tiles=None, required_tiles=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    tiles = list(tiles) if tiles else srtm_tile_names(bbox)
    if tiles and required_tiles is not None:
        wanted = {
            dem_sources.tile_name(lat, lon)
            for lat, lon in required_tiles
        }
        tiles = [tile for tile in tiles if tile in wanted]
    meter = _Speed()
    pending = []
    for tile in tiles:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        target = destination / f"{tile}.hgt"
        if target.exists():
            if _hgt_size_ok(target):
                continue
            target.unlink(missing_ok=True)
        for existing in destination.glob(f"{tile}.*"):
            if existing.suffix.lower() in (".tif", ".hgt"):
                break
        else:
            cached = _cached_tile(cache_dir, tile)
            if cached is not None and cached.exists():
                if _hgt_size_ok(cached):
                    shutil.copy2(cached, target)
                    if progress:
                        progress(f"Using cached DEM tile {tile}")
                    continue
                # Обрезанный тайл не должен переиспользоваться вечно.
                cached.unlink(missing_ok=True)
            pending.append((tile, target, cached))
    if not pending:
        return tiles

    workers = min(DEM_DOWNLOAD_WORKERS, len(pending))
    source_title = i18n.tr(dem_sources.source("srtm").title)

    def fetch(item):
        tile, target, cached = item
        url = (
            "https://s3.amazonaws.com/elevation-tiles-prod/skadi/"
            f"{_tile_directory(tile)}/{tile}.hgt.gz"
        )
        if progress:
            progress(
                i18n.trf(
                    "Загрузка тайла рельефа: {} ({})",
                    f"{tile}.hgt.gz", source_title,
                )
            )
        temporary = destination / f"{tile}.hgt.gz"
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("DEM download cancelled")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if meter.add(len(chunk)) and status:
                        status(
                            f"Загрузка рельефа: "
                            f"{disk_space.format_decimal(meter.total / 1048576)}"
                            f" МБ · "
                            f"{disk_space.format_decimal(meter.speed / 1048576)}"
                            f" МБ/с"
                        )
            with gzip.open(temporary, "rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        except InterruptedError:
            raise
        except Exception as exc:
            if progress:
                progress(f"WARNING: cannot download {tile}: {exc}")
            target.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        if target.exists() and not _hgt_size_ok(target):
            if progress:
                progress(i18n.trf(
                    "WARNING: тайл рельефа {} имеет неожиданный размер", tile
                ))
            target.unlink(missing_ok=True)
        if cached is not None and target.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, cached)

    workers = min(DEM_DOWNLOAD_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in pending]
        for future in as_completed(futures):
            future.result()
    return tiles


def copy_dem_tiles(bbox, source_dir, destination, progress=None):
    source_dir = Path(source_dir).expanduser()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for tile in srtm_tile_names(bbox):
        for extension in (".tif", ".hgt", ".zip"):
            candidate = source_dir / f"{tile}{extension}"
            if candidate.exists():
                shutil.copy2(candidate, destination / candidate.name)
                if progress:
                    progress(f"Using {candidate.name}")
                break
        else:
            if progress:
                progress(f"WARNING: DEM tile not found: {tile}")


def _padded_bbox(bbox, padding=COG_PADDING):
    """Область с запасом вокруг: у края не остаётся полосы nodata."""
    return BBox(
        max(-180.0, bbox.lon_min - padding),
        max(-90.0, bbox.lat_min - padding),
        min(180.0, bbox.lon_max + padding),
        min(90.0, bbox.lat_max + padding),
    )


def _cached_cog(cache_dir, source_key, bbox):
    if not cache_dir:
        return None
    folder = Path(cache_dir) / "cog" / source_key
    name = (
        f"v1_{bbox.lon_min:.6f}_{bbox.lat_min:.6f}_"
        f"{bbox.lon_max:.6f}_{bbox.lat_max:.6f}.tif"
    )
    return folder / name


def _remote_file_name(url):
    """Имя скачиваемого файла из URL для журнала (без query-строки)."""
    path = url.split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def _item_overlaps(item, bbox):
    """Пересекается ли объект STAC с областью.

    STAC иногда отдаёт тайлы на градус южнее и западнее запроса.  Они не
    покрывают область, но их ссылки подписываются и читаются при сборке
    мозаики, поэтому лишние объекты отбрасываются сразу.
    """
    values = item.get("bbox")
    if not values or len(values) != 4:
        return True
    west, south, east, north = values
    return not (
        east <= bbox.lon_min or west >= bbox.lon_max
        or north <= bbox.lat_min or south >= bbox.lat_max
    )


def _signed_pc_url(href, label, progress=None):
    """Подписанная ссылка Planetary Computer; None — подпись не удалась.

    Подпись — сетевой запрос с ретраями: исчерпание их не должно
    валить всю стадию рельефа.  Источник пропускается, недостающие
    тайлы добираются SRTM.
    """
    try:
        return dem_sources.sign_pc(href)
    except Exception as exc:  # noqa: BLE001 - сеть или лимит подписей
        if progress:
            progress(f"WARNING: не удалось подписать ссылку {label}: {exc}")
        return None


def _cog_sources(source_key, bbox, progress=None, cancel=None):
    """Ссылки на растровые источники набора для области."""
    item = dem_sources.source(source_key)
    if item.direct_tiles:
        return _direct_sources(item, bbox, progress=progress, cancel=cancel)
    try:
        items = dem_sources.stac_items(
            item.collection,
            (bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - каталог STAC отвечает медленно
        items = []
    items = [entry for entry in items if _item_overlaps(entry, bbox)]
    if source_key == "3dep":
        # В коллекции есть и 1″, и 1/3″; нужен более подробный слой.
        preferred = [entry for entry in items
                     if entry.get("id", "").endswith("-13")]
        if preferred:
            items = preferred
    if progress:
        progress(f"Найдено тайлов {item.short}: {len(items)}")
    sources = []
    if source_key == "cop30":
        # Открытое зеркало в AWS неполное, часть тайлов лежит только в
        # каталоге STAC: сначала пробуем адрес без подписи, а для
        # остальных подписываем ссылку из каталога.
        entries = []
        for entry in items:
            tile = dem_sources.item_tile(entry)
            if tile is None:
                continue
            entries.append((tile, dem_sources.item_asset_href(entry)))
        existing = dem_sources.cop30_existing_tiles(
            [tile for tile, _href in entries]
        )
        for tile, href in entries:
            if tile in existing:
                url = dem_sources.aws_tile_url("cop30", *tile)
            elif href:
                url = _signed_pc_url(href, item.short, progress)
                if url is None:
                    continue
            else:
                continue
            sources.append("/vsicurl/" + url)
            if progress:
                progress(
                    i18n.trf(
                        "Загрузка тайла рельефа: {} ({})",
                        _remote_file_name(url), item.short,
                    )
                )
        if not sources:
            # Каталог STAC недоступен: остаются тайлы открытого зеркала.
            for tile in dem_sources.cop30_existing_tiles(
                dem_sources.tiles_for_bbox(bbox)
            ):
                url = dem_sources.aws_tile_url("cop30", *tile)
                sources.append("/vsicurl/" + url)
                if progress:
                    progress(
                        i18n.trf(
                            "Загрузка тайла рельефа: {} ({})",
                            _remote_file_name(url), item.short,
                        )
                    )
        return sources
    for entry in items:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        href = dem_sources.item_asset_href(entry)
        if not href:
            continue
        url = _signed_pc_url(href, item.short, progress)
        if url is None:
            continue
        sources.append("/vsicurl/" + url)
        if progress:
            progress(
                i18n.trf(
                    "Загрузка тайла рельефа: {} ({})",
                    _remote_file_name(url), item.short,
                )
            )
    return sources


def _direct_sources(item, bbox, progress=None, cancel=None):
    """Ссылки на тайлы набора без каталога STAC.

    Адрес файла строится по координатам тайла, наличие проверяется
    HEAD-запросом: у FABDEM тайлы опубликованы не для всей суши.
    """
    tiles = dem_sources.existing_tiles(
        item, dem_sources.tiles_for_bbox(bbox)
    )
    sources = []
    for lat, lon in sorted(tiles):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        url = dem_sources.source_tile_url(item.key, lat, lon)
        sources.append("/vsicurl/" + url)
        if progress:
            progress(
                i18n.trf(
                    "Загрузка тайла рельефа: {} ({})",
                    _remote_file_name(url), item.short,
                )
            )
    return sources


def _native_resolution(path):
    """Шаг сетки источника в градусах (None — не удалось открыть)."""
    gdal, _, _ = _gdal()
    dataset = gdal.Open(str(path))
    if dataset is None:
        return None
    try:
        transform = dataset.GetGeoTransform()
        spatial_ref = dataset.GetSpatialRef()
        step = max(abs(transform[1]), abs(transform[5]))
        if spatial_ref is not None and spatial_ref.IsGeographic():
            return step
        return step / 111320.0
    finally:
        dataset = None


def _warp_callback(cancel, status, label, guard=None):
    """Обратный вызов GDAL: отмена, сторож скорости и проценты."""
    state = {"last": 0.0}

    def callback(complete, message, data):
        if cancel is not None and cancel.is_set():
            return 0
        if guard is not None and guard.check():
            # Медленный набор прерывается: его тайлы берёт следующий.
            return 0
        now = time.monotonic()
        if status and now - state["last"] >= 0.5:
            state["last"] = now
            status(f"Загрузка рельефа {label}: {complete:.0%}")
        return 1

    return callback


def _warp_with_retry(gdal, target, sources, options, label, cancel=None,
                     progress=None, refresh=None, guard=None):
    """gdal.Warp с повторами: сетевой сбой не должен валить рельеф.

    ``refresh`` вызывается перед повторной попыткой и возвращает новые
    источники: подписанные ссылки Planetary Computer живут час, а сборка
    большого окна может идти дольше.  Если после всех попыток растр так и
    не собрался, в сообщении остаётся причина от GDAL — иначе в журнале
    видно только название набора.

    ``guard`` прерывает сборку медленного набора без повторных попыток:
    медленное зеркало от повтора быстрее не станет.
    """
    target = Path(target)
    detail = ""
    for attempt in range(1, WARP_ATTEMPTS + 1):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        try:
            result = gdal.Warp(str(target), sources, options=options)
        except Exception as exc:  # noqa: BLE001 - сеть или GDAL
            detail = str(exc).strip()[:300]
            result = None
        if result is not None:
            return result
        target.unlink(missing_ok=True)
        if guard is not None and guard.slow:
            raise SlowDownload(guard.reason())
        if attempt == WARP_ATTEMPTS:
            break
        reason = f": {detail}" if detail else ""
        if progress:
            progress(
                f"WARNING: сборка рельефа {label} не удалась{reason}; повтор"
            )
        time.sleep(WARP_RETRY_DELAY * attempt)
        if refresh is not None:
            sources = refresh()
            if not sources:
                break
    if cancel is not None and cancel.is_set():
        raise InterruptedError("DEM download cancelled")
    reason = f": {detail}" if detail else ""
    raise RuntimeError(f"Не удалось собрать рельеф {label}{reason}")


def download_cog_dem(source_key, bbox, destination, progress=None, cancel=None,
                     cache_dir=None, status=None):
    """Скачивает окно растрового набора (COG) в локальный GeoTIFF.

    Целиком многогигабайтные тайлы не качаются: GDAL читает нужные блоки
    по HTTP Range.  Результат — один файл на область в EPSG:4326.
    """
    gdal, _, _ = _gdal()
    item = dem_sources.source(source_key)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"dem_{source_key}.tif"
    cached = _cached_cog(cache_dir, source_key, bbox)
    if cached is not None and cached.exists():
        if _valid_raster(cached):
            shutil.copy2(cached, target)
            if progress:
                progress(f"Использован кэш рельефа: {item.short}")
            return target
        # Недописанный растр от прерванной загрузки не переиспользуем.
        cached.unlink(missing_ok=True)

    padded = _padded_bbox(bbox)
    if progress:
        progress(f"Загрузка рельефа {item.short}")
    sources = _cog_sources(source_key, bbox, progress=progress, cancel=cancel)
    if not sources:
        raise RuntimeError(
            f"Нет данных {item.short} для выбранного охвата"
        )

    resolution = _native_resolution(sources[0]) or item.native_resolution
    span_x = padded.lon_max - padded.lon_min
    span_y = padded.lat_max - padded.lat_min
    width = max(1, int(math.ceil(span_x / resolution)))
    height = max(1, int(math.ceil(span_y / resolution)))
    if width * height > MAX_DEM_PIXELS:
        factor = math.sqrt(width * height / MAX_DEM_PIXELS)
        width = max(1, int(width / factor))
        height = max(1, int(height / factor))
        reduced = max(span_x / width, span_y / height)
        if progress:
            progress(
                "Большой охват: разрешение снижено до "
                f"~{reduced * 111320:.0f} м"
            )

    temporary = target.with_name(target.name + ".part")
    options = gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:4326",
        outputBounds=(padded.lon_min, padded.lat_min,
                      padded.lon_max, padded.lat_max),
        width=width,
        height=height,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Int16,
        dstNodata=-32768,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES", "TILED=YES"],
        multithread=True,
        callback=_warp_callback(cancel, status, item.short),
    )
    _warp_with_retry(
        gdal, temporary, sources, options, item.short,
        cancel=cancel, progress=progress,
        refresh=lambda: _cog_sources(
            source_key, bbox, progress=progress, cancel=cancel
        ),
    )
    temporary.replace(target)
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary_cache = cached.with_name(cached.name + ".tmp")
        shutil.copy2(target, temporary_cache)
        temporary_cache.replace(cached)
    return target


def download_dem(source_key, bbox, destination, progress=None, cancel=None,
                 cache_dir=None, status=None, required_tiles=None):
    """Скачивает рельеф выбранного источника для области."""
    if source_key == dem_sources.MANUAL_SOURCE:
        raise ValueError("Manual DEM source does not download tiles")
    if source_key == "srtm":
        return download_srtm(
            bbox, destination, progress=progress, cancel=cancel,
            cache_dir=cache_dir, status=status, required_tiles=required_tiles,
        )
    return download_cog_dem(
        source_key, bbox, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status,
    )


# --- Мозаика рельефа ----------------------------------------------------

# Порядок наборов в мозаике: чем позже набор в списке, тем выше его
# приоритет — данные более приоритетного набора перекрывают остальные.
AUTO_ORDER = ("srtm", dem_sources.AUTO_BACKGROUND, "fabdem", "alos", "sonny", "3dep")


def _cells_window(cells):
    """Прямоугольник, покрывающий тайлы 1°×1°."""
    if not cells:
        return None
    lats = [cell[0] for cell in cells]
    lons = [cell[1] for cell in cells]
    return BBox(min(lons), min(lats), max(lons) + 1.0, max(lats) + 1.0)


def _mosaic_grid(span_x, span_y, resolutions):
    """Шаг сетки мозаики: самый подробный, который влезает в бюджет.

    Возвращает тройку ``(шаг, ширина, высота)``.  Если ни один шаг не
    помещается в предел по числу пикселей, берётся самый грубый и он
    дополнительно укрупняется.
    """
    candidates = sorted({value for value in resolutions if value > 0})
    if not candidates:
        candidates = [1.0 / 3600.0]
    for resolution in candidates:
        width = max(1, int(math.ceil(span_x / resolution)))
        height = max(1, int(math.ceil(span_y / resolution)))
        if width * height <= MAX_DEM_PIXELS:
            return resolution, width, height
    resolution = candidates[-1]
    width = max(1, int(math.ceil(span_x / resolution)))
    height = max(1, int(math.ceil(span_y / resolution)))
    factor = math.sqrt(width * height / MAX_DEM_PIXELS)
    width = max(1, int(width / factor))
    height = max(1, int(height / factor))
    # Шаг по длинной оси, а не всегда по X: для вытянутого охвата
    # span_x / width занижает реальное разрешение.
    return max(span_x / width, span_y / height), width, height


def _layer_options(gdal, bounds, width, height, cancel, status, label,
                   guard=None):
    """Параметры приведения растра к общей сетке мозаики."""
    return gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:4326",
        outputBounds=(bounds.lon_min, bounds.lat_min,
                      bounds.lon_max, bounds.lat_max),
        width=width,
        height=height,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Int16,
        dstNodata=-32768,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES", "TILED=YES"],
        multithread=True,
        callback=_warp_callback(cancel, status, label, guard),
    )


def _write_layer(gdal, sources, bounds, width, height, destination, name,
                 cancel, status, label, progress=None, guard=None):
    """Приводит набор источников к общей сетке и пишет слой мозаики.

    Источники, которые не открылись (отсутствующий тайл набора),
    пропускаются: в мозаике останется пустое место.

    ``guard`` следит за скоростью: медленный набор прерывается
    исключением :class:`SlowDownload`.
    """
    # Удалённые COG читаются по HTTP Range, и чтение многих маленьких
    # блоков через сеть — самая долгая часть.  Окна тайлов скачиваются
    # параллельно в локальные файлы, а warp идёт уже по ним.
    parts_dir = None
    if len(sources) > 1:
        local = _fetch_cog_windows(
            gdal, sources, bounds, width, height, destination, name, cancel,
            progress=progress, guard=guard,
        )
        if local is not None:
            sources = local
            parts_dir = Path(destination) / f"{name}.parts"
    try:
        return _warp_sources(
            gdal, sources, bounds, width, height, destination, name,
            cancel, status, label, progress=progress, guard=guard,
        )
    finally:
        if parts_dir is not None:
            shutil.rmtree(parts_dir, ignore_errors=True)


def _warp_sources(gdal, sources, bounds, width, height, destination, name,
                  cancel, status, label, progress=None, guard=None):
    datasets = []
    for source in sources:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        try:
            dataset = gdal.Open(source)
        except Exception:  # noqa: BLE001 - нет данных за набором
            dataset = None
        if dataset is not None:
            datasets.append(dataset)
    if not datasets:
        return None
    target = Path(destination) / name
    temporary = target.with_name(target.name + ".part")
    options = _layer_options(
        gdal, bounds, width, height, cancel, status, label, guard
    )
    try:
        _warp_with_retry(
            gdal, temporary, datasets, options, label,
            cancel=cancel, progress=progress, guard=guard,
        )
    finally:
        for dataset in datasets:
            dataset = None
    temporary.replace(target)
    return target


def _fetch_cog_windows(gdal, sources, bounds, width, height, destination,
                       name, cancel, progress=None, guard=None):
    """Готовит удалённые COG локально, параллельно.

    Каждый источник приводится к общей сетке мозаики отдельным warp'ом:
    области вне тайла получают nodata, поэтому при финальной сборке
    источники не перекрывают друг друга нулями.  Возвращает список
    локальных путей или None, если скачивать нечего.

    Часть источников остаётся удалённой: каждая локальная копия —
    это целая сетка мозаики, и без ограничения их число растёт вместе
    с числом тайлов, а место на диске никто не проверял.
    """
    remote = [
        index for index, source in enumerate(sources)
        if str(source).startswith("/vsicurl/")
    ]
    if len(remote) < 2:
        return None
    remote = remote[:MAX_COG_PREFETCH_PARTS]
    # Места под части может не хватить: каждая часть — это сетка
    # мозаики целиком (высоты int16, два байта на пиксель).
    per_part = width * height * 2
    free = disk_space.free_bytes(destination)
    if free is not None:
        available = free - disk_space.RESERVE
        if available < per_part:
            if progress:
                progress(
                    "WARNING: мало места для предзагрузки рельефа — "
                    "тайлы читаются по сети"
                )
            return None
        limit = min(len(remote), int(available // per_part))
        if limit < 2:
            if progress:
                progress(
                    "WARNING: мало места для предзагрузки рельефа — "
                    "тайлы читаются по сети"
                )
            return None
        remote = remote[:limit]
    parts_dir = Path(destination) / f"{name}.parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    local = list(sources)
    changed = False

    def fetch(index):
        source = sources[index]
        if cancel is not None and cancel.is_set():
            return index, None
        try:
            dataset = gdal.Open(source)
            if dataset is None:
                return index, None
            transform = dataset.GetGeoTransform()
            step = max(abs(transform[1]), abs(transform[5]))
            span_x = bounds.lon_max - bounds.lon_min
            span_y = bounds.lat_max - bounds.lat_min
            if step > 0 and span_x * span_y / (step * step) > (
                    MAX_COG_PREFETCH_PIXELS):
                dataset = None
                return index, None
            path = parts_dir / f"{index}.tif"
            options = gdal.WarpOptions(
                format="GTiff",
                dstSRS="EPSG:4326",
                outputBounds=(bounds.lon_min, bounds.lat_min,
                              bounds.lon_max, bounds.lat_max),
                width=width,
                height=height,
                resampleAlg="bilinear",
                outputType=gdal.GDT_Int16,
                dstNodata=-32768,
                creationOptions=["COMPRESS=LZW", "BIGTIFF=YES", "TILED=YES"],
                callback=_warp_callback(cancel, None, name, guard),
            )
            result = gdal.Warp(str(path), dataset, options=options)
            dataset = None
            if result is None:
                path.unlink(missing_ok=True)
                return index, None
            result = None
            return index, path
        except Exception:  # noqa: BLE001 - тайл останется удалённым
            return index, None

    workers = max(1, min(COG_PREFETCH_WORKERS, len(remote)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, path in pool.map(fetch, remote):
            if path is not None:
                local[index] = path
                changed = True
    if guard is not None and guard.slow:
        shutil.rmtree(parts_dir, ignore_errors=True)
        raise SlowDownload(guard.reason())
    if not changed:
        shutil.rmtree(parts_dir, ignore_errors=True)
        return None
    return local


def _cog_layer(source_key, cells, bounds, width, height, destination,
               progress=None, cancel=None, status=None):
    """Слой мозаики из растрового набора (COG читается по HTTP Range)."""
    gdal, _, _ = _gdal()
    item = dem_sources.source(source_key)
    window = _cells_window(cells)
    sources = _cog_sources(source_key, window, progress=progress, cancel=cancel)
    if not sources:
        return None
    guard = _SpeedGuard(
        measure=lambda: _layer_bytes(destination, f"layer_{source_key}.tif")
    )
    return _write_layer(
        gdal, sources, bounds, width, height, destination,
        f"layer_{source_key}.tif", cancel, status, item.short,
        progress=progress, guard=guard,
    )


def _srtm_layer(cells, bounds, width, height, destination, cache_dir=None,
                 progress=None, cancel=None, status=None, name="layer_srtm.tif"):
    """Слой мозаики из тайлов SRTM."""
    gdal, _, _ = _gdal()
    names = [dem_sources.tile_name(*cell) for cell in sorted(cells)]
    if not names:
        return None
    download_srtm(
        None, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status, tiles=names,
    )
    files = [str(Path(destination) / f"{tile}.hgt") for tile in names]
    files = [item for item in files if Path(item).exists()]
    if not files:
        return None
    return _write_layer(
        gdal, files, bounds, width, height, destination,
        name, cancel, status, "SRTM v3", progress=progress,
    )


def _sonny_layers(cells, catalogue, tile_dir, bounds, width, height,
                  destination, progress=None, cancel=None, status=None):
    """Слои мозаики из наборов Sonny DTM.

    Наборы разной подробности кладутся отдельными слоями: более
    подробный идёт позже и перекрывает более грубый.
    """
    gdal, _, _ = _gdal()
    assignment = sonny.assign_datasets(catalogue, cells)
    groups = {}
    for cell, dataset_key in assignment.items():
        groups.setdefault(dataset_key, set()).add(cell)
    layers = []
    for dataset in sorted(sonny.DATASETS, key=lambda item: -item.rank):
        dataset_cells = groups.get(dataset.key)
        if not dataset_cells:
            continue
        # Сторож для каждого набора отдельно: локальная сборка слоя
        # между загрузками не должна выглядеть медленной сетью.
        guard = _SpeedGuard()
        paths = sonny.tile_paths(
            catalogue, tile_dir, dataset.key, dataset_cells,
            progress=progress, cancel=cancel, on_bytes=guard.add,
        )
        files = [str(path) for _cell, path in paths]
        if not files:
            continue
        layer = _write_layer(
            gdal, files, bounds, width, height, destination,
            f"layer_sonny_{dataset.key}.tif", cancel, status,
            i18n.tr(dataset.title), progress=progress,
        )
        if layer is not None:
            layers.append(layer)
    return layers


def _mosaic_resolution(groups):
    """Шаги сетки наборов, входящих в мозаику."""
    resolutions = []
    for key in ("3dep", "cop30", "alos", "srtm"):
        if groups.get(key):
            resolutions.append(dem_sources.source(key).native_resolution)
    for dataset_key in groups.get("sonny", ()):
        dataset = sonny.DATASET_BY_KEY.get(dataset_key)
        if dataset is not None:
            resolutions.append(dataset.native_resolution)
    return resolutions


def _slow_source_targets(moved, left):
    """Куда ушли тайлы медленного набора, для сообщения в журнал."""
    parts = [
        f"{i18n.tr(dem_sources.source(key).short)}: {len(cells)}"
        for key, cells in moved.items()
    ]
    if left:
        # Нераспределённые тайлы добирает SRTM, как и упавший набор.
        parts.append(
            f"{i18n.tr(dem_sources.source('srtm').short)}: {len(left)}"
        )
    return ", ".join(parts)


def _replace_slow_source(key, cells, groups, coverage, order, pending):
    """Отдаёт тайлы медленного набора следующим по приоритету.

    Возвращает ``(moved, left)``: наборы с их новыми тайлами и тайлы,
    для которых подходящего набора в списке не осталось (их доберёт
    SRTM).  Наборы, чей слой ещё не собран, добавляются в очередь
    ``pending``: для новых ячеек нужен отдельный слой.
    """
    moved = {}
    left = set(cells)
    position = order.index(key)
    for candidate in order[position + 1:]:
        if not left:
            break
        covered = left & coverage.get(candidate, set())
        if not covered:
            continue
        groups.setdefault(candidate, set()).update(covered)
        moved[candidate] = moved.get(candidate, set()) | covered
        left -= covered
        if candidate not in pending:
            pending.append(candidate)
    return moved, left


def download_auto_dem(bbox, destination, progress=None, cancel=None,
                      cache_dir=None, status=None, availability=None,
                      sonny_catalogue=None, sonny_tiles_dir=None,
                      priority=None, used=None, counts=None,
                      required_tiles=None):
    """Собирает мозаику рельефа в режиме «Автоматически».

    Для каждого тайла 1°×1° выбирается лучший доступный набор: USGS
    3DEP, затем Sonny DTM, в горах — ALOS AW3D30, фон — Copernicus
    GLO-30, в крайнем случае SRTM.  Наборы кладутся в один растр в
    порядке приоритета, поэтому данные более приоритетного набора
    перекрывают менее приоритетный.

    ``priority`` задаёт свой порядок выбора наборов: для набора Sonny
    DTM это ``(Sonny, FABDEM, Copernicus, SRTM)``.  В ``used`` дописываются
    ключи наборов, вошедших в мозаику, — для строки атрибуции; в
    ``counts`` — число тайлов, доставшихся каждому набору.

    ``required_tiles`` — тайлы, где есть суша (Natural Earth и берег из
    OSM).  Остальные не скачиваются: их высоты всё равно станут nodata
    после маски воды.

    Если суша есть, а тайлов для неё подобрать не удалось, бросается
    :class:`TerrainUnavailable` с понятной причиной: непроверенные из-за
    сети источники или отсутствие покрытия.  Пустой ``required_tiles`` —
    это океанский охват, а не ошибка: функция просто ничего не делает.
    """
    gdal, _, _ = _gdal()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    layers_dir = destination / "_layers"
    if layers_dir.exists():
        shutil.rmtree(layers_dir, ignore_errors=True)
    layers_dir.mkdir(parents=True, exist_ok=True)

    catalogue = sonny.load_catalogue(sonny_catalogue)
    sonny_cells = set(sonny.catalogue_tiles(sonny_catalogue))
    order = tuple(priority) if priority else dem_sources.auto_priority(bbox)
    if availability is None:
        availability = dem_sources.auto_coverage(
            bbox, sonny_tiles=sonny_cells, keys=order,
        )
    plan = dem_sources.auto_plan(
        bbox, availability, sonny_cells, priority=order
    )
    if required_tiles is not None:
        plan = {
            cell: key for cell, key in plan.items()
            if cell in required_tiles
        }
    if not plan:
        if required_tiles is not None and not required_tiles:
            # Суши в охвате нет: скачивать нечего, и в журнал ничего
            # дополнительно писать не нужно — это скажет вызывающая
            # стадия своим сообщением.
            return
        unchecked = [
            dem_sources.source(key).short for key in order
            if availability.get(key) is not None
            and not availability[key].checked
        ]
        if unchecked:
            # Источники не проверены из-за сети: раньше здесь писалось
            # «нет суши», что вводило в заблуждение.
            raise TerrainUnavailable(_connection_warning())
        raise TerrainUnavailable(
            i18n.tr("Рельеф: для суши выбранного охвата нет данных DEM")
        )

    groups = {}
    for cell, key in plan.items():
        groups.setdefault(key, set()).add(cell)
    if progress:
        parts = []
        for key in order:
            if groups.get(key):
                short = i18n.tr(dem_sources.source(key).short)
                parts.append(f"{short}: {len(groups[key])}")
        progress("Мозаика рельефа — " + ", ".join(parts))

    padded = _padded_bbox(bbox)
    span_x = padded.lon_max - padded.lon_min
    span_y = padded.lat_max - padded.lat_min
    sonny_datasets = set()
    if groups.get("sonny"):
        sonny_datasets = set(sonny.assign_datasets(catalogue, groups["sonny"]))
    resolution_groups = dict(groups)
    resolution_groups["sonny"] = sonny_datasets
    resolution, width, height = _mosaic_grid(
        span_x, span_y, _mosaic_resolution(resolution_groups)
    )
    if progress:
        progress(
            f"Разрешение мозаики: ~{resolution * 111320:.0f} м "
            f"({width}×{height})"
        )

    layers = []
    served = {}
    failed_cells = set()
    coverage = {
        key: dem_sources.coverage(availability, key, sonny_cells)
        for key in order
    }
    pending = [key for key in AUTO_ORDER if groups.get(key)]
    while pending:
        key = pending.pop(0)
        cells = groups.get(key, set()) - served.get(key, set())
        if not cells:
            continue
        if key == "srtm":
            layer = _srtm_layer(
                cells, padded, width, height, layers_dir, cache_dir=cache_dir,
                progress=progress, cancel=cancel, status=status,
            )
            made = [layer] if layer else []
        else:
            # Сбой набора (сеть, истёкшая подпись) не должен валить весь
            # рельеф: его тайлы ниже доберёт SRTM, как и при пустом STAC.
            try:
                if key == "sonny":
                    made = _sonny_layers(
                        cells, catalogue, sonny_tiles_dir, padded, width,
                        height, layers_dir, progress=progress, cancel=cancel,
                        status=status,
                    )
                else:
                    layer = _cog_layer(
                        key, cells, padded, width, height, layers_dir,
                        progress=progress, cancel=cancel, status=status,
                    )
                    made = [layer] if layer else []
            except SlowDownload as exc:
                # Медленное зеркало не ждём: тайлы берут наборы ниже по
                # списку приоритета, для новых ячеек собирается слой.
                groups.pop(key, None)
                moved, left = _replace_slow_source(
                    key, cells, groups, coverage, order, pending,
                )
                failed_cells |= left
                if progress:
                    progress(i18n.trf(
                        "WARNING: {} отдаёт слишком медленно ({}); "
                        "тайлы берут: {}",
                        i18n.tr(dem_sources.source(key).short),
                        exc,
                        _slow_source_targets(moved, left),
                    ))
                continue
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 - набор заменит SRTM
                if progress:
                    progress(
                        f"WARNING: не удалось собрать "
                        f"{dem_sources.source(key).short}: {exc}"
                    )
                made = []
        if not made:
            failed_cells |= cells
            continue
        layers.extend(made)
        served.setdefault(key, set()).update(cells)
        if used is not None and key not in used:
            used.append(key)

    # Набор не удалось получить (каталог STAC недоступен, тайла нет в
    # зеркале): его тайлы добираются SRTM.  Слой ставится в начало —
    # приоритет у SRTM самый низкий.  Если SRTM уже в мозаике, для
    # недостающих ячеек делается отдельный слой: иначе в DEM остаются
    # дырки на месте упавшего набора.
    if failed_cells:
        fill = ("layer_srtm.tif" if "srtm" not in served
                else "layer_srtm_fill.tif")
        layer = _srtm_layer(
            failed_cells, padded, width, height, layers_dir,
            cache_dir=cache_dir, progress=progress, cancel=cancel,
            status=status, name=fill,
        )
        if layer:
            layers.insert(0, layer)
            served.setdefault("srtm", set()).update(failed_cells)
            if used is not None and "srtm" not in used:
                used.append("srtm")

    if counts is not None:
        counts.clear()
        counts.update({key: len(value) for key, value in served.items()})

    if not layers:
        if net.offline():
            raise TerrainUnavailable(_connection_warning())
        raise RuntimeError("Нет данных рельефа для выбранного охвата")

    target = destination / "dem_auto.tif"
    if len(layers) == 1:
        # Один набор — слой уже лежит на нужной сетке, склеивать нечего.
        shutil.move(str(layers[0]), str(target))
        shutil.rmtree(layers_dir, ignore_errors=True)
        return target
    temporary = target.with_name(target.name + ".part")
    options = _layer_options(gdal, padded, width, height, cancel, status,
                             "мозаика")
    result = gdal.Warp(str(temporary), [str(path) for path in layers],
                       options=options)
    if result is None:
        temporary.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        raise RuntimeError("Не удалось собрать мозаику рельефа")
    result = None
    temporary.replace(target)
    shutil.rmtree(layers_dir, ignore_errors=True)
    return target


def download_sonny_dem(bbox, destination, progress=None, cancel=None,
                       cache_dir=None, status=None, availability=None,
                       sonny_catalogue=None, sonny_tiles_dir=None,
                       used=None, required_tiles=None):
    """Скачивает рельеф Sonny DTM, а недостающие тайлы — фоном FABDEM.

    Набор выбран один, но тайлы в нём могут быть разной подробности:
    0,5″ для Австрии и Швейцарии, 1″ для остальной Европы.  Вне покрытия
    Sonny фоном идут FABDEM, затем Copernicus GLO-30.
    """
    return download_auto_dem(
        bbox, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status, availability=availability,
        sonny_catalogue=sonny_catalogue, sonny_tiles_dir=sonny_tiles_dir,
        priority=(dem_sources.SONNY_SOURCE, "fabdem",
                  dem_sources.AUTO_BACKGROUND, "srtm"),
        used=used, required_tiles=required_tiles,
    )


def _download_cop30(config, bbox, progress=None, cancel=None, status=None,
                    required_tiles=None):
    """GLO-30 с добором недостающих тайлов SRTM v3.

    Если GLO-30 покрывает весь охват, работает обычный путь с кэшем
    окон.  Иначе тайлы раскладываются по наборам: покрытые берутся из
    GLO-30, остальные — из SRTM v3; в ``terrain_fallback`` попадает
    сообщение для панели.
    """
    availability = dem_sources.auto_coverage(
        bbox, keys=("cop30", "srtm")
    )
    state = availability.get("cop30")
    required = (
        set(required_tiles) if required_tiles is not None
        else set(dem_sources.tiles_for_bbox(bbox))
    )
    covered = (set(state.covered_tiles)
               if state is not None and state.checked else set())
    if not required:
        return []
    if required <= covered:
        download_cog_dem(
            "cop30", bbox, config.terrain_input_dir,
            progress=progress, cancel=cancel,
            cache_dir=config.terrain_cache_dir, status=status,
        )
        return ["cop30"]
    used = []
    counts = {}
    download_auto_dem(
        bbox, config.terrain_input_dir,
        progress=progress, cancel=cancel,
        cache_dir=config.terrain_cache_dir, status=status,
        availability=availability, used=used, counts=counts,
        priority=("cop30", "srtm"), required_tiles=required_tiles,
    )
    if "srtm" in used:
        if "cop30" in used:
            config.terrain_fallback = i18n.trf(
                "GLO-30: {} из {} тайлов заменены SRTM v3.",
                counts.get("srtm", 0), sum(counts.values()),
            )
        else:
            config.terrain_fallback = i18n.tr(
                "GLO-30 недоступен: использован SRTM v3."
            )
    return used


def _clear_terrain_input(config):
    """Убирает прошлые тайлы: иначе в сборку попадут данные другого набора."""
    try:
        entries = list(config.terrain_input_dir.iterdir())
    except OSError:
        return
    for path in entries:
        if path.is_file() and path.suffix.lower() in (".tif", ".hgt", ".zip", ".vrt"):
            path.unlink(missing_ok=True)


def _relief_creation_options():
    """Параметры GTiff для растров рельефа.

    Многопоточное сжатие (NUM_THREADS) в GDAL 3.8 портит крупные файлы:
    полосы данных ложатся не по своим смещениям, файл раздувается в
    несколько раз и читается с ошибкой.  Растры рельефа пишем однопоточно.
    """
    return ["COMPRESS=LZW", "BIGTIFF=YES"]


RASTER_BLOCK_SIDE = 2048


def _write_error(destination_path, exc):
    """Понятная ошибка записи вместо сообщения GDAL о месте.

    GDAL на заполненном томе пишет «TIFFAppendToStrip:Write error», из
    чего не видно, что закончилось место.  Если места действительно
    нет, сообщаем об этом прямо.
    """
    free = disk_space.free_bytes(destination_path)
    if free is not None and free < disk_space.RESERVE:
        return disk_space.out_of_space(Path(destination_path).name)
    return exc


def _transform_raster(source_path, destination_path, operation, dtype=None,
                      nodata=None, cancel=None):
    """Пишет растр поблочно, применяя поэлементную функцию.

    Читать весь растр в numpy нельзя: на области 300 км массив DEM
    высокого разрешения занимает больше четырёх гигабайт.  Блок
    2048×2048 — это около 16 МБ на float32.

    ``dtype`` — тип данных выхода (по умолчанию float32); для значений
    0…255 достаточно байта на пиксель.  ``nodata`` — значение nodata на
    выходе (по умолчанию берётся из источника): пиксели с nodata
    источника остаются nodata, иначе маска ``useSrcNoData`` в шаблоне
    не работает.
    """
    gdal, _, _ = _gdal()
    numpy = _numpy()
    source = gdal.Open(str(source_path))
    if source is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")
    driver = gdal.GetDriverByName("GTiff")
    width = source.RasterXSize
    height = source.RasterYSize
    source_band = source.GetRasterBand(1)
    source_nodata = source_band.GetNoDataValue()
    if nodata is None:
        nodata = source_nodata
    if dtype is None:
        dtype = gdal.GDT_Float32
    byte_output = dtype == gdal.GDT_Byte
    # Места под растр может не хватить: без проверки GDAL обрывает
    # запись посреди работы и оставляет битый файл.
    disk_space.require_space(
        destination_path,
        width * height * (
            disk_space.FILE_BYTE_PIXEL if byte_output
            else disk_space.FILE_FLOAT_PIXEL
        ),
        "Недостаточно места для растра {}: нужно ≈{}, свободно {}.",
        Path(destination_path).name,
    )
    output = driver.Create(
        str(destination_path),
        width,
        height,
        1,
        dtype,
        options=_relief_creation_options(),
    )
    output.SetGeoTransform(source.GetGeoTransform())
    output.SetProjection(source.GetProjection())
    target_band = output.GetRasterBand(1)
    if nodata is not None:
        target_band.SetNoDataValue(nodata)
    try:
        for y in range(0, height, RASTER_BLOCK_SIDE):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Relief preparation cancelled")
            rows = min(RASTER_BLOCK_SIDE, height - y)
            for x in range(0, width, RASTER_BLOCK_SIDE):
                columns = min(RASTER_BLOCK_SIDE, width - x)
                array = source_band.ReadAsArray(x, y, columns, rows)
                if array is None:
                    continue
                block = operation(array.astype("float32"))
                if nodata is not None and source_nodata is not None:
                    invalid = array == source_nodata
                    if invalid.any():
                        block[invalid] = nodata
                if byte_output:
                    block = numpy.clip(numpy.rint(block), 0, 255).astype("uint8")
                try:
                    target_band.WriteArray(block, x, y)
                except RuntimeError as exc:
                    error = _write_error(destination_path, exc)
                    if error is exc:
                        raise
                    raise error from exc
        target_band.FlushCache()
    except Exception:
        # Незаконченный файл не оставляем: он только зря занимает место.
        Path(destination_path).unlink(missing_ok=True)
        raise
    finally:
        output = None
        source = None


def _remove_stale_projected_dem(config):
    """Убрать копию DEM в EPSG:3857 из проектов прежних версий."""
    stale = config.raster_dir / "merged_dem_3857.tif"
    stale.unlink(missing_ok=True)
    stale.with_suffix(stale.suffix + ".sha256").unlink(missing_ok=True)


def build_dem(config, bbox, progress=None, cancel=None):
    gdal, _, _ = _gdal()
    tiles = sorted(
        [path for path in config.terrain_input_dir.iterdir() if path.suffix.lower() in (".tif", ".hgt")]
    )
    if not tiles:
        raise RuntimeError(
            f"No DEM tiles found in {config.terrain_input_dir}. "
            "Enable SRTM download or provide a DEM directory."
        )
    source = str(tiles[0])
    vrt_path = None
    if len(tiles) > 1:
        if progress:
            progress(f"Merging {len(tiles)} DEM tile(s)")
        vrt_path = config.temp_dir / "input_terrain.vrt"
        source = gdal.BuildVRT(str(vrt_path), [str(path) for path in tiles])
        if source is None:
            raise RuntimeError("Failed to build a VRT from DEM tiles")
    crop = config.crop_path
    merged = config.raster_dir / "merged_dem.tif"
    # Прежний файл мог быть жёсткой ссылкой на кэш: перед записью его
    # обязательно убираем, иначе GDAL перезапишет кэш на месте.
    merged.unlink(missing_ok=True)
    options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=str(crop),
        cropToCutline=True,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
        callback=_warp_callback(cancel, None, "DEM"),
    )
    result = gdal.Warp(str(merged), source, options=options)
    source = None
    if vrt_path is not None:
        vrt_path.unlink(missing_ok=True)
    if result is None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM preparation cancelled")
        raise RuntimeError("Failed to crop and merge DEM tiles")
    result = None

    _remove_stale_projected_dem(config)
    return merged


def _dem_vertical_scale(path):
    """Во сколько метров один шаг растра по горизонтали.

    gdaldem считает уклон как разницу высот, делённую на расстояние
    между пикселями, и умножает разницу на ``scale`` — это перевод
    единицы длины СК в метры.  Для географической СК шаг задан в
    градусах, поэтому нужен 111120 (доля градуса в метрах).  Для
    проекционной СК в метрах масштаб равен единице: с зашитым 111120
    уклон занижался в сотни тысяч раз, а отмывка получалась плоской.
    """
    gdal, _, osr = _gdal()
    dataset = gdal.Open(str(path))
    if dataset is None:
        return GEOGRAPHIC_SCALE
    try:
        projection = dataset.GetProjection()
        if not projection:
            return GEOGRAPHIC_SCALE
        srs = osr.SpatialReference()
        srs.ImportFromWkt(projection)
        if srs.IsGeographic():
            return GEOGRAPHIC_SCALE
        # Проекционная СК: сколько метров в одной единице длины.
        units = srs.GetLinearUnits()
        if units and units > 0:
            return units
        return 1.0
    finally:
        dataset = None


def _generate_slope(config, merged, progress=None, cancel=None):
    """Ветка уклона: считается по нативному DEM и пишется байтом."""
    gdal, _, _ = _gdal()
    if progress:
        progress("Generating slope")
    raw = config.temp_dir / "slope_raw.tif"
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        slopeFormat="degree",
        scale=_dem_vertical_scale(merged),
        creationOptions=_relief_creation_options(),
        callback=_warp_callback(cancel, None, "slope"),
    )
    result = gdal.DEMProcessing(
        str(raw), str(merged), "slope", options=options
    )
    if result is None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Relief preparation cancelled")
        raise RuntimeError("Failed to generate slope")
    result = None

    # Отсечение пологих склонов и квантование до градуса: шкалы уклонов
    # дискретные с шагом 5° и 2°, поэтому байта достаточно, а файл
    # вчетверо легче Float32. 255 — nodata; уклон не бывает выше 90°.
    if progress:
        progress("Writing slope")
    slope = config.raster_dir / "slope.tif"
    # Прежний файл мог быть ссылкой на кэш: убираем перед записью.
    slope.unlink(missing_ok=True)
    try:
        _transform_raster(
            raw, slope, lambda array: array * (array > 1.04),
            dtype=gdal.GDT_Byte, nodata=255, cancel=cancel,
        )
    finally:
        raw.unlink(missing_ok=True)


def _generate_hillshade(config, merged, dem_width, dem_height,
                        progress=None, cancel=None):
    """Ветка отмывки: оттенок считается по нативному DEM."""
    gdal, _, _ = _gdal()
    numpy = _numpy()
    if progress:
        progress("Generating hillshade")
    hillshade = config.raster_dir / "hillshade.tif"
    # Как gdaldem hillshade по умолчанию: солнце 315°/45°, без вертикального
    # преувеличения. Многопроходная отмывка с altitude=70 и zFactor=2 давала
    # почти белое полотно, на котором рельеф не читался.
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        azimuth=315,
        altitude=45,
        zFactor=1,
        scale=_dem_vertical_scale(merged),
        creationOptions=_relief_creation_options(),
        callback=_warp_callback(cancel, None, "hillshade"),
    )
    disk_space.require_space(
        hillshade,
        dem_width * dem_height * disk_space.FILE_BYTE_PIXEL,
        "Недостаточно места для растра {}: нужно ≈{}, свободно {}.",
        "оттенок",
    )
    hillshade.unlink(missing_ok=True)
    result = gdal.DEMProcessing(
        str(hillshade), str(merged), "hillshade", options=options
    )
    if result is None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Relief preparation cancelled")
        raise RuntimeError("Failed to generate hillshade")
    result = None

    # 10 % подсветки: самые глубокие тени не проваливаются в чёрный.
    # Значения 0…255 помещаются в байт: растр вчетверо легче, а кэш
    # GDAL и память QGIS не растут вчетверо.
    composite_path = config.raster_dir / "hillshade_composite.tif"
    composite_path.unlink(missing_ok=True)
    _transform_raster(
        hillshade,
        composite_path,
        lambda array: numpy.clip(
            255.0 * (0.10 + 0.90 * (array / 255.0)), 0, 255
        ),
        dtype=gdal.GDT_Byte, cancel=cancel,
    )
    hillshade.unlink(missing_ok=True)


def generate_hillshade_slope(config, bbox, progress=None, cancel=None):
    gdal, _, _ = _gdal()
    merged = config.raster_dir / "merged_dem.tif"
    if not merged.exists():
        raise RuntimeError("merged_dem.tif is missing")

    dem_ds = gdal.Open(str(merged))
    dem_width, dem_height = dem_ds.RasterXSize, dem_ds.RasterYSize
    dem_ds = None
    # Уклон и отмывка считаются по нативному DEM: QGIS передискретизирует
    # растры бикубом при увеличении, поэтому запас пикселей в файле не
    # улучшает картинку, а только раздувает растр и время подготовки.
    warning = disk_space.space_warning(
        config.raster_dir,
        disk_space.relief_estimate(dem_width, dem_height),
        "Недостаточно места для рельефа: нужно ≈{}, свободно {}. "
        "Подготовка может прерваться.",
    )
    if warning and progress:
        progress(f"WARNING: {warning}")

    # Ветки уклона и отмывки независимы: обе читают только merged_dem.tif,
    # а пишут каждая в свои файлы.  Параллельный запуск сокращает время
    # рельефа почти вдвое.  Ошибка одной ветки не оставляет вторую
    # брошенной: выход из пула дожидается обеих.
    from concurrent.futures import ThreadPoolExecutor

    from . import gdal_log

    def guarded(function):
        # GDAL хранит обработчик ошибок отдельно для каждого потока.
        def run():
            gdal_log.LOG.install()
            try:
                function()
            finally:
                gdal_log.LOG.restore()
        return run

    with ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="xtopo-relief"
    ) as pool:
        futures = [
            pool.submit(
                guarded(
                    lambda: _generate_slope(
                        config, merged, progress, cancel=cancel
                    )
                )
            ),
            pool.submit(
                guarded(
                    lambda: _generate_hillshade(
                        config, merged, dem_width, dem_height, progress,
                        cancel=cancel,
                    )
                )
            ),
        ]
        # Выход из with уже дождался обеих веток, поэтому result() не
        # блокирует и лишь поднимает исключение первой упавшей ветки.
        for future in futures:
            future.result()
    return config.raster_dir / "hillshade_composite.tif"


class _EnvelopeGrid:
    """Пространственный индекс по охватам: сетка ячеек → набор значений.

    Достаточно для задачи «найти линии, чей охват пересекает полигон»:
    геометрическая проверка (GEOS) остаётся только для кандидатов.
    """

    def __init__(self, envelope, cells=64):
        self.cell = max(
            envelope[1] - envelope[0], envelope[3] - envelope[2]
        ) / cells or 1e-9
        self.origin = (envelope[0], envelope[2])
        self.cells = {}

    def keys(self, envelope):
        x0 = int((envelope[0] - self.origin[0]) // self.cell)
        x1 = int((envelope[1] - self.origin[0]) // self.cell)
        y0 = int((envelope[2] - self.origin[1]) // self.cell)
        y1 = int((envelope[3] - self.origin[1]) // self.cell)
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                yield cx, cy

    def add(self, envelope, value):
        for key in self.keys(envelope):
            self.cells.setdefault(key, set()).add(value)

    def remove(self, envelope, value):
        for key in self.keys(envelope):
            values = self.cells.get(key)
            if values is not None:
                values.discard(value)

    def find(self, envelope):
        found = set()
        for key in self.keys(envelope):
            found.update(self.cells.get(key, ()))
        return found


def _collect_water_polygons(water_paths, target_srs):
    """Полигоны воды в системе координат контуров.

    Полигоны не объединяются в один: Union всех озёр региона стоит
    полминуты, а бОльшая часть горизонталей воды не касается.
    """
    _, ogr, osr = _gdal()
    if target_srs is not None:
        target_srs = target_srs.Clone()
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    polygons = []
    envelope = None
    for path in water_paths:
        if not path.exists():
            continue
        dataset = ogr.Open(str(path))
        if dataset is None:
            continue
        for water_layer in dataset:
            source_srs = water_layer.GetSpatialRef()
            transform = None
            if source_srs is not None:
                source_srs = source_srs.Clone()
                source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if (source_srs is not None and target_srs is not None
                    and not source_srs.IsSame(target_srs)):
                transform = osr.CoordinateTransformation(source_srs, target_srs)
            for feature in water_layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                    continue
                flat = ogr.GT_Flatten(geometry.GetGeometryType())
                parts = (
                    [geometry.GetGeometryRef(index)
                     for index in range(geometry.GetGeometryCount())]
                    if flat == ogr.wkbMultiPolygon else [geometry]
                )
                for part in parts:
                    part = part.Clone()
                    if transform is not None:
                        part.Transform(transform)
                    part_env = part.GetEnvelope()
                    polygons.append(part)
                    envelope = (
                        part_env if envelope is None else (
                            min(envelope[0], part_env[0]),
                            max(envelope[1], part_env[1]),
                            min(envelope[2], part_env[2]),
                            max(envelope[3], part_env[3]),
                        )
                    )
        dataset = None
    return polygons, envelope


def _subtract_water_from_layer(layer, water_paths, target_srs,
                               touched_out=None, only_keys=None,
                               key_field="SRC"):
    """Режет контуры, заходящие на воду, по полигонам воды.

    Обход перевернут: контуры попадают в сетку по охватам, а для каждого
    полигона воды геометрические проверки выполняются только с кандидатами
    из соседних ячеек.  Прямой обход (каждая линия против каждого озера)
    стоил десятки секунд на регионе вроде Женевского озера.

    ``touched_out`` собирает ID обрезанных линий, а ``only_keys``
    ограничивает проверку линиями с этими ключами: обзорная копия
    упрощена максимум на 10 м, поэтому воду достаточно проверять там,
    где её уже коснулась подробная линия.  На остальных линиях
    отклонение упрощения меньше пикселя обзорного масштаба.
    """
    if only_keys is not None and not only_keys:
        return 0
    _, ogr, _ = _gdal()
    polygons, envelope = _collect_water_polygons(water_paths, target_srs)
    if not polygons:
        return 0
    definition = layer.GetLayerDefn()
    id_index = definition.GetFieldIndex("ID") if touched_out is not None else -1
    key_index = definition.GetFieldIndex(key_field) if only_keys is not None else -1

    def line_parts(geometry):
        if ogr.GT_Flatten(geometry.GetGeometryType()) == ogr.wkbLineString:
            return [geometry.Clone()]
        parts = []
        for index in range(geometry.GetGeometryCount()):
            parts.extend(line_parts(geometry.GetGeometryRef(index)))
        return parts

    # Море — один мультиполигон с тысячами дырок-островов.  Проверка
    # Intersects с ним для каждой горизонтали заново обходит все кольца:
    # на архипелаге это десятки секунд.  PreparedGeometry строит индекс
    # рёбер один раз, а Difference по-прежнему считается по исходному
    # полигону, поэтому результат не меняется.
    prepared = []
    for polygon in polygons:
        create = getattr(polygon, "CreatePreparedGeometry", None)
        try:
            prepared_polygon = create() if create is not None else None
        except Exception:  # noqa: BLE001 - без индекса результат тот же
            prepared_polygon = None
        prepared.append(prepared_polygon)

    index = _EnvelopeGrid(envelope)
    feature_envelopes = {}
    layer.ResetReading()
    feature = layer.GetNextFeature()
    while feature is not None:
        if only_keys is not None and (
                key_index < 0
                or feature.GetField(key_index) not in only_keys):
            feature = layer.GetNextFeature()
            continue
        geometry = feature.GetGeometryRef()
        if geometry is not None and not geometry.IsEmpty():
            feature_envelopes[feature.GetFID()] = geometry.GetEnvelope()
        feature = layer.GetNextFeature()
    for fid, feature_envelope in feature_envelopes.items():
        index.add(feature_envelope, fid)

    changed = 0
    for position, polygon in enumerate(polygons):
        polygon_envelope = polygon.GetEnvelope()
        for fid in list(index.find(polygon_envelope)):
            feature = layer.GetFeature(fid)
            if feature is None:
                continue
            if only_keys is not None:
                if key_index < 0 or feature.GetField(key_index) not in only_keys:
                    continue
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            prepared_polygon = prepared[position]
            if prepared_polygon is not None:
                intersects = prepared_polygon.Intersects(geometry)
            else:
                intersects = polygon.Intersects(geometry)
            if not intersects:
                continue
            if touched_out is not None and id_index >= 0:
                touched_out.add(feature.GetField(id_index))
            clipped = geometry.Difference(polygon)
            parts = ([] if clipped is None or clipped.IsEmpty()
                     else line_parts(clipped))
            changed += 1
            index.remove(feature_envelopes.pop(fid), fid)
            if not parts:
                layer.DeleteFeature(fid)
                feature = None
                continue
            feature.SetGeometry(parts[0])
            layer.SetFeature(feature)
            feature_envelopes[fid] = parts[0].GetEnvelope()
            index.add(feature_envelopes[fid], fid)
            for part in parts[1:]:
                additional = feature.Clone()
                additional.SetGeometry(part)
                additional.SetFID(-1)
                layer.CreateFeature(additional)
                new_fid = additional.GetFID()
                if new_fid >= 0:
                    feature_envelopes[new_fid] = part.GetEnvelope()
                    index.add(feature_envelopes[new_fid], new_fid)
            feature = None
    return changed


def _water_mask_paths(config):
    """Слои воды, по которым режутся изолинии.

    Берётся и постоянная вода, и вода с ``intermittent=yes`` (пересыхающие
    озёра и водохранилища): по сухому озеру горизонтали не нужны, а без
    этого срезания контуры шли по площадиrelation'а вроде Chott el Djerid
    (relation/3969167).  Растворённый слой предпочтительнее исходного.
    """
    paths = []
    groups = (
        ("water_dissolved.sqlite", "water.sqlite"),
        ("water_intermittent_dissolved.sqlite", "water_intermittent.sqlite"),
    )
    for names in groups:
        for name in names:
            path = config.vector_dir / name
            if path.exists():
                paths.append(path)
                break
    return paths


def osm_land_tiles(source, bbox):
    """Тайлы 1°×1°, где OSM отмечает берег или остров.

    Natural Earth 1:10m не содержит часть мелких атоллов.  Такой тайл
    нельзя пропускать только по ней: берег из OSM показывает, что суша
    там есть.
    """
    from osgeo import ogr

    dataset = getattr(source, "dataset", None)
    files = getattr(dataset, "feature_files", None) or {}
    tiles = set()
    for name in ("coastline", "island"):
        path = files.get(name)
        if not path:
            continue
        data = ogr.Open(str(path))
        if data is None:
            continue
        try:
            layer = data.GetLayer(0)
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                envelope = geometry.GetEnvelope()
                # Горизонтальный или вертикальный участок берега имеет
                # нулевой размер по одной оси.  Небольшой запас не даёт
                # потерять тайл и безвредно захватывает соседний на
                # стыке градусов.
                pad = 1e-9
                min_x = max(envelope[0] - pad, bbox.lon_min)
                min_y = max(envelope[2] - pad, bbox.lat_min)
                max_x = min(envelope[1] + pad, bbox.lon_max)
                max_y = min(envelope[3] + pad, bbox.lat_max)
                if min_x >= max_x or min_y >= max_y:
                    continue
                tiles.update(
                    dem_sources.tiles_for_bbox(
                        BBox(min_x, min_y, max_x, max_y)
                    )
                )
        finally:
            data = None
    return tiles


def _rasterize_water_mask(config, raster, water_paths, cancel=None):
    """Растровая маска воды на сетке растра."""
    gdal, _, _ = _gdal()
    transform = raster.GetGeoTransform()
    width = raster.RasterXSize
    height = raster.RasterYSize
    path = config.temp_dir / f"{Path(raster.GetDescription()).stem}_water_mask.tif"
    path.unlink(missing_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    mask = driver.Create(
        str(path), width, height, 1, gdal.GDT_Byte,
        options=["COMPRESS=LZW", "TILED=YES", "SPARSE_OK=TRUE"],
    )
    if mask is None:
        raise RuntimeError(f"Cannot create water mask: {path}")
    try:
        mask.SetGeoTransform(transform)
        mask.SetProjection(raster.GetProjection())
        band = mask.GetRasterBand(1)
        band.Fill(0)
        band.SetNoDataValue(0)
        for water in water_paths:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Relief preparation cancelled")
            gdal.Rasterize(
                mask, str(water),
                options=gdal.RasterizeOptions(
                    burnValues=[1],
                    allTouched=False,
                ),
            )
    finally:
        mask = None
    return path


def _mask_raster_water(config, name, water_paths, cancel=None):
    """Заменить пиксели воды в растре на nodata.

    Отрицательные высоты на суше (впадины, Мёртвое море в окружении)
    остаются: маска строится по полигонам воды, а не по знаку высоты.
    """
    gdal, _, _ = _gdal()
    path = config.raster_dir / name
    if not path.exists():
        return 0
    source = gdal.Open(str(path))
    if source is None:
        return 0
    mask_path = None
    temporary = path.with_name(f"{path.stem}_masked{path.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        width = source.RasterXSize
        height = source.RasterYSize
        source_band = source.GetRasterBand(1)
        nodata = source_band.GetNoDataValue()
        if nodata is None:
            nodata = -32768.0
        mask_path = _rasterize_water_mask(config, source, water_paths, cancel)
        mask = gdal.Open(str(mask_path))
        if mask is None:
            raise RuntimeError(f"Cannot open water mask: {mask_path}")
        mask_band = mask.GetRasterBand(1)
        disk_space.require_space(
            temporary,
            width * height * disk_space.FILE_FLOAT_PIXEL,
            "Недостаточно места для растра {}: нужно ≈{}, свободно {}.",
            name,
        )
        driver = gdal.GetDriverByName("GTiff")
        output = driver.Create(
            str(temporary), width, height, 1, gdal.GDT_Float32,
            options=_relief_creation_options(),
        )
        if output is None:
            raise RuntimeError(f"Cannot create masked raster: {temporary}")
        output.SetGeoTransform(source.GetGeoTransform())
        output.SetProjection(source.GetProjection())
        target_band = output.GetRasterBand(1)
        target_band.SetNoDataValue(nodata)
        masked = 0
        try:
            for y in range(0, height, RASTER_BLOCK_SIDE):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Relief preparation cancelled")
                rows = min(RASTER_BLOCK_SIDE, height - y)
                for x in range(0, width, RASTER_BLOCK_SIDE):
                    columns = min(RASTER_BLOCK_SIDE, width - x)
                    block = source_band.ReadAsArray(x, y, columns, rows)
                    water = mask_band.ReadAsArray(x, y, columns, rows)
                    if block is None or water is None:
                        continue
                    hit = water > 0
                    if hit.any():
                        block = block.astype("float32")
                        # Уже пустые пиксели не считаем: это не глубина.
                        hit &= block != nodata
                        if hit.any():
                            block[hit] = nodata
                            masked += int(hit.sum())
                    target_band.WriteArray(block, x, y)
            target_band.FlushCache()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            output = None
            mask = None
        # Windows не даёт заменить файл, пока он открыт GDAL.
        source = None
        os.replace(temporary, path)
        return masked
    finally:
        source = None
        if mask_path is not None:
            Path(mask_path).unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def mask_dem_by_water(config, progress=None, cancel=None):
    """Маскировать морское дно и озёра во всех растрах DEM.

    Возвращает число заменённых пикселей.  Без слоёв воды маска не
    строится: лучше оставить глубины, чем залить водой всю сушу.
    """
    water_paths = _water_mask_paths(config)
    if not water_paths:
        if progress:
            progress(
                "WARNING: Маска воды: слои воды не найдены, "
                "отрицательные глубины остаются"
            )
        return 0
    name = "merged_dem.tif"
    total = _mask_raster_water(config, name, water_paths, cancel)
    if total and progress:
        progress(f"Маска воды: {name} — {total} пикселей")
    if progress:
        progress(f"Маска воды: заменено пикселей — {total}")
    return total


def _contour_workers(tiles):
    """Сколько потоков считает контуры тайлов.

    Занимаем три четверти ядер: замер на реальном рельефе показывает,
    что дальше потолок — GEOS держит GIL, и лишние потоки мешают.
    """
    from .memory import CONTOUR_WORKER_BYTES, worker_cap

    available = os.cpu_count() or 2
    limit = max(1, int(available * CONTOUR_TILE_WORKERS_RATIO))
    return max(1, min(tiles, limit, worker_cap(CONTOUR_WORKER_BYTES, cpu_limit=limit)))


def _contour_tile_side(pixels):
    """Сколько тайлов по стороне нарезать DEM.

    Тайлов тем больше, чем больше растр: у большого DEM выигрыш от
    параллельности перекрывает работу на нахлёст, а тайл в памяти
    должен оставаться небольшим.
    """
    if pixels < CONTOUR_TILE_PIXELS:
        return 1
    side = 2
    while pixels / (side * side) > CONTOUR_TILE_MAX_PIXELS:
        side += 1
    return side


def _contour_tiles(raster, side, halo=CONTOUR_HALO):
    """Окна нарезки DEM: ядро тайла и окно с запасом вокруг него."""
    width = raster.RasterXSize
    height = raster.RasterYSize
    step_x = width // side
    step_y = height // side
    tiles = []
    for row in range(side):
        for column in range(side):
            xoff = column * step_x
            yoff = row * step_y
            xsize = step_x if column < side - 1 else width - xoff
            ysize = step_y if row < side - 1 else height - yoff
            window_xoff = max(0, xoff - halo)
            window_yoff = max(0, yoff - halo)
            tiles.append({
                "core": (xoff, yoff, xsize, ysize),
                "window": (
                    window_xoff,
                    window_yoff,
                    min(width - window_xoff, xsize + 2 * halo),
                    min(height - window_yoff, ysize + 2 * halo),
                ),
            })
    return tiles


def _core_box(transform, core):
    """Границы ядра тайла в координатах карты."""
    xoff, yoff, xsize, ysize = core
    corners = [
        (xoff, yoff),
        (xoff + xsize, yoff),
        (xoff, yoff + ysize),
        (xoff + xsize, yoff + ysize),
    ]
    xs = []
    ys = []
    for x, y in corners:
        xs.append(transform[0] + x * transform[1] + y * transform[2])
        ys.append(transform[3] + x * transform[4] + y * transform[5])
    return min(xs), max(xs), min(ys), max(ys)


def _clip_to_core(geometry, box):
    """Части линии, попавшие в ядро тайла.

    Линия целиком внутри ядра остаётся как есть; пересекающая границу
    обрезается по ней, поэтому кусок соседнего тайла продолжается с
    той же точки и шва не видно.
    """
    if geometry is None or geometry.IsEmpty():
        return []
    envelope = geometry.GetEnvelope()
    if (envelope[0] >= box[0] and envelope[1] <= box[1]
            and envelope[2] >= box[2] and envelope[3] <= box[3]):
        return [geometry]
    if (envelope[1] < box[0] or envelope[0] > box[1]
            or envelope[3] < box[2] or envelope[2] > box[3]):
        # Ядро линия не задевает — обрезать нечего.
        return []
    from osgeo import ogr

    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint(box[0], box[2])
    ring.AddPoint(box[1], box[2])
    ring.AddPoint(box[1], box[3])
    ring.AddPoint(box[0], box[3])
    ring.AddPoint(box[0], box[2])
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    try:
        clipped = geometry.Intersection(polygon)
    except Exception:  # noqa: BLE001 - битая геометрия не должна ронять сборку
        return []
    return _geometry_lines(clipped) if clipped is not None else []


def _contour_tile(task, simplify=0.0):
    """Контуры одного тайла: только то, что попадает в его ядро.

    Тайл считается по окну с запасом, а в результат попадает только
    ядро: так линия у края получается такой же, как при расчёте по
    всему DEM, и тайлы не дублируют друг друга.

    ``simplify`` — допуск упрощения в единицах СК.  Контуры считаются
    один раз; рядом с подробным слоем сразу пишется упрощённая копия
    для обзорных масштабов.  Второй проход по DEM стоил бы столько же,
    сколько первый, а упрощение в потоке тайла почти ничего не добавляет.

    Вода в растре не вырезается: контуры считаются по всему DEM, а
    полигоны воды вычитаются из готовых линий.  Так концы изолиний
    лежат точно на берегу, а не обрываются за пиксель до него.

    Возвращает слой с отрезками: файл на тайл не пишется — создание
    GeoPackage обходится дороже самой генерации.  При ``simplify > 0``
    в наборе два слоя: подробный и обзорный.
    """
    source_path, tile, step = task
    gdal, ogr, osr = _gdal()
    raster = gdal.Open(source_path)
    if raster is None:
        raise RuntimeError(f"Cannot open DEM: {source_path}")
    band = raster.GetRasterBand(1)
    transform = raster.GetGeoTransform()
    xoff, yoff, xsize, ysize = tile["window"]
    # Окно читается прямо в память: копия массива в памяти обошлась бы
    # вдвое дороже по месту.
    memory = gdal.Translate(
        "", source_path, format="MEM",
        srcWin=[xoff, yoff, xsize, ysize],
    )
    memory_band = memory.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        nodata = -32768.0
    memory_band.SetNoDataValue(nodata)
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromWkt(raster.GetProjection())

    contours = ogr.GetDriverByName("Memory").CreateDataSource("contours")
    contour_layer = contours.CreateLayer(
        "isolines", spatial_ref, ogr.wkbLineString)
    # GDAL пишет контуры в поля ID и ELEV: без обоих он падает с
    # «Invalid index».
    contour_layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    contour_layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    # useNoData: у растра есть nodata (дырки покрытия) — через такие
    # пиксели контуры не проводятся.
    nodata = memory_band.GetNoDataValue()
    if nodata is not None:
        gdal.ContourGenerate(
            memory_band, float(step), 0.0, [], 1, nodata,
            contour_layer, 0, 1)
    else:
        gdal.ContourGenerate(
            memory_band, float(step), 0.0, [], 0, 0.0,
            contour_layer, 0, 1)

    box = _core_box(transform, tile["core"])
    result = ogr.GetDriverByName("Memory").CreateDataSource("tile")
    layer = result.CreateLayer("isolines", spatial_ref, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    overview = None
    if simplify > 0:
        overview = result.CreateLayer(
            "isolines_overview", spatial_ref, ogr.wkbLineString
        )
        overview.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
        overview.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
        overview.CreateField(ogr.FieldDefn("SRC", ogr.OFTInteger))
    written = 0
    overview_written = 0
    for feature in contour_layer:
        geometry = feature.GetGeometryRef()
        elevation = feature.GetField("ELEV")
        for piece in _clip_to_core(geometry, box):
            source_id = written
            item = ogr.Feature(layer.GetLayerDefn())
            item.SetField("ID", source_id)
            item.SetField("ELEV", elevation)
            # Копировать геометрию поточечно нельзя: контуров миллионы
            # точек, и обход через Python занимает больше, чем весь
            # расчёт.  Лишнее измерение убирает сам writer.
            item.SetGeometry(piece.Clone())
            layer.CreateFeature(item)
            item = None
            written += 1
            if overview is None:
                continue
            simplified = piece.Simplify(simplify)
            for part in _geometry_lines(simplified):
                item = ogr.Feature(overview.GetLayerDefn())
                item.SetField("ID", overview_written)
                item.SetField("ELEV", elevation)
                item.SetField("SRC", source_id)
                item.SetGeometry(part)
                overview.CreateFeature(item)
                item = None
                overview_written += 1
    memory = None
    raster = None
    return result, written


def _merge_contour_tile(target, dataset, written=0, layer_index=0,
                        source_offset=0):
    """Склеивает контуры одного тайла в слой, перенумеровывая ID.

    Тайл склеивается сразу, как только посчитан: держать результаты
    всех тайлов в памяти до конца нельзя, на больших охватах это
    гигабайты.  ``layer_index`` выбирает подробный или обзорный слой
    тайла.  ``source_offset`` переводит локальные ID подробных линий
    тайла в глобальные ID обзорного слоя (поле ``SRC``).  Возвращает
    число записанных линий.
    """
    from osgeo import ogr

    source_layer = dataset.GetLayer(layer_index)
    definition = target.GetLayerDefn()
    source_index = definition.GetFieldIndex("SRC") if source_offset else -1
    for feature in source_layer:
        item = ogr.Feature(definition)
        for index in range(definition.GetFieldCount()):
            item.SetField(index, feature.GetField(index))
        item.SetField("ID", written)
        if source_index >= 0:
            item.SetField(
                source_index,
                feature.GetField(source_index) + source_offset,
            )
        item.SetGeometry(feature.GetGeometryRef().Clone())
        target.CreateFeature(item)
        item = None
        written += 1
    return written


OVERVIEW_SIMPLIFY_METERS = 10.0


def _contour_simplify_tolerance(spatial_ref, step):
    """Допуск упрощения обзорных контуров в единицах их СК.

    Не больше 10 м и не больше шага изолиний: при шаге 10 м допуск
    10 м, при более мелком шаге он ужимается до шага.  В
    географической СК допуск переводится в градусы через
    ``GEOGRAPHIC_SCALE``.
    """
    meters = min(OVERVIEW_SIMPLIFY_METERS, float(step))
    if meters <= 0:
        return 0.0
    try:
        # Пустая СК ведёт себя как географическая — так же, как в
        # _dem_vertical_scale.
        if spatial_ref is None or not spatial_ref.ExportToWkt():
            return meters / GEOGRAPHIC_SCALE
        if spatial_ref.IsGeographic():
            return meters / GEOGRAPHIC_SCALE
        units = spatial_ref.GetLinearUnits()
    except Exception:  # noqa: BLE001 - неизвестная или пустая СК
        return meters / GEOGRAPHIC_SCALE
    if units and units > 0:
        return meters / units
    return meters


def _smoothed_isolines_dem(config, progress=None):
    """Двукратный DEM для «Сглаживать контуры»; None — сглаживание выключено.

    Уклон и отмывка считаются по нативному DEM, но сглаживание контуров
    по-прежнему берёт более подробную сетку.  Файл временный: он нужен
    только на время расчёта контуров и удаляется вызывающим кодом.
    """
    if not config.smooth_isolines:
        return None
    gdal, _, _ = _gdal()
    merged = config.raster_dir / "merged_dem.tif"
    dataset = gdal.Open(str(merged))
    if dataset is None:
        return None
    width, height = dataset.RasterXSize, dataset.RasterYSize
    dataset = None
    destination = config.temp_dir / "isolines_dem_x2.tif"
    destination.unlink(missing_ok=True)
    disk_space.require_space(
        destination,
        width * height * 4 * disk_space.FILE_FLOAT_PIXEL,
        "Недостаточно места для растра {}: нужно ≈{}, свободно {}.",
        "укруплённый DEM",
    )
    options = gdal.WarpOptions(
        width=width * 2,
        height=height * 2,
        resampleAlg=config.terrain_resample_method,
        outputType=gdal.GDT_Float32,
        creationOptions=_relief_creation_options(),
    )
    result = gdal.Warp(str(destination), str(merged), options=options)
    if result is None:
        raise RuntimeError("Failed to upscale DEM for isolines")
    result = None
    return destination


ISOLINES_OVERVIEW_SUFFIX = "_overview"


def generate_isolines(config, bbox, progress=None, cancel=None):
    """Контуры высот: подробные и упрощённые для обзорных масштабов.

    Контуры считаются один раз.  Подробный слой получает все вершины,
    обзорный — упрощённую копию с допуском ``OVERVIEW_SIMPLIFY_METERS``;
    обе пишутся из одного прохода по DEM.
    """
    smooth_source = _smoothed_isolines_dem(config, progress)
    source = smooth_source or (config.raster_dir / "merged_dem.tif")
    if not source.exists():
        raise RuntimeError("DEM raster is missing")
    try:
        destination = _generate_isolines_processes(
            config, source, progress, cancel,
        )
        if destination is not None:
            return destination
        shapes = _contour_shapefiles(config, source, progress, cancel)
    finally:
        if smooth_source is not None:
            smooth_source.unlink(missing_ok=True)
    destination = None
    touched = set()
    for shape_path, suffix in shapes:
        result = _finish_isoline_set(
            config, shape_path, suffix, progress,
            touched_out=touched if not suffix else None,
            only_keys=touched if suffix else None,
        )
        if destination is None:
            destination = result
    return destination


def _new_contour_layer(dataset, spatial_ref, name="isolines",
                       with_source=False):
    """Слой контуров с полями, которые ждёт GDAL ContourGenerate."""
    ogr = _gdal()[1]
    layer = dataset.CreateLayer(name, spatial_ref, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    if with_source:
        layer.CreateField(ogr.FieldDefn("SRC", ogr.OFTInteger))
    return layer


def _write_contour_feature(layer, geometry, elevation, identifier,
                           source=None):
    """Пишет один отрезок и возвращает следующий номер."""
    ogr = _gdal()[1]
    item = ogr.Feature(layer.GetLayerDefn())
    item.SetField("ID", identifier)
    item.SetField("ELEV", elevation)
    if source is not None:
        item.SetField("SRC", source)
    item.SetGeometry(geometry.Clone())
    layer.CreateFeature(item)
    item = None
    return identifier + 1


def _contour_untiled(config, source, spatial_ref, simplify, outputs):
    """Контуры мелкого DEM: один расчёт, две записи."""
    gdal, ogr, _ = _gdal()
    memory = gdal.Translate("", str(source), format="MEM")
    contours = ogr.GetDriverByName("Memory").CreateDataSource("contours")
    contour_layer = _new_contour_layer(contours, spatial_ref)
    memory_band = memory.GetRasterBand(1)
    nodata = memory_band.GetNoDataValue()
    if nodata is not None:
        gdal.ContourGenerate(
            memory_band,
            float(config.isolines_step),
            0.0,
            [],
            1,
            nodata,
            contour_layer,
            0,
            1,
        )
    else:
        gdal.ContourGenerate(
            memory_band,
            float(config.isolines_step),
            0.0,
            [],
            0,
            0.0,
            contour_layer,
            0,
            1,
        )
    targets = [dataset.GetLayer(0) for _path, dataset in outputs]
    counters = [0] * len(targets)
    for feature in contour_layer:
        geometry = feature.GetGeometryRef()
        elevation = feature.GetField("ELEV")
        source_id = counters[0]
        counters[0] = _write_contour_feature(
            targets[0], geometry, elevation, counters[0]
        )
        if len(targets) > 1:
            simplified = geometry.Simplify(simplify)
            for part in _geometry_lines(simplified):
                counters[1] = _write_contour_feature(
                    targets[1], part, elevation, counters[1],
                    source=source_id,
                )
    memory = None


def _contour_tiled(config, source, raster, side, simplify, outputs,
                   progress, cancel):
    """Контуры крупного DEM: тайлы считаются один раз на оба слоя."""
    tiles = _contour_tiles(raster, side, CONTOUR_HALO)
    tasks = [
        (str(source), tile, config.isolines_step)
        for tile in tiles
    ]
    workers = _contour_workers(len(tasks))
    targets = [dataset.GetLayer(0) for _path, dataset in outputs]
    counters = [0] * len(targets)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_contour_tile, task, simplify): index
            for index, task in enumerate(tasks)
        }
        done = 0
        for future in as_completed(futures):
            result, _count = future.result()
            # Тайл склеивается сразу: держать все результаты в
            # памяти до конца расчёта нельзя.
            detail_offset = counters[0]
            for index, target in enumerate(targets):
                counters[index] = _merge_contour_tile(
                    target, result, counters[index], layer_index=index,
                    source_offset=detail_offset if index else 0,
                )
            result = None
            done += 1
            if progress:
                progress(f"Contour lines: tile {done} of {len(tasks)}")
            if cancel is not None and cancel.is_set():
                for pending in futures:
                    pending.cancel()
                raise InterruptedError("Preparation cancelled")
    if progress:
        progress(f"Contour lines: {counters[0]} segments")


def _contour_shapefiles(config, source, progress, cancel):
    """Пишет подробный и обзорный shapefile за один расчёт контуров."""
    gdal, ogr, osr = _gdal()
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Preparation cancelled")
    if progress:
        progress("Generating contour lines")
    raster = gdal.Open(str(source))
    if raster is None:
        raise RuntimeError(f"Cannot open DEM: {source}")
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromWkt(raster.GetProjection())
    simplify = _contour_simplify_tolerance(
        spatial_ref, config.isolines_step
    )
    driver = ogr.GetDriverByName("ESRI Shapefile")
    outputs = []
    try:
        detailed_path = config.temp_dir / "isolines_full.shp"
        dataset = driver.CreateDataSource(str(detailed_path))
        _new_contour_layer(dataset, spatial_ref)
        outputs.append((detailed_path, dataset))
        if simplify > 0:
            overview_path = (
                config.temp_dir
                / f"isolines_full{ISOLINES_OVERVIEW_SUFFIX}.shp"
            )
            dataset = driver.CreateDataSource(str(overview_path))
            _new_contour_layer(dataset, spatial_ref, with_source=True)
            outputs.append((overview_path, dataset))
        pixels = raster.RasterXSize * raster.RasterYSize
        side = _contour_tile_side(pixels)
        if side <= 1:
            _contour_untiled(
                config, source, spatial_ref, simplify, outputs
            )
        else:
            _contour_tiled(
                config, source, raster, side, simplify, outputs,
                progress, cancel,
            )
    finally:
        raster = None
        for _path, dataset in outputs:
            dataset = None
    return [
        (path, "" if index == 0 else ISOLINES_OVERVIEW_SUFFIX)
        for index, (path, _dataset) in enumerate(outputs)
    ]


def _worker_python():
    """Python, в котором импортируются плагин и GDAL, или ``None``.

    Отдельный интерпретатор нужен потому, что контуры и геометрические
    операции держат GIL.  В QGIS на Linux ``sys.executable`` — обычный
    Python; в Windows рядом с qgis-bin.exe лежит python3.exe.  Если ни
    один вариант не импортирует плагин, стадия считается в одном
    процессе — медленнее, но без риска.
    """
    global _WORKER_PYTHON
    if _WORKER_PYTHON is not None:
        return _WORKER_PYTHON
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Дочерний Python должен видеть те же каталоги, что и QGIS: на Windows
    # пакет qgis лежит в apps/qgis*/python и не попадает в sys.path
    # обычного python3.exe автоматически.
    entries = [str(root)]
    entries.extend(
        item for item in sys.path if item and os.path.isdir(item)
    )
    if env.get("PYTHONPATH"):
        entries.extend(env["PYTHONPATH"].split(os.pathsep))
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entries))
    script = Path(__file__).with_name("terrain_worker.py")
    candidates = []
    if sys.executable:
        candidates.append(sys.executable)
        folder = Path(sys.executable).parent
        for name in ("python3.exe", "python.exe", "python3", "python"):
            candidates.append(str(folder / name))
    try:
        from qgis.core import QgsApplication

        prefix = Path(QgsApplication.prefixPath())
        for relative in ("bin", "apps/Python312", "apps/Python311"):
            for name in ("python3.exe", "python.exe", "python3", "python"):
                candidates.append(str(prefix / relative / name))
    except Exception:  # noqa: BLE001 - QGIS может быть не импортирован
        pass
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen or not Path(candidate).is_file():
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                [candidate, str(script), "--check"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            _WORKER_PYTHON = (candidate, env)
            return _WORKER_PYTHON
    _WORKER_PYTHON = (None, env)
    return _WORKER_PYTHON


def _isolines_process_workers(tiles):
    """Сколько процессов одновременно обрабатывают тайлы.

    Потолок по ядрам тот же, что у потоков, но память считается иначе:
    процесс держит окно DEM и полигоны воды и ледников.
    """
    from .memory import worker_cap

    available = os.cpu_count() or 2
    limit = max(1, int(available * CONTOUR_TILE_WORKERS_RATIO))
    return max(1, min(
        tiles, limit, worker_cap(ISOLINE_PROCESS_BYTES, cpu_limit=limit)
    ))


def _quote_sql(name):
    return '"' + str(name).replace('"', '""') + '"'


def _quote_sql_path(path):
    return str(path).replace("'", "''")


def _merge_tile_layers(destination, sources, offsets, src_offsets=None):
    """Склеивает слои ``isolines`` из файлов тайлов.

    ``offsets`` сдвигает поле ``ID``, ``src_offsets`` — ссылку ``SRC``
    обзорного слоя на подробную линию.  Первый файл копируется целиком:
    так сохраняются схема слоя и пространственный индекс, а вставка
    остальных идёт одним SQL-запросом на тайл.
    """
    _, ogr, _ = _gdal()
    if not sources:
        return 0
    destination.unlink(missing_ok=True)
    shutil.copy2(sources[0], destination)
    dataset = ogr.Open(str(destination), 1)
    if dataset is None:
        raise RuntimeError(f"Cannot open contour tile: {destination}")
    layer = dataset.GetLayer(0)
    definition = layer.GetLayerDefn()
    fields = [
        definition.GetFieldDefn(index).GetName()
        for index in range(definition.GetFieldCount())
    ]
    geometry_name = layer.GetGeometryColumn() or "GEOMETRY"
    names = fields + [geometry_name]
    targets = [_quote_sql(name) for name in names]
    dataset.ExecuteSQL("DELETE FROM isolines")
    for position, source in enumerate(sources):
        dataset.ExecuteSQL(
            f"ATTACH DATABASE '{_quote_sql_path(source)}' AS src"
        )
        select = []
        for name in names:
            if name.lower() == "id":
                select.append(f"{_quote_sql(name)} + {int(offsets[position])}")
            elif src_offsets is not None and name.lower() == "src":
                select.append(
                    f"{_quote_sql(name)} + {int(src_offsets[position])}"
                )
            else:
                select.append(_quote_sql(name))
        dataset.ExecuteSQL(
            f"INSERT INTO isolines ({', '.join(targets)}) "
            f"SELECT {', '.join(select)} FROM src.isolines"
        )
        dataset.ExecuteSQL("DETACH DATABASE src")
    count = layer.GetFeatureCount()
    dataset = None
    return count


def _write_full_layer(source, destination):
    """Пишет итоговый ``isolines_full`` из склеенного слоя тайлов.

    Схема и сжатие геометрии должны совпадать с прежним конвейером:
    слой — SpatiaLite с ``LINESTRING`` и ``COMPRESS_GEOM``.  Промежуточный
    shapefile больше не нужен — прямой перевод даёт те же данные и заметно
    быстрее.
    """
    gdal, _, _ = _gdal()
    destination.unlink(missing_ok=True)
    result = gdal.VectorTranslate(
        str(destination), str(source),
        options=[
            "-f", "SQLite",
            "-nln", "isolines",
            "-dsco", "SPATIALITE=YES",
            "-lco", "FORMAT=SPATIALITE",
            "-lco", "GEOMETRY_NAME=GEOMETRY",
            "-lco", "LAUNDER=NO",
            "-lco", "COMPRESS_GEOM=YES",
        ],
    )
    if result is None:
        raise RuntimeError(f"Failed to write contour lines: {destination}")
    result = None


def _clean_isolines_tiles(config):
    """Убирает промежуточные файлы тайлов, оставшиеся от прошлой сборки."""
    for pattern in (
        "isolines_tile*.sqlite*",
        "isolines_full_raw*.sqlite*",
        "isolines_job_*.json",
        "isolines_split_job_*.json",
        "isolines_merge_job_*.json",
        "isolines_worker_*.log",
        "isolines_merge_*.log",
        "isolines_full*_shape.*",
        "isolines_full.shp",
        "isolines_full.shx",
        "isolines_full.dbf",
        "isolines_full.prj",
        "isolines_full_overview.shp",
        "isolines_full_overview.shx",
        "isolines_full_overview.dbf",
        "isolines_full_overview.prj",
    ):
        for path in config.temp_dir.glob(pattern):
            path.unlink(missing_ok=True)
    for pattern in (
        "isolines_regular_tile*.sqlite*",
        "isolines_glacier_tile*.sqlite*",
    ):
        for path in config.vector_dir.glob(pattern):
            path.unlink(missing_ok=True)


class _IsolineWorkerError(RuntimeError):
    """Процесс тайла завершился с ошибкой."""


def _read_worker_result(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _run_workers(jobs, python, env, workers, progress, cancel,
                 log_prefix="isolines_worker",
                 progress_template="Contour lines: tile {done} of {total}"):
    """Запускает процессы заданий и возвращает их результаты."""
    script = Path(__file__).with_name("terrain_worker.py")
    pending = list(jobs)
    running = {}
    results = []
    done = 0
    try:
        while pending or running:
            while pending and len(running) < workers:
                job, job_path = pending.pop(0)
                index = job["index"]
                log_path = Path(job["temp_dir"]) / f"{log_prefix}_{index}.log"
                log_file = log_path.open("w", encoding="utf-8")
                process = subprocess.Popen(
                    [python, str(script), str(job_path)],
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                running[index] = (process, log_path, log_file)
            finished = [
                index for index, (process, _log, _file) in running.items()
                if process.poll() is not None
            ]
            for index in finished:
                process, log_path, log_file = running.pop(index)
                log_file.close()
                try:
                    text = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    text = ""
                result = _read_worker_result(text)
                if process.returncode != 0 or result is None:
                    tail = "\n".join(text.splitlines()[-20:])
                    raise _IsolineWorkerError(
                        f"процесс {index} не отработал: {tail}"
                    )
                results.append(result)
                done += 1
                if progress and progress_template:
                    progress(
                        progress_template.format(
                            done=done, total=len(jobs)
                        )
                    )
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            if not finished:
                time.sleep(0.2)
    finally:
        for process, _log_path, log_file in running.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            log_file.close()
    results.sort(key=lambda item: item["index"])
    return results


def _chunk_sources(config, results, prefix, kind, tail, offsets):
    """Файлы частей одного слоя тайлов и сдвиги поля ``ID``.

    ``kind`` — ``detail`` или ``overview``: от него зависит, сколько строк
    было в тайле после вычитания воды.  Части идут в порядке номеров,
    поэтому итоговые строки остаются в прежнем порядке.
    """
    sources = []
    shifts = []
    for result in results:
        rows = result[
            "detail_rows" if kind == "detail" else "overview_rows"
        ]
        parts = max(1, math.ceil(rows / ISOLINE_CHUNK)) if rows else 0
        for part in range(parts):
            sources.append(
                config.vector_dir
                / f"isolines_{prefix}_tile{result['index']}_p{part}{tail}.sqlite"
            )
            shifts.append(offsets[result["index"]])
    return sources, shifts


def _merge_isolines_parallel(config, results, split_results, simplify, progress,
                             glacier_available, python, env, workers, cancel):
    """Склеивает результаты тайлов в итоговые слои.

    Полные слои и пары ``regular``/``glacier`` независимы, поэтому
    склеиваются отдельными процессами: перенос сотен мегабайт и запись
    SpatiaLite плохо делят GIL и заметно ускоряются параллельно.
    """
    detail_offsets = {}
    overview_offsets = {}
    detail_total = 0
    overview_total = 0
    for result in results:
        index = result["index"]
        detail_offsets[index] = detail_total
        overview_offsets[index] = overview_total
        detail_total += result["detail"]
        overview_total += result["overview"]

    destination = config.vector_dir / "isolines_full.sqlite"
    overview_destination = config.vector_dir / "isolines_full_overview.sqlite"
    jobs = []
    detail_sources = [
        config.temp_dir / f"isolines_tile{result['index']}.sqlite"
        for result in results
    ]
    tasks = [{
        "full": True,
        "raw": str(config.temp_dir / "isolines_full_raw.sqlite"),
        "destination": str(destination),
        "sources": [str(path) for path in detail_sources],
        "offsets": [detail_offsets[result["index"]] for result in results],
    }]
    jobs.append({
        "mode": "merge", "index": len(jobs), "tasks": tasks,
        "temp_dir": str(config.temp_dir),
    })
    if simplify > 0:
        overview_sources = [
            config.temp_dir / f"isolines_tile{result['index']}_overview.sqlite"
            for result in results
        ]
        tasks = [{
            "full": True,
            "raw": str(
                config.temp_dir / "isolines_full_overview_raw.sqlite"
            ),
            "destination": str(overview_destination),
            "sources": [str(path) for path in overview_sources],
            "offsets": [
                overview_offsets[result["index"]] for result in results
            ],
            "src_offsets": [
                detail_offsets[result["index"]] for result in results
            ],
        }]
        jobs.append({
            "mode": "merge", "index": len(jobs), "tasks": tasks,
            "temp_dir": str(config.temp_dir),
        })

    regular_overview = (
        config.vector_dir / "isolines_regular_overview.sqlite"
    )
    glacier_overview = (
        config.vector_dir / "isolines_glacier_overview.sqlite"
    )
    if glacier_available:
        regular_destination = config.vector_dir / "isolines_regular.sqlite"
        glacier_destination = config.vector_dir / "isolines_glacier.sqlite"
        regular_sources, regular_offsets = _chunk_sources(
            config, results, "regular", "detail", "", detail_offsets,
        )
        glacier_sources, glacier_offsets = _chunk_sources(
            config, results, "glacier", "detail", "", detail_offsets,
        )
        tasks = [
            {
                "destination": str(regular_destination),
                "sources": [str(path) for path in regular_sources],
                "offsets": regular_offsets,
            },
            {
                "destination": str(glacier_destination),
                "sources": [str(path) for path in glacier_sources],
                "offsets": glacier_offsets,
            },
        ]
        if simplify > 0:
            regular_overview_sources, regular_overview_offsets = (
                _chunk_sources(
                    config, results, "regular", "overview", "_overview",
                    overview_offsets,
                )
            )
            glacier_overview_sources, glacier_overview_offsets = (
                _chunk_sources(
                    config, results, "glacier", "overview", "_overview",
                    overview_offsets,
                )
            )
            tasks.append({
                "destination": str(regular_overview),
                "sources": [str(path) for path in regular_overview_sources],
                "offsets": regular_overview_offsets,
                "src_offsets": [
                    detail_offsets[
                        int(Path(path).name.split("_tile")[1].split("_")[0])
                    ]
                    for path in regular_overview_sources
                ],
            })
            tasks.append({
                "destination": str(glacier_overview),
                "sources": [str(path) for path in glacier_overview_sources],
                "offsets": glacier_overview_offsets,
                "src_offsets": [
                    detail_offsets[
                        int(Path(path).name.split("_tile")[1].split("_")[0])
                    ]
                    for path in glacier_overview_sources
                ],
            })
        jobs.append({
            "mode": "merge", "index": len(jobs), "tasks": tasks,
            "temp_dir": str(config.temp_dir),
        })

    if progress:
        progress("Склейка контуров")
    job_paths = []
    for job in jobs:
        path = config.temp_dir / f"isolines_merge_job_{job['index']}.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        job_paths.append((job, path))
    _run_workers(
        job_paths, python, env, workers, progress, cancel,
        log_prefix="isolines_merge", progress_template=None,
    )

    if not glacier_available:
        # Ледников нет: все линии остаются обычными, как в
        # _finish_isoline_set.
        shutil.copy2(destination, config.vector_dir / "isolines_regular.sqlite")
        (config.vector_dir / "isolines_glacier.sqlite").unlink(
            missing_ok=True
        )
        if simplify > 0:
            shutil.copy2(
                overview_destination,
                config.vector_dir / "isolines_regular_overview.sqlite",
            )
            glacier_overview.unlink(missing_ok=True)
        else:
            (config.vector_dir / "isolines_regular_overview.sqlite").unlink(
                missing_ok=True
            )
            glacier_overview.unlink(missing_ok=True)
    elif simplify == 0:
        overview_destination.unlink(missing_ok=True)
        regular_overview.unlink(missing_ok=True)
        glacier_overview.unlink(missing_ok=True)

    if progress:
        water_cut = sum(result["water_cut"] for result in results)
        progress(f"Contour lines cut by water: {water_cut}")
        progress(f"Contour lines: {detail_total} segments")
        if glacier_available:
            detail_results = [
                result for result in split_results
                if result.get("kind") == "detail"
            ]
            progress(
                "Glacier contour lines ready: "
                f"{sum(result['glacier'] for result in detail_results)}"
            )
            progress(
                "Contour lines outside glaciers: "
                f"{sum(result['regular'] for result in detail_results)}"
            )
    for path in config.temp_dir.glob("isolines_tile*.sqlite*"):
        path.unlink(missing_ok=True)
    for path in config.temp_dir.glob("isolines_full_raw*.sqlite*"):
        path.unlink(missing_ok=True)
    for path in config.vector_dir.glob("isolines_regular_tile*.sqlite*"):
        path.unlink(missing_ok=True)
    for path in config.vector_dir.glob("isolines_glacier_tile*.sqlite*"):
        path.unlink(missing_ok=True)
    for _job, path in job_paths:
        path.unlink(missing_ok=True)
    return destination


def _has_glacier_polygons(config):
    """Есть ли в слое ледников хотя бы один непустой полигон."""
    _, ogr, _ = _gdal()
    path = config.vector_dir / "glacier.sqlite"
    if not path.exists():
        return False
    dataset = ogr.Open(str(path))
    if dataset is None:
        return False
    layer = dataset.GetLayer(0)
    if layer is None:
        dataset = None
        return False
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None and not geometry.IsEmpty():
            dataset = None
            return True
    dataset = None
    return False


def _generate_isolines_processes(config, source, progress, cancel):
    """Считает изолинии тайлами в отдельных процессах.

    Возвращает путь к ``isolines_full.sqlite`` или ``None``, если
    процессы недоступны: тогда вызывающий код использует прежний
    последовательный путь.
    """
    if os.environ.get(NO_ISOLINE_PROCESSES_ENV):
        return None
    if config.parallel_workers == 1:
        return None
    gdal, _, osr = _gdal()
    raster = gdal.Open(str(source))
    if raster is None:
        return None
    pixels = raster.RasterXSize * raster.RasterYSize
    side = _contour_tile_side(pixels)
    if side <= 1:
        raster = None
        return None
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromWkt(raster.GetProjection())
    simplify = _contour_simplify_tolerance(
        spatial_ref, config.isolines_step
    )
    tiles = _contour_tiles(raster, side, CONTOUR_HALO)
    raster = None
    workers = _isolines_process_workers(len(tiles))
    if workers < 2:
        return None
    python, env = _worker_python()
    if python is None:
        if progress:
            progress(
                "WARNING: отдельный Python для тайлов не найден; "
                "контуры считаются одним потоком"
            )
        return None

    glacier_available = _has_glacier_polygons(config)
    water_paths = [str(path) for path in _water_mask_paths(config)]
    _clean_isolines_tiles(config)
    jobs = []
    for index, tile in enumerate(tiles):
        job = {
            "mode": "contour",
            "index": index,
            "source": str(source),
            "tile": tile,
            "step": config.isolines_step,
            "simplify": simplify,
            "temp_dir": str(config.temp_dir),
            "vector_dir": str(config.vector_dir),
            "water_paths": water_paths,
        }
        job_path = config.temp_dir / f"isolines_job_{index}.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        jobs.append((job, job_path))
    if progress:
        progress("Generating contour lines")
    try:
        results = _run_workers(
            jobs, python, env, workers, progress, cancel,
        )
        split_results = []
        if glacier_available:
            split_jobs = []
            job_index = len(jobs)
            for result in results:
                index = result["index"]
                for kind, tail, suffix_tail in (
                    ("detail", "", ""),
                    ("overview", "_overview", "_overview"),
                ):
                    rows = result[
                        "detail_rows" if kind == "detail"
                        else "overview_rows"
                    ]
                    if not rows:
                        continue
                    parts = max(1, math.ceil(rows / ISOLINE_CHUNK))
                    for part in range(parts):
                        start = part * ISOLINE_CHUNK + 1
                        end = min(rows, (part + 1) * ISOLINE_CHUNK) + 1
                        suffix = f"_tile{index}_p{part}{suffix_tail}"
                        job = {
                            "mode": "split",
                            "index": job_index,
                            "source": str(
                                config.temp_dir
                                / f"isolines_tile{index}{tail}.sqlite"
                            ),
                            "kind": kind,
                            "suffix": suffix,
                            "fid_range": [start, end],
                            "temp_dir": str(config.temp_dir),
                            "vector_dir": str(config.vector_dir),
                        }
                        job_path = (
                            config.temp_dir
                            / f"isolines_split_job_{job_index}.json"
                        )
                        job_path.write_text(
                            json.dumps(job), encoding="utf-8"
                        )
                        split_jobs.append((job, job_path))
                        job_index += 1
            split_results = _run_workers(
                split_jobs, python, env, workers, progress, cancel,
                progress_template=(
                    "Разрезание контуров по ледникам: {done} из {total}"
                ),
            )
        return _merge_isolines_parallel(
            config, results, split_results, simplify, progress,
            glacier_available, python, env, workers, cancel,
        )
    except _IsolineWorkerError as exc:
        if progress:
            progress(
                f"WARNING: {exc}; контуры считаются одним потоком"
            )
        _clean_isolines_tiles(config)
        return None
    except InterruptedError:
        _clean_isolines_tiles(config)
        raise
    except Exception as exc:  # noqa: BLE001 - работает запасной путь
        if progress:
            progress(
                f"WARNING: {exc}; контуры считаются одним потоком"
            )
        _clean_isolines_tiles(config)
        return None
    finally:
        for pattern in (
            "isolines_job_*.json",
            "isolines_split_job_*.json",
            "isolines_merge_job_*.json",
            "isolines_worker_*.log",
            "isolines_merge_*.log",
        ):
            for path in config.temp_dir.glob(pattern):
                path.unlink(missing_ok=True)


def _finish_isoline_set(config, shape_path, suffix, progress,
                        touched_out=None, only_keys=None):
    """Вода, SQLite и ледники для одного набора контуров."""
    gdal, ogr, osr = _gdal()
    dataset = ogr.Open(str(shape_path), 1)
    layer = dataset.GetLayer(0)
    spatial_ref = layer.GetSpatialRef()
    # Вода вычитается из готовых контуров: конец линии ложится точно на
    # берег.  Растеризация воды в nodata расширяла её на пиксель, и
    # изолинии обрывались, не доходя до воды.
    changed = _subtract_water_from_layer(
        layer, _water_mask_paths(config), spatial_ref,
        touched_out=touched_out, only_keys=only_keys,
    )
    if progress and changed:
        progress(f"Contour lines cut by water: {changed}")
    layer.SyncToDisk()
    dataset = None

    options = [
        "-nln", "isolines",
        "-dsco", "SPATIALITE=YES",
        "-lco", "COMPRESS_GEOM=YES",
    ]
    destination = config.vector_dir / f"isolines_full{suffix}.sqlite"
    try:
        result = gdal.VectorTranslate(
            str(destination), str(shape_path), options=options
        )
    except Exception:
        result = None
    if result is None:
        # -skipfailures отключает пакетную запись и в несколько раз
        # замедляет перенос, поэтому используется только как запасной путь.
        destination.unlink(missing_ok=True)
        try:
            result = gdal.VectorTranslate(
                str(destination), str(shape_path),
                options=options + ["-skipfailures"],
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to write contour lines: {exc}") from exc
    if result is None:
        raise RuntimeError("Failed to write contour lines")
    result = None
    glacier_destination = _generate_glacier_isolines(
        config, shape_path, progress, suffix=suffix
    )
    for extension in (".shp", ".shx", ".dbf", ".prj"):
        (config.temp_dir / f"isolines_full{suffix}{extension}").unlink(
            missing_ok=True
        )
    regular_destination = (
        config.vector_dir / f"isolines_regular{suffix}.sqlite"
    )
    if glacier_destination is None:
        # Ледников нет: все изолинии остаются обычными.
        (config.vector_dir / f"isolines_glacier{suffix}.sqlite").unlink(
            missing_ok=True
        )
        shutil.copy2(destination, regular_destination)
    return destination


def _geometry_lines(geometry):
    """Двумерные линии из результата пересечения геометрий."""
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return []
    kind = geometry.GetGeometryType()
    if kind in (ogr.wkbLineString, ogr.wkbLineString25D):
        return [geometry.Clone()]
    if kind in (ogr.wkbMultiLineString, ogr.wkbMultiLineString25D):
        # Части нужно копировать: ссылка GetGeometryRef() принадлежит
        # родительской геометрии, и после её освобождения указатель
        # становится недействительным (падение в GetPointCount).
        return [
            part.Clone()
            for part in (
                geometry.GetGeometryRef(index)
                for index in range(geometry.GetGeometryCount())
            )
            if part is not None and not part.IsEmpty()
        ]
    if kind in (ogr.wkbGeometryCollection, ogr.wkbGeometryCollection25D):
        lines = []
        for index in range(geometry.GetGeometryCount()):
            lines.extend(_geometry_lines(geometry.GetGeometryRef(index)))
        return lines
    return []


def _flat_line(geometry):
    """Копия линии без третьего измерения.

    Точки копируются поштучно только когда геометрия не линейная: у
    контуров их миллионы, и обход через Python занимает больше, чем
    весь остальной расчёт.  Простая линия копируется целиком — лишнее
    измерение убирает сам writer.
    """
    from osgeo import ogr

    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbLineString:
        return geometry.Clone()
    line = ogr.Geometry(ogr.wkbLineString)
    for point in geometry.GetPoints():
        line.AddPoint(point[0], point[1])
    return line


class _PolygonIndex:
    """Пространственный индекс полигонов: равномерная сетка по рамке.

    Нужен, чтобы для каждой изолинии перебирать только те ледники, чья
    рамка её пересекает. Свой фильтр GDAL для слоёв SQLite/SpatiaLite
    работает неверно (часть объектов не попадает в выборку), поэтому
    отбор ведём сами.
    """

    def __init__(self, items, cells=32):
        self.items = items
        self.cells = {}
        if not items:
            return
        minx = min(item[1][0] for item in items)
        maxx = max(item[1][1] for item in items)
        miny = min(item[1][2] for item in items)
        maxy = max(item[1][3] for item in items)
        self.minx = minx
        self.miny = miny
        self.width = (maxx - minx) or 1e-9
        self.height = (maxy - miny) or 1e-9
        self.columns = max(1, int(cells ** 0.5))
        self.rows = max(1, int(cells ** 0.5))
        for index, (_geometry, envelope) in enumerate(items):
            for column in range(self._column(envelope[0]), self._column(envelope[1]) + 1):
                for row in range(self._row(envelope[2]), self._row(envelope[3]) + 1):
                    self.cells.setdefault((column, row), []).append(index)

    def _column(self, x):
        column = int((x - self.minx) / self.width * self.columns)
        return min(self.columns - 1, max(0, column))

    def _row(self, y):
        row = int((y - self.miny) / self.height * self.rows)
        return min(self.rows - 1, max(0, row))

    def candidates(self, envelope):
        """Полигоны, рамка которых пересекает рамку запроса."""
        if not self.items:
            return []
        found = set()
        for column in range(self._column(envelope[0]), self._column(envelope[1]) + 1):
            for row in range(self._row(envelope[2]), self._row(envelope[3]) + 1):
                found.update(self.cells.get((column, row), ()))
        result = []
        for index in found:
            geometry, box = self.items[index]
            if (box[1] < envelope[0] or box[0] > envelope[1]
                    or box[3] < envelope[2] or box[2] > envelope[3]):
                continue
            result.append(geometry)
        return result


def _glacier_polygons(config):
    """Полигоны ледников со слоя glacier.sqlite."""
    from osgeo import ogr

    glacier = config.vector_dir / "glacier.sqlite"
    if not glacier.exists():
        return []
    dataset = ogr.Open(str(glacier))
    if dataset is None:
        return []
    layer = dataset.GetLayer(0)
    polygons = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        polygons.append((geometry.Clone(), geometry.GetEnvelope()))
    dataset = None
    return polygons


def _split_isolines_by_glaciers(config, shape_path, progress=None,
                                suffix="", fid_range=None):
    """Делит изолинии на части внутри ледников и вне их.

    Внутри ледника изолинии относятся к леднику (в исходном проекте они
    красились синим), поэтому обычные изолинии на площади ледника убираются.
    Возвращает путь к слою изолиний внутри ледников; None — если ледников
    нет или слой с ними недоступен.

    ``fid_range`` — половина открытого интервала ``(start, end)`` по
    ``ogc_fid``.  Так тяжёлый тайл режется на части, не копируя его в
    отдельные файлы: строки обрабатываются независимо.
    """
    from osgeo import ogr

    polygons = _glacier_polygons(config)
    if not polygons:
        return None
    source = ogr.Open(str(shape_path))
    if source is None:
        return None
    polygon_index = _PolygonIndex(polygons)
    glacier_destination = (
        config.vector_dir / f"isolines_glacier{suffix}.sqlite"
    )
    regular_destination = (
        config.vector_dir / f"isolines_regular{suffix}.sqlite"
    )
    glacier_destination.unlink(missing_ok=True)
    regular_destination.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("SQLite")
    glacier_output = None
    regular_output = None
    glacier_written = 0
    regular_written = 0
    try:
        source_layer = source.GetLayer(0)
        if fid_range is not None:
            start, end = fid_range
            source_layer.SetAttributeFilter(
                f"ogc_fid >= {int(start)} AND ogc_fid < {int(end)}"
            )
        definition = source_layer.GetLayerDefn()
        field_count = definition.GetFieldCount()
        glacier_output = driver.CreateDataSource(str(glacier_destination))
        regular_output = driver.CreateDataSource(str(regular_destination))
        spatial_ref = source_layer.GetSpatialRef()
        glacier_target = glacier_output.CreateLayer(
            "isolines", spatial_ref, ogr.wkbLineString
        )
        regular_target = regular_output.CreateLayer(
            "isolines", spatial_ref, ogr.wkbLineString
        )
        for index in range(field_count):
            field = definition.GetFieldDefn(index)
            glacier_target.CreateField(field)
            regular_target.CreateField(field)
        # Без транзакции SQLite фиксирует каждую линию отдельно, и
        # запись изолиний занимает больше времени, чем их расчёт.
        glacier_target.StartTransaction()
        regular_target.StartTransaction()
        for feature in source_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            envelope = geometry.GetEnvelope()
            remainder = geometry.Clone()
            inside = []
            for polygon in polygon_index.candidates(envelope):
                try:
                    intersection = remainder.Intersection(polygon)
                except Exception:  # noqa: BLE001 - битая геометрия ледника
                    continue
                pieces = _geometry_lines(intersection)
                if not pieces:
                    # Ледник не задевает линию: разность всё равно не
                    # изменится, а операция дороже пересечения.
                    continue
                inside.extend(pieces)
                try:
                    remainder = remainder.Difference(polygon)
                except Exception:  # noqa: BLE001 - битая геометрия ледника
                    continue
                if remainder is None or remainder.IsEmpty():
                    remainder = None
                    break
            for piece in inside:
                item = ogr.Feature(glacier_target.GetLayerDefn())
                for index in range(field_count):
                    item.SetField(index, feature.GetField(index))
                item.SetGeometry(_flat_line(piece))
                glacier_target.CreateFeature(item)
                glacier_written += 1
            outside = _geometry_lines(remainder) if remainder is not None else []
            for piece in outside:
                item = ogr.Feature(regular_target.GetLayerDefn())
                for index in range(field_count):
                    item.SetField(index, feature.GetField(index))
                item.SetGeometry(_flat_line(piece))
                regular_target.CreateFeature(item)
                regular_written += 1
        glacier_target.CommitTransaction()
        regular_target.CommitTransaction()
        glacier_output = None
        regular_output = None
    except Exception as exc:  # noqa: BLE001 - слой не должен ломать сборку
        if progress:
            progress(f"Glacier contour lines skipped: {exc}")
        glacier_destination.unlink(missing_ok=True)
        regular_destination.unlink(missing_ok=True)
        return None
    finally:
        source = None
    if progress:
        progress(f"Glacier contour lines ready: {glacier_written}")
        progress(f"Contour lines outside glaciers: {regular_written}")
    return glacier_destination


def _generate_glacier_isolines(config, shape_path, progress=None, suffix="",
                               fid_range=None):
    """Контурные линии, обрезанные по полигонам ледников.

    Обычные изолинии при этом тоже пересобираются: на площади ледников
    их нет, там рисуются ледниковые (синие).
    """
    return _split_isolines_by_glaciers(
        config, shape_path, progress, suffix=suffix, fid_range=fid_range
    )


def _extend_used(used_out, used):
    """Дописывает ключи использованных наборов в список для атрибуции."""
    if used_out is None:
        return
    for key in used:
        if key not in used_out:
            used_out.append(key)


def prepare_terrain_raster(config, bbox, progress=None, cancel=None,
                           status=None, used_out=None, required_tiles=None):
    """Скачивание тайлов и сборка сырого DEM (без воды и изолиний).

    Не зависит от слоёв проекта, поэтому в pipeline эта стадия работает
    параллельно обработке слоёв.  Возвращает ключ кэша DEM — его ждут
    :func:`prepare_terrain_relief` и :func:`prepare_terrain_isolines`.

    ``required_tiles`` — тайлы с сушей: океанские не скачиваются, их
    высоты всё равно станут nodata после маски воды.

    Если DEM получить не удалось (нет сети, прокси или покрытия), стадия
    не прерывает сборку карты: в журнал пишется ``WARNING`` с причиной,
    а функция возвращает ``None``.

    В ``used_out`` дописываются ключи наборов DEM, вошедших в мозаику:
    по ним пишется атрибуция.  Сама атрибуция пишется в конце сборки
    проекта одним проходом — переписывать архив проекта из этого потока
    нельзя.
    """
    if not config.generate_terrain:
        return None
    config.terrain_fallback = ""
    # Проверяем кэш до скачивания тайлов: ключ подсказки не требует
    # содержимого тайлов, поэтому повторная подготовка не ходит в сеть
    # и не собирает мозаику заново.
    hint = terrain_cache.dem_hint(config, bbox, required_tiles)
    cached = terrain_cache.restore_dem_by_hint(config, hint)
    if cached is not None:
        used = cached.get("used") or ()
        _extend_used(used_out, used)
        config.terrain_fallback = str(cached.get("fallback") or "")
        if progress:
            progress("Использован кэш рельефа: повторная обработка не требуется")
        return str(cached.get("signature") or "")
    config.terrain_input_dir.mkdir(parents=True, exist_ok=True)
    source_key = config.resolved_terrain_source()
    source = dem_sources.source(source_key)
    used = []
    try:
        if source.manual:
            if not config.terrain_src_dir:
                raise RuntimeError(
                    "terrain_src_dir is required for the manual DEM source"
                )
            _clear_terrain_input(config)
            copy_dem_tiles(
                bbox, config.terrain_src_dir, config.terrain_input_dir,
                progress=progress,
            )
        elif config.download_terrain_tiles:
            _clear_terrain_input(config)
            if source_key == dem_sources.AUTO_SOURCE:
                download_auto_dem(
                    bbox,
                    config.terrain_input_dir,
                    progress=progress,
                    cancel=cancel,
                    cache_dir=config.terrain_cache_dir,
                    status=status,
                    sonny_catalogue=sonny.catalogue_dir(),
                    sonny_tiles_dir=sonny.tile_cache_dir(config.projects_dir),
                    used=used,
                    required_tiles=required_tiles,
                )
            elif source_key == dem_sources.SONNY_SOURCE:
                download_sonny_dem(
                    bbox,
                    config.terrain_input_dir,
                    progress=progress,
                    cancel=cancel,
                    cache_dir=config.terrain_cache_dir,
                    status=status,
                    sonny_catalogue=sonny.catalogue_dir(),
                    sonny_tiles_dir=sonny.tile_cache_dir(config.projects_dir),
                    used=used,
                )
            elif source_key == "cop30":
                used = _download_cop30(
                    config,
                    bbox,
                    progress=progress,
                    cancel=cancel,
                    status=status,
                    required_tiles=required_tiles,
                )
            else:
                download_dem(
                    source_key,
                    bbox,
                    config.terrain_input_dir,
                    progress=progress,
                    cancel=cancel,
                    cache_dir=config.terrain_cache_dir,
                    status=status,
                    required_tiles=required_tiles,
                )
    except TerrainUnavailable as exc:
        # Сеть или покрытие не дали DEM: карта собирается без рельефа,
        # а в журнале остаётся понятная причина вместо «нет суши».
        if progress:
            progress(f"WARNING: {exc}")
        return None
    if used:
        _extend_used(used_out, used)

    for archive in list(config.terrain_input_dir.glob("*.zip")):
        shutil.unpack_archive(str(archive), str(config.terrain_input_dir))
        archive.unlink(missing_ok=True)

    if not any(path.is_file() for path in config.terrain_input_dir.iterdir()):
        if required_tiles is not None and not required_tiles:
            message = i18n.tr(
                "Рельеф: в охвате нет суши, тайлы DEM не нужны"
            )
        elif (not source.manual and config.download_terrain_tiles
              and net.offline()):
            message = _connection_warning()
        else:
            message = i18n.tr(
                "Рельеф: тайлы DEM не найдены — проверьте выбранный "
                "источник рельефа и доступность сети"
            )
        if progress:
            progress(f"WARNING: {message}")
        return None

    # Хест исходных тайлов считаем до конвертации: содержимое .hgt
    # стабильно, а результат конвертации может отличаться от запуска
    # к запуску.
    source_digest = terrain_cache.dem_source_digest(config)

    gdal, _, _ = _gdal()
    for hgt in list(config.terrain_input_dir.glob("*.hgt")):
        temporary = hgt.with_suffix(".tif")
        gdal.Translate(str(temporary), str(hgt))
        hgt.unlink(missing_ok=True)

    if progress:
        progress("Preparing terrain")
    # Рельеф и изолинии считаются долго, поэтому готовый результат
    # переиспользуется, если не изменились версия плагина, параметры
    # и исходные данные (см. core/terrain_cache.py).
    dem_key = terrain_cache.dem_signature(
        config, bbox, source_digest, required_tiles=required_tiles,
    )
    if terrain_cache.restore_dem(config, dem_key):
        if progress:
            progress("Использован кэш рельефа: повторная обработка не требуется")
    else:
        build_dem(config, bbox, progress=progress, cancel=cancel)
        if terrain_cache.store_dem(config, dem_key):
            terrain_cache.store_dem_hint(
                config, hint, dem_key, used=used,
                fallback=config.terrain_fallback,
            )
    if config.download_terrain_tiles:
        for path in config.terrain_input_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
    _remove_stale_projected_dem(config)
    return dem_key


def prepare_terrain_relief(config, bbox, dem_key, progress=None,
                           cancel=None):
    """Маска воды, уклон и отмывка по готовому сырому DEM.

    Сырой DEM можно готовить параллельно со слоями, но маска воды
    зависит от ``water_dissolved`` и строится только после береговой
    линии.  Поэтому уклон, отмывка и изолинии считаются здесь, а не в
    :func:`prepare_terrain_raster`.
    """
    if not config.generate_terrain or dem_key is None:
        return
    signature = terrain_cache.relief_signature(config, bbox, dem_key)
    if terrain_cache.restore_relief(config, signature):
        if progress:
            progress(
                "Использован кэш маскированного рельефа: "
                "повторная обработка не требуется"
            )
        return
    mask_dem_by_water(config, progress=progress, cancel=cancel)
    if config.generate_terrain_hillshade_slope:
        generate_hillshade_slope(
            config, bbox, progress=progress, cancel=cancel,
        )
    else:
        for name in ("slope.tif", "hillshade_composite.tif"):
            (config.raster_dir / name).unlink(missing_ok=True)
    terrain_cache.store_relief(config, signature)


def prepare_terrain_isolines(config, bbox, dem_key, progress=None,
                             cancel=None):
    """Изолинии по готовому растру.

    Контуры режутся по воде и ледникам — слоям проекта, поэтому стадия
    идёт после обработки слоёв (см. core.pipeline).
    """
    if not config.generate_terrain or not config.generate_terrain_isolines:
        return
    if dem_key is None:
        return
    isolines_key = terrain_cache.isolines_signature(config, bbox, dem_key)
    if terrain_cache.restore_isolines(config, isolines_key):
        if progress:
            progress("Использован кэш изолиний: повторная обработка не требуется")
    else:
        generate_isolines(config, bbox, progress=progress, cancel=cancel)
        terrain_cache.store_isolines(config, isolines_key)


def prepare_terrain(config, bbox, progress=None, cancel=None, status=None):
    """Последовательный вариант: растр, маска воды, уклон, изолинии."""
    dem_key = prepare_terrain_raster(
        config, bbox, progress=progress, cancel=cancel, status=status,
    )
    prepare_terrain_relief(
        config, bbox, dem_key, progress=progress, cancel=cancel,
    )
    prepare_terrain_isolines(
        config, bbox, dem_key, progress=progress, cancel=cancel,
    )
