import html
import json
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

from .bbox import parse_bbox
from . import dem_sources, map_units, text_size
from .map_language import DETAIL_VARIABLE
from .resources import plugin_version, resource_path, template_path

TEMPLATE_FONT = "Century Schoolbook L"
SERIF_FONTS = (
    TEMPLATE_FONT,
    "Noto Serif",
    "DejaVu Serif",
    "Liberation Serif",
    "Times New Roman",
)

# Во сколько раз область карты увеличивается относительно вписанного вида.
DEFAULT_ZOOM = 1.0

# Множитель «Увеличение» в строке состояния: иконки и подписи рисуются
# в 1.5 раза крупнее (QgsMapCanvas::setMagnificationFactor).
MAGNIFICATION_FACTOR = 1.5

# Уровень сжатия .qgz.  Проект — это один qgs на десятки мегабайт, и на
# уровне 6 deflate тратит около 0,7 с на запись.  Уровень 1 пишет втрое
# быстрее, а архив больше всего на пару мегабайт; читается он быстрее.
ARCHIVE_COMPRESSLEVEL = 1

# Фильтры правил, ссылающиеся на слои, которых может не быть в готовом
# проекте: пустые слои вырезаются при сборке, а обращение overlay_* к
# отсутствующему слою обращает фильтр в NULL.  try(...) возвращает
# запасное значение, и объекты остаются на карте.
OVERLAY_FILTERS = (
    (
        "if(overlay_within('spoil heap'),false,true)",
        "try(overlay_within('spoil heap'), false) = false",
    ),
    (
        "if(overlay_within('prison'),false,true)",
        "try(overlay_within('prison'), false) = false",
    ),
    (
        "if(overlay_contains('earth bank gully') OR "
        "overlay_contains('cliff'),0,1)",
        "if(try(overlay_contains('earth bank gully') OR "
        "overlay_contains('cliff'), false),0,1)",
    ),
)

# Передискретизация растров рельефа.  При увеличении карты растр
# показывается крупнее исходного разрешения: без передискретизации
# QGIS берёт соседние пиксели, и отмывка рассыпается на квадраты.
#
# Ранняя передискретизация (этап «provider») отдаёт пересчёт самому
# GDAL: он читает растр сразу в нужном масштабе, это быстрее и
# экономнее по памяти, чем фильтр QGIS.  Бикубическая передискретизация
# при увеличении сглаживает укрупнение.
RESAMPLING_ELEMENT_PATTERN = re.compile(r"<resampling\b[^>]*/>")
RASTER_RESAMPLER_PATTERN = re.compile(r"<rasterresampler\b[^>]*/>")
RESAMPLING_STAGE_PATTERN = re.compile(
    r"<resamplingStage>[^<]*</resamplingStage>"
)
GDAL_PROVIDER_PATTERN = re.compile(r"<provider[^>]*>\s*gdal\s*</provider>")

# Штатные 5 кандидатов линейных подписей на сантиметр заставляют PAL
# перебирать в разы больше позиций. 0.5 заметно ускоряет плотные
# подписи, почти не меняя карту.
LABEL_CANDIDATES_PATTERN = re.compile(
    r"(<CandidatesLinePerCM\b[^>]*>)\s*([0-9.]+)\s*"
    r"(</CandidatesLinePerCM>)"
)
LABEL_CANDIDATES_PER_CM = 0.5


def resolve_label_font():
    try:
        from qgis.PyQt.QtGui import QFontDatabase
    except ImportError:
        return None
    try:
        families = set(QFontDatabase().families())
    except TypeError:
        families = set(QFontDatabase.families())
    except Exception:
        return None
    for family in SERIF_FONTS:
        if family in families:
            return family
    return None


def project_file(config):
    return config.layers_dir / f"{config.project_name}.qgz"


def project_file_path(projects_dir, project_name):
    """Путь к файлу проекта по каталогу проектов и имени.

    None, если имя проекта недопустимо.
    """
    from .config import normalize_project_name, project_name_error

    name = normalize_project_name(project_name) or "automap"
    if project_name_error(name):
        return None
    return Path(projects_dir).expanduser() / name / f"{name}.qgz"


