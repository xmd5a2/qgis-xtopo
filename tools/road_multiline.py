#!/usr/bin/env python3
"""Переводит упакованные дорожные слои шаблона на MultiLineString.

После упаковки `highway_main.sqlite`, `path.sqlite`, `track.sqlite` и
`railway_all.sqlite` содержат именованные цепочки и упакованные
безымянные дороги в виде MultiLineString.  Слои, которые читают эти
файлы, должны объявлять тот же тип геометрии.

Скрипт правит `automap.qgz` и идемпотентен.

Запуск: `python3 tools/road_multiline.py`
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

LAYERS = (
    "highway main",
    "highway main overview",
    "[blocking] highway main",
    "[blocking] highway main overview",
    "path",
    "track",
    "[blocking] track",
    "railway all",
    "[blocking] railway_all",
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


def patch_layer(block):
    """Меняет LineString на MultiLineString в одном слое."""
    changed = False
    if 'wkbType="LineString"' in block:
        block = block.replace(
            'wkbType="LineString"', 'wkbType="MultiLineString"'
        )
        changed = True
    if "geometrytype=LineString" in block:
        block = block.replace(
            "geometrytype=LineString", "geometrytype=MultiLineString"
        )
        changed = True
    return block, changed


def patch_project(text):
    changed = []
    for name in LAYERS:
        region = layer_region(text, name)
        if region is None:
            continue
        start, end = region
        block, layer_changed = patch_layer(text[start:end])
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
        print("Дорожные слои уже MultiLineString")
        return 0
    contents[project] = patched.encode("utf-8")
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    print("MultiLineString: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
