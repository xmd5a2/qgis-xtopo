#!/usr/bin/env python3
"""Переводит упакованные слои шаблона на MultiLineString/MultiPolygon.

Дороги, железные дороги и простые линии после упаковки содержат
MultiLineString, площадные слои — MultiPolygon.  Слои шаблона, которые
читают эти файлы, должны объявлять тот же тип геометрии.

Скрипт правит `automap.qgz` и идемпотентен.

Запуск: `python3 tools/road_multiline.py`
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

LINE_LAYERS = (
    "highway main",
    "highway main overview",
    "[blocking] highway main",
    "[blocking] highway main overview",
    "path",
    "track",
    "[blocking] track",
    "railway all",
    "[blocking] railway_all",
    "stream",
    "tree row",
    "power minor line",
    "embankment",
    "cutline",
)

POLYGON_LAYERS = (
    "wood",
    "grass",
    "farmland",
    "scrub",
    "landuse residential",
    "[blocking] landuse residential",
)


def layer_region(text, name):
    """Границы `<maplayer>` с указанным именем."""
    marker = f"<layername>{name}</layername>"
    position = text.find(marker)
    if position < 0:
        return None
    start = text.rfind("<maplayer", 0, position)
    end = text.find("</maplayer>", position)
    if start < 0 or end < 0:
        return None
    return start, end + len("</maplayer>")


def patch_layer(block, source, target):
    """Меняет тип геометрии в одном слое."""
    changed = False
    markup = f'wkbType="{source}"'
    if markup in block:
        block = block.replace(markup, f'wkbType="{target}"')
        changed = True
    datasource = f"geometrytype={source}"
    if datasource in block:
        block = block.replace(datasource, f"geometrytype={target}")
        changed = True
    return block, changed


def patch_project(text):
    changed = []
    targets = [
        (name, "LineString", "MultiLineString") for name in LINE_LAYERS
    ]
    targets.extend(
        (name, "Polygon", "MultiPolygon") for name in POLYGON_LAYERS
    )
    for name, source, target in targets:
        region = layer_region(text, name)
        if region is None:
            continue
        start, end = region
        block, layer_changed = patch_layer(
            text[start:end], source, target
        )
        if layer_changed:
            text = text[:start] + block + text[end:]
            changed.append(name)
    return text, changed


def main():
    with zipfile.ZipFile(TEMPLATE) as archive:
        names = archive.namelist()
        contents = {name: archive.read(name) for name in names}
    project = next(
        (name for name in names if name.endswith(".qgs")), None
    )
    if project is None:
        print("В шаблоне нет файла проекта", file=sys.stderr)
        return 1
    text = contents[project].decode("utf-8")
    patched, changed = patch_project(text)
    if not changed:
        print("Упакованные слои уже MultiLineString/MultiPolygon")
        return 0
    contents[project] = patched.encode("utf-8")
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    print("MultiLineString/MultiPolygon: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
