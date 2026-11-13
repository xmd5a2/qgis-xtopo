"""Предварительная вырезка области из выгрузок OSM через osmium.

Чтение большой выгрузки целиком драйвером GDAL занимает минуты, хотя
нужна только небольшая область.  osmium вырезает её за секунды, и дальше
GDAL работает с маленьким файлом.

Вырезка идёт с запасом вокруг области: так в результат попадают линии и
полингоны, пересекающие границу, — иначе часть объектов теряется.

Отдельно выбираются объекты, охватывающие всю область целиком (горные
массивы и хребты): их геометрия лежит далеко за пределами области, и
обычная вырезка их не затрагивает, а на карте они нужны.

Вырезанные области нескольких карт склеиваются: отношение, чьи члены
лежат в двух странах (например, песчаная пустыня Grand Erg Oriental на
границе Алжира и Туниса), в каждой выгрузке обрезано, и драйвер GDAL
такое отношение теряет («Relation ... has missing ways»).
"""

from pathlib import Path

from . import osmium_tool

# Запас вокруг области вырезки (в градусах и доля от размера области).
# Запас нужен для линий и полигонов, пересекающих границу: osmium оставляет
# только объекты, у которых есть вершина внутри области, а такие объекты
# приходят из выгрузки целиком.  Запас в 0.3° (~30 км) почти ничего не
# стоит — время вырезки определяется чтением выгрузки, а не площадью.
MARGIN_DEGREES = 0.3
MARGIN_RATIO = 0.15

# Объекты, охватывающие область целиком: их вершины лежат за пределами
# вырезки, поэтому для них делается отдельный проход по всей карте.
COVERING_EXPRESSIONS = (
    "w/region:type=mountain_area",
    "r/region:type=mountain_area",
    "w/natural=massif",
    "r/natural=massif",
    "w/natural=mountain_range",
    "r/natural=mountain_range",
)

PBF_SUFFIXES = (".pbf", ".osm.pbf")


def padded_bbox(bbox):
    """Область вырезки с запасом вокруг заданной области."""
    width = bbox.lon_max - bbox.lon_min
    height = bbox.lat_max - bbox.lat_min
    margin = max(MARGIN_DEGREES, MARGIN_RATIO * max(width, height))
    return (
        bbox.lon_min - margin,
        bbox.lat_min - margin,
        bbox.lon_max + margin,
        bbox.lat_max + margin,
    )


def _bbox_argument(bbox):
    lon_min, lat_min, lon_max, lat_max = padded_bbox(bbox)
    return f"{lon_min:.6f},{lat_min:.6f},{lon_max:.6f},{lat_max:.6f}"


def _is_pbf(path):
    name = Path(path).name.lower()
    return name.endswith(PBF_SUFFIXES)


def _temporary_dir(config):
    directory = Path(config.temp_dir) / "osmium"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _merge_files(executable, paths, destination, cancel=None, progress=None):
    """Склеивает файлы osmium в один, оставляя по одной версии объекта.

    ``osmium merge`` сохраняет все версии объекта из пересекающихся
    выгрузок: это поведение history-файла.  Драйвер GDAL OSM читает
    обычный файл и обрывается на повторяющихся id.  ``apply-changes``
    применяет выгрузки как изменения и оставляет последнюю версию
    каждого объекта.
    """
    if progress:
        progress("Склейка вырезанных областей…")
    osmium_tool.run(
        executable,
        [
            "apply-changes", "--overwrite", "-o", str(destination),
            *[str(item) for item in paths],
        ],
        cancel=cancel,
    )


def prefilter_sources(paths, bbox, config, progress=None, cancel=None):
    """Готовит уменьшенные копии исходных выгрузок.

    Возвращает тройку (пути, временные файлы, файлы горных массивов).
    Если osmium недоступен или отработал с ошибкой, возвращаются исходные
    пути: обработка продолжается прежним способом, просто медленнее.

    Файлы горных массивов читаются без пространственного фильтра
    драйвера: их геометрия лежит за пределами вырезанной области, и
    фильтр отбросил бы их целиком.
    """
    original = [str(item) for item in paths]
    if not original:
        return original, [], []
    executable = osmium_tool.find_osmium(config.temp_dir)
    if not executable:
        return original, [], []
    directory = _temporary_dir(config)
    temporary = []
    covering = []
    result = []
    extracted = []
    try:
        for position, source in enumerate(original, 1):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("PBF processing cancelled")
            if not _is_pbf(source):
                result.append(source)
                continue
            stem = Path(source).stem.replace(".", "_")
            extracted_path = directory / f"{stem}_{position}_extract.osm.pbf"
            if progress:
                progress(
                    f"osmium: вырезка области из {Path(source).name} "
                    f"({position}/{len(original)})…"
                )
            osmium_tool.run(
                executable,
                [
                    "extract",
                    "-b", _bbox_argument(bbox),
                    "--strategy=smart",
                    "-S", "types=any",
                    "-o", str(extracted_path),
                    "--overwrite",
                    source,
                ],
                cancel=cancel,
            )
            temporary.append(extracted_path)
            extracted.append(str(extracted_path))
            covering_path = directory / f"{stem}_{position}_covering.osm.pbf"
            if progress:
                progress(
                    f"osmium: поиск горных массивов в {Path(source).name}…"
                )
            osmium_tool.run(
                executable,
                [
                    "tags-filter",
                    source,
                    *COVERING_EXPRESSIONS,
                    "-o", str(covering_path),
                    "--overwrite",
                ],
                cancel=cancel,
            )
            temporary.append(covering_path)
            covering.append(str(covering_path))
        # Области нескольких карт склеиваются: отношение, разорванное
        # границей стран, в одной выгрузке не собрать, и драйвер его теряет.
        if len(extracted) > 1:
            merged = directory / "extracts_merged.osm.pbf"
            _merge_files(
                executable, extracted, merged, cancel=cancel,
                progress=progress,
            )
            temporary.append(merged)
            extracted = [str(merged)]
        if len(covering) > 1:
            merged_covering = directory / "covering_merged.osm.pbf"
            _merge_files(
                executable, covering, merged_covering, cancel=cancel,
                progress=progress,
            )
            temporary.append(merged_covering)
            covering = [str(merged_covering)]
        result.extend(extracted)
        result.extend(covering)
    except InterruptedError:
        for path in temporary:
            Path(path).unlink(missing_ok=True)
        raise
    except Exception:
        for path in temporary:
            Path(path).unlink(missing_ok=True)
        return original, [], []
    return result, [str(path) for path in temporary], covering
