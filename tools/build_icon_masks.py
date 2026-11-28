#!/usr/bin/env python3
"""Пересобирает маски значков (MaskMarker) в ``resources/icons``.

Маска значка — это белая копия рисунка, увеличенная равномерным
отступом: поле вокруг значка должно быть одинаковым и снаружи, и в
вырезах.  Простое увеличение силуэта давало неровное поле, поэтому
основа растрируется, буферизуется и снова превращается в контур.

Основа (белая копия значка) лежит в ``<defs>`` того же файла, поэтому
отступ можно пересчитать в любой момент, не правя рисунок вручную:

    python3 tools/build_icon_masks.py --dry-run
    python3 tools/build_icon_masks.py --margin 0.1667
    python3 tools/build_icon_masks.py --init bridge bridge_rail
    python3 tools/build_icon_masks.py --rebase

Основа маски — сам рисунок значка, а не старая белая копия с вшитой
обводкой: ``--rebase`` пересобирает основу из значка, если рисунок
изменился.

Отступ считается от размера значка в шаблоне ``automap.qgz``: при
уменьшении значка поле тоже должно уменьшиться.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.PyQt.QtCore import QRectF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from qgis.PyQt.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "resources" / "icons"
TEMPLATE = ROOT / "automap.qgz"

# Отступ маски от контура значка, мм.  Поле одинаково с обеих сторон
# контура.
MARGIN_MM = 0.25 / 1.5
# Размер значка, для которого считается отступ, если в шаблоне нет
# symbol layer с этим значком.
NOMINAL_SIZE_MM = 3.4
# Растр, в котором растрируется основа.
PIXELS = 2048
# Упрощение контура, доля пикселя.
SIMPLIFY_PX = 1.2
# Упрощение основания: она нужна только как образец для отступа, и
# мелкие детали на ней не видны.
BASE_SIMPLIFY_PX = 4.0
# Идентификатор основания в <defs>.
BASE_ID = "base"


def template_sizes(template=None):
    """Максимальный размер значка (мм) для каждого файла маски.

    Размер берётся у всех SvgMarker шаблона, а не только у символов с
    готовой маской: новому значку маску ещё предстоит собрать.
    """
    import zipfile

    with zipfile.ZipFile(template or TEMPLATE) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".qgs"))
        root = ET.fromstring(archive.read(name).decode("utf-8"))
    sizes = {}
    for symbol_layer in root.iter("layer"):
        if symbol_layer.get("class") != "SvgMarker":
            continue
        options = {
            option.get("name"): option.get("value")
            for option in symbol_layer.findall("Option/Option")
        }
        icon = Path(options.get("name") or "").name
        if not icon:
            continue
        try:
            size = float(options.get("size") or 0)
        except ValueError:
            continue
        if not size:
            continue
        mask_file = (
            icon if icon.endswith("_m.svg")
            else Path(icon).stem + "_m.svg"
        )
        sizes[mask_file] = max(sizes.get(mask_file, 0.0), size)
    return sizes


def svg_size(root):
    width = root.get("width") or "0"
    height = root.get("height") or "0"
    width = float("".join(c for c in width if c.isdigit() or c == ".") or 0)
    height = float("".join(c for c in height if c.isdigit() or c == ".") or 0)
    viewbox = root.get("viewBox")
    if viewbox:
        parts = [float(value) for value in viewbox.replace(",", " ").split()]
        if len(parts) == 4:
            width = width or parts[2]
            height = height or parts[3]
    if not viewbox:
        viewbox = f"0 0 {width:g} {height:g}"
    return width, height, viewbox


def base_data(root):
    """Контур основания из <defs>.

    У SVG свой namespace, поэтому ищем по имени тега, а не по точному
    совпадению.
    """
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path" and element.get("id") == BASE_ID:
            return element.get("d") or ""
    return ""


def render_file(path, pixels=PIXELS):
    """Растр белого контура из файла SVG."""
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        raise SystemExit(f"неверный SVG: {path}")
    image = QImage(pixels, pixels, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    stride = image.bytesPerLine()
    bits = image.constBits()
    bits.setsize(image.byteCount())
    import numpy

    raw = numpy.frombuffer(bytes(bits), dtype="uint8").reshape(
        pixels, stride,
    )
    return (raw[:, : pixels * 4].reshape(pixels, pixels, 4)[:, :, 3] > 16)


def render(text, width, height, viewbox, pixels=PIXELS):
    """Растр белого контура."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" '
        f'height="{height:g}" viewBox="{viewbox}">'
        f'<path fill="#ffffff" fill-rule="evenodd" d="{text}"/>'
        "</svg>"
    )
    renderer = QSvgRenderer(bytearray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise SystemExit("неверный SVG")
    image = QImage(pixels, pixels, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    stride = image.bytesPerLine()
    bits = image.constBits()
    bits.setsize(image.byteCount())
    import numpy

    raw = numpy.frombuffer(bytes(bits), dtype="uint8").reshape(
        pixels, stride,
    )
    return (raw[:, : pixels * 4].reshape(pixels, pixels, 4)[:, :, 3] > 16)


def polygonize(mask):
    """Вектор контура растра."""
    from osgeo import gdal, ogr, osr

    driver = gdal.GetDriverByName("MEM")
    dataset = driver.Create("", PIXELS, PIXELS, 1, gdal.GDT_Byte)
    band = dataset.GetRasterBand(1)
    band.WriteArray(mask.astype("uint8"))
    band.SetNoDataValue(0)
    vector = ogr.GetDriverByName("Memory").CreateDataSource("")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    layer = vector.CreateLayer("mask", srs, ogr.wkbPolygon)
    gdal.Polygonize(band, band, layer, 0, [], callback=None)
    geometry = None
    for feature in layer:
        part = feature.GetGeometryRef()
        if part is None or part.IsEmpty():
            continue
        # Копия: геометрия feature живёт до конца итерации.
        if geometry is None:
            geometry = part.Clone()
        else:
            merged = geometry.Union(part)
            if merged is not None:
                geometry = merged
    dataset = None
    vector = None
    return geometry


def geometry_data(geometry, scale):
    """Контур геометрии в виде данных пути SVG.

    Команда ``L`` не пишется: координаты идут подряд, SVG так понимает
    такой путь, а файл получается заметно короче.
    """
    from osgeo import ogr

    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    polygons = []
    if kind == ogr.wkbPolygon:
        polygons = [geometry]
    elif kind == ogr.wkbMultiPolygon:
        polygons = [
            geometry.GetGeometryRef(index)
            for index in range(geometry.GetGeometryCount())
        ]
    pieces = []
    for polygon in polygons:
        for index in range(polygon.GetGeometryCount()):
            ring = polygon.GetGeometryRef(index)
            points = []
            for point in range(ring.GetPointCount()):
                x, y = ring.GetPoint(point)[:2]
                points.append(f"{x / scale:.2f} {y / scale:.2f}")
            if points:
                pieces.append("M " + " ".join(points) + " Z")
    return " ".join(pieces)


def icon_base(path, width, height, viewbox):
    """Силуэт значка для новой маски.

    Основа новой маски — видимый силуэт самого значка: та же геометрия,
    что рисует карта.  У готовых масок основа уже лежит в ``<defs>``.
    """
    geometry = polygonize(render_file(path))
    if geometry is None or geometry.IsEmpty():
        return ""
    simplified = geometry.SimplifyPreserveTopology(
        BASE_SIMPLIFY_PX * width / PIXELS,
    )
    return geometry_data(simplified, PIXELS / width)


def write_mask(path, width, height, viewbox, base, data):
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width:g}" height="{height:g}" viewBox="{viewbox}">\n'
        "  <defs>\n"
        f'    <path id="{BASE_ID}" fill-rule="evenodd" d="{base}"/>\n'
        "  </defs>\n"
        f'  <path fill="#ffffff" fill-rule="evenodd" d="{data}"/>\n'
        "</svg>\n"
    )
    path.write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--margin", type=float, default=MARGIN_MM,
                        help="отступ маски от контура значка, мм")
    parser.add_argument("--init", nargs="+", metavar="ICON",
                        help="создать маски для значков, у которых их ещё нет")
    parser.add_argument("--rebase", nargs="*", metavar="ICON", default=None,
                        help="пересобрать основу из значка (без имён — всё)")
    parser.add_argument("--template", help="шаблон для размеров значков")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not QGuiApplication.instance():
        QGuiApplication([])
    rebase = args.rebase
    if rebase is not None and not rebase:
        rebase = [
            path.name[:-6] for path in sorted(ICONS.glob("*_m.svg"))
            if (ICONS / (path.name[:-6] + ".svg")).exists()
        ]
    for icon_name in rebase or []:
        stem = icon_name[:-4] if icon_name.endswith(".svg") else icon_name
        icon = ICONS / (stem + ".svg")
        if not icon.exists():
            print("нет значка:", icon.name)
            continue
        width, height, viewbox = svg_size(ET.parse(icon).getroot())
        base = icon_base(icon, width, height, viewbox)
        if not base:
            print("пустой значок:", icon.name)
            continue
        print("основа из значка:", icon.name)
        if not args.dry_run:
            write_mask(ICONS / (stem + "_m.svg"), width, height, viewbox,
                       base, "")
    for icon_name in args.init or []:
        stem = icon_name[:-4] if icon_name.endswith(".svg") else icon_name
        mask_path = ICONS / (stem + "_m.svg")
        if mask_path.exists():
            print("маска уже есть:", mask_path.name)
            continue
        icon = ICONS / (stem + ".svg")
        if not icon.exists():
            print("нет значка:", icon.name)
            continue
        width, height, viewbox = svg_size(ET.parse(icon).getroot())
        base = icon_base(icon, width, height, viewbox)
        if not base:
            print("пустой значок:", icon.name)
            continue
        print("основа маски:", mask_path.name)
        if not args.dry_run:
            # Контур маски дорисует обычный проход ниже.
            write_mask(mask_path, width, height, viewbox, base, "")

    sizes = template_sizes(args.template)
    changed = 0
    for path in sorted(ICONS.glob("*_m.svg")):
        root = ET.parse(path).getroot()
        width, height, viewbox = svg_size(root)
        base = base_data(root)
        if not base:
            print("нет основания в <defs>:", path.name)
            continue
        size_mm = sizes.get(path.name)
        if size_mm:
            margin = args.margin * width / size_mm
        else:
            margin = width * args.margin / NOMINAL_SIZE_MM
        distance = margin * PIXELS / width
        geometry = polygonize(render(base, width, height, viewbox))
        if geometry is None or geometry.IsEmpty():
            print("пустое основание:", path.name)
            continue
        expanded = geometry.Buffer(distance, 8)
        simplified = expanded.SimplifyPreserveTopology(
            SIMPLIFY_PX * width / PIXELS,
        )
        data = geometry_data(simplified, PIXELS / width)
        if not args.dry_run:
            write_mask(path, width, height, viewbox, base, data)
        changed += 1
        print(
            f"{path.name}: размер {size_mm or '-'} мм, "
            f"отступ {margin:.1f} единиц ({args.margin:.4f} мм)"
        )
    print(f"файлов: {changed}")


if __name__ == "__main__":
    sys.exit(main())