# Каталог, в который собираются слои текущей подготовки.  Прежние слои
# остаются на месте до самого конца: сбой или отмена не оставляют проект
# без данных.
STAGE_DIRNAME = ".xtopo_stage"

# Каталог прежних слоёв, пока их заменяют готовыми.
OLD_DIRNAME = ".xtopo_old"


def staging_dir(config):
    return config.project_dir / STAGE_DIRNAME


def begin_staging(config):
    """Готовит отдельный каталог для слоёв новой сборки.

    Пути слоёв в файле проекта относительные, поэтому собранный набор
    переносится в каталог проекта как есть.  Пока сборка идёт, прежний
    проект остаётся целым и открывается.
    """
    stage = staging_dir(config)
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    (stage / "vector").mkdir(parents=True, exist_ok=True)
    (stage / "raster").mkdir(parents=True, exist_ok=True)
    config.stage_dir = str(stage)
    return stage


def discard_staging(config):
    """Убирает каталог подготовки; прежние слои не трогает."""
    stage = staging_dir(config)
    config.stage_dir = ""
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)


def _swap(source, destination):
    """Подменяет файл или каталог, прежний убирает в корзину."""
    trash = destination.with_name(f"{OLD_DIRNAME}_{destination.name}")
    if trash.exists():
        if trash.is_dir():
            shutil.rmtree(trash, ignore_errors=True)
        else:
            trash.unlink(missing_ok=True)
    if destination.exists():
        os.replace(destination, trash)
    try:
        os.replace(source, destination)
    except OSError:
        # Не перенелось — возвращаем прежнее на место.
        if trash.exists() and not destination.exists():
            os.replace(trash, destination)
        raise
    if trash.is_dir():
        shutil.rmtree(trash, ignore_errors=True)
    else:
        trash.unlink(missing_ok=True)


def commit_staging(config):
    """Подменяет прежние слои и файл проекта подготовленными."""
    stage = staging_dir(config)
    config.stage_dir = ""
    if not stage.exists():
        return None
    project_dir = config.project_dir
    for name in ("vector", "raster"):
        source = stage / name
        if source.exists():
            _swap(source, project_dir / name)
    for name in (f"{config.project_name}.qgz", "crop.geojson"):
        source = stage / name
        if source.exists():
            _swap(source, project_dir / name)
    shutil.rmtree(stage, ignore_errors=True)
    return project_file(config)


def prepare_project(config, write_archive=True):
    config.ensure_directories()
    # Иконки нужны проекту (шаблон ссылается на них относительным путём),
    # но обновлять их при каждой подготовке незачем: копируются только
    # новые и устаревшие файлы.
    _copy_missing(
        Path(resource_path("icons")), config.projects_dir / "icons",
        overwrite=False, newer=True,
    )
    _write_crop(config)
    path = project_file(config)
    # Always start from the pristine template: empty-layer cleanup removes
    # layers from the project file, and a reused file could never get them
    # back after a later run produced data for the same layers.
    #
    # ``write_archive=False`` откладывает запись до конца подготовки:
    # тогда пустые слои вырезаются до сжатия, а 70-МБ qgs не сжимается
    # дважды.  Файл проекта нужен только после сборки слоёв.
    if write_archive:
        rewrite_project_archive(
            template_path(),
            path,
            config.project_name,
            dem_attribution=(
                dem_sources.attribution_for(config.resolved_terrain_source())
                if config.generate_terrain else ""
            ),
            units=config.isolines_units,
        )
    # config.ini читает только tools/check_pbf_coverage.py, поэтому
    # пользовательская правка не должна затираться каждой подготовкой.
    settings = config.config_dir / "config.ini"
    if not settings.exists():
        config.to_file(settings)
    return path


def finish_project(config, dem_attribution=""):
    """Собирает готовый архив проекта из шаблона одним проходом.

    Вызывается после подготовки слоёв: пустые слои вырезаются до сжатия,
    поэтому qgs сжимается один раз и уже в сокращённом виде.
    """
    path = project_file(config)
    rewrite_project_archive(
        template_path(),
        path,
        config.project_name,
        dem_attribution=dem_attribution,
        units=config.isolines_units,
        layers_base=config.layers_dir,
    )
    return path


