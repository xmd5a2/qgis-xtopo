import html
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from .bbox import parse_bbox
from .resources import resource_path

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
        Path(resource_path("automap.qgz")),
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
    if new_font:
        updated = updated.replace(old_font, new_font)
    if updated == text:
        return False
    encoded = updated.encode("utf-8")

    def transform(item_name, data):
        return encoded if item_name == name else data

    return _rewrite_archive(path, transform)


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
        if not fit_project_extent(iface) and hasattr(iface, "zoomFull"):
            try:
                iface.zoomFull()
            except Exception:
                pass
        set_canvas_magnification(iface)
        return
    from qgis.core import QgsProject

    QgsProject.instance().read(path)
