"""Линия подписи вдоль сложного полигона — без GRASS.

Заменяет ``grass7:v.voronoi.skeleton`` в рецептах: подпись идёт вдоль формы
(«банан»), плавно и без петель.  Нужны только numpy и GDAL/OGR, которые
всегда есть в QGIS, — ставить GRASS не требуется.

Порядок:

1. у многочастного объекта берётся только самая большая часть; мелкие
   дыры заливаются, контур упрощается до трети ячейки растра;
2. строится маска полигона и поле расстояний до краёв;
3. концы линии — самые удалённые точки контура (у ледников — по
   расстоянию вдоль фигуры, чтобы не упираться в тонкий мыс);
4. между концами ищется кратчайший путь со штрафом за близость к краю:
   он идёт по середине, но остаётся кратчайшим, поэтому петель и крюков
   не бывает;
5. путь сглаживается, притягивается к геометрическому центру (только если
   это не прижимает линию к контуру) и остаётся внутри полигона.

Координаты — в системе исходной геометрии.
"""

import heapq
import math

import numpy as np
from osgeo import ogr

# Ячейка растра около толщины штриха подписи.
CELL_METRES = 50.0
MIN_CELLS = 18
MAX_CELLS = 128
# Доля наибольшего зазора, из которой строится ядро.
CORE_SHARE = 0.2
# Мелкая дыра: не больше стольких ячеек по площади или доли площади объекта.
HOLE_CELLS = 4.0
HOLE_SHARE = 0.02
# Шаг прореживания готовой линии, в ячейках растра.
FINAL_CELLS = 3.0
MAX_POINTS = 200
# Ледники: концы ищем по расстоянию вдоль фигуры, а сетка не грубее
# этого — иначе узкие ветки и дыры теряются и линия идёт не по главному
# потоку.
GEODESIC_CELLS = 64
GEODESIC_TARGET = 24
WIDTH_BONUS = 45
METRES_LONGITUDE = 111320.0
METRES_LATITUDE = 110574.0

_STEPS = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
          (-1, -1, 1.41421356), (-1, 1, 1.41421356),
          (1, -1, 1.41421356), (1, 1, 1.41421356))
_PENALTY = {}


# ------------------------------------------------------------------ публичное

def label_line_geometry(geometry, main_flow=False):
    """Линия подписи как геометрия OGR или None, если её построить нельзя."""
    ogr.UseExceptions()
    points = label_line(geometry, main_flow=main_flow)
    if len(points) < 2:
        return None
    line = ogr.Geometry(ogr.wkbLineString)
    for x, y in points:
        line.AddPoint_2D(float(x), float(y))
    return line


def label_line(geom, main_flow=False):
    """Точки линии подписи для полигона.

    У многочастного объекта линия строится только для самой большой
    части: подпись не должна переходить из одного контура в другой
    через пустое пространство.
    """
    ogr.UseExceptions()
    if geom is None or geom.IsEmpty():
        return []
    if geom.GetGeometryName() == "POLYGON":
        parts = [geom]
    else:
        parts = [geom.GetGeometryRef(index)
                 for index in range(geom.GetGeometryCount())]
    parts = [part for part in parts if part is not None and not part.IsEmpty()]
    if not parts:
        return []
    measured = []
    for part in parts:
        try:
            measured.append((part, part.GetArea()))
        except Exception:  # noqa: BLE001 - битая геометрия не должна ронять слой
            continue
    if not measured:
        return []
    part, _area = max(measured, key=lambda item: item[1])
    try:
        centre = part.Centroid()
        cells = grid_size(part, centre.GetY())
        if main_flow:
            cells = max(cells, GEODESIC_CELLS)
        return _axis_of_part(part, cells, main_flow)
    except Exception:  # noqa: BLE001 - битая геометрия не должна ронять слой
        return []


# ---------------------------------------------------------------- растр