def _copy_missing(source, destination, overwrite=False, newer=False):
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            _copy_missing(item, target, overwrite=overwrite, newer=newer)
        elif overwrite or not target.exists() or (
            newer and target.stat().st_mtime < item.stat().st_mtime
        ):
            shutil.copy2(item, target)


def _write_crop(config):
    bbox = parse_bbox(config.bbox)
    path = config.crop_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bbox.polygon_geojson(), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _maplayer_spans(text):
    """Границы блоков ``maplayer`` через str.find.

    Регулярное выражение с ``.*?`` на 74 МБ проекта идёт около 0,4 с;
    поиск подстроки в C — в двадцать раз быстрее и даёт те же границы.
    """
    position = 0
    opening = "<maplayer"
    closing = "</maplayer>"
    while True:
        start = text.find(opening, position)
        if start < 0:
            break
        boundary = start + len(opening)
        if boundary < len(text) and text[boundary] not in " \t\r\n>/":
            position = boundary
            continue
        open_end = text.find(">", boundary)
        if open_end < 0:
            break
        end = text.find(closing, open_end)
        if end < 0:
            break
        end += len(closing)
        yield start, end
        position = end


LAYER_TREE_PATTERN = re.compile(
    r'<layer-tree-layer\b[^>]*\bid="([^"]+)"[^>]*(?:/>|>.*?</layer-tree-layer>)',
    re.S,
)
LEGEND_PATTERN = re.compile(r"<legendlayer\b[^>]*>.*?</legendlayer>", re.S)
LEGEND_LAYER_ID_PATTERN = re.compile(r'<legendlayerfile\b[^>]*\blayerid="([^"]+)"')
TREE_ITEM_PATTERN = re.compile(r"<item>([^<]+)</item>")
LAYER_REFERENCE_PATTERN = re.compile(r'<layer\s[^>]*\bid="([^"]+)"[^>]*/>')
LAYER_SETTING_PATTERN = re.compile(r'<layer-setting\b[^>]*\bid="([^"]+)"[^>]*/>')
EXPANDED_LEGEND_PATTERN = re.compile(
    r'<expanded-legend-nodes\b[^>]*\bid="([^"]+)"[^>]*/>'
)
CHECKED_LEGEND_PATTERN = re.compile(
    r'<checked-legend-nodes\b[^>]*\bid="([^"]+)"[^>]*>.*?</checked-legend-nodes>',
    re.S,
)


