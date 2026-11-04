import html
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from .bbox import parse_bbox
from .map_language import DETAIL_VARIABLE
from .resources import resource_path, template_path

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
    return config.project_dir / f"{config.project_name}.qgz"


def project_file_path(projects_dir, project_name):
    """Путь к файлу проекта по каталогу проектов и имени.

    None, если имя проекта недопустимо.
    """
    from .config import normalize_project_name, project_name_error

    name = normalize_project_name(project_name) or "automap"
    if project_name_error(name):
        return None
    return Path(projects_dir).expanduser() / name / f"{name}.qgz"


def prepare_project(config):
    config.ensure_directories()
    _copy_missing(
        Path(resource_path("icons")), config.projects_dir / "icons",
        overwrite=True,
    )
    _copy_missing(
        Path(resource_path("queries")), config.config_dir / "queries",
        overwrite=True,
    )
    _write_crop(config)
    path = project_file(config)
    # Always start from the pristine template: empty-layer cleanup removes
    # layers from the project file, and a reused file could never get them
    # back after a later run produced data for the same layers.
    rewrite_project_archive(
        template_path(),
        path,
        config.project_name,
    )
    config.to_file(config.config_dir / "config.ini")
    return path


def _copy_missing(source, destination, overwrite=False):
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            _copy_missing(item, target, overwrite=overwrite)
        elif overwrite or not target.exists():
            shutil.copy2(item, target)


def _write_crop(config):
    bbox = parse_bbox(config.bbox)
    path = config.project_dir / "crop.geojson"
    path.write_text(
        json.dumps(bbox.polygon_geojson(), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


MAPLAYER_PATTERN = re.compile(r"<maplayer\b[^>]*>.*?</maplayer>", re.S)
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


def _datasource_has_features(path, datasource):
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


def _remove_empty_layers(text, base_dir):
    removed = set()

    def scan(match):
        block = match.group(0)
        layer_id = _element(block, "id")
        datasource = _element(block, "datasource")
        if layer_id and datasource is not None:
            if _is_empty_datasource(html.unescape(datasource), base_dir):
                removed.add(layer_id)
        return block

    MAPLAYER_PATTERN.sub(scan, text)
    if not removed:
        return text, removed

    def drop_layer(match):
        block = match.group(0)
        layer_id = _element(block, "id")
        return "" if layer_id in removed else block

    text = MAPLAYER_PATTERN.sub(drop_layer, text)

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
    return text, removed


def _rewrite_archive(path, transform):
    path = Path(path)
    if not path.exists():
        return False
    temporary = path.with_suffix(path.suffix + ".tmp")
    changed = False
    with zipfile.ZipFile(path) as archive:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                updated = transform(item.filename, data)
                if updated != data:
                    changed = True
                output.writestr(item, updated)
    if changed:
        os.replace(temporary, path)
    else:
        temporary.unlink(missing_ok=True)
    return changed


def clean_project_layers(path):
    """Remove layers whose data source is missing or contains no features.

    The project template references every layer of the full recipe.  A
    prepared project only has files for the selected layers, so the rest
    would trigger the "unavailable layers" prompt on open.
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


def repair_project(path):
    """Patch fonts and drop empty layers in a single archive rewrite."""
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
VARIABLE_VALUE_PATTERN = re.compile(r"<value>(.*?)</value>", re.S)


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


def rewrite_project_archive(source, destination, project_name):
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
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                name = item.filename
                if name.endswith(".qgs"):
                    text = data.decode("utf-8")
                    text = text.replace('projectname="automap"', f'projectname="{project_name}"')
                    text = text.replace("<title>automap</title>", f"<title>{project_name}</title>")
                    text = text.replace(
                        "\n  <properties>\n",
                        "\n  <properties>\n"
                        '    <qgis_xtopo type="bool">true</qgis_xtopo>\n',
                        1,
                    )
                    if new_font:
                        text = text.replace(old_font, new_font)
                    text = _set_detail_variable(text)
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