def grid_size(geom, latitude):
    """Сколько ячеек по большей стороне нужно объекту.

    Ячейка около CELL_METRES, но у больших объектов сетка не раздувается
    сверх MAX_CELLS.
    """
    x0, x1, y0, y1 = geom.GetEnvelope()
    longitude = METRES_LONGITUDE * max(0.01, math.cos(math.radians(latitude)))
    across_x = (x1 - x0) * longitude
    across_y = (y1 - y0) * METRES_LATITUDE
    major = max(across_x, across_y)
    minor = min(across_x, across_y)
    cells = int(round(major / CELL_METRES))
    # Узкая сторона должна занимать хотя бы несколько ячеек, иначе путь
    # поперёк не строится и расчёт повторяется на сетке вдвое мельче.
    if minor > 1e-9:
        cells = max(cells, int(round(5.0 * major / minor)))
    return max(MIN_CELLS, min(MAX_CELLS, cells))


def fill_small_holes(part, min_area):
    """Убирает мелкие дыры: просеки и прогалины в лесу."""
    count = part.GetGeometryCount()
    if count <= 1:
        return part
    keep = []
    for index in range(1, count):
        ring = part.GetGeometryRef(index)
        if ring is None:
            continue
        probe = ogr.Geometry(ogr.wkbPolygon)
        probe.AddGeometry(ring)
        if probe.GetArea() >= min_area:
            keep.append(index)
    if len(keep) == count - 1:
        return part
    # AddGeometry сам копирует геометрию, отдельный Clone не нужен.
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(part.GetGeometryRef(0))
    for index in keep:
        polygon.AddGeometry(part.GetGeometryRef(index))
    return polygon


def _rasterize(rings, x0, y0, x1, y1, nx, ny):
    """Маска полигона в сетке nx×ny (пиксель в центре ячейки)."""
    cell_x = (x1 - x0) / nx
    cell_y = (y1 - y0) / ny
    rows = np.arange(ny)
    starts_all = []
    ends_all = []
    for ring in rings:
        px = (ring[:, 0] - x0) / cell_x
        py = (y1 - ring[:, 1]) / cell_y
        px = np.append(px, px[0])
        py = np.append(py, py[0])
        ax, ay = px[:-1][None, :], py[:-1][None, :]
        bx, by = px[1:][None, :], py[1:][None, :]
        yc = (rows + 0.5)[:, None]
        crosses = (ay <= yc) != (by <= yc)
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = ax + (yc - ay) * (bx - ax) / (by - ay)
        xs = np.where(crosses, xs, np.inf)
        xs.sort(axis=1)
        starts, ends = xs[:, 0::2], xs[:, 1::2]
        pairs = min(starts.shape[1], ends.shape[1])
        for k in range(pairs):
            start, end = starts[:, k], ends[:, k]
            valid = np.isfinite(start) & np.isfinite(end)
            if not valid.any():
                continue
            i0 = np.clip(np.ceil(start - 0.5), 0, nx - 1).astype("int64")
            i1 = np.clip(np.floor(end - 0.5), 0, nx - 1).astype("int64")
            valid &= i1 >= i0
            if not valid.any():
                continue
            base = rows[valid] * (nx + 1)
            starts_all.append(base + i0[valid])
            ends_all.append(base + i1[valid] + 1)
    if not starts_all:
        return np.zeros((ny, nx), dtype=bool)
    size = ny * (nx + 1)
    starts = np.concatenate(starts_all)
    ends = np.concatenate(ends_all)
    diff = (np.bincount(starts, minlength=size).reshape(ny, nx + 1)
            - np.bincount(ends, minlength=size).reshape(ny, nx + 1))
    return np.cumsum(diff[:, :nx], axis=1) % 2 == 1


def _distance_field(mask):
    """Точное расстояние L1 до границы (в ячейках)."""
    ny, nx = mask.shape
    rows = np.arange(ny, dtype="float64")[:, None]
    columns = np.arange(nx, dtype="float64")[None, :]
    source = np.where(mask, np.inf, 0.0)
    forward = np.minimum.accumulate(source - rows, axis=0) + rows
    backward = np.minimum.accumulate((source + rows)[::-1], axis=0)[::-1] - rows
    vertical = np.minimum(forward, backward)
    forward = np.minimum.accumulate(vertical - columns, axis=1) + columns
    backward = np.minimum.accumulate(
        (vertical + columns)[:, ::-1], axis=1
    )[:, ::-1] - columns
    return np.minimum(forward, backward)


