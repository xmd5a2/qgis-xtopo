#!/usr/bin/env python3
"""Обновляет маски значков (MaskMarker) в automap.qgz.

QGIS предупреждает, если разные источники маскируют один слой разными
наборами symbol layer id. Шаблон хранит для всех источников один и тот же
набор: у каждой цели маскируются все её линейные symbol layer (границы).
Заливки (`SimpleFill`, `SVGFill`, `CentroidFill`) целями маски не
добавляются: значок на площади не должен оставлять в ней дыру. Уже
записанные заливки сохраняются, пока их id существуют в рендерере.

Скрипт читает цели из существующих масок (устаревшие и точечные слои
отбрасываются), при необходимости добавляет отсутствующие линейные
symbol layer id и записывает одинаковый список во все MaskMarker. Проект
не открывается в QGIS: слои шаблона невалидны, и QGIS сохранил бы их
исходный XML вместо изменённых рендереров.

    python3 tools/update_icon_masks.py --dry-run
    python3 tools/update_icon_masks.py
    python3 tools/update_icon_masks.py --output /tmp/automap.qgz
"""

import argparse
import os
import sys
import uuid
import zipfile
from xml.etree import ElementTree

DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "automap.qgz",
)


# Symbol layer, рисующие границы. Заливки в маски не добавляются:
# значок не должен оставлять дыру в площади.
LINE_CLASSES = {
    "SimpleLine",
    "MarkerLine",
    "ArrowLine",
    "InterpolatedLine",
    "HashLine",
}


def parse_mask_pairs(value):
    """Пары (id слоя, id symbol layer) из mask_symbollayers."""
    pairs = []
    if not value:
        return pairs
    if "," in value:
        for part in value.split(";"):
            if part and "," in part:
                layer_id, symbol_layer_id = part.split(",", 1)
                pairs.append((layer_id, symbol_layer_id))
    else:
        parts = value.split(";")
        for index in range(0, len(parts) - 1, 2):
            pairs.append((parts[index], parts[index + 1]))
    return pairs


def parse_mask_targets(value):
    """id целевых слоёв из значения mask_symbollayers (новый и старый формат)."""
    return {layer_id for layer_id, _ in parse_mask_pairs(value)}


def renderer_layers(layer):
    renderer = layer.find("renderer-v2")
    if renderer is None:
        return []
    return list(renderer.iter("layer"))


def mask_option(symbol_layer):
    for option in symbol_layer.findall("./Option/Option"):
        if option.get("name") == "mask_symbollayers":
            return option
    return None


def collect_targets(layers, mask_layers):
    target_ids = set()
    for symbol_layer in mask_layers:
        option = mask_option(symbol_layer)
        if option is not None:
            target_ids |= parse_mask_targets(option.get("value") or "")
    targets = []
    for layer_id in target_ids:
        layer = layers.get(layer_id)
        if layer is None:
            continue
        if layer.get("geometry") not in ("Line", "Polygon"):
            continue
        targets.append(layer)
    names = {layer.findtext("layername") for layer in targets}
    if any("isolines" in (name or "") for name in names):
        for layer in layers.values():
            if (
                "isolines" in (layer.findtext("layername") or "")
                and layer not in targets
                and layer.get("geometry") == "Line"
            ):
                targets.append(layer)
    targets.sort(key=lambda layer: (
        layer.findtext("layername") or "",
        layer.findtext("id"),
    ))
    return targets


def update(root, dry_run=False):
    project_layers = root.find("projectlayers")
    layers = {
        layer.findtext("id"): layer
        for layer in project_layers.findall("maplayer")
    }
    assigned = 0
    for layer in layers.values():
        for symbol_layer in renderer_layers(layer):
            if not symbol_layer.get("id"):
                symbol_layer.set("id", "{" + str(uuid.uuid4()) + "}")
                assigned += 1

    mask_layers = [
        symbol_layer
        for layer in layers.values()
        for symbol_layer in layer.iter("layer")
        if symbol_layer.get("class") == "MaskMarker"
    ]
    targets = collect_targets(layers, mask_layers)
    existing = {}
    for symbol_layer in mask_layers:
        option = mask_option(symbol_layer)
        if option is None:
            continue
        for layer_id, symbol_layer_id in parse_mask_pairs(
                option.get("value") or ""):
            existing.setdefault(layer_id, set()).add(symbol_layer_id)
    ids = {}
    for layer in targets:
        layer_id = layer.findtext("id")
        renderer = {
            symbol_layer.get("id")
            for symbol_layer in renderer_layers(layer)
            if symbol_layer.get("id")
        }
        borders = {
            symbol_layer.get("id")
            for symbol_layer in renderer_layers(layer)
            if symbol_layer.get("id")
            and symbol_layer.get("class") in LINE_CLASSES
        }
        ids[layer_id] = sorted((existing.get(layer_id, set()) & renderer)
                               | borders)
    parts = []
    for layer in targets:
        layer_id = layer.findtext("id")
        for symbol_layer_id in ids[layer_id]:
            parts.extend((layer_id, symbol_layer_id))
    value = ";".join(parts)

    if not dry_run:
        for symbol_layer in mask_layers:
            option = mask_option(symbol_layer)
            if option is None:
                parent = symbol_layer.find("./Option")
                if parent is None:
                    parent = ElementTree.SubElement(symbol_layer, "Option")
                option = ElementTree.SubElement(
                    parent, "Option", {"name": "mask_symbollayers"})
            option.set("type", "QString")
            option.set("value", value)

    return {
        "assigned": assigned,
        "sources": len(mask_layers),
        "targets": len(targets),
        "refs": len(parts) // 2,
        "length": len(value),
        "value": value,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", help="куда записать qgz (по умолчанию на месте)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with zipfile.ZipFile(args.template) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    qgs = next(data for info, data in entries if info.filename.endswith(".qgs"))
    root = ElementTree.fromstring(qgs)
    stats = update(root, dry_run=args.dry_run)
    print(
        f"источников {stats['sources']}, целей {stats['targets']}, "
        f"ссылок {stats['refs']}, назначено id {stats['assigned']}"
    )
    if args.dry_run:
        return

    # Без пересборки zip: меняем только qgs, остальные вложения копируем.
    output = args.output or args.template
    temporary = output + ".tmp"
    with zipfile.ZipFile(temporary, "w") as archive:
        for info, data in entries:
            if info.filename.endswith(".qgs"):
                data = ElementTree.tostring(
                    root, encoding="utf-8", xml_declaration=True)
            archive.writestr(info, data)
    os.replace(temporary, output)
    print(f"записан {output}")


if __name__ == "__main__":
    sys.exit(main())