def _element(block, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    if match is None:
        return None
    return match.group(1).strip()


def _sqlite_has_features(path, datasource):
    """Быстрая проверка Spatialite без открытия драйвера OGR.

    ``None`` — файл не подходит для быстрого пути, нужен общий способ.
    """
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            match = re.search(r"(?:^|\|)layername=([^|]+)", datasource)
            if match:
                table = match.group(1)
            else:
                rows = database.execute(
                    "SELECT f_table_name FROM geometry_columns"
                ).fetchall()
                if len(rows) != 1:
                    return None
                table = rows[0][0]
            if not table:
                return None
            row = database.execute(
                f'SELECT 1 FROM "{table}" LIMIT 1'
            ).fetchone()
            return row is not None
    except (sqlite3.Error, OSError):
        return None


def _datasource_has_features(path, datasource):
    if path.suffix.lower() == ".sqlite":
        fast = _sqlite_has_features(path, datasource)
        if fast is not None:
            return fast
    try:
        from osgeo import ogr
    except ImportError:
        return True
    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return False
    try:
        match = re.search(r"(?:^|\|)layername=([^|]+)", datasource)
        if match:
            layer = dataset.GetLayerByName(match.group(1))
            return layer is not None and layer.GetNextFeature() is not None
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayerByIndex(index)
            if layer is not None and layer.GetNextFeature() is not None:
                return True
    except Exception:
        return True
    finally:
        dataset = None
    return False


def _is_empty_datasource(datasource, base_dir):
    value = (datasource or "").strip()
    if not value:
        return True
    path_text = value.split("|", 1)[0].strip()
    # Provider URIs (XYZ, WMS, databases) are not local files and are kept.
    if "=" in path_text:
        return False
    path = Path(path_text)
    if not path.is_absolute():
        path = Path(base_dir) / path_text
    if not path.exists():
        return True
    if path.suffix.lower() in (".sqlite", ".gpkg", ".geojson", ".json", ".shp"):
        return not _datasource_has_features(path, value)
    return False


def _drop_removed_references(text, removed):
    """Убирает ссылки на удалённые слои из дерева, легенды и порядка.

    Теги дерева и легенды лежат вне блоков ``maplayer``, а сами блоки
    занимают почти весь текст проекта.  Поэтому проходы по ссылкам
    выполняются по кускам между слоями, а не по всему документу:
    иначе удаление слоёв стоило бы нескольких полных сканирований
    стилей на десятки мегабайт.
    """
    def drop_tree(match):
        return "" if match.group(1) in removed else match.group(0)

    text = LAYER_TREE_PATTERN.sub(drop_tree, text)

    def drop_legend(match):
        block = match.group(0)
        if any(item in removed for item in LEGEND_LAYER_ID_PATTERN.findall(block)):
            return ""
        return block

    text = LEGEND_PATTERN.sub(drop_legend, text)
    text = TREE_ITEM_PATTERN.sub(
        lambda match: "" if match.group(1).strip() in removed else match.group(0),
        text,
    )
    for pattern in (
        LAYER_REFERENCE_PATTERN,
        LAYER_SETTING_PATTERN,
        EXPANDED_LEGEND_PATTERN,
        CHECKED_LEGEND_PATTERN,
    ):
        text = pattern.sub(
            lambda match: "" if match.group(1) in removed else match.group(0),
            text,
        )
    return text


def _empty_layer_ids(blocks, base_dir):
    """ID слоёв с пустым источником."""
    return {
        layer_id
        for _start, _end, _block, layer_id, datasource in blocks
        if layer_id and datasource is not None
        and _is_empty_datasource(datasource, base_dir)
    }


def _remove_empty_layers(text, base_dir):
    removed = set()
    blocks = []
    for start, end in _maplayer_spans(text):
        block = text[start:end]
        layer_id = _element(block, "id")
        datasource = _element(block, "datasource")
        if datasource is not None:
            datasource = html.unescape(datasource)
        blocks.append((start, end, block, layer_id, datasource))
    removed = _empty_layer_ids(blocks, base_dir)
    if not removed:
        return text, removed

    pieces = []
    previous = 0
    for start, end, block, layer_id, _datasource in blocks:
        pieces.append(
            _drop_removed_references(text[previous:start], removed)
        )
        if layer_id not in removed:
            pieces.append(block)
        previous = end
    pieces.append(_drop_removed_references(text[previous:], removed))
    return "".join(pieces), removed


def _rewrite_archive(path, transform):
    path = Path(path)
    if not path.exists():
        return False
    temporary = path.with_suffix(path.suffix + ".tmp")
    changed = False
    with zipfile.ZipFile(path) as archive:
        with zipfile.ZipFile(
            temporary, "w", zipfile.ZIP_DEFLATED,
            compresslevel=ARCHIVE_COMPRESSLEVEL,
        ) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                updated = transform(item.filename, data)
                if updated != data:
                    changed = True
                output.writestr(
                    item, updated, compresslevel=ARCHIVE_COMPRESSLEVEL
                )
    if changed:
        os.replace(temporary, path)
    else:
        temporary.unlink(missing_ok=True)
    return changed


def clean_project_layers(path, dem_attribution=None):
    """Remove layers whose data source is missing or contains no features.

    The project template references every layer of the full recipe.  A
    prepared project only has files for the selected layers, so the rest
    would trigger the "unavailable layers" prompt on open.

    ``dem_attribution`` — атрибуция наборов DEM.  Она пишется здесь же,
    вторую перезапись архива проекта делать не нужно.
    """
    path = Path(path)
    if not path.exists():
        return False
    base_dir = path.parent

    def transform(name, data):
        if not name.endswith(".qgs"):
            return data
        text = data.decode("utf-8")
        updated, _removed = _remove_empty_layers(text, base_dir)
        if dem_attribution:
            updated = _set_project_variable(
                updated, "dem_attribution", dem_attribution
            )
        return updated.encode("utf-8") if updated != text else data

    return _rewrite_archive(path, transform)


def patch_overlay_filters(text):
    """Оборачивает обращения к overlay_* в try(...).

    Пустые слои (spoil heap, prison, cliff, earth bank gully)
    вырезаются при сборке проекта.  Обращение overlay_* к
    отсутствующему слою — ошибка выражения, и весь фильтр правила
    становится NULL: объекты пропадают с карты.  try(...) вместо
    ошибки возвращает запасное значение, и фильтр работает.
    """
    for old, new in OVERLAY_FILTERS:
        text = text.replace(old, new)
    return text


def _set_attribute(element, name, value):
    """Заменяет или добавляет атрибут самозакрывающегося тега."""
    if re.search(rf'{name}="', element):
        return re.sub(rf'{name}="[^"]*"', f'{name}="{value}"', element)
    return re.sub(r"\s*/>$", f' {name}="{value}" />', element)


def _provider_resampling(match):
    """Включает раннюю передискретизацию и бикуб при увеличении."""
    element = match.group(0)
    element = _set_attribute(element, "enabled", "true")
    return _set_attribute(element, "zoomedInResamplingMethod", "cubic")


def _filter_resampling(match):
    """Бикубическая передискретизация при увеличении в фильтре QGIS."""
    return _set_attribute(match.group(0), "zoomedInResampler", "cubic")


def _patch_raster_block(block):
    if not GDAL_PROVIDER_PATTERN.search(block):
        return block
    block = RESAMPLING_ELEMENT_PATTERN.sub(
        _provider_resampling, block,
    )
    block = RESAMPLING_STAGE_PATTERN.sub(
        "<resamplingStage>provider</resamplingStage>", block,
    )
    block = RASTER_RESAMPLER_PATTERN.sub(
        _filter_resampling, block,
    )
    return block


def _apply_edits(text, edits, font_old=None, font_new=None):
    """Собирает текст один раз по списку правок ``(начало, конец, текст)``."""
    if not edits:
        return text
    edits.sort()
    pieces = []
    previous = 0
    replace_font = bool(font_old and font_new)
    for start, end, replacement in edits:
        piece = text[previous:start]
        if replace_font:
            piece = piece.replace(font_old, font_new)
            replacement = replacement.replace(font_old, font_new)
        pieces.append(piece)
        pieces.append(replacement)
        previous = end
    tail = text[previous:]
    if replace_font:
        tail = tail.replace(font_old, font_new)
    pieces.append(tail)
    return "".join(pieces)


def _raster_edits(text):
    """Правки растровых слоёв: их всего несколько из сотен."""
    edits = []
    for start, end in _maplayer_spans(text):
        open_end = text.find(">", start)
        if 'type="raster"' not in text[start:open_end + 1]:
            continue
        block = text[start:end]
        patched = _patch_raster_block(block)
        if patched != block:
            edits.append((start, end, patched))
    return edits


def patch_raster_resampling(text):
    """Настраивает передискретизацию растров рельефа.

    Растры читает GDAL, поэтому у него свой этап передискретизации и
    свой метод при увеличении.  Слой-плитка (подложка OpenStreetMap)
    не трогаем: у него нет файла на диске и пересчитывать нечего.
    """
    return _apply_edits(text, _raster_edits(text))


def patch_label_candidates(text):
    """Ограничивает число кандидатов линейных подписей.

    Штатные 5 на сантиметр заставляют PAL перебирать в разы больше
    позиций. 0.5 заметно ускоряет рендер плотных подписей.  Уже
    уменьшенные и пользовательские значения не перебиваем: правим
    только штатную пятёрку.
    """
    match = LABEL_CANDIDATES_PATTERN.search(text)
    if match is None:
        return text
    try:
        value = float(match.group(2))
    except ValueError:
        return text
    if value != 5.0:
        return text
    replacement = (
        f"{match.group(1)}{LABEL_CANDIDATES_PER_CM:g}{match.group(3)}"
    )
    return text[:match.start()] + replacement + text[match.end():]


def repair_project(path):
    """Чинит шрифт, пустые слои, фильтры, растры и подписи."""
    path = Path(path)
    if not path.exists():
        return False
    base_dir = path.parent
    font = resolve_label_font()
    old_font = f'fontFamily="{TEMPLATE_FONT}"'
    new_font = f'fontFamily="{font}"' if font and font != TEMPLATE_FONT else None

    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".qgs")]
        if not names:
            return False
        name = names[0]
        text = archive.read(name).decode("utf-8")

    updated, _removed = _remove_empty_layers(text, base_dir)
    updated = patch_overlay_filters(updated)
    updated = patch_raster_resampling(updated)
    updated = patch_label_candidates(updated)
    if new_font:
        updated = updated.replace(old_font, new_font)
    if updated == text:
        return False
    encoded = updated.encode("utf-8")

    def transform(item_name, data):
        return encoded if item_name == name else data

    return _rewrite_archive(path, transform)