def local_max_map(distance, radius):
    """Наибольший зазор в окне вокруг каждой ячейки.

    Направления применяются последовательно, но без np.pad: он на
    маленьких массивах дороже самих сдвигов.
    """
    result = distance
    for _ in range(radius):
        updated = result.copy()
        np.maximum(updated[1:, :], result[:-1, :], out=updated[1:, :])
        result = updated
        updated = result.copy()
        np.maximum(updated[:-1, :], result[1:, :], out=updated[:-1, :])
        result = updated
        updated = result.copy()
        np.maximum(updated[:, 1:], result[:, :-1], out=updated[:, 1:])
        result = updated
        updated = result.copy()
        np.maximum(updated[:, :-1], result[:, 1:], out=updated[:, :-1])
        result = updated
    return result


# ------------------------------------------------------------ поиск пути

def _penalty_tables(alpha):
    """Таблица цены шага по зазору: зазор — целое число ячеек."""
    cached = _PENALTY.get(alpha)
    if cached is not None:
        return cached
    scale = 100
    tables = [
        [int(round(length * (1.0 + alpha / ((c + 1.0) ** 2)) * scale))
         for c in range(256)]
        for _, _, length in _STEPS
    ]
    _PENALTY[alpha] = tables
    return tables


def centered_path(mask, distance, start, end, alpha=20.0):
    """Кратчайший путь со штрафом за близость к краю.

    Стоимость шага растёт у границы, поэтому путь идёт по середине, но
    остаётся кратчайшим: петель и крюков не бывает.  Горячий цикл работает
    со списками Python — обращение к элементу списка дешевле индексации
    массива.
    """
    height, width = mask.shape
    total = height * width
    flat_mask = mask.ravel().tolist()
    flat_distance = np.maximum(distance.ravel(), 0).astype(np.int64).tolist()
    tables = _penalty_tables(alpha)
    top = len(tables[0]) - 1
    best = [-1] * total
    previous = [-1] * total
    closed = bytearray(total)
    start_index = start[0] * width + start[1]
    end_index = end[0] * width + end[1]
    best[start_index] = 0
    heap = [(0, start_index)]
    while heap:
        cost, node = heapq.heappop(heap)
        if closed[node]:
            continue
        closed[node] = 1
        if node == end_index:
            break
        row = node // width
        column = node - row * width
        for direction, (d_row, d_column, _length) in enumerate(_STEPS):
            n_row = row + d_row
            n_column = column + d_column
            if n_row < 0 or n_row >= height or n_column < 0 or n_column >= width:
                continue
            n_index = n_row * width + n_column
            if not flat_mask[n_index]:
                continue
            clearance = flat_distance[n_index]
            if clearance > top:
                clearance = top
            new_cost = cost + tables[direction][clearance]
            if best[n_index] < 0 or new_cost < best[n_index]:
                best[n_index] = new_cost
                previous[n_index] = node
                heapq.heappush(heap, (new_cost, n_index))
    if best[end_index] < 0:
        return []
    path = []
    node = end_index
    while node >= 0:
        row = node // width
        path.append((row, node - row * width))
        node = previous[node]
    return path[::-1]


# ---------------------------------------------------------------- концы

def _local_width(distance, cell, radius=4):
    """Толщина объекта у ячейки: наибольший зазор рядом."""
    row, column = cell
    height, width = distance.shape
    top = max(0, row - radius)
    bottom = min(height, row + radius + 1)
    left = max(0, column - radius)
    right = min(width, column + radius + 1)
    return float(distance[top:bottom, left:right].max())


