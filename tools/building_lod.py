#!/usr/bin/env python3
"""Убирает мелкие здания на низких уровнях детализации.

Слой `building` содержит больше миллиона полигонов.  На 1:50 000 они
занимают почти половину кадра и рисуются дольше остальных слоёв.  При
этом половина зданий меньше пикселя: на таком масштабе их не видно, но
провайдер всё равно читает и отдаёт их рендереру.

Скрипт оборачивает три правила слоя в одно родительское `lod` с
проверкой `@level_of_detail`: чем ниже детализация, тем крупнее должны
быть здания.  Родительский фильтр вычисляется один раз на объект, а
встроенное `ELSE` внутри остаётся бесплатным.  Если повесить то же
условие на само правило `else`, QGIS считает выражение на каждый объект
и слой становится медленнее.

Правила `abandoned` и `ruins` не фильтруются — таких объектов мало, и
они важны как ориентиры.  При `@level_of_detail >= 6` показываются все
здания — на тех масштабах, где слой вообще виден.  На 1:30 000 и
крупнее фильтр тоже не действует: там объектов мало и важна каждая
деталь, а подписи зданий появляются только с 1:20 000.

Порог 100 м² на обычной детализации 4 меняет 0,42 % пикселей кадра
1:50 000 — в пределах одного процента.  Ниже пороги растут, выше —
падают; на 6 не действуют.

Правка нужна один раз: готовые проекты не чинятся, они готовятся
заново из исправленного шаблона.

Скрипт идемпотентен: повторный запуск ничего не меняет.

Запуск: `python3 tools/building_lod.py`
"""

import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

# Минимальная площадь в м²: здание видно, если его площадь не меньше.
# Детализация 6 показывает всё, 5 — от 50 м², 4 — от 100 м² (обычный
# уровень), 3 — от 175 м², 2 — от 300 м².
THRESHOLDS = {6: 0, 5: 50, 4: 100, 3: 175, 2: 300}

# На таких масштабах и крупнее фильтр не нужен: зданий в кадре мало.
# Запас к 1:30 000 страхует округление масштаба при рендере.
SCALE_ESCAPE = 32000

ABANDONED = (
    '("abandoned" = \'yes\' OR "disused" = \'yes\') AND '
    '("building" != \'collapsed\' OR "ruins" != \'yes\')'
)
RUINS = (
    '"ruins" = \'yes\' OR "historic" = \'ruins\' OR '
    '"building" = \'collapsed\''
)


def visibility():
    """Выражение видимости зданий по уровню детализации.

    Шестой уровень и крупные масштабы обходятся без порога площади:
    на шестом порог равен нулю, поэтому ``area_m2 >= 0`` верно для
    любого здания.  Остальным уровням соответствует порог из
    ``THRESHOLDS``.  Сравнение с порогом одно; ``CASE`` выбирает только
    число, поэтому выражение дешевле цепочки из пяти условий.
    """
    cases = [
        "WHEN coalesce(@level_of_detail, 6) >= 6 THEN 0",
        f"WHEN @level_of_detail >= 5 THEN {THRESHOLDS[5]}",
        f"WHEN @level_of_detail >= 4 THEN {THRESHOLDS[4]}",
        f"WHEN @level_of_detail >= 3 THEN {THRESHOLDS[3]}",
        f"ELSE {THRESHOLDS[2]}",
    ]
    return (
        f"(@map_scale <= {SCALE_ESCAPE} OR "
        f'coalesce("area_m2", 0) >= '
        f"CASE {' '.join(cases)} END)"
    )


def layer_rules(root):
    """Элемент `<rules>` рендерера слоя `building`."""
    layer = next(
        element for element in root.iter("maplayer")
        if element.findtext("layername") == "building"
    )
    renderer = layer.find("renderer-v2")
    if renderer is None:
        raise SystemExit("У слоя building нет renderer-v2")
    rules = renderer.find("rules")
    if rules is None:
        raise SystemExit("У слоя building нет правил рендерера")
    return rules


def restore_child_filters(rules):
    """Возвращает исходные фильтры трём правилам слоя."""
    expected = {
        "abandoned": ABANDONED,
        "ruins": RUINS,
        "else": "ELSE",
    }
    changed = False
    for rule in rules.findall("rule"):
        label = rule.get("label")
        if label not in expected:
            continue
        if rule.get("filter") != expected[label]:
            if label == "else":
                rule.set("filter", "ELSE")
            else:
                rule.set("filter", expected[label])
            changed = True
    return changed


def patch_root(root):
    """Оборачивает правила в родительское. Возвращает изменён ли проект."""
    rules = layer_rules(root)
    parent = next(
        (
            rule for rule in rules.findall("rule")
            if rule.get("label") == "lod"
        ),
        None,
    )
    changed = restore_child_filters(parent if parent is not None else rules)
    if parent is None:
        parent = ET.Element(
            "rule",
            {
                "key": "{" + str(uuid.uuid4()) + "}",
                "label": "lod",
                "filter": visibility(),
            },
        )
        for rule in rules.findall("rule"):
            rules.remove(rule)
            parent.append(rule)
        rules.append(parent)
        changed = True
    elif parent.get("filter") != visibility():
        parent.set("filter", visibility())
        changed = True
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
    root = ET.fromstring(contents[project].decode("utf-8"))
    if not patch_root(root):
        print("Слой building уже ограничен по уровню детализации")
        return 0
    contents[project] = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    print("building: добавлен фильтр по @level_of_detail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
