"""Предварительная оценка выреза SliceOSM.

До заказа выреза сервис не сообщает ни размер файла, ни число узлов.
Зато он публикует карту плотности OSM-узлов: 4096×4096 пикселей на
уровне z12, где значение пикселя задают красный и зелёный каналы
(``red * 256 + green``).  По этой же карте сервис проверяет свой предел
по узлам, поэтому оценка совпадает с его собственным расчётом.

Объём PBF зависит не только от числа узлов, но и от плотности тегов:
в городе на узел приходится больше байт, чем в сельской местности.
Поэтому байты на узел берутся из плотности узлов на квадратный
километр.  Время сборки сервисом почти не зависит от объёма: заметную
часть занимает фиксированная подготовка задания.
"""

import hashlib
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import extracts, measure, performance

# Карта плотности лежит рядом с вырезами: оба кэша относятся к SliceOSM.
NODES_IMAGE = "slice_nodes.png"
NODES_IMAGE_URL = extracts.SliceOsm.API + "nodes.png"
# Плотность населения и OSM меняется медленно; карту обновляем раз в месяц.
NODES_IMAGE_TTL = 30 * 24 * 60 * 60
NODES_IMAGE_MIN_SIZE = 1_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Карта плотности и расчёт сервиса: 4096 пикселей на z12.
BASE_ZOOM = 12
BASE_SIZE = 1 << BASE_ZOOM
MAX_TILES = 256
NODE_SCALE = 32

# Сборка выреза на сервисе: фиксированная часть и добавка на миллион
# узлов.  Получено по замерам: 0,86 млн узлов — 34,7 с, 2,97 млн — 38,1 с.
SLICE_BUILD_FIXED_SECONDS = 33.0
SLICE_BUILD_SECONDS_PER_MNODE = 1.6
# Верхняя граница ожидания в очереди: сервис обрабатывает задания
# несколькими воркерами, поэтому реальное ожидание обычно меньше.
SLICE_QUEUE_SECONDS = 35.0

# Байты PBF на узел: в сельской местности около 8, в плотном городе
# до 16.  Плотность, при которой достигается верхняя граница, — 2000
# узлов на км².
SLICE_SIZE_BASE = 7.5
SLICE_SIZE_DENSE = 8.5
SLICE_DENSITY_FULL = 2000.0

DEFAULT_DOWNLOAD_RATE = 1_000_000.0


@dataclass(frozen=True)
class RgbImage:
    """RGB888-буфер карты плотности; строки могут быть выровнены."""

    width: int
    height: int
    stride: int
    data: bytes

    def pixel(self, x, y):
        offset = y * self.stride + x * 3
        return self.data[offset], self.data[offset + 1]

    def block(self, x, y, size):
        """Сумма ``red * 256 + green`` по квадрату ``size × size``."""
        total = 0
        for row in range(y, y + size):
            offset = row * self.stride + x * 3
            end = offset + size * 3
            red = sum(self.data[offset:end:3])
            green = sum(self.data[offset + 1:end:3])
            total += red * 256 + green
        return total


@dataclass(frozen=True)
class SliceEstimate:
    """Оценка выреза: объём, время сборки, скачивания и подготовки."""

    nodes: int
    size_bytes: int
    build_seconds: float
    queue_seconds: float
    download_seconds: float
    prepare_seconds: float
    total_seconds: float
    queue_size: int = 0
    nodes_limit: int = 0
    cached_bytes: int = 0

    @property
    def over_limit(self):
        return bool(self.nodes_limit and self.nodes > self.nodes_limit)


def nodes_image_path(config):
    return config.geofabrik_dir / NODES_IMAGE


def _valid_png_bytes(data):
    if len(data) < NODES_IMAGE_MIN_SIZE:
        return False
    if not data.startswith(_PNG_SIGNATURE):
        return False
    return b"IEND" in data[-12:]


