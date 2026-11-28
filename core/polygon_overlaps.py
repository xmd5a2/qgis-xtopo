"""Вырезание меньших полигонов из больших.

OsmAnd при равном ``order`` рисует меньший полигон выше. В QGIS порядок
задаётся слоями, поэтому вложенный луг или посёлок оказывается под полем.
Здесь для заданных пар меньший полигон вычитается из большего: дырка
показывает нижний слой независимо от порядка слоёв.

Обрабатываются только производные слои проекта; исходные данные OSM и
подписи не меняются.
"""
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import i18n
from .cpu import cpu_workers

# Пары (слой, из которого вычитаем, вычитаемый слой). Обычно первый слой
# закрывает второй; у прозрачных штриховок (скалы, осыпи) первый слой —
# фон, который просвечивает сквозь штриховку. Если второй полигон меньше,
# из первого вычитается их пересечение.
# Острова и островки (place=island/islet) лежат в одном слое island.
DEFAULT_PAIRS = (
    ("farmland", "grass"),
    ("farmland", "landuse_residential"),
    ("farmland", "industrial"),
    ("farmland", "vineyard"),
    ("farmland", "orchard"),
    ("farmland", "farmyard"),
    ("farmland", "allotments"),
    ("farmland", "greenhouse_horticulture"),
    ("farmland", "plant_nursery"),
    ("grass", "landuse_residential"),
    ("orchard", "landuse_residential"),
    ("grass", "industrial"),
    ("grass", "vineyard"),
    ("grass", "sand"),
    ("grass", "bare_rock"),
    ("grass", "scree"),
    ("grass", "beach"),
    ("grass", "shingle"),
    ("water", "island"),
    ("water", "wood"),
    ("water", "scrub"),
    ("water", "sand"),
    ("water", "bare_rock"),
    ("water", "beach"),
    ("water", "shingle"),
    ("wetland", "sand"),
    ("wetland", "shingle"),
    ("wetland", "scrub"),
    ("wetland", "water_intermittent"),
    ("glacier", "scree"),
    ("glacier", "bare_rock"),
    ("wood", "landuse_residential"),
    ("wood", "bare_rock"),
    ("wood", "shingle"),
    ("wood", "industrial"),
    ("wood", "plant_nursery"),
    ("wood", "cemetery"),
    ("cemetery", "wood"),
    ("scrub", "bare_rock"),
    ("scrub", "plant_nursery"),
    ("scree", "bare_rock"),
    ("farmyard", "greenhouse_horticulture"),
    ("landuse_residential", "sand"),
    ("allotments", "landuse_residential"),
)

# Имя слоя в шаблоне не всегда совпадает с именем файла запроса.
LAYER_FILES = {
    "water": "water_dissolved",
    "water_intermittent": "water_intermittent_dissolved",
}

# Минимальная площадь пересечения: мелкие щели по границам не вырезаем,
# иначе на плотной застройке появляются тысячи невидимых дырок.
DEFAULT_MIN_AREA_M2 = 100.0
# Островки по определению маленькие: для них порог ещё ниже, иначе они
# останутся скрытыми под водой.
PAIR_MIN_AREA_M2 = {
    ("water", "island"): 10.0,
}
METRES_PER_DEGREE = 111320.0

# С какого числа кандидатов площади пересечений считаются параллельно.
# На архипелаге один полигон моря пересекает больше тысячи островов, и
# последовательные Intersection занимают секунды.
PARALLEL_INTERSECTIONS_MIN = 64


def _prepared(geometry):
    """Подготовленная геометрия GEOS или None, если она недоступна."""
    try:
        return geometry.CreatePreparedGeometry()
    except (AttributeError, RuntimeError):
        return None


def _prepared_intersects(prepared, geometry):
    """Проверка пересечения без падения на некорректной геометрии."""
    try:
        return prepared.Intersects(geometry)
    except Exception:
        return True


