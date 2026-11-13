"""Рельеф Sonny: LiDAR DTM Европы с Google Drive.

Наборы разложены по общим папкам Google Drive: коллективная «Европа 1″»
и отдельные папки Австрии и Швейцарии с более подробной моделью 0,5″.
Плагин склеивает их в один источник «Sonny DTM» и сам выбирает набор
под каждый тайл охвата.  Набор 3″ (около 90 м) не используется: там,
где нет 1″, фоном идёт Copernicus GLO-30 с 30 м.

Списки файлов читаются одним запросом к ``embeddedfolderview`` и
кэшируются на диске: при старте плагина они обновляются в фоне, чтобы
интерфейс не ждал сеть.  Сами файлы — zip с одним ``.hgt`` внутри.

Особенности формата:

* ``.hgt`` без заголовка, big-endian int16, размер ``N×N`` (3601 для
  1″, 7201 для 0,5″);
* GDAL читает ``.hgt`` только если имя файла соответствует SRTM
  (``N46E009.hgt``) — из имени берётся привязка;
* драйвер SRTMHGT понимает 1201 и 3601 точек, поэтому для тайлов 0,5″
  (7201) нужен VRT с ``VRTRawRasterBand`` (см. :func:`hgt_vrt`);
* вода и пустоты заполнены нулями, а не ``-32768``.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import i18n


USER_AGENT = "qgis-xtopo/1.0"

# Страница со списком файлов папки: отдаёт всё содержимое одним
# запросом, в отличие от обычной страницы папки (там первая страница).
FOLDER_VIEW = "https://docs.google.com/embeddedfolderview?id={folder}"

# Файл каталога: zip с одним тайлом.  ``confirm=t`` обязателен — без
# него Google отдаёт HTML-страницу с предупреждением о проверке.
DOWNLOAD = (
    "https://drive.usercontent.google.com/download"
    "?id={file_id}&export=download&confirm=t"
)

# Сколько живёт кэш списка тайлов, дней.
CACHE_DAYS = 7

# Ширина тайла в точках: 1″ — 3601, 0,5″ — 7201.
SAMPLES_05 = 7201
SAMPLES_1 = 3601

# Размеры, которые драйвер SRTMHGT понимает без обёртки.
SRTMHGT_SAMPLES = (SAMPLES_1,)


@dataclass(frozen=True)
class SonnyDataset:
    """Один набор данных Sonny на Google Drive."""

    key: str
    title: str
    folder_id: str
    # Подшаг сетки в долях градуса.
    step: float
    # Сколько точек в тайле по стороне.
    samples: int
    # resourcekey нужен коллективным папкам; у отдельных стран его нет.
    resource_key: str = ""
    # Порядок при выборе набора для тайла: чем меньше, тем лучше.
    rank: int = 0

    @property
    def native_resolution(self):
        return self.step


DATASETS = (
    SonnyDataset(
        key="at-05s",
        title="Sonny DTM 0,5″ (Австрия)",
        folder_id="1GEXis6pHcqn3MqvsDBuOlRU5kSS1O6Xk",
        step=0.5 / 3600.0,
        samples=SAMPLES_05,
        rank=0,
    ),
    SonnyDataset(
        key="ch-05s",
        title="Sonny DTM 0,5″ (Швейцария)",
        folder_id="1_e5EtPYiVp9OMfrlqR9ZNFjzr7i6Jz4G",
        step=0.5 / 3600.0,
        samples=SAMPLES_05,
        rank=1,
    ),
    SonnyDataset(
        key="europe-1s",
        title="Sonny DTM 1″ (Европа)",
        folder_id="0BxphPoRgwhnoWkRoTFhMbTM3RDA",
        resource_key="0-wRe5bWl96pwvQ9tAfI9cQg",
        step=1.0 / 3600.0,
        samples=SAMPLES_1,
        rank=2,
    ),
)

DATASET_BY_KEY = {dataset.key: dataset for dataset in DATASETS}

# Имя файла тайла: ``N46E009.zip`` или ``N46E009.hgt``.
_TILE_PATTERN = re.compile(r"^([NS]\d{2})([EW]\d{3})(?:\.[A-Za-z0-9]+)?$")
_ENTRY_PATTERN = re.compile(
    r'id="entry-([^"]+)"[^>]*>.*?flip-entry-title">([^<]+)<', re.S
)


def listing_url(dataset):
    """Адрес страницы со списком файлов набора."""
    url = FOLDER_VIEW.format(folder=dataset.folder_id)
    if dataset.resource_key:
        url += f"&resourcekey={dataset.resource_key}"
    return url + "#list"


def parse_listing(html):
    """Разбор страницы: имя файла → идентификатор в Google Drive.

    Формат страницы: ``<div class="flip-entry" id="entry-<ID>">`` и
    ``<div class="flip-entry-title">N46E009.zip</div>``.
    """
    result = {}
    for file_id, name in _ENTRY_PATTERN.findall(html):
        if name.lower().endswith(".zip"):
            result[name] = file_id
    return result


def _http_bytes(url, timeout=60, retries=3, cancel=None):
    """Загрузка с повторами: Drive иногда отвечает 429."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    last = None
    for _attempt in range(retries):
        if cancel is not None and cancel.is_set():
            raise InterruptedError
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if cancel is not None and cancel.is_set():
                raise InterruptedError
    raise RuntimeError(f"request failed: {last}")