VARIABLE_NAMES_PATTERN = re.compile(
    r'<variableNames type="QStringList">(.*?)</variableNames>', re.S
)
VARIABLE_VALUES_PATTERN = re.compile(
    r'<variableValues type="QStringList">(.*?)</variableValues>', re.S
)
# Пустое значение QGIS пишет самозакрывающимся тегом `<value />`;
# пропуск такого тега сдвигал все последующие значения относительно имён.
VARIABLE_VALUE_PATTERN = re.compile(
    r"<value\b[^>]*?/>|<value\b[^>]*?>(.*?)</value>", re.S
)


def _set_detail_variable(text, level=None):
    """Записывает уровень детализации в переменные проекта шаблона.

    Значения переменных лежат отдельным списком и связаны с именами по
    позиции, поэтому правится значение той же позиции, что и имя
    `level_of_detail`. Если имени в списке нет, значение не трогается.
    """
    names = VARIABLE_NAMES_PATTERN.search(text)
    values = VARIABLE_VALUES_PATTERN.search(text)
    if names is None or values is None:
        return text
    names_list = [item.group(1) for item in
                  VARIABLE_VALUE_PATTERN.finditer(names.group(1))]
    if DETAIL_VARIABLE not in names_list:
        return text
    position = names_list.index(DETAIL_VARIABLE)
    entries = list(VARIABLE_VALUE_PATTERN.finditer(values.group(1)))
    if position >= len(entries):
        return text
    if level is None:
        from .map_language import level_of_detail

        level = level_of_detail()
    start = values.start(1) + entries[position].start()
    end = values.start(1) + entries[position].end()
    return f"{text[:start]}<value>{level}</value>{text[end:]}"