def _diameter_points(rings, distance=None, x0=0.0, y1=0.0,
                     cell_x=1.0, cell_y=1.0, nx=1, ny=1):
    """Две самые удалённые вершины контура из широких участков."""
    points = np.vstack(rings)
    if len(points) > 320:
        step = len(points) // 320 + 1
        points = points[::step]
    if distance is not None:
        rows = np.clip(np.rint((y1 - points[:, 1]) / cell_y - 0.5).astype(int),
                       0, ny - 1)
        columns = np.clip(
            np.rint((points[:, 0] - x0) / cell_x - 0.5).astype(int), 0, nx - 1)
        widths = local_max_map(distance, 2)[rows, columns]
        wide = widths >= 0.25 * widths.max()
        if wide.sum() >= 4:
            points = points[wide]
    # острый угол — шип или тонкий мыс: подпись оттуда начинать не нужно
    if len(points) > 8:
        previous = np.roll(points, 1, axis=0)
        following = np.roll(points, -1, axis=0)
        first = points - previous
        second = following - points
        norms = (np.linalg.norm(first, axis=1)
                 * np.linalg.norm(second, axis=1))
        cosine = np.divide(
            (first * second).sum(axis=1), norms,
            out=np.ones(len(points)), where=norms > 0)
        angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
        wide = angle > 35.0
        if wide.sum() > 8:
            points = points[wide]
    dx = points[:, 0][:, None] - points[:, 0][None, :]
    dy = points[:, 1][:, None] - points[:, 1][None, :]
    squared = dx * dx + dy * dy
    index = int(np.argmax(squared))
    i, j = divmod(index, len(points))
    return points[i], points[j]


