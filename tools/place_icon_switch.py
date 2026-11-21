#!/usr/bin/env python3
"""Убирает переключатель значков населённых пунктов из шаблона.

В символах слоёв ``places main`` и ``places main major`` было
data-defined свойство ``enabled`` с выражением
``IF(@use_place_icon='true',1,0)``.  Переменная проекта всегда равна
``true``, поэтому выражение ничего не переключало, но вычислялось на
каждый объект.  Скрипт удаляет выражения и саму переменную; значки
остаются включёнными.

Скрипт идемпотентен: повторный запуск ничего не меняет.

Запуск: `python3 tools/place_icon_switch.py`
"""

import os
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"
VARIABLE = "use_place_icon"


def patch_project(root):
    """Удаляет выражения и переменную, возвращает число правок."""
    parents = {child: parent for parent in root.iter() for child in parent}
    removed = 0
    for option in list(root.iter("Option")):
        if option.get("name") != "expression":
            continue
        if VARIABLE not in (option.get("value") or ""):
            continue
        parent = parents.get(option)
        if parent is not None:
            parent.remove(option)
            removed += 1

    for properties in root.iter():
        names = properties.find("variableNames")
        values = properties.find("variableValues")
        if names is None or values is None:
            continue
        name_items = names.findall("value")
        value_items = values.findall("value")
        labels = [item.text for item in name_items]
        if VARIABLE not in labels:
            continue
        index = labels.index(VARIABLE)
        names.remove(name_items[index])
        values.remove(value_items[index])
        removed += 1
    return removed


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
    removed = patch_project(root)
    if not removed:
        print("Переключатель значков уже убран")
        return 0

    contents[project] = ElementTree.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    os.replace(temporary, TEMPLATE)
    print("Убрано правок: %d" % removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