# --- Кэш списка тайлов -----------------------------------------------

def catalogue_dir():
    """Каталог кэша списка тайлов.

    Список покрытия общий для всех проектов, поэтому он лежит в
    пользовательском кэше, а не рядом с данными проекта.
    """
    try:
        from qgis.PyQt.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(
            QStandardPaths.GenericCacheLocation
        )
    except Exception:  # noqa: BLE001 - вне QGIS путь берётся из дома
        base = ""
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / "qgis-xtopo" / "sonny"


def tile_cache_dir(projects_dir):
    """Каталог скачанных тайлов рельефа Sonny."""
    return Path(projects_dir) / "qgisxtopo-cache" / "sonny" / "tiles"


def catalogue_path(directory=None):
    """Путь к файлу кэша со списком тайлов."""
    folder = Path(directory) if directory else catalogue_dir()
    return folder / "listings.json"


def load_catalogue(directory=None):
    """Кэш списков тайлов: ``{набор: {файл: id}}``."""
    try:
        data = json.loads(
            catalogue_path(directory).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    datasets = data.get("datasets")
    if not isinstance(datasets, dict):
        return {}
    return {
        key: dict(value)
        for key, value in datasets.items()
        if isinstance(value, dict)
    }


def store_catalogue(datasets, directory=None):
    """Сохраняет списки тайлов на диск."""
    path = catalogue_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched": int(time.time()),
        "datasets": datasets,
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def catalogue_age_days(directory=None):
    """Сколько дней прошло с обновления кэша (None — кэша нет)."""
    try:
        data = json.loads(
            catalogue_path(directory).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    fetched = data.get("fetched")
    if not isinstance(fetched, (int, float)):
        return None
    return (time.time() - fetched) / 86400.0


def refresh_catalogue(directory=None, progress=None, cancel=None):
    """Обновляет списки тайлов всех наборов.

    Вызывается в фоне при старте плагина: сеть не должна блокировать
    интерфейс.  Ошибка одного набора не мешает остальным, а его прежний
    список остаётся в кэше.
    """
    datasets = {
        key: value
        for key, value in load_catalogue(directory).items()
        if key in DATASET_BY_KEY
    }
    updated = False
    for dataset in DATASETS:
        if cancel is not None and cancel.is_set():
            raise InterruptedError
        if progress:
            progress(
                i18n.trf(
                    "Обновление списка тайлов: {}", i18n.tr(dataset.title)
                )
            )
        try:
            html = _http_bytes(
                listing_url(dataset), timeout=20, retries=2, cancel=cancel
            )
        except InterruptedError:
            raise
        except Exception:  # noqa: BLE001 - сеть не должна ломать запуск
            continue
        if cancel is not None and cancel.is_set():
            raise InterruptedError
        listing = parse_listing(html.decode("utf-8", "replace"))
        if listing:
            datasets[dataset.key] = listing
            updated = True
    if updated:
        store_catalogue(datasets, directory)
    return datasets


# --- Тайлы ------------------------------------------------------------

def tile_from_name(name):
    """Имя файла → тайл ``(широта, долгота)`` — юго-западный угол."""
    stem = os.path.splitext(os.path.basename(name))[0]
    match = _TILE_PATTERN.match(stem)
    if match is None:
        return None
    lat = int(match.group(1)[1:])
    lon = int(match.group(2)[1:])
    if match.group(1)[0].upper() == "S":
        lat = -lat
    if match.group(2)[0].upper() == "W":
        lon = -lon
    return lat, lon


def tile_name(lat, lon):
    """Тайл → имя файла в стиле SRTM."""
    north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{north}{east}"


def dataset_cells(catalogue, dataset_key):
    """Тайлы 1°×1°, покрытые набором: ``{(широта, долгота)}``."""
    cells = set()
    for name in catalogue.get(dataset_key, {}):
        tile = tile_from_name(name)
        if tile is not None:
            cells.add(tile)
    return cells


def catalogue_tiles(directory=None):
    """Покрытие тайлами: ``тайл → набор``.

    Для одного тайла подходит лучший набор: 0,5″ важнее 1″.
    Наборы сравниваются по ``rank``.
    """
    result = {}
    for dataset_key, listing in load_catalogue(directory).items():
        dataset = DATASET_BY_KEY.get(dataset_key)
        if dataset is None:
            continue
        for name in listing:
            tile = tile_from_name(name)
            if tile is None:
                continue
            current = result.get(tile)
            if current is None or dataset.rank < DATASET_BY_KEY[
                current
            ].rank:
                result[tile] = dataset_key
    return result


def assign_datasets(catalogue, cells):
    """Выбор набора данных для каждого тайла: ``{тайл: набор}``."""
    wanted = set(cells)
    result = {}
    for dataset in DATASETS:
        for tile in dataset_cells(catalogue, dataset.key):
            if tile not in wanted:
                continue
            current = result.get(tile)
            if current is None or dataset.rank < DATASET_BY_KEY[
                current
            ].rank:
                result[tile] = dataset.key
    return result


def tile_sources(catalogue, dataset_key, tiles):
    """Файлы тайлов набора для скачивания: ``[(тайл, id)]``."""
    listing = catalogue.get(dataset_key, {})
    result = []
    for tile in sorted(tiles):
        name = f"{tile_name(*tile)}.zip"
        file_id = listing.get(name)
        if file_id:
            result.append((tile, file_id))
    return result


def hgt_vrt(hgt_path, step, samples):
    """VRT-обёртка для ``.hgt``: драйвер SRTMHGT знает только 1201 и 3601.

    Обёртка описывает сырой бинарный файл: big-endian int16, без
    заголовка, строки по ``samples`` значений.  Привязка берётся из
    имени файла — как это делает драйвер SRTMHGT: первый отсчёт стоит
    ровно на юго-западном углу тайла, поэтому начало растра сдвинуто на
    половину пикселя.
    """
    path = Path(hgt_path)
    vrt_path = path.with_suffix(".vrt")
    # Свежий VRT уже описывает этот файл: перезапись не нужна.  Это важно
    # при параллельном рендере соседних тайлов — GDAL не должен открывать
    # файл в момент записи.
    try:
        if (vrt_path.stat().st_mtime >= path.stat().st_mtime and
                vrt_path.stat().st_size > 0):
            text = vrt_path.read_text(encoding="utf-8", errors="replace")
            # Недописанный VRT от прерванной записи не годится: GDAL его
            # не откроет, поэтому такой файл перезаписываем.
            if "<VRTDataset" in text and "</VRTDataset>" in text:
                return vrt_path
    except OSError:
        pass
    tile = tile_from_name(path.name)
    if tile is None:
        raise ValueError(f"unexpected tile name: {path.name}")
    lat, lon = tile
    origin_x = lon - step / 2.0
    origin_y = lat + 1.0 + step / 2.0
    content = (
        "<VRTDataset rasterXSize=\"{samples}\" rasterYSize=\"{samples}\">\n"
        "  <SRS>EPSG:4326</SRS>\n"
        "  <GeoTransform>{ox}, {step}, 0.0, {oy}, 0.0, {ystep}</GeoTransform>\n"
        "  <VRTRasterBand dataType=\"Int16\" band=\"1\" "
        "subClass=\"VRTRawRasterBand\">\n"
        "    <SourceFilename relativeToVRT=\"1\">{name}</SourceFilename>\n"
        "    <ImageOffset>0</ImageOffset>\n"
        "    <PixelOffset>2</PixelOffset>\n"
        "    <LineOffset>{line}</LineOffset>\n"
        "    <ByteOrder>MSB</ByteOrder>\n"
        "  </VRTRasterBand>\n"
        "</VRTDataset>\n".format(
            samples=samples,
            ox=origin_x,
            oy=origin_y,
            ystep=-step,
            step=step,
            name=path.name,
            line=samples * 2,
        ))
    # Уникальное временное имя и атомарная замена: два потока могут
    # одновременно создавать VRT одного тайла.
    temporary = vrt_path.with_name(
        f"{vrt_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(vrt_path)
    return vrt_path


def _extract(zip_path, destination):
    """Распаковывает архив с одним ``.hgt``."""
    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name for name in archive.namelist()
            if name.lower().endswith(".hgt")
        ]
        if not names:
            raise RuntimeError(f"no .hgt inside {zip_path}")
        archive.extract(names[0], destination)
    return Path(destination) / os.path.basename(names[0])


def download_tile(dataset, file_id, name, destination, progress=None):
    """Скачивает один тайл и возвращает путь к файлу для чтения GDAL.

    Для 1″ драйвер SRTMHGT читает ``.hgt`` сам, для 0,5″ возвращается
    VRT-обёртка.  ``name`` — имя скачиваемого файла для журнала.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    url = DOWNLOAD.format(file_id=file_id)
    if progress:
        progress(
            i18n.trf(
                "Загрузка тайла рельефа: {} ({})",
                name, i18n.tr(dataset.title),
            )
        )
    data = _http_bytes(url, timeout=300, retries=3)
    if not data.startswith(b"PK"):
        # Google отдаёт HTML со страницей ошибки или предупреждения.
        raise RuntimeError("unexpected response, not a zip archive")
    with tempfile.TemporaryDirectory(dir=str(destination)) as work:
        archive = Path(work) / "tile.zip"
        archive.write_bytes(data)
        hgt = _extract(archive, work)
        target = destination / hgt.name
        shutil.move(str(hgt), str(target))
    if dataset.samples in SRTMHGT_SAMPLES:
        return target
    return hgt_vrt(target, dataset.step, dataset.samples)


def tile_paths(catalogue, tile_dir, dataset_key, tiles, progress=None,
               cancel=None):
    """Скачивает тайлы набора и возвращает пути для чтения GDAL.

    Файлы кладутся в кэш, поэтому повторная сборка проекта не качает их
    заново.
    """
    dataset = DATASET_BY_KEY[dataset_key]
    folder = Path(tile_dir) / dataset_key
    result = []
    for tile, file_id in tile_sources(catalogue, dataset_key, tiles):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{tile_name(*tile)}.hgt"
        if target.exists():
            path = target
        else:
            path = download_tile(
                dataset, file_id, f"{tile_name(*tile)}.zip",
                folder, progress=progress,
            )
        if path.suffix.lower() == ".hgt" and dataset.samples not in (
            SRTMHGT_SAMPLES
        ):
            path = hgt_vrt(target, dataset.step, dataset.samples)
        result.append((tile, path))
    return result