def _valid_png(path):
    """Целый ли PNG: сигнатура, размер и завершающий чанк IEND."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size < NODES_IMAGE_MIN_SIZE:
            return False
        with path.open("rb") as handle:
            if handle.read(8) != _PNG_SIGNATURE:
                return False
            handle.seek(size - 12)
            return b"IEND" in handle.read(12)
    except OSError:
        return False


def ensure_nodes_image(config, cancel=None, progress=None, force=False):
    """Карта плотности узлов; при необходимости скачивает и кэширует.

    Файл небольшой (3,1 МБ), поэтому качается целиком одним ответом:
    ``download_file`` рассчитан на большие выгрузки, а этот хост не
    поддерживает HEAD и отдаёт ответ частями.  ``force`` обновляет карту
    по кнопке, не дожидаясь конца срока кэша.  Старая карта лучше
    отсутствия: если обновление не удалось, но прежний файл цел,
    возвращается он.
    """
    path = nodes_image_path(config)
    valid = _valid_png(path)
    if valid and not force:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = NODES_IMAGE_TTL
        if age < NODES_IMAGE_TTL:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if progress:
            progress("SliceOSM: загрузка карты плотности узлов")
        data = extracts.fetch_bytes(
            NODES_IMAGE_URL, cancel=cancel, timeout=extracts.HTTP_TIMEOUT,
        )
        if not _valid_png_bytes(data):
            raise extracts.ExtractError(
                "SliceOSM: сервер отдал повреждённую карту плотности узлов"
            )
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        temporary.unlink(missing_ok=True)
    except InterruptedError:
        raise
    except Exception:
        if _valid_png(path):
            return path
        raise
    return path


def slice_cache_path(config, bbox):
    """Путь готового выреза для рамки — тот же, что у подготовки данных."""
    key = hashlib.sha1(bbox.as_csv().encode("utf-8")).hexdigest()[:12]
    return config.geofabrik_dir / f"slice_{key}.osm.pbf"


def cached_slice_size(config, bbox):
    """Размер готового выреза в кэше.

    Битый или оборванный вырез не считается готовым: по одному
    ``st_size > 0`` пользователь увидит «карта скачана» при пустой
    области.
    """
    path = slice_cache_path(config, bbox)
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0 or not extracts.looks_like_pbf(path):
        return 0
    return size


def _tile_x(lon, zoom):
    return (lon + 180.0) / 360.0 * (1 << zoom)


def _tile_y(lat, zoom):
    lat = max(-85.05112878, min(85.05112878, lat))
    return (
        (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi)
        / 2.0 * (1 << zoom)
    )


def _tile_bounds(bbox, zoom):
    """Диапазон тайлов zoom, пересекающих рамку (включительно)."""
    count = 1 << zoom
    x0 = int(math.floor(_tile_x(bbox.lon_min, zoom)))
    x1 = int(math.floor(_tile_x(bbox.lon_max, zoom) - 1e-12))
    y0 = int(math.floor(_tile_y(bbox.lat_max, zoom)))
    y1 = int(math.floor(_tile_y(bbox.lat_min, zoom) - 1e-12))
    x0 = max(0, min(count - 1, x0))
    x1 = max(x0, min(count - 1, x1))
    y0 = max(0, min(count - 1, y0))
    y1 = max(y0, min(count - 1, y1))
    return x0, x1, y0, y1


def _tile_count(bounds):
    x0, x1, y0, y1 = bounds
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def _covering(bbox):
    """Уровень и диапазон тайлов — как в веб-интерфейсе SliceOSM.

    Берётся самый подробный зум, у которого тайлов не больше лимита:
    на уровне выше лимита тайлов уже больше, чем нужно для оценки.
    """
    zoom = 0
    bounds = _tile_bounds(bbox, 0)
    for candidate in range(0, 15):
        candidate_bounds = _tile_bounds(bbox, candidate)
        if _tile_count(candidate_bounds) > MAX_TILES:
            break
        zoom = candidate
        bounds = candidate_bounds
    return zoom, bounds


def _pixel_sum(image, zoom, x, y):
    """Значение тайла: сумма пикселей z12, как в сервисе."""
    if zoom < BASE_ZOOM:
        size = 1 << (BASE_ZOOM - zoom)
        block = getattr(image, "block", None)
        if block is not None:
            return block(x * size, y * size, size)
        total = 0
        for ix in range(x * size, x * size + size):
            for iy in range(y * size, y * size + size):
                red, green = image.pixel(ix, iy)
                total += red * 256 + green
        return total
    if zoom == BASE_ZOOM:
        block = getattr(image, "block", None)
        if block is not None:
            return block(x, y, 1)
        red, green = image.pixel(x, y)
        return red * 256 + green
    factor = 1 << (zoom - BASE_ZOOM)
    px = x // factor
    py = y // factor
    block = getattr(image, "block", None)
    if block is not None:
        value = block(px, py, 1)
    else:
        red, green = image.pixel(px, py)
        value = red * 256 + green
    return value / (factor * factor)


def estimate_nodes(bbox, image):
    """Ожидаемое число узлов в рамке по карте плотности."""
    if image.width != BASE_SIZE or image.height != BASE_SIZE:
        raise ValueError(
            "SliceOSM: карта плотности узлов должна быть 4096×4096"
        )
    zoom, (x0, x1, y0, y1) = _covering(bbox)
    total = 0.0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            total += _pixel_sum(image, zoom, x, y)
    return int(total * NODE_SCALE)


def bytes_per_node(nodes, area_km2):
    """Байты PBF на узел с поправкой на плотность населения."""
    density = max(0, int(nodes or 0)) / max(1.0, float(area_km2 or 0.0))
    share = min(1.0, density / SLICE_DENSITY_FULL)
    return SLICE_SIZE_BASE + SLICE_SIZE_DENSE * share


def _int_value(payload, key):
    try:
        return max(0, int((payload or {}).get(key) or 0))
    except (TypeError, ValueError):
        return 0


def estimate(bbox, image=None, nodes=None, service=None,
             cached_bytes=0, cpu=1.0, workers=1, terrain_seconds=0.0,
             download_rate=DEFAULT_DOWNLOAD_RATE):
    """Собрать оценку по карте плотности, размеру кэша и состоянию сервиса.

    ``nodes`` можно передать явно — для тестов и для уточнения, когда
    сервис уже сообщил ``NodesTotal``.  Если размер выреза известен
    (``cached_bytes``), сборка и скачивание не оцениваются.
    """
    service = service or {}
    cached_bytes = max(0, int(cached_bytes or 0))
    queue_size = _int_value(service, "QueueSize")
    nodes_limit = _int_value(service, "NodesLimit")
    if cached_bytes:
        nodes = max(0, int(nodes or 0))
        size_bytes = cached_bytes
        build_seconds = 0.0
        queue_seconds = 0.0
        download_seconds = 0.0
    else:
        if nodes is None:
            if image is None:
                raise ValueError("SliceOSM: нет карты плотности узлов")
            nodes = estimate_nodes(bbox, image)
        nodes = max(0, int(nodes))
        width, height = measure.frame_sizes_km(bbox)
        area_km2 = max(1.0, float(width) * float(height))
        size_bytes = int(round(nodes * bytes_per_node(nodes, area_km2)))
        build_seconds = (
            SLICE_BUILD_FIXED_SECONDS
            + SLICE_BUILD_SECONDS_PER_MNODE * nodes / 1e6
        )
        queue_seconds = queue_size * SLICE_QUEUE_SECONDS
        download_seconds = (
            size_bytes / download_rate if download_rate > 0 else 0.0
        )
    prepare_seconds = performance.estimate_map_seconds(
        size_bytes / (1024 * 1024),
        cpu=cpu,
        workers=workers,
        terrain_seconds=terrain_seconds,
    )
    total_seconds = (
        queue_seconds + build_seconds + download_seconds + prepare_seconds
    )
    return SliceEstimate(
        nodes=nodes,
        size_bytes=size_bytes,
        build_seconds=build_seconds,
        queue_seconds=queue_seconds,
        download_seconds=download_seconds,
        prepare_seconds=prepare_seconds,
        total_seconds=total_seconds,
        queue_size=queue_size,
        nodes_limit=nodes_limit,
        cached_bytes=cached_bytes,
    )
