"""Проверка положения пиков OSM по рельефу.

Модуль общий для пайплайна плагина и tools/check_peak_placement.py.
Реализация использует только numpy: scipy в QGIS может отсутствовать.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

try:
    from scipy import ndimage as _scipy_ndimage
except ImportError:  # в QGIS на Windows scipy обычно нет
    _scipy_ndimage = None

# Размер ячейки сетки, к которой приводятся все наборы рельефа.
PIXEL_DEGREES = 1.0 / 3600.0

# Теги, по которым объект считается названным.
NAME_TAGS = ("name", "alt_name", "official_name", "short_name", "old_name",
             "loc_name", "int_name", "nat_name", "reg_name", "sorting_name")

# Допуск шума SRTM: вершиной считаем точку, не ниже максимума в окне.
SUMMIT_TOLERANCE = 8.0
SUMMIT_TOLERANCE_WIDE = 15.0


# Пороги для растровых DSM (ALOS, Copernicus, NASADEM).  Поверхность у них
# другая: растительность, шум и пропуски сдвигают часть метрик на границу
# порога.  Профиль поднимает самые неустойчивые пороги, чтобы не ловить
# верные пики.  SRTM и SRTM-подобные источники остаются на базовом правиле.
SOURCE_THRESHOLD_PROFILES = {
    "alos": {
        "no_crest_450": 80.0,
        "far_drop": 260.0,
        "near_max_d": 400.0,
        "stream_distance": 50.0,
        "ele_below": 4.6,
        "summit_far_neighbour": 900.0,
    },
    "cop30": {
        "no_crest_450": 80.0,
        "far_drop": 260.0,
        "near_max_d": 400.0,
        "stream_distance": 50.0,
        "ele_below": 4.6,
        "summit_far_neighbour": 900.0,
    },
    # У NASADEM near_max_d остаётся базовым: с 400 теряется ошибочный пик,
    # который базовая пара «провал в 200 м + вершина далеко» ещё ловит.
    # Дополнительно первая конъюнкция не срабатывает в 100 м от вершины:
    # на этой поверхности так помечались верные пики у самой вершины.
    "nasadem": {
        "no_crest_450": 80.0,
        "far_drop": 260.0,
        "stream_distance": 50.0,
        "ele_below": 4.6,
        "summit_far_neighbour": 900.0,
        "valley_summit_min": 100.0,
    },
    # Мозаика «Автоматически» и набор Sonny DTM: за основу взят профиль
    # Copernicus — это фон мозаики.  Точные пороги для них, как и для
    # остальных наборов, нужно подбирать на размеченной выборке.
    "auto": {
        "no_crest_450": 80.0,
        "far_drop": 260.0,
        "near_max_d": 400.0,
        "stream_distance": 50.0,
        "ele_below": 4.6,
        "summit_far_neighbour": 900.0,
    },
    "sonny": {
        "no_crest_450": 80.0,
        "far_drop": 260.0,
        "near_max_d": 400.0,
        "stream_distance": 50.0,
        "ele_below": 4.6,
        "summit_far_neighbour": 900.0,
    },
}

# Базовые пороги правила (SRTM и источники без отдельного профиля).
BASE_THRESHOLDS = {
    "radius": 300,
    "deep_radius": 600,
    "deep_drop": 450.0,
    "ridge_cross": -10.0,
    "summit_limit": 200.0,
    "sharp_radius": 150.0,
    "sharp_cross": -15.0,
    "ring_radius": 200.0,
    "ring_700_radius": 700.0,
    "ring_150_radius": 150.0,
    "divide_acc": 3,
    "divide_sigma": 2.0,
    "ridge_radius": 0,
    "off_ridge": 40.0,
    "no_steep_crest": 250.0,
    "pit_drop": 120.0,
    "far_drop": 240.0,
    "far_divide": 50.0,
    "no_crest_450": 40.0,
    "valley_cross900": 120.0,
    "valley_drop_high": 350.0,
    "shift_drop": 500.0,
    "valley_ring_soft": 0.45,
    "near_max_d": 300.0,
    "ele_below": 4.0,
    "ring_600_relative": 0.5,
    "ele_diff": 10.0,
    "smooth_150": 90.0,
    "summit_far": 600.0,
    "summit_far_neighbour": 600.0,
    "ridge_drop": 170.0,
    "neighbour_nearest": 1400.0,
    "valley_ring_wide": 0.65,
    "stream_distance": 200.0,
    "valley_summit_min": 0.0,
}


def thresholds_for_source(source_key):
    """Пороги правила для источника рельефа плагина."""
    thresholds = dict(BASE_THRESHOLDS)
    thresholds.update(SOURCE_THRESHOLD_PROFILES.get(source_key, {}))
    return thresholds


class _Ndimage:
    """Минимальная замена scipy.ndimage: в QGIS на Windows scipy нет."""

    @staticmethod
    def _gaussian_kernel(sigma):
        radius = max(1, int(round(4.0 * sigma)))
        positions = np.arange(-radius, radius + 1, dtype="float64")
        weights = np.exp(-0.5 * (positions / sigma) ** 2)
        return weights / weights.sum()

    @staticmethod
    def _convolve1d(array, kernel, axis):
        radius = len(kernel) // 2
        padding = [(0, 0), (0, 0)]
        padding[axis] = (radius, radius)
        # scipy.ndimage по умолчанию использует mode="reflect":
        # край отражается без повтора краевой ячейки (numpy "symmetric").
        padded = np.pad(array, padding, mode="symmetric")
        result = np.empty_like(array, dtype="float64")
        if axis == 0:
            for column in range(array.shape[1]):
                result[:, column] = np.convolve(
                    padded[:, column], kernel, mode="valid"
                )
        else:
            for row in range(array.shape[0]):
                result[row, :] = np.convolve(
                    padded[row, :], kernel, mode="valid"
                )
        return result

    @classmethod
    def gaussian_filter(cls, array, sigma, **kwargs):
        if sigma <= 0:
            return np.array(array, dtype=array.dtype, copy=True)
        kernel = cls._gaussian_kernel(float(sigma))
        result = cls._convolve1d(
            cls._convolve1d(array, kernel, 0), kernel, 1
        )
        return result.astype(array.dtype, copy=False)

    @staticmethod
    def maximum_filter(array, size=3, mode="nearest"):
        radius = size // 2
        pad_mode = "edge" if mode == "nearest" else mode
        padded = np.pad(array, radius, mode=pad_mode)
        result = np.full(array.shape, -np.inf, dtype=array.dtype)
        for row in range(size):
            for column in range(size):
                result = np.maximum(
                    result,
                    padded[row:row + array.shape[0],
                           column:column + array.shape[1]],
                )
        return result

    @staticmethod
    def map_coordinates(array, coordinates, order=1, mode="nearest"):
        rows, columns = coordinates
        rows = np.clip(rows, 0, array.shape[0] - 1)
        columns = np.clip(columns, 0, array.shape[1] - 1)
        row0 = np.floor(rows).astype(np.intp)
        column0 = np.floor(columns).astype(np.intp)
        row1 = np.minimum(row0 + 1, array.shape[0] - 1)
        column1 = np.minimum(column0 + 1, array.shape[1] - 1)
        delta_row = rows - row0
        delta_column = columns - column0
        return (
            array[row0, column0] * (1 - delta_row) * (1 - delta_column)
            + array[row1, column0] * delta_row * (1 - delta_column)
            + array[row0, column1] * (1 - delta_row) * delta_column
            + array[row1, column1] * delta_row * delta_column
        )

    @staticmethod
    def _binary_filter(mask, structure, use_max):
        radius = structure.shape[0] // 2
        padded = np.pad(mask, radius, mode="constant", constant_values=False)
        if use_max:
            result = np.zeros(mask.shape, dtype=bool)
            for row in range(structure.shape[0]):
                for column in range(structure.shape[1]):
                    if not structure[row, column]:
                        continue
                    piece = padded[row:row + mask.shape[0],
                                   column:column + mask.shape[1]]
                    result |= piece
        else:
            result = np.ones(mask.shape, dtype=bool)
            for row in range(structure.shape[0]):
                for column in range(structure.shape[1]):
                    if not structure[row, column]:
                        continue
                    piece = padded[row:row + mask.shape[0],
                                   column:column + mask.shape[1]]
                    result &= piece
        return result

    @classmethod
    def binary_closing(cls, mask, structure=None):
        if structure is None:
            structure = np.ones((3, 3), dtype=bool)
        return cls._binary_filter(
            cls._binary_filter(mask, structure, True), structure, False
        )

    @staticmethod
    def laplace(array):
        padded = np.pad(array, 1, mode="symmetric")
        return (
            padded[:-2, 1:-1] + padded[2:, 1:-1]
            + padded[1:-1, :-2] + padded[1:-1, 2:]
            - 4.0 * array
        )


ndimage = _scipy_ndimage if _scipy_ndimage is not None else _Ndimage()


def fill_nan(array):
    """Заполняет пропуски DEM ближайшими известными значениями."""
    mask = np.isnan(array)
    if not mask.any():
        return array
    if _scipy_ndimage is not None:
        indices = _scipy_ndimage.distance_transform_edt(
            mask, return_distances=False, return_indices=True
        )
        return array[tuple(indices)]
    valid_rows, valid_columns = np.nonzero(~mask)
    nan_rows, nan_columns = np.nonzero(mask)
    if not len(valid_rows):
        return array
    result = np.array(array, dtype="float32", copy=True)
    valid_values = array[valid_rows, valid_columns]
    if len(nan_rows) * len(valid_rows) <= 4_000_000:
        for start in range(0, len(nan_rows), 2048):
            rows = nan_rows[start:start + 2048]
            columns = nan_columns[start:start + 2048]
            distance = (
                (rows[:, None] - valid_rows[None, :]) ** 2
                + (columns[:, None] - valid_columns[None, :]) ** 2
            )
            result[rows, columns] = valid_values[np.argmin(distance, axis=1)]
        return result
    # Крупный пропуск: заполняем средним по известным соседям.
    for _ in range(256):
        mask = np.isnan(result)
        if not mask.any():
            break
        padded = np.pad(result, 1, mode="edge")
        neighbours = np.stack([
            padded[0:-2, 0:-2], padded[0:-2, 1:-1], padded[0:-2, 2:],
            padded[1:-1, 0:-2], padded[1:-1, 2:],
            padded[2:, 0:-2], padded[2:, 1:-1], padded[2:, 2:],
        ])
        known = ~np.isnan(neighbours)
        counts = known.sum(axis=0)
        sums = np.where(known, neighbours, 0.0).sum(axis=0)
        mean = np.divide(
            sums, counts,
            out=np.full(result.shape, np.nan, dtype="float64"),
            where=counts > 0,
        )
        result[mask] = mean[mask]
    mask = np.isnan(result)
    if mask.any():
        result[mask] = float(np.nanmean(result))
    return result


def sample(array, rows, columns):
    """Билинейная выборка из массива (order=1, mode="nearest")."""
    return _Ndimage.map_coordinates(array, (rows, columns))


@dataclass
class PeakPoint:
    """Пик для проверки: минимум полей, нужных правилу."""

    osm_id: str
    lon: float
    lat: float
    ele: Optional[float] = None
    named: bool = False

    @property
    def category(self):
        if self.named:
            return "named"
        if self.ele is not None:
            return "ele_only"
        return "anonymous"

def half_pixels(metres, lat):
    """Полуразмер окна DEM в пикселях для радиуса в метрах."""
    metres_per_degree_lat = 111320.0
    metres_per_degree_lon = max(20000.0, metres_per_degree_lat * math.cos(math.radians(lat)))
    half_y = metres / metres_per_degree_lat / PIXEL_DEGREES
    half_x = metres / metres_per_degree_lon / PIXEL_DEGREES
    return max(1, int(round(half_x))), max(1, int(round(half_y)))


@dataclass
class Metrics:
    dem: float = float("nan")
    max3: float = float("nan")
    min3: float = float("nan")
    max5: float = float("nan")
    drop: dict = field(default_factory=dict)
    summit_distance: float = float("nan")
    summit_drop: float = float("nan")
    cross300: float = float("nan")
    cross600: float = float("nan")
    ridge_distance: float = float("nan")
    divide_distance: float = float("nan")
    sharp_distance: float = float("nan")
    sharp_100_distance: float = float("nan")
    sharp_300_distance: float = float("nan")
    ridge_strict_distance: float = float("nan")
    divide_strict_distance: float = float("nan")
    sharp_300_strict_distance: float = float("nan")
    sharp_150_strict_distance: float = float("nan")
    sharp_450_distance: float = float("nan")
    near_max_distance: float = float("nan")
    near_max_drop: float = float("nan")
    ring_higher: float = float("nan")
    ring_700_higher: float = float("nan")
    ring_1200_higher: float = float("nan")
    stream_distance: float = float("nan")
    ring_150_lower: float = float("nan")
    ring_600_lower: float = float("nan")
    ring_200_lower: float = float("nan")
    ring_300_lower: float = float("nan")
    ring_450_lower: float = float("nan")
    cross900: float = float("nan")
    curvature: float = float("nan")
    relief: float = float("nan")
    neighbour_nearest: float = float("nan")
    ring_600_relative: float = float("nan")
    ele_diff: float = float("nan")
    ele_below: float = float("nan")

    @property
    def on_summit(self):
        """Точка стоит на локальном максимуме DEM в пределах ~90 м."""
        if math.isnan(self.dem) or math.isnan(self.max3):
            return False
        return self.max3 - self.dem <= SUMMIT_TOLERANCE

    @property
    def on_summit_wide(self):
        """Точка стоит на вершине с точностью до ~150 м."""
        if math.isnan(self.dem) or math.isnan(self.max5):
            return False
        return self.max5 - self.dem <= SUMMIT_TOLERANCE_WIDE

    @property
    def in_pit(self):
        """Точка стоит в локальном минимуме DEM — яма или дно долины."""
        if math.isnan(self.dem) or math.isnan(self.min3) or math.isnan(self.max3):
            return False
        return self.dem - self.min3 <= 3.0 and self.max3 - self.dem > 3.0

    def drop_at(self, radius):
        value = self.drop.get(radius)
        return float("nan") if value is None else value


def find_summit(window, lat, dem_value, tolerance=50.0):
    """Ближайший локальный максимум DEM.

    Локальный максимум — ячейка не ниже всех восьми соседей.  Он не
    обязан быть выдающейся вершиной: на хребте это может быть бугор
    рядом с точкой.  Допускаются максимумы ниже точки на tolerance
    метров — на хребте соседний бугор часто чуть ниже.

    Возвращает (расстояние в метрах, превышение над точкой, строка,
    столбец) либо None.
    """
    maximum = ndimage.maximum_filter(window, size=3, mode="nearest")
    is_peak = window >= maximum - 1e-6
    rows, columns = window.shape
    row_center = rows // 2
    column_center = columns // 2
    rows_index, columns_index = np.nonzero(is_peak)
    best = None
    metres_lat = 111320.0
    metres_lon = metres_lat * math.cos(math.radians(lat))
    for row, column in zip(rows_index, columns_index):
        height = float(window[row, column])
        if height < dem_value - tolerance:
            continue
        dy = (row - row_center) * metres_lat / 3600.0
        dx = (column - column_center) * metres_lon / 3600.0
        distance = math.hypot(dx, dy)
        if best is None or distance < best[0]:
            best = (distance, height - dem_value, int(row), int(column))
    return best


def find_near_max(smoothed, lat, height, tolerance=60.0):
    """Ближайший локальный максимум сглаженной поверхности.

    Отличается от find_summit тем, что работает по сглаженному DEM:
    одиночные шумовые выбросы SRTM не создают лишних «вершин».
    Возвращает (расстояние, превышение над точкой) либо None.
    """
    maximum = ndimage.maximum_filter(smoothed, size=3, mode="nearest")
    is_peak = smoothed >= maximum - 1e-6
    rows, columns = smoothed.shape
    row_center = rows // 2
    column_center = columns // 2
    rows_index, columns_index = np.nonzero(is_peak)
    best = None
    metres_lat = 111320.0
    metres_lon = metres_lat * math.cos(math.radians(lat))
    for row, column in zip(rows_index, columns_index):
        value = float(smoothed[row, column])
        if value < height - tolerance:
            continue
        dy = (row - row_center) * metres_lat / 3600.0
        dx = (column - column_center) * metres_lon / 3600.0
        distance = math.hypot(dx, dy)
        if best is None or distance < best[0]:
            best = (distance, value - height)
    return best


def cross_profile(smoothed, lat, radius):
    """Поперечный профиль склона для каждой ячейки окна.

    Для каждой ячейки берётся линия падения склона и две точки на
    расстоянии radius поперёк неё.  Возвращает превышение более высокой
    из них над ячейкой: отрицательное значение — гребень (обе стороны
    ниже), положительное — долина или склон.
    """
    gradient_y, gradient_x = np.gradient(smoothed)
    norm = np.hypot(gradient_x, gradient_y) + 1e-9
    across_x = gradient_y / norm
    across_y = -gradient_x / norm
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    row_grid, column_grid = np.indices(smoothed.shape)
    sides = []
    for sign in (1, -1):
        sample_rows = row_grid + sign * across_y * radius / metres_lat
        sample_columns = column_grid + sign * across_x * radius / metres_lon
        sides.append(ndimage.map_coordinates(
            smoothed, [sample_rows, sample_columns], order=1, mode="nearest"
        ))
    return np.maximum(sides[0] - smoothed, sides[1] - smoothed)


# Радиусы (в метрах), на которых ищется выпуклость гребня.  Мелкие
# ловят острые гребни, крупные — широкие и пологие.
RIDGE_SCALES = (60.0, 90.0, 150.0, 240.0, 360.0, 500.0)


def ridge_mask(window, lat, ridge_cross=-10.0, radius=None):
    """Ячейки-гребни: поперёк склона рельеф ниже с обеих сторон.

    Выпуклость проверяется на нескольких масштабах: ячейка считается
    гребнем, если хотя бы на одном из них обе стороны поперёк склона
    ниже.  Иначе широкие и пологие гребни оставались бы незамеченными.
    """
    smoothed = ndimage.gaussian_filter(window, 1.0)
    scales = (radius,) if radius else RIDGE_SCALES
    cross = None
    for scale in scales:
        current = cross_profile(smoothed, lat, scale)
        cross = current if cross is None else np.minimum(cross, current)
    mask = cross <= ridge_cross
    # Закрываем разрывы от шума, чтобы гребень был линией, а не точками.
    return ndimage.binary_closing(mask, structure=np.ones((3, 3)))


# Соседние ячейки в порядке обхода для стока по D8.
FLOW_NEIGHBOURS = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1),
    (0, 1), (1, -1), (1, 0), (1, 1),
)


def flow_accumulation(surface, lat):
    """Площадь водосбора каждой ячейки по D8, в числе ячеек.

    Ячейка, в которую не притекает вода, имеет площадь 1.  Настоящий
    хребет — это водораздел: с него вода только стекает, поэтому
    площадь минимальна.  Склон ниже гребня собирает воду с части
    склона и имеет площадь заметно больше.
    """
    rows, columns = surface.shape
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    steepest = np.zeros(surface.shape, dtype=np.int8)
    slope = np.zeros(surface.shape)
    for index, (dy, dx) in enumerate(FLOW_NEIGHBOURS):
        distance = math.hypot(dy * metres_lat, dx * metres_lon)
        neighbour = np.full(surface.shape, np.nan)
        source_rows = slice(max(0, dy), rows + min(0, dy))
        source_columns = slice(max(0, dx), columns + min(0, dx))
        target_rows = slice(max(0, -dy), rows + min(0, -dy))
        target_columns = slice(max(0, -dx), columns + min(0, -dx))
        neighbour[target_rows, target_columns] = surface[
            source_rows, source_columns
        ]
        drop = np.nan_to_num((surface - neighbour) / distance, nan=-1e9)
        better = drop > slope
        slope = np.where(better, drop, slope)
        steepest = np.where(better, index, steepest)
    # Адрес стока каждой ячейки считается один раз векторно: прежний
    # цикл делал divmod для каждой ячейки и был самой долгой частью
    # проверки пиков.
    row_grid, column_grid = np.indices(surface.shape)
    step_rows = np.array([item[0] for item in FLOW_NEIGHBOURS])
    step_columns = np.array([item[1] for item in FLOW_NEIGHBOURS])
    target_rows = row_grid + step_rows[steepest]
    target_columns = column_grid + step_columns[steepest]
    valid = (
        (target_rows >= 0) & (target_rows < rows)
        & (target_columns >= 0) & (target_columns < columns)
    )
    targets = np.where(
        valid, target_rows * columns + target_columns, -1
    ).astype(np.int64).ravel()
    order = np.argsort(surface, axis=None)[::-1].tolist()
    # Список Python быстрее массива NumPy в этом цикле: обращений
    # десятки тысяч, и накладные расходы на скаляр NumPy заметны.
    area = [1.0] * surface.size
    for cell in order:
        target = int(targets[cell])
        if target >= 0:
            area[target] += area[cell]
    return np.array(area, dtype=np.float64).reshape(surface.shape)


def divide_mask(window, lat, threshold=3, sigma=2.0):
    """Ячейки водоразделов: собирают не больше threshold ячеек.

    Поверхность сглаживается, иначе шум SRTM плодит ложные водоразделы
    на ровном месте.
    """
    surface = ndimage.gaussian_filter(window, sigma)
    return flow_accumulation(surface, lat) <= threshold


def mask_distance(mask, lat, fallback=float("nan")):
    """Расстояние от центра окна до ближайшей ячейки маски, м."""
    if not mask.any():
        return fallback
    rows, columns = np.nonzero(mask)
    row_center = mask.shape[0] // 2
    column_center = mask.shape[1] // 2
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    return float(np.min(np.hypot(
        (rows - row_center) * metres_lat,
        (columns - column_center) * metres_lon,
    )))


def divide_distance(window, lat, threshold=3, sigma=2.0):
    """Расстояние до ближайшей ячейки водораздела, м."""
    return mask_distance(divide_mask(window, lat, threshold, sigma), lat)


def ring_higher_fraction(surface, lat, radius=200.0, samples=180):
    """Доля кольца вокруг точки, где рельеф выше неё.

    На вершине доля нулевая, на хребте выше только дуга вверх по
    гребню, на склоне — примерно половина кольца, в долине — почти всё.
    """
    rows, columns = surface.shape
    row_center, column_center = rows // 2, columns // 2
    height = float(surface[row_center, column_center])
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    angles = np.linspace(0, 2 * math.pi, samples, endpoint=False)
    rows_ring = row_center + np.sin(angles) * radius / metres_lat
    columns_ring = column_center + np.cos(angles) * radius / metres_lon
    values = ndimage.map_coordinates(
        surface, [rows_ring, columns_ring], order=1, mode="nearest"
    )
    return float(np.mean(values > height + 1.0))


def ring_lower_drop(surface, lat, radius=150.0, samples=180):
    """Насколько точка выше самой низкой точки кольца вокруг неё, м.

    Большое значение означает, что точка стоит на краю обрыва, а не в
    седловине.
    """
    rows, columns = surface.shape
    row_center, column_center = rows // 2, columns // 2
    height = float(surface[row_center, column_center])
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    angles = np.linspace(0, 2 * math.pi, samples, endpoint=False)
    rows_ring = row_center + np.sin(angles) * radius / metres_lat
    columns_ring = column_center + np.cos(angles) * radius / metres_lon
    values = ndimage.map_coordinates(
        surface, [rows_ring, columns_ring], order=1, mode="nearest"
    )
    return float(height - np.min(values))


def measure(peak, dem, radii, thresholds):
    """Метрики положения пика по рельефу.

    Считается одно окно по радиусу 1500 м, из него берутся и локальные
    максимумы (3x3 и 5x5), и максимум в радиусе 600 м.  Максимумы
    считаются по слегка сглаженной поверхности: одиночные шумовые
    выбросы SRTM иначе завышают «провал от вершины».
    """
    metrics = Metrics()
    outer = max(radii) if radii else 1500
    half_x, half_y = half_pixels(outer, peak.lat)
    window = dem.window(peak.lon, peak.lat, half_x, half_y)
    if window is None or np.isnan(window).all():
        return metrics
    window = fill_nan(window)
    rows, columns = window.shape
    row_center = rows // 2
    column_center = columns // 2

    # Точка попадает в угол четырёх ячеек DEM, поэтому высота в точке —
    # среднее этих четырёх ячеек (билинейная интерполяция).
    corner = window[
        row_center - 1:row_center + 1, column_center - 1:column_center + 1
    ]
    metrics.dem = float(np.mean(corner))
    metrics.min3 = float(np.min(
        window[
            row_center - 1:row_center + 2, column_center - 1:column_center + 2
        ]
    ))
    smoothed = ndimage.gaussian_filter(window, sigma=1.0)
    metrics.max3 = float(np.max(smoothed[
        row_center - 1:row_center + 2, column_center - 1:column_center + 2
    ]))
    metrics.max5 = float(np.max(smoothed[
        max(0, row_center - 2):row_center + 3, max(0, column_center - 2):column_center + 3
    ]))

    for radius in radii:
        if radius >= outer:
            metrics.drop[radius] = float(np.max(smoothed) - metrics.dem)
            continue
        inner_x, inner_y = half_pixels(radius, peak.lat)
        metrics.drop[radius] = float(
            np.max(
                smoothed[
                    row_center - inner_y:row_center + inner_y + 1,
                    column_center - inner_x:column_center + inner_x + 1,
                ]
            )
            - metrics.dem
        )
    # Поперечный профиль склона: на хребте рельеф ниже по обе стороны от
    # линии падения, в долине — выше, на склоне — выше с одной стороны.
    # Считается для каждой ячейки окна, чтобы найти ближайший гребень.
    metrics.cross300 = float(
        cross_profile(smoothed, peak.lat, 300)[row_center, column_center]
    )
    metrics.cross600 = float(
        cross_profile(smoothed, peak.lat, 600)[row_center, column_center]
    )
    ridges = ridge_mask(
        window, peak.lat, thresholds["ridge_cross"],
        thresholds["ridge_radius"] or None,
    )
    metrics.ridge_distance = mask_distance(
        ridges, peak.lat, float(outer)
    )
    # Тот же гребень, но с более строгим порогом: обе стороны ниже
    # сразу на 20 м.  Отсекает пологие выпуклости, которые гребнем
    # считать нельзя.
    strict_ridges = ridge_mask(window, peak.lat, -20.0, None)
    metrics.ridge_strict_distance = mask_distance(
        strict_ridges, peak.lat, float(outer)
    )

    surface = ndimage.gaussian_filter(window, thresholds["divide_sigma"])
    area = flow_accumulation(surface, peak.lat)
    divides = area <= thresholds["divide_acc"]
    metrics.divide_distance = mask_distance(divides, peak.lat, float(outer))
    # Строгий водораздел: в ячейку не собирается вовсе (площадь 1).
    metrics.divide_strict_distance = mask_distance(
        area <= 1, peak.lat, float(outer)
    )
    # Острый гребень: поперёк склона рельеф заметно ниже уже на 150 м.
    # Отдельно от широких гребней: пик должен стоять и у такого гребня.
    sharp = cross_profile(
        smoothed, peak.lat, thresholds["sharp_radius"]
    ) <= thresholds["sharp_cross"]
    metrics.sharp_distance = mask_distance(sharp, peak.lat, float(outer))
    # Тот же признак на других масштабах: острый перепад на 100 м и на
    # 300 м поперёк склона.
    for scale, name in ((100.0, "sharp_100_distance"),
                        (300.0, "sharp_300_distance")):
        profile = cross_profile(smoothed, peak.lat, scale)
        setattr(
            metrics, name,
            mask_distance(
                profile <= thresholds["sharp_cross"], peak.lat, float(outer)
            ),
        )
    # Крутой гребень: перепад 25 м на 300 м, перепад 25 м на 150 м и
    # гребень на 450 м.
    metrics.sharp_300_strict_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 300.0) <= -25.0,
        peak.lat, float(outer),
    )
    metrics.sharp_150_strict_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 150.0) <= -25.0,
        peak.lat, float(outer),
    )
    metrics.sharp_450_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 450.0) <= -15.0,
        peak.lat, float(outer),
    )
    metrics.ring_higher = ring_higher_fraction(
        smoothed, peak.lat, thresholds["ring_radius"]
    )
    metrics.ring_700_higher = ring_higher_fraction(
        smoothed, peak.lat, thresholds["ring_700_radius"]
    )
    metrics.ring_1200_higher = ring_higher_fraction(smoothed, peak.lat, 1200.0)
    # Расстояние до ближайшей ячейки с заметным водосбором — ручья или
    # тальца.  Пик рядом с ручьём стоит в долине, а не на гребне.
    metrics.stream_distance = mask_distance(
        area > 20, peak.lat, float(outer)
    )
    metrics.ring_150_lower = ring_lower_drop(
        smoothed, peak.lat, thresholds["ring_150_radius"]
    )
    metrics.ring_600_lower = ring_lower_drop(smoothed, peak.lat, 600.0)
    metrics.ring_200_lower = ring_lower_drop(smoothed, peak.lat, 200.0)
    metrics.ring_300_lower = ring_lower_drop(smoothed, peak.lat, 300.0)
    metrics.ring_450_lower = ring_lower_drop(smoothed, peak.lat, 450.0)
    # Местный рельеф: размах высот в окне.  В Гималаях он в разы больше,
    # чем на Кавказе, поэтому часть признаков нормируется на него.
    metrics.relief = float(np.nanmax(smoothed) - np.nanmin(smoothed))
    metrics.ring_600_relative = metrics.ring_600_lower / max(
        metrics.relief, 1.0
    )
    # Поперечный профиль на 900 м: в Гималаях нужен большой масштаб,
    # иначе высокие склоны выглядят как провалы.
    metrics.cross900 = float(
        cross_profile(smoothed, peak.lat, 900.0)[row_center, column_center]
    )
    # Кривизна рельефа в точке: вогнутость (дно долины) положительна.
    metrics.curvature = float(
        ndimage.laplace(smoothed)[row_center, column_center]
    )

    near_max = find_near_max(smoothed, peak.lat, metrics.dem)
    if near_max is not None:
        metrics.near_max_distance = near_max[0]
        metrics.near_max_drop = near_max[1]

    # Расхождение высоты из OSM с рельефом.  У настоящей вершины оно
    # невелико; если же ele совсем не совпадает с DEM, пик поставлен
    # не на ту вершину.
    if peak.ele:
        metrics.ele_diff = abs(peak.ele - metrics.dem) / peak.ele * 100.0
        metrics.ele_below = (peak.ele - metrics.dem) / peak.ele * 100.0

    summit = find_summit(window, peak.lat, metrics.dem)
    if summit is not None:
        metrics.summit_distance = summit[0]
        metrics.summit_drop = summit[1]
    else:
        # Отдельной вершины в окне нет: считаем, что ближайшая — за
        # пределом поиска, а превышение — максимум окна.
        metrics.summit_distance = float(outer)
        metrics.summit_drop = float(np.max(smoothed) - metrics.dem)
    return metrics


def neighbour_stats(results, radius=2000.0):
    """Сравнение каждого пика с соседними пиками OSM.

    Ошибочный пик обычно оказывается ниже соседей: рядом стоят
    настоящие вершины, а он — в долине между ними.  Возвращает словарь
    osm_id -> (число соседей в радиусе, максимальная разница высот DEM
    с соседом, расстояние до ближайшего более высокого соседа).
    """
    grid = defaultdict(list)
    for peak, metrics, _verdict, _flags, _mismatch in results:
        if math.isnan(metrics.dem):
            continue
        grid[(int(peak.lon / 0.05), int(peak.lat / 0.05))].append((peak, metrics.dem))
    stats = {}
    for peak, metrics, _verdict, _flags, _mismatch in results:
        if math.isnan(metrics.dem):
            continue
        key = (int(peak.lon / 0.05), int(peak.lat / 0.05))
        neighbours = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(grid.get((key[0] + dx, key[1] + dy), ()))
        count = 0
        drop = 0.0
        nearest = float("nan")
        for other, other_dem in neighbours:
            if other is peak:
                continue
            distance = math.hypot(
                (other.lon - peak.lon) * 111320.0 * math.cos(math.radians(peak.lat)),
                (other.lat - peak.lat) * 111320.0,
            )
            if distance > radius:
                continue
            count += 1
            difference = other_dem - metrics.dem
            if difference > 10 and (math.isnan(nearest) or distance < nearest):
                nearest = distance
            if difference > drop:
                drop = difference
        stats[peak.osm_id] = (count, drop, nearest)
    return stats


def classify(peak, metrics, thresholds, neighbour=None):
    """Вердикт по пику.

    Проверяются только пики без названия и без ele: у остальных
    положение не трогаем.  Критерий ошибки — пик далеко от ближайшей
    локальной вершины рельефа и заметно ниже неё.

    error — безымянный пик явно не на вершине;
    suspect — безымянный пик с умеренными признаками ошибки;
    skipped — у пика есть название или ele (не проверяем);
    ok — безымянный пик стоит на вершине.
    """
    flags = []
    drop = metrics.drop_at(thresholds["radius"])
    deep = metrics.drop_at(thresholds["deep_radius"])
    # Расстояние до ближайшего соседнего пика OSM, который выше данного.
    neighbour_nearest = float("nan")
    if neighbour is not None:
        neighbour_nearest = neighbour[2]

    if peak.category == "named":
        return "skipped", ["не проверяется (с названием)"], None

    if not math.isnan(deep) and deep >= thresholds["deep_drop"]:
        flags.append(
            f"ниже вершины на {deep:.0f} м в радиусе {thresholds['deep_radius']} м"
        )
    if metrics.in_pit:
        flags.append("локальный минимум DEM (дно долины или яма)")
    elif not metrics.on_summit:
        flags.append("не локальный максимум DEM в пределах 90 м")
    elif not metrics.on_summit_wide:
        flags.append("не локальный максимум DEM в пределах 150 м")

    distance = metrics.summit_distance
    summit_drop = metrics.summit_drop
    if not math.isnan(distance):
        flags.append(
            f"ближайшая вершина рельефа в {distance:.0f} м"
            f", выше на {summit_drop:.0f} м"
        )
    if not math.isnan(metrics.cross600):
        if metrics.cross600 > thresholds["ridge_cross"]:
            flags.append(
                "не на хребте: поперёк склона выше на "
                f"{metrics.cross600:.0f} м (600 м)"
            )
        else:
            flags.append(
                "на хребте: поперёк склона ниже на "
                f"{-metrics.cross600:.0f} м (600 м)"
            )
    if not math.isnan(distance) and distance > thresholds["summit_limit"]:
        flags.append(
            f"ближайший локальный максимум дальше {distance:.0f} м"
        )
    if not math.isnan(metrics.ridge_distance):
        flags.append(f"до ближайшего гребня {metrics.ridge_distance:.0f} м")
    if not math.isnan(metrics.divide_distance):
        flags.append(
            f"до ближайшего водораздела {metrics.divide_distance:.0f} м"
        )
    if not math.isnan(metrics.sharp_distance):
        flags.append(f"до острого гребня {metrics.sharp_distance:.0f} м")
    if not math.isnan(metrics.ring_higher):
        flags.append(f"выше точки {metrics.ring_higher:.0%} кольца в 200 м")

    if neighbour is not None:
        _count, neighbour_drop, nearest = neighbour
        if neighbour_drop >= 30:
            where = (
                f", ближайший выше в {nearest:.0f} м"
                if not math.isnan(nearest)
                else ""
            )
            flags.append(
                f"ниже соседних пиков OSM на {neighbour_drop:.0f} м{where}"
            )

    # Вердикт: пик считается ошибочным, если он не на вершине и не на
    # хребте.  Правило — набор признаков, каждый из которых требует
    # выполнения двух условий: одиночное условие слишком часто
    # срабатывает на настоящих вершинах (например, «нет острого гребня
    # в 100 м» — у настоящей круглой вершины его и не бывает).
    # Все признаки считаются по рельефу SRTM.

    def far(value, limit, less=False):
        """Значение отличается от порога в нужную сторону."""
        if math.isnan(value):
            return False
        return value < limit if less else value > limit

    # Правило — набор конъюнкций: ошибка отмечается, если сработала хотя бы
    # одна.  Каждая конъюнкция требует двух признаков сразу: одиночное
    # условие срабатывает и на настоящих вершинах.
    reasons = []
    # 1. Далеко от строгого водораздела и поперёк склона в 900 м рельеф
    #    заметно выше — пик на дне долины.  Для части растровых DSM
    #    дополнительно требуется, чтобы и вершина была не рядом.
    if (far(metrics.divide_strict_distance, thresholds["far_divide"])
            and far(metrics.cross900, thresholds["valley_cross900"])
            and (thresholds["valley_summit_min"] <= 0.0
                 or far(metrics.summit_distance,
                        thresholds["valley_summit_min"]))):
        reasons.append(
            "далеко от водораздела и поперёк склона выше на "
            f"{metrics.cross900:.0f} м (900 м)"
        )
    # 2. Нет острого гребня: перепад 25 м на 150 м и 15 м на 450 м.
    if (far(metrics.sharp_150_strict_distance,
            thresholds["no_steep_crest"])
            and far(metrics.sharp_450_distance, thresholds["no_crest_450"])):
        reasons.append("рядом нет острого гребня")
    # 3. Рельеф вокруг пологий, настоящей вершины рядом нет.
    if (far(metrics.sharp_150_strict_distance, thresholds["smooth_150"])
            and far(metrics.summit_distance, thresholds["summit_far"])):
        reasons.append(
            "пологое место без вершины, ближайшая вершина рельефа в "
            f"{metrics.summit_distance:.0f} м"
        )
    # 4. Много более высокого рельефа в 1200 м и провал в 300 м — пик
    #    в окружении других вершин, а не на гребне.
    if (far(metrics.ring_1200_higher, thresholds["valley_ring_wide"])
            and far(metrics.ring_300_lower, thresholds["pit_drop"])):
        reasons.append("вокруг много более высокого рельефа")
    # 5. Пик не на гребне и рельеф рядом намного ниже точки.
    if (far(metrics.ridge_strict_distance, thresholds["off_ridge"])
            and far(metrics.ring_200_lower, thresholds["ridge_drop"])):
        reasons.append("не на гребне и рельеф рядом ниже")
    # 6. Рельеф сильно ниже точки в 200 м, а ближайшая вершина далеко.
    if (far(metrics.ring_200_lower, thresholds["far_drop"])
            and far(metrics.near_max_distance, thresholds["near_max_d"])):
        reasons.append("рельеф рядом намного ниже, вершина далеко")
    # 7. Провал в 600 м больше половины местного рельефа и рядом ручей —
    #    пик в долине.
    if (far(metrics.ring_600_relative, thresholds["ring_600_relative"])
            and far(metrics.stream_distance, thresholds["stream_distance"],
                    less=True)):
        reasons.append("в долине, рядом ручей")
    # 8. Высота из OSM заметно выше рельефа и глубокий провал — пик
    #     сдвинут вниз от настоящей вершины.
    if (far(metrics.ele_below, thresholds["ele_below"])
            and far(metrics.ring_450_lower, thresholds["shift_drop"])):
        reasons.append(
            f"высота OSM выше рельефа на {metrics.ele_below:.0f} %"
        )
    # 9. Настоящей вершины рядом нет, но есть более высокий пик OSM
    #     далеко — пик стоит на пологом участке под ним.
    if (far(metrics.summit_distance, thresholds["summit_far_neighbour"])
            and far(neighbour_nearest, thresholds["neighbour_nearest"])):
        reasons.append(
            "вершины рядом нет, а более высокий пик OSM в "
            f"{neighbour_nearest:.0f} м"
        )
    # 10. Точка в глубокой долине: много кольца выше и большой провал.
    if (far(metrics.ring_700_higher, thresholds["valley_ring_soft"])
            and far(metrics.ring_450_lower, thresholds["valley_drop_high"])):
        reasons.append("в окружении много более высокого рельефа")
    # 11. Высота из OSM и рельеф расходятся больше чем на 10 % — пик
    #     поставлен не на ту вершину.
    if far(metrics.ele_diff, thresholds["ele_diff"]):
        reasons.append(
            f"высота OSM и рельеф расходятся на {metrics.ele_diff:.0f} %"
        )

    flags.extend(reasons)
    verdict = "error" if reasons else "ok"
    return verdict, flags, None

