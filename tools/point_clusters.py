#!/usr/bin/env python3
"""Убирает кластеризацию точек в шаблоне.

``QgsPointClusterRenderer`` на каждом кадре группирует точки, хотя при
крупных масштабах кластеры почти не образуются.  Внешний рендерер
хранит внутри обычный ``RuleRenderer``; скрипт разворачивает его,
сохраняя правила, символы и подписи.  Точки после этого рисуются
отдельно, без кластерного символа.

Скрипт идемпотентен: повторный запуск ничего не меняет.

Запуск: `python3 tools/point_clusters.py`
"""

import os
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"


def patch_project(root):
    """Разворачивает вложенный рендерер у всех кластерных слоёв."""
    changed = []
    for layer in root.iter("maplayer"):
        outer = layer.find("renderer-v2")
        if outer is None or outer.get("type") != "pointCluster":
            continue
        inner = outer.find("renderer-v2")
        if inner is None:
            continue
        index = list(layer).index(outer)
        outer.remove(inner)
        layer.remove(outer)
        layer.insert(index, inner)
        changed.append(layer.findtext("layername") or "")
    return changed


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
    root = ElementTree.fromstring(contents[project])
    changed = patch_project(root)
    if not changed:
        print("Кластеризация уже убрана")
        return 0

    contents[project] = ElementTree.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    os.replace(temporary, TEMPLATE)
    print("Кластеризация убрана: %d слоёв" % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