def _intersection_areas(geometry, candidates, cancel=None, workers=None):
    """Площади пересечения кандидатов с геометрией, параллельно.

    Возвращает список в порядке кандидатов.  ``geometry`` не меняется,
    поэтому результат от порядка не зависит.  Площадь нужна только для
    отсева заведомо мелких пересечений: точная разность всё равно
    пересчитывается последовательно, иначе изменился бы результат при
    перекрывающихся кандидатах.

    ``workers`` задаёт потолок потоков: когда несколько слоёв режутся
    одновременно, внутренняя параллельность делится между ними.
    """
    if workers is None:
        workers = min(cpu_workers(limit=8), len(candidates))
    else:
        workers = min(max(1, workers), len(candidates))
    if workers <= 1:
        return None
    size = (len(candidates) + workers - 1) // workers
    chunks = [
        candidates[start:start + size]
        for start in range(0, len(candidates), size)
    ]

    def measure(chunk):
        upper = geometry.Clone()
        prepared = _prepared(upper)
        areas = []
        for position, candidate in enumerate(chunk):
            if (cancel is not None and position % 64 == 0
                    and cancel.is_set()):
                raise InterruptedError("Preparation cancelled")
            if prepared is not None and not _prepared_intersects(
                prepared, candidate
            ):
                areas.append(0.0)
                continue
            intersection = _intersection(upper, candidate)
            areas.append(
                0.0 if intersection is None or intersection.IsEmpty()
                else _area_m2(intersection)
            )
        return areas

    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        parts = list(pool.map(measure, chunks))
    return [area for part in parts for area in part]