def _set_project_variable(text, name, value):
    """Записывает переменную проекта, добавляя её при отсутствии.

    Значения переменных лежат отдельным списком и связаны с именами по
    позиции.  Если переменной в шаблоне нет, она добавляется в конец
    обоих списков.
    """
    names = VARIABLE_NAMES_PATTERN.search(text)
    values = VARIABLE_VALUES_PATTERN.search(text)
    if names is None or values is None:
        return text
    names_list = [item.group(1) for item in
                  VARIABLE_VALUE_PATTERN.finditer(names.group(1))]
    entries = list(VARIABLE_VALUE_PATTERN.finditer(values.group(1)))
    escaped = html.escape(str(value), quote=False)
    if name in names_list:
        position = names_list.index(name)
        if position >= len(entries):
            return text
        start = values.start(1) + entries[position].start()
        end = values.start(1) + entries[position].end()
        return f"{text[:start]}<value>{escaped}</value>{text[end:]}"
    names_insert = names.end(1)
    values_insert = values.end(1)
    text = (
        text[:values_insert]
        + f"\n        <value>{escaped}</value>"
        + text[values_insert:]
    )
    text = (
        text[:names_insert]
        + f"\n        <value>{html.escape(name, quote=False)}</value>"
        + text[names_insert:]
    )
    return text