def _coarse_mask(mask, target=GEODESIC_TARGET):
    """Маска в грубой сетке: тонкие части не теряются (объединение по блоку)."""
    stride = max(1, int(round(max(mask.shape) / target)))
    if stride == 1:
        return mask, 1
    height, width = mask.shape
    rows = height // stride * stride
    columns = width // stride * stride
    blocks = mask[:rows, :columns].reshape(rows // stride, stride,
                                           columns // stride, stride)
    return blocks.any(axis=(1, 3)), stride


def _geodesic_far(mask, start):
    """Самая «хорошая» ячейка по расстоянию вдоль фигуры.

    «Хорошая» — далёкая и при этом в широком месте: иначе конец упирается
    в тонкий мыс, а не в широкую лопасть.
    """
    height, width = mask.shape
    flat = mask.ravel().tolist()
    padded = np.zeros((height + 2, width + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    distance = _distance_field(padded)[1:-1, 1:-1]
    flat_distance = distance.ravel().tolist()
    width_map = local_max_map(distance, 2).ravel().tolist()
    best = [-1] * (height * width)
    start_index = start[0] * width + start[1]
    best[start_index] = 0
    heap = [(0, start_index)]
    steps = ((-1, 0, 100), (1, 0, 100), (0, -1, 100), (0, 1, 100),
             (-1, -1, 141), (-1, 1, 141), (1, -1, 141), (1, 1, 141))
    while heap:
        cost, node = heapq.heappop(heap)
        if cost > best[node]:
            continue
        row = node // width
        column = node - row * width
        for d_row, d_column, step in steps:
            n_row = row + d_row
            n_column = column + d_column
            if 0 <= n_row < height and 0 <= n_column < width:
                n_index = n_row * width + n_column
                if not flat[n_index]:
                    continue
                new_cost = cost + step
                if best[n_index] < 0 or new_cost < best[n_index]:
                    best[n_index] = new_cost
                    heapq.heappush(heap, (new_cost, n_index))
    score = -1
    choice = start_index
    for index, value in enumerate(best):
        if value < 0:
            continue
        candidate = value + WIDTH_BONUS * width_map[index]
        if candidate > score:
            score = candidate
            choice = index
    return divmod(choice, width)


def _snap_to_mask(mask, row, column, radius):
    """Ближайшая ячейка маски: центр грубого блока может быть вне фигуры."""
    height, width = mask.shape
    best = None
    for d_row in range(-radius, radius + 1):
        r = row + d_row
        if r < 0 or r >= height:
            continue
        for d_column in range(-radius, radius + 1):
            c = column + d_column
            if c < 0 or c >= width or not mask[r, c]:
                continue
            distance = d_row * d_row + d_column * d_column
            if best is None or distance < best[0]:
                best = (distance, (r, c))
    return None if best is None else best[1]


def _geodesic_corners(mask):
    """Концы основной формы по расстоянию вдоль неё, а не по прямой."""
    coarse, stride = _coarse_mask(mask)
    first = np.argwhere(coarse)[0]
    far = _geodesic_far(coarse, (int(first[0]), int(first[1])))
    second = _geodesic_far(coarse, far)

    def to_full(cell):
        return _snap_to_mask(mask, cell[0] * stride + stride // 2,
                             cell[1] * stride + stride // 2, stride + 2)

    return to_full(far), to_full(second)


def _segment_outside(mask, first, second, samples=32):
    """Доля прямой между концами, выходящая за маску."""
    outside = 0
    for k in range(1, samples):
        t = k / samples
        row = int(round(first[0] + (second[0] - first[0]) * t))
        column = int(round(first[1] + (second[1] - first[1]) * t))
        if not mask[row, column]:
            outside += 1
    return outside / (samples - 1)


def _extend_to_boundary(mask, cell, other):
    """От ячейки гребня — наружу, до границы маски."""
    row, column = cell
    d_row = row - other[0]
    d_column = column - other[1]
    length = max(abs(d_row), abs(d_column), 1)
    step_row = d_row / length
    step_column = d_column / length
    height, width = mask.shape
    last = cell
    for step in range(1, max(height, width) + 1):
        r = int(round(row + step_row * step))
        c = int(round(column + step_column * step))
        if r < 0 or r >= height or c < 0 or c >= width:
            break
        if not mask[r, c]:
            break
        last = (r, c)
    return last


def _ridge_corners(mask, distance, share=0.6):
    """Концы по гребню поля расстояний: пара самых далёких ячеек.

    Нужны, когда диаметр по прямой цепляется за вогнутую кромку: у озера
    с тонким рукавом оба конца оказываются на одной стороне, и подпись
    идёт по краю, а не по середине.
    """
    ridge = distance >= share * float(distance.max())
    rows, columns = np.nonzero(ridge)
    if len(rows) < 4:
        return None
    if len(rows) > 300:
        step = len(rows) // 300 + 1
        rows, columns = rows[::step], columns[::step]
    d_row = rows[:, None] - rows[None, :]
    d_column = columns[:, None] - columns[None, :]
    index = int(np.argmax(d_row * d_row + d_column * d_column))
    i, j = divmod(index, len(rows))
    first = _extend_to_boundary(mask, (int(rows[i]), int(columns[i])),
                                (int(rows[j]), int(columns[j])))
    second = _extend_to_boundary(mask, (int(rows[j]), int(columns[j])),
                                 (int(rows[i]), int(columns[i])))
    return first, second


def _nearest_cell(target, x0, y1, cell_x, cell_y, nx, ny, mask):
    """Ближайшая ячейка маски к точке."""
    column = int(round((target[0] - x0) / cell_x - 0.5))
    row = int(round((y1 - target[1]) / cell_y - 0.5))
    column = min(max(column, 0), nx - 1)
    row = min(max(row, 0), ny - 1)
    if mask[row, column]:
        return (row, column)
    best = None
    for d_row in range(-3, 4):
        for d_column in range(-3, 4):
            n_row, n_column = row + d_row, column + d_column
            if not (0 <= n_row < ny and 0 <= n_column < nx):
                continue
            if not mask[n_row, n_column]:
                continue
            distance = d_row * d_row + d_column * d_column
            if best is None or distance < best[0]:
                best = (distance, (n_row, n_column))
    return None if best is None else best[1]


# -------------------------------------------------------------- сглаживание

def _chaikin(points, iterations=2):
    array = np.asarray(points, dtype="float64")
    if len(array) < 3:
        return array
    for _ in range(iterations):
        left = 0.75 * array[:-1] + 0.25 * array[1:]
        right = 0.25 * array[:-1] + 0.75 * array[1:]
        merged = np.empty((left.shape[0] * 2, 2), dtype="float64")
        merged[0::2] = left
        merged[1::2] = right
        array = np.vstack([array[0], merged, array[-1]])
    return array


def _gaussian_smooth(points, sigma=1.5, iterations=2):
    """Гауссово сглаживание линии без np.pad — он дорог для (N, 2)."""
    array = np.asarray(points, dtype="float64")
    if len(array) < 5:
        return array
    radius = max(1, int(round(3 * sigma)))
    offsets = np.arange(-radius, radius + 1, dtype="float64")
    weights = np.exp(-0.5 * (offsets / sigma) ** 2)
    weights /= weights.sum()
    count = len(array)
    for _ in range(iterations):
        padded = np.empty((count + 2 * radius, 2), dtype="float64")
        padded[:radius] = array[0]
        padded[radius:radius + count] = array
        padded[radius + count:] = array[-1]
        smoothed = np.zeros_like(array)
        for offset, weight in zip(offsets.astype(int), weights):
            smoothed += weight * padded[radius + offset:radius + offset + count]
        smoothed[0] = array[0]
        smoothed[-1] = array[-1]
        array = smoothed
    return array


def _resample(points, step):
    array = np.asarray(points, dtype="float64")
    if len(array) < 3 or step <= 0:
        return array
    result = [array[0]]
    last_x, last_y = float(array[0][0]), float(array[0][1])
    for point in array[1:]:
        x, y = float(point[0]), float(point[1])
        if math.hypot(x - last_x, y - last_y) >= step:
            result.append(point)
            last_x, last_y = x, y
    result.append(array[-1])
    return np.asarray(result)


def densify(points, step):
    """Вставляет промежуточные точки: проверять надо и отрезки."""
    array = np.asarray(points, dtype="float64")
    if len(array) < 2 or step <= 0:
        return array
    result = [array[0]]
    for index in range(1, len(array)):
        start, end = array[index - 1], array[index]
        length = math.hypot(float(end[0]) - float(start[0]),
                            float(end[1]) - float(start[1]))
        pieces = max(1, int(np.ceil(length / step)))
        for piece in range(1, pieces + 1):
            result.append(start + (end - start) * (piece / pieces))
    return np.asarray(result)


def keep_inside_mask(line, path, mask, x0, y1, cell_x, cell_y, nx, ny):
    """Точки вне маски возвращает на исходный путь (быстро, без GEOS)."""
    points = np.asarray(line, dtype="float64")
    if len(points) < 2:
        return line
    rows = np.rint((y1 - points[:, 1]) / cell_y - 0.5).astype(int)
    columns = np.rint((points[:, 0] - x0) / cell_x - 0.5).astype(int)
    rows = np.clip(rows, 0, ny - 1)
    columns = np.clip(columns, 0, nx - 1)
    outside = ~mask[rows, columns]
    if not outside.any():
        return points
    result = points.copy()
    for index in np.nonzero(outside)[0]:
        distances = ((path[:, 0] - points[index][0]) ** 2
                     + (path[:, 1] - points[index][1]) ** 2)
        result[index] = path[int(np.argmin(distances))]
    return result


def bend_to_centre(geom, line):
    """Плавно изгибает линию к геометрическому центру фигуры.

    Изгиб принимается, только если линия осталась внутри и не стала ближе
    к контуру: у узких объектов центр масс бывает ближе к берегу, чем сама
    линия.
    """
    points = np.asarray(line, dtype="float64")
    if len(points) < 5:
        return line
    centre = geom.Centroid()
    middle = points[len(points) // 2]
    offset = np.array([centre.GetX() - middle[0], centre.GetY() - middle[1]])
    if float(np.hypot(*offset)) == 0:
        return line
    weight = np.sin(np.pi * np.linspace(0.0, 1.0, len(points))) ** 2
    before, mean = _clearance_stats(geom, points)
    boundary = geom.GetBoundary()
    probe = ogr.Geometry(ogr.wkbPoint)
    probe.SetPoint_2D(0, centre.GetX(), centre.GetY())
    if boundary.Distance(probe) < 1.2 * mean:
        return points
    for share in (1.0, 0.6, 0.3, 0.15):
        candidate = points + weight[:, None] * offset * share
        if not _all_inside(geom, candidate):
            continue
        if _min_clearance(geom, candidate) >= before:
            return candidate
    return points


def _all_inside(geom, points, step=3):
    probe = ogr.Geometry(ogr.wkbPoint)
    for x, y in points[::step]:
        probe.SetPoint_2D(0, x, y)
        if not geom.Contains(probe):
            return False
    return True


def _clearance_stats(geom, points, step=3):
    """Минимальный и средний зазор до контура за один проход."""
    boundary = geom.GetBoundary()
    if boundary is None or boundary.IsEmpty():
        return 0.0, 0.0
    probe = ogr.Geometry(ogr.wkbPoint)
    low = None
    total = 0.0
    count = 0
    for x, y in points[::step]:
        probe.SetPoint_2D(0, x, y)
        value = boundary.Distance(probe)
        total += value
        if low is None or value < low:
            low = value
        count += 1
    return (0.0 if low is None else low), total / max(count, 1)


def _mean_clearance(geom, points, step=2):
    boundary = geom.GetBoundary()
    if boundary is None or boundary.IsEmpty():
        return 0.0
    probe = ogr.Geometry(ogr.wkbPoint)
    total = 0.0
    count = 0
    for x, y in points[::step]:
        probe.SetPoint_2D(0, x, y)
        total += boundary.Distance(probe)
        count += 1
    return total / max(count, 1)


def _min_clearance(geom, points, step=2):
    boundary = geom.GetBoundary()
    if boundary is None or boundary.IsEmpty():
        return 0.0
    probe = ogr.Geometry(ogr.wkbPoint)
    low = None
    for x, y in points[::step]:
        probe.SetPoint_2D(0, x, y)
        value = boundary.Distance(probe)
        if low is None or value < low:
            low = value
    return 0.0 if low is None else low


# ------------------------------------------------------------- осевая линия

def _single_polygon(geometry, clone=True):
    """Один полигон: после Simplify геометрия может стать мультиполигоном.

    ``clone=False`` — когда родительская геометрия остаётся живой: тогда
    копия не нужна, а копирование большого контура стоит дорого.
    """
    if geometry is None or geometry.IsEmpty():
        return None
    if geometry.GetGeometryName() == "POLYGON":
        return geometry.Clone() if clone else geometry
    best = None
    best_area = -1.0
    for index in range(geometry.GetGeometryCount()):
        part = geometry.GetGeometryRef(index)
        if part is None:
            continue
        area = part.GetArea()
        if area > best_area:
            best, best_area = part, area
    if best is not None and clone:
        best = best.Clone()
    return best


def _axis_of_part(part, cells, main_flow=False):
    """Осевая линия части; если сетка слишком груба, пробуем мельче."""
    for attempt in range(4):
        line = _axis_attempt(part, min(cells * (2 ** attempt), 1024), main_flow)
        if len(line) >= 2:
            return line
    return []


def _axis_attempt(part, cells, main_flow=False):
    # Входная геометрия принадлежит вызывающему и живёт весь вызов.
    part = _single_polygon(part, clone=False)
    if part is None:
        return []
    original = part
    # GetEnvelope отдаёт (minX, maxX, minY, maxY)
    min_x, max_x, min_y, max_y = part.GetEnvelope()
    cell_degrees = max(max_x - min_x, max_y - min_y, 1e-12) / cells
    part = fill_small_holes(
        part, max((HOLE_CELLS * cell_degrees) ** 2,
                  HOLE_SHARE * part.GetArea()))
    simplified = part.Simplify(cell_degrees * 0.3)
    single = _single_polygon(simplified, clone=False)
    if single is not None and not single.IsEmpty():
        part = single
    rings = []
    for index in range(part.GetGeometryCount()):
        ring = part.GetGeometryRef(index)
        if ring is None:
            continue
        rings.append(np.asarray(ring.GetPoints(), dtype="float64")[:, :2])
    points = np.vstack(rings)
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    width = max(x1 - x0, 1e-12)
    height = max(y1 - y0, 1e-12)
    nx = ny = cells
    if width >= height:
        cell = width / nx
        ny = max(6, int(round(height / cell)))
    else:
        cell = height / ny
        nx = max(6, int(round(width / cell)))
    mask = _rasterize(rings, x0, y0, x1, y1, nx, ny)
    if not mask.any():
        return []
    # Рамка из False: иначе у ячеек на краю массива зазор завышен —
    # край массива не считается границей фигуры.
    padded = np.zeros((ny + 2, nx + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    distance = _distance_field(padded)[1:-1, 1:-1]
    cell_x = (x1 - x0) / nx
    cell_y = (y1 - y0) / ny
    # концы линии — самые удалённые точки контура: подпись идёт от края до края
    corners = _diameter_points(rings, distance, x0, y1, cell_x, cell_y, nx, ny)
    first = _nearest_cell(corners[0], x0, y1, cell_x, cell_y, nx, ny, mask)
    second = _nearest_cell(corners[1], x0, y1, cell_x, cell_y, nx, ny, mask)
    # У крупных изогнутых объектов (ледники, заливы) диаметр по прямой
    # упирается в тонкие мысы: концы берём по расстоянию вдоль фигуры.
    precomputed_path = None
    if main_flow and cells >= GEODESIC_CELLS:
        geodesic = _geodesic_corners(mask)
        if geodesic[0] is not None and geodesic[1] is not None:
            first, second = geodesic
            # У трёхветвистых фигур (Glacier de Cheilon) диаметр цепляется
            # за два боковых мыса, а широкий главный язык остаётся без
            # подписи. Пара «север-юг» выигрывает, только если её путь
            # проходит через заметно более широкую часть фигуры и не
            # зажат в узкий перешеек.
            rows, columns = np.nonzero(mask)
            north = (int(rows.min()), int(columns[int(np.argmin(rows))]))
            south = (int(rows.max()), int(columns[int(np.argmax(rows))]))
            if north != south:
                def _path_core_width(start, end):
                    middle = centered_path(mask, distance, start, end)
                    if len(middle) < 5:
                        return None
                    values = [distance[r, c] for r, c in middle]
                    cut = max(1, int(len(values) * 0.15))
                    core = values[cut:len(values) - cut]
                    if not core:
                        return None
                    return float(np.mean(core)), float(min(core)), middle

                ns = _path_core_width(north, south)
                gs = _path_core_width(first, second)
                if (ns is not None and gs is not None
                        and ns[1] > gs[1]
                        and ns[0] >= 1.25 * gs[0]):
                    first, second = north, south
                    # Путь уже посчитан для сравнения ширин — не строим
                    # его заново ниже.
                    precomputed_path = ns[2]
                elif gs is not None:
                    precomputed_path = gs[2]
    elif (first is not None and second is not None
          and _segment_outside(mask, first, second) > 0.15):
        ridge = _ridge_corners(mask, distance)
        if ridge is not None:
            first, second = ridge
    if first is None or second is None or first == second:
        return []
    # начинаем с более широкого конца: подпись идёт из широкой части
    if _local_width(distance, second) > _local_width(distance, first):
        first, second = second, first
        # Прежний код строил путь уже от нового начала; разворот готового
        # пути даёт другой выбор при равной стоимости. Пересчитываем.
        precomputed_path = None
    path = precomputed_path
    if path is None:
        path = centered_path(mask, distance, first, second)
    if len(path) < 3:
        return []
    line = np.array([(x0 + (c + 0.5) * cell_x, y1 - (r + 0.5) * cell_y)
                     for r, c in path])
    step = FINAL_CELLS * 0.5 * (cell_x + cell_y)
    path_line = line.copy()
    line = _resample(_chaikin(line), step)
    line = _gaussian_smooth(line, sigma=1.5, iterations=2)
    line = keep_inside_mask(line, path_line, mask, x0, y1, cell_x, cell_y, nx, ny)
    if len(line) > 4:
        line[0] = line[0] + 0.5 * (line[1] - line[0])
        line[-1] = line[-1] + 0.5 * (line[-2] - line[-1])
    line = _resample(line, FINAL_CELLS * 0.5 * (cell_x + cell_y))
    line = bend_to_centre(original, line)
    line = densify(line, FINAL_CELLS * 0.5 * (cell_x + cell_y))
    line = keep_inside_mask(line, path_line, mask, x0, y1, cell_x, cell_y, nx, ny)
    if len(line) > 2 and line[0][0] > line[-1][0]:
        line = line[::-1]
    if len(line) > MAX_POINTS:
        line = line[np.linspace(0, len(line) - 1, MAX_POINTS).astype(int)]
    return [tuple(point) for point in line]


def _length(points):
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    return total