def resolve_polygon_overlaps(config, progress=None, cancel=None):
    """Вырезает меньшие полигоны из больших для пар по умолчанию.

    Слои, между которыми нет зависимости, режутся одновременно: они
    читают разные файлы, а результат зависит только от порядка
    «верхний — нижний», который задают рёбра графа.
    """
    notify = progress or (lambda message: None)
    pairs = _configured_pairs(config)
    min_area = float(
        getattr(config, "polygon_overlap_min_area", DEFAULT_MIN_AREA_M2)
    )
    grouped = {}
    for upper, lower in pairs:
        grouped.setdefault(upper, []).append(lower)
    uppers = list(grouped)
    edges = _layer_dependencies(grouped)

    def process(upper, workers):
        """Режет один верхний слой; возвращает результаты и предупреждение."""
        upper_path = _layer_path(config, upper)
        if not upper_path.exists():
            return upper, [], None
        available = [
            (
                lower,
                _layer_path(config, lower),
                PAIR_MIN_AREA_M2.get((upper, lower), min_area),
            )
            for lower in grouped[upper]
            if _layer_path(config, lower).exists()
        ]
        if not available:
            return upper, [], None
        try:
            changed = _cut_layer(
                upper_path, available, cancel,
                intersection_workers=workers,
            )
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 - сборка не должна падать
            return upper, [], i18n.trf(
                "WARNING: пересечения {}: {}; слой не изменён",
                upper, exc,
            )
        return upper, [
            (upper, lower, changed.get(lower, 0))
            for lower, _, _ in available
        ], None

    # Слои, между которыми нет зависимости, можно резать одновременно:
    # они читают разные файлы, а результат зависит только от порядка
    # «верхний — нижний», который задают рёбра.  Внутри уровня потоки
    # делятся между слоями, чтобы не перегружать процессор.
    done = set()
    pending = list(uppers)
    by_upper = {}
    while pending:
        ready = [name for name in pending if edges[name].issubset(done)]
        if not ready:
            # Цикл в пользовательских парах: сохраняем прежний порядок.
            ready = [pending[0]]
        level_workers = min(len(ready), cpu_workers(limit=4))
        workers = (
            max(1, cpu_workers(limit=8) // level_workers)
            if level_workers > 1 else None
        )
        if level_workers > 1:
            with ThreadPoolExecutor(max_workers=level_workers) as pool:
                futures = [
                    pool.submit(process, name, workers) for name in ready
                ]
                for future in as_completed(futures):
                    upper, items, warning = future.result()
                    by_upper[upper] = items
                    if warning:
                        notify(warning)
        else:
            upper, items, warning = process(ready[0], None)
            by_upper[upper] = items
            if warning:
                notify(warning)
        done.update(ready)
        pending = [name for name in pending if name not in done]

    results = []
    for upper in uppers:
        results.extend(by_upper.get(upper, ()))
    _report_summary(results, notify)
    return results


def _layer_dependencies(grouped):
    """Какие слои должны быть вырезаны до данного.

    Пара «верхний — нижний» задаёт порядок только тогда, когда нижний
    слой сам переписывается на этом шаге.  Если он идёт раньше, верхний
    должен увидеть уже изменённый файл; если позже — наоборот, нижний
    обязан дождаться верхнего.  Слои без таких рёбер независимы.
    """
    positions = {name: index for index, name in enumerate(grouped)}
    edges = {name: set() for name in grouped}
    for upper, lowers in grouped.items():
        for lower in lowers:
            index = positions.get(lower)
            if index is None or lower == upper:
                continue
            if index < positions[upper]:
                edges[upper].add(lower)
            else:
                edges[lower].add(upper)
    return edges


def _report_summary(results, notify):
    """Пишет одну итоговую строку вместо строки на каждую пару слоёв."""
    if not results:
        return
    changed = sum(count for _, _, count in results)
    if not changed:
        notify(i18n.tr("Вырезание пересечений: изменений нет"))
        return
    details = ", ".join(
        f"{upper}/{lower} — {count}"
        for upper, lower, count in results
        if count
    )
    notify(
        i18n.trf(
            "Вырезание пересечений: изменено {} объектов ({})",
            changed, details,
        )
    )


def _layer_path(config, name):
    return config.vector_dir / f"{LAYER_FILES.get(name, name)}.sqlite"


def _configured_pairs(config):
    value = getattr(config, "polygon_overlap_pairs", None)
    if not value:
        return DEFAULT_PAIRS
    pairs = []
    for item in value:
        if isinstance(item, str) and "/" in item:
            pairs.append(tuple(part.strip() for part in item.split("/", 1)))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((item[0], item[1]))
    return tuple(pairs) or DEFAULT_PAIRS


def _cut_layer(upper_path, lowers, cancel, intersection_workers=None):
    """Переписывает верхний слой, вычитая меньшие полигоны нижних слоёв."""
    from osgeo import ogr

    ogr.UseExceptions()
    upper_dataset = ogr.Open(str(upper_path), 0)
    if upper_dataset is None:
        raise RuntimeError("не удалось открыть слой")
    upper_layer = upper_dataset.GetLayer(0)
    lower_layers = []
    try:
        for lower_name, lower_path, min_area in lowers:
            dataset = ogr.Open(str(lower_path), 0)
            if dataset is None:
                raise RuntimeError(f"не удалось открыть {lower_name}")
            lower_layers.append((lower_name, min_area, dataset, dataset.GetLayer(0)))
    except Exception:
        lower_layers.clear()
        upper_dataset = None
        raise
    source_definition = upper_layer.GetLayerDefn()
    output_path = upper_path.with_name(f".{upper_path.stem}.holes.sqlite")
    output_path.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("SQLite")
    output_dataset = driver.CreateDataSource(str(output_path))
    # Разность может превратить полигон в мультиполигон, поэтому тип
    # заранее не фиксируем.
    output_layer = output_dataset.CreateLayer(
        upper_path.stem, upper_layer.GetSpatialRef(), ogr.wkbUnknown,
    )
    for index in range(source_definition.GetFieldCount()):
        output_layer.CreateField(source_definition.GetFieldDefn(index))

    changed = {lower_name: 0 for lower_name, _, _, _ in lower_layers}
    written = 0
    output_layer.StartTransaction()
    try:
        for feature in upper_layer:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            geometry = geometry.Clone()
            polygonal = _polygonal_parts(geometry)
            if polygonal is None:
                # Линии и прочие не-полигоны в площадных слоях не трогаем.
                _write_feature(
                    output_layer, feature, geometry,
                )
                written += 1
                continue
            geometry = polygonal
            if not geometry.IsValid():
                geometry = geometry.MakeValid()
                geometry = _polygonal_parts(geometry) or geometry
                if geometry is None or geometry.IsEmpty():
                    continue
            original_area = geometry.Area()
            # Подготовленная копия исходной геометрии — надмножество
            # текущей, поэтому её можно использовать для отсева на всех
            # проходах: лишние кандидаты отсеются точной разностью.
            base_geometry = geometry
            prepared = _prepared(base_geometry)
            for lower_name, lower_min_area, lower_layer in (
                (name, area, layer) for name, area, _, layer in lower_layers
            ):
                lower_layer.SetSpatialFilter(geometry)
                candidates = []
                for candidate in lower_layer:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("Preparation cancelled")
                    lower_geometry = candidate.GetGeometryRef()
                    if lower_geometry is None or lower_geometry.IsEmpty():
                        continue
                    lower_geometry = _polygonal_parts(lower_geometry)
                    if lower_geometry is None:
                        continue
                    if lower_geometry.Area() >= original_area:
                        continue
                    if prepared is not None and not _prepared_intersects(
                        prepared, lower_geometry
                    ):
                        continue
                    candidates.append(lower_geometry.Clone())
                lower_layer.SetSpatialFilter(None)
                # Геометрия только уменьшается, поэтому площадь
                # пересечения с ней не больше исходной: мелкие можно
                # отсеять, не трогая текущую геометрию.
                areas = None
                if len(candidates) >= PARALLEL_INTERSECTIONS_MIN:
                    areas = _intersection_areas(
                        geometry, candidates, cancel,
                        workers=intersection_workers,
                    )
                for position, lower_geometry in enumerate(candidates):
                    if areas is not None and areas[position] < lower_min_area:
                        continue
                    intersection = _intersection(geometry, lower_geometry)
                    if intersection is None or intersection.IsEmpty():
                        continue
                    if _area_m2(intersection) < lower_min_area:
                        continue
                    cut = _difference(geometry, lower_geometry)
                    if cut is None or cut.IsEmpty():
                        continue
                    cut = _polygonal_parts(cut)
                    if cut is None or cut.IsEmpty():
                        continue
                    geometry = cut
                    changed[lower_name] += 1
            geometry = _polygonal_parts(geometry) or geometry
            _write_feature(
                output_layer, feature, geometry,
            )
            written += 1
        output_layer.CommitTransaction()
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        for _, _, _, lower_layer in lower_layers:
            lower_layer.SetSpatialFilter(None)
        lower_layers.clear()
        output_dataset = None
        upper_dataset = None
    if not any(changed.values()) or written == 0:
        output_path.unlink(missing_ok=True)
    else:
        output_path.replace(upper_path)
    return changed


def _intersection(first, second):
    try:
        return first.Intersection(second)
    except Exception:
        fixed = second.MakeValid()
        if fixed is None or fixed.IsEmpty():
            return None
        try:
            return first.Intersection(fixed)
        except Exception:
            return None


def _difference(first, second):
    try:
        return first.Difference(second)
    except Exception:
        fixed = second.MakeValid()
        if fixed is None or fixed.IsEmpty():
            return None
        try:
            return first.Difference(fixed)
        except Exception:
            return None


def _polygonal_parts(geometry):
    """Возвращает полигональную часть геометрии или None."""
    from osgeo import ogr

    name = geometry.GetGeometryName()
    if name in ("POLYGON", "MULTIPOLYGON"):
        return geometry
    if name != "GEOMETRYCOLLECTION":
        return None
    result = ogr.Geometry(ogr.wkbMultiPolygon)
    _collect_polygons(geometry, result)
    if result.IsEmpty():
        return None
    return result


def _collect_polygons(geometry, result):
    name = geometry.GetGeometryName()
    if name == "POLYGON":
        result.AddGeometry(geometry)
    elif name in ("MULTIPOLYGON", "GEOMETRYCOLLECTION"):
        for index in range(geometry.GetGeometryCount()):
            _collect_polygons(geometry.GetGeometryRef(index), result)


def _write_feature(output_layer, feature, geometry):
    from osgeo import ogr

    output_feature = ogr.Feature(output_layer.GetLayerDefn())
    # Поля копируются одним вызовом C++: прежний цикл GetField/SetField
    # на каждом объекте стоил около сотни миллисекунд на тысячи объектов.
    output_feature.SetFrom(feature)
    output_feature.SetGeometry(geometry)
    if output_layer.CreateFeature(output_feature) != ogr.OGRERR_NONE:
        raise RuntimeError(f"не удалось записать объект {feature.GetFID()}")


def _area_m2(geometry):
    """Площадь геометрии в квадратных метрах в EPSG:4326."""
    surface = _polygonal_parts(geometry)
    if surface is None:
        return 0.0
    centroid = surface.Centroid()
    if centroid is None or centroid.IsEmpty():
        return 0.0
    latitude = centroid.GetY()
    return (
        surface.Area()
        * (METRES_PER_DEGREE ** 2)
        * math.cos(math.radians(latitude))
    )