def _updated_variable_contents(names_content, values_content, updates):
    """Новые списки переменных без пересборки всего текста проекта."""
    name_entries = list(VARIABLE_VALUE_PATTERN.finditer(names_content))
    name_list = [entry.group(1) for entry in name_entries]
    value_entries = list(VARIABLE_VALUE_PATTERN.finditer(values_content))
    replacements = []
    for name, value in updates:
        if name not in name_list:
            continue
        position = name_list.index(name)
        if position >= len(value_entries):
            continue
        entry = value_entries[position]
        escaped = html.escape(str(value), quote=False)
        if entry.group(1) is None:
            replacements.append(
                (entry.start(), entry.end(), f"<value>{escaped}</value>")
            )
        else:
            replacements.append((entry.start(1), entry.end(1), escaped))
    for start, end, replacement in sorted(replacements, reverse=True):
        values_content = (
            values_content[:start] + replacement + values_content[end:]
        )
    for name, value in updates:
        if name in name_list:
            continue
        escaped = html.escape(str(value), quote=False)
        names_content += (
            f"\n        <value>{html.escape(name, quote=False)}</value>"
        )
        values_content += f"\n        <value>{escaped}</value>"
    return names_content, values_content


def _variable_edits(text, updates, level=None):
    """Правки переменных проекта одним проходом по их небольшим спискам."""
    names = VARIABLE_NAMES_PATTERN.search(text)
    values = VARIABLE_VALUES_PATTERN.search(text)
    if names is None or values is None:
        return []
    name_list = [
        item.group(1)
        for item in VARIABLE_VALUE_PATTERN.finditer(names.group(1))
    ]
    resolved = []
    for name, value in updates:
        if name == DETAIL_VARIABLE:
            if name not in name_list:
                continue
            if value is None:
                if level is None:
                    from .map_language import level_of_detail

                    level = level_of_detail()
                value = level
        resolved.append((name, value))
    names_content, values_content = _updated_variable_contents(
        names.group(1), values.group(1), resolved
    )
    edits = []
    if names_content != names.group(1):
        edits.append((names.start(1), names.end(1), names_content))
    if values_content != values.group(1):
        edits.append((values.start(1), values.end(1), values_content))
    return edits


def _marker_edits(text, marker, replacement):
    """Правки всех вхождений фиксированной строки."""
    edits = []
    position = 0
    while True:
        found = text.find(marker, position)
        if found < 0:
            break
        edits.append((found, found + len(marker), replacement))
        position = found + len(marker)
    return edits


def _project_text_edits(text, project_name, dem_attribution, units="m"):
    """Все правки qgs для новой подготовки, кроме замены шрифта."""
    units = map_units.normalize(units)
    edits = _marker_edits(
        text, 'projectname="automap"', f'projectname="{project_name}"'
    )
    edits.extend(_marker_edits(
        text, "<title>automap</title>", f"<title>{project_name}</title>"
    ))
    marker = "\n  <properties>\n"
    position = text.find(marker)
    if position >= 0:
        edits.append((
            position, position + len(marker),
            marker + '    <qgis_xtopo type="bool">true</qgis_xtopo>\n',
        ))
    edits.extend(_variable_edits(text, (
        (DETAIL_VARIABLE, None),
        (text_size.VARIABLE, text_size.multiplier()),
        (map_units.VARIABLE, units),
        (map_units.DATA_VARIABLE, units),
        ("dem_attribution", dem_attribution),
        ("xtopo_version", plugin_version()),
    )))
    edits.extend(_marker_edits(
        text,
        '<Ellipsoid type="QString">NONE</Ellipsoid>',
        '<Ellipsoid type="QString">EPSG:7030</Ellipsoid>',
    ))
    edits.extend(_raster_edits(text))
    return edits


