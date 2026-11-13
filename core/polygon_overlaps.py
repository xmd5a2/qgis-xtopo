"""Вырезание меньших полигонов из больших.

OsmAnd при равном ``order`` рисует меньший полигон выше. В QGIS порядок
задаётся слоями, поэтому вложенный луг или посёлок оказывается под полем.
Здесь для заданных пар меньший полигон вычитается из большего: дырка
показывает нижний слой независимо от порядка слоёв.

Обрабатываются только производные слои проекта; исходные данные OSM и
подписи не меняются.
"""
import math

from . import i18n

# Пары (верхний слой, нижний слой). Верхний слой сейчас закрывает нижний;
# если нижний полигон меньше, из верхнего вычитается их пересечение.
# Острова и островки (place=island/islet) лежат в одном слое island.
DEFAULT_PAIRS = (
    ("farmland", "grass"),
    ("farmland", "landuse_residential"),
    ("farmland", "industrial"),
    ("farmland", "vineyard"),
    ("farmland", "orchard"),
    ("farmland", "farmyard"),
    ("grass", "landuse_residential"),
    ("orchard", "landuse_residential"),
    ("grass", "industrial"),
    ("grass", "vineyard"),
    ("water", "island"),
    ("water", "wood"),
    ("water", "scrub"),
    ("allotments", "landuse_residential"),
)

# Имя слоя в шаблоне не всегда совпадает с именем файла запроса.
LAYER_FILES = {
    "water": "water_dissolved",
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


def resolve_polygon_overlaps(config, progress=None, cancel=None):
    """Вырезает меньшие полигоны из больших для пар по умолчанию."""
    notify = progress or (lambda message: None)
    pairs = _configured_pairs(config)
    min_area = float(
        getattr(config, "polygon_overlap_min_area", DEFAULT_MIN_AREA_M2)
    )
    grouped = {}
    for upper, lower in pairs:
        grouped.setdefault(upper, []).append(lower)
    results = []
    for upper, lowers in grouped.items():
        upper_path = _layer_path(config, upper)
        if not upper_path.exists():
            continue
        available = [
            (
                lower,
                _layer_path(config, lower),
                PAIR_MIN_AREA_M2.get((upper, lower), min_area),
            )
            for lower in lowers
            if _layer_path(config, lower).exists()
        ]
        if not available:
            continue
        notify(
            i18n.trf(
                "Вырезание пересечений: {} / {}",
                upper, ", ".join(lower for lower, _, _ in available),
            )
        )
        try:
            changed = _cut_layer(
                upper_path, available, cancel,
            )
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 - сборка не должна падать
            notify(
                i18n.trf(
                    "WARNING: пересечения {}: {}; слой не изменён",
                    upper, exc,
                )
            )
            continue
        for lower, _, _ in available:
            count = changed.get(lower, 0)
            results.append((upper, lower, count))
            notify(
                i18n.trf(
                    "Вырезание пересечений: {} / {} — изменено {} объектов",
                    upper, lower, count,
                )
            )
    return results


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


def _cut_layer(upper_path, lowers, cancel):
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
                    output_layer, source_definition, feature, geometry,
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
            for lower_name, lower_min_area, lower_layer in (
                (name, area, layer) for name, area, _, layer in lower_layers
            ):
                lower_layer.SetSpatialFilter(geometry)
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
                lower_layer.SetSpatialFilter(None)
            geometry = _polygonal_parts(geometry) or geometry
            _write_feature(
                output_layer, source_definition, feature, geometry,
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


def _write_feature(output_layer, source_definition, feature, geometry):
    from osgeo import ogr

    output_feature = ogr.Feature(output_layer.GetLayerDefn())
    for index in range(source_definition.GetFieldCount()):
        output_feature.SetField(index, feature.GetField(index))
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
