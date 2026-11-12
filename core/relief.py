"""Тип рельефа охвата по встроенной грубой сетке высот.

Авторежиму рельефа нужно понять, горный ли регион, ещё до скачивания
DEM. Сетка 1°×1° с минимальной и максимальной высотой собрана из
ETOPO5 (5′, NOAA) и лежит в ``resources/data``: расчёт занимает доли
миллисекунды и не требует сети. Горным считается охват, где не меньше
четверти суши имеет перепад высот от 800 м внутри тайла.
"""

import math
from functools import lru_cache
from pathlib import Path

GRID_PARTS = ("data", "relief_grid.npz")

# Перепад высот внутри тайла 1°×1°, по которому тайл считается горным.
MOUNTAIN_RELIEF = 800.0

# Доля горных тайлов, при которой горным считается весь охват. Граница
# отделяет Данию и Польшу (0–8 %) от Турции, Непала и Кавказа (75–88 %).
MOUNTAIN_SHARE = 0.25


@lru_cache(maxsize=1)
def _grid():
    """Пара массивов (максимум, минимум) высот 1°×1° или None."""
    try:
        import numpy as np

        from .resources import resource_path

        path = Path(resource_path(*GRID_PARTS))
        if not path.exists():
            return None
        with np.load(path) as data:
            return data["high"].astype("float32"), data["low"].astype("float32")
    except Exception:  # noqa: BLE001 - без сетки остаётся Copernicus
        return None


def grid_path():
    """Путь к встроенной сетке высот."""
    from .resources import resource_path

    return Path(resource_path(*GRID_PARTS))


def tile_relief(lat, lon, grid=None):
    """Перепад высот в тайле 1°×1°; None для океанского тайла."""
    grid = grid if grid is not None else _grid()
    if grid is None:
        return None
    high, low = grid
    row = 89 - math.floor(lat)
    col = math.floor(lon) + 180
    if not (0 <= row < high.shape[0] and 0 <= col < high.shape[1]):
        return None
    top = float(high[row, col])
    if top <= 0:
        return None
    return top - max(float(low[row, col]), 0.0)


def mountain_share(bbox, grid=None):
    """Доля горных тайлов суши в охвате; None, если суши нет или нет сетки."""
    grid = grid if grid is not None else _grid()
    if grid is None or bbox is None:
        return None
    import numpy as np

    high, low = grid
    first_lat = max(-90, math.floor(bbox.lat_min))
    last_lat = min(89, math.ceil(bbox.lat_max) - 1)
    first_lon = max(-180, math.floor(bbox.lon_min))
    last_lon = min(179, math.ceil(bbox.lon_max) - 1)
    if first_lat > last_lat or first_lon > last_lon:
        return None
    rows = slice(89 - last_lat, 89 - first_lat + 1)
    cols = slice(first_lon + 180, last_lon + 180 + 1)
    top = high[rows, cols]
    land = top > 0
    total = int(land.sum())
    if not total:
        return None
    relief = top - np.maximum(low[rows, cols], 0.0)
    return float((relief[land] >= MOUNTAIN_RELIEF).sum()) / total


def is_mountainous(bbox):
    """Горный ли регион: для него ALOS идёт раньше Copernicus."""
    share = mountain_share(bbox)
    return share is not None and share >= MOUNTAIN_SHARE