def rewrite_project_archive(source, destination, project_name, dem_attribution="",
                            units="m", layers_base=None):
    """Переписывает шаблон в архив проекта.

    ``layers_base`` — каталог готовых слоёв: если задан, из qgs сразу
    вырезаются слои с пустым или отсутствующим источником.  Так архив
    пишется один раз и сжимает уже сокращённый проект.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.exists():
        raise FileNotFoundError(f"Project template not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    font = resolve_label_font()
    old_font = f'fontFamily="{TEMPLATE_FONT}"'
    new_font = f'fontFamily="{font}"' if font and font != TEMPLATE_FONT else None

    with zipfile.ZipFile(source) as archive:
        with zipfile.ZipFile(
            temporary, "w", zipfile.ZIP_DEFLATED,
            compresslevel=ARCHIVE_COMPRESSLEVEL,
        ) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                name = item.filename
                if name.endswith(".qgs"):
                    text = data.decode("utf-8")
                    # Сначала вырезаем пустые слои: правки по маркерам
                    # дешевле на сокращённом тексте, а результат тот же.
                    if layers_base is not None:
                        text, _removed = _remove_empty_layers(
                            text, Path(layers_base)
                        )
                    edits = _project_text_edits(
                        text, project_name, dem_attribution, units
                    )
                    text = _apply_edits(text, edits, old_font, new_font)
                    data = text.encode("utf-8")
                    name = f"{project_name}.qgs"
                elif name.endswith(".qgd"):
                    name = f"{project_name}.qgd"
                output.writestr(name, data)

    os.replace(temporary, destination)
    return destination


def fit_project_extent(iface, zoom=DEFAULT_ZOOM):
    """Zoom the canvas to the prepared project bbox (crop.geojson).

    zoom > 1 увеличивает карту относительно вписанного вида.
    """
    if iface is None or not hasattr(iface, "mapCanvas"):
        return False
    try:
        from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle

        project = QgsProject.instance()
        canvas = iface.mapCanvas()
        target = canvas.mapSettings().destinationCrs()
        extent = None
        for candidate in project.mapLayers().values():
            source = candidate.source() or ""
            if source.endswith("crop.geojson") or candidate.name() == "BBOX":
                if candidate.isValid() and not candidate.extent().isEmpty():
                    extent = candidate.extent()
                    if candidate.crs() != target:
                        transform = QgsCoordinateTransform(
                            candidate.crs(), target, project
                        )
                        extent = transform.transformBoundingBox(extent)
                break

        if extent is None or extent.isEmpty():
            extent = QgsRectangle()
            for candidate in project.mapLayers().values():
                source = candidate.source() or ""
                if source.startswith(("crs=", "type=xyz", "url=")):
                    continue
                if not candidate.isValid() or candidate.extent().isEmpty():
                    continue
                layer_extent = candidate.extent()
                if candidate.crs() != target:
                    transform = QgsCoordinateTransform(
                        candidate.crs(), target, project
                    )
                    layer_extent = transform.transformBoundingBox(layer_extent)
                extent.combineExtentWith(layer_extent)

        if extent.isEmpty():
            return False
        extent.scale(1.05)
        try:
            factor = float(zoom)
        except (TypeError, ValueError):
            factor = 1.0
        if factor > 0:
            extent.scale(1.0 / factor)
        # QgsMapCanvas делит переданный setExtent охват на magnificationFactor.
        # Компенсируем это, чтобы при увеличении 150% была видна та же
        # область, что и при 100%, а не только её центральная часть.
        magnification = getattr(canvas, "magnificationFactor", None)
        if callable(magnification):
            try:
                value = float(magnification())
                if value > 0:
                    extent.scale(value)
            except (TypeError, ValueError):
                pass
        canvas.setExtent(extent)
        canvas.refresh()
        return True
    except Exception:
        return False


def set_canvas_magnification(iface, factor=MAGNIFICATION_FACTOR):
    """Задаёт множитель «Увеличение» в строке состояния QGIS."""
    if iface is None or not hasattr(iface, "mapCanvas"):
        return False
    try:
        canvas = iface.mapCanvas()
        setter = getattr(canvas, "setMagnificationFactor", None)
        if setter is None:
            return False
        setter(float(factor))
        return True
    except Exception:
        return False


def open_project(path, iface=None, repair=True):
    path = str(path)
    if repair and path.lower().endswith(".qgz"):
        try:
            repair_project(path)
        except Exception:
            pass
    if iface is not None and hasattr(iface, "addProject"):
        iface.addProject(path)
        # Увеличение меняет видимый охват, поэтому сначала задаём 150%,
        # а затем вписываем BBOX с учётом этого множителя.
        set_canvas_magnification(iface)
        if not fit_project_extent(iface) and hasattr(iface, "zoomFull"):
            try:
                iface.zoomFull()
            except Exception:
                pass
        return
    from qgis.core import QgsProject

    QgsProject.instance().read(path)
