#!/usr/bin/env python3
"""Скрывает мелкие озёра на обзорных масштабах по площади.

Слой ``water`` уже ограничивает воду по ``area_m2``, но на 1:300 000 и
мельче пороги измеряются сотнями квадратных метров: на охвате Исландии
это оставляет почти все 28 тысяч полигонов.  Скрипт оставляет прежнее
правило 1:300 000–1:400 000 и добавляет для 1:400 000 и мельче полосы
1:400 000–1:1M, 1:1M–1:3M и 1:3M+ с порогами по геодезической площади.
Те же полосы, кроме 1:3M+, получает ``water intermittent``.

Порог зависит от детализации; LOD 6 не обнуляется.  Подписи воды
получают тот же порог, чтобы названия скрытых озёр не оставались на
карте.

Запуск: ``python3 tools/water_lod.py [--dry-run]``.
"""

import argparse
import copy
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

# Минимальная площадь в м² по полосам масштаба.  Ключ — LOD.
THRESHOLDS = {
    400000: {2: 320000, 3: 224000, 4: 160000, 5: 96000, 6: 64000},
    1000000: {
        2: 2000000, 3: 1400000, 4: 1000000, 5: 600000, 6: 400000,
    },
    3000000: {
        2: 18000000, 3: 12600000, 4: 9000000, 5: 5400000, 6: 3600000,
    },
}
BANDS = (
    (400000, 1000000, "400000 - 1000000"),
    (1000000, 3000000, "1000000 - 3000000"),
    (3000000, None, "3000000 - 0"),
)
INTERMITTENT_BANDS = BANDS[:2]

WATER_NAMES_BASE = (
    "natural IS NOT NULL OR landuse IS NOT NULL OR "
    "waterway != 'riverbank' OR water != 'river'"
)


def area_filter(values):
    """Фильтр по геодезической площади для одной полосы масштаба."""
    cases = [
        f"WHEN coalesce(@level_of_detail, 4) >= 6 "
        f"THEN {values[6]}",
        f"WHEN coalesce(@level_of_detail, 4) >= 5 "
        f"THEN {values[5]}",
        f"WHEN coalesce(@level_of_detail, 4) >= 4 "
        f"THEN {values[4]}",
        f"WHEN coalesce(@level_of_detail, 4) >= 3 "
        f"THEN {values[3]}",
        f"ELSE {values[2]}",
    ]
    return f'coalesce("area_geodesic", 0) >= CASE {" ".join(cases)} END'


def layer_element(root, name):
    return next(
        element for element in root.iter("maplayer")
        if element.findtext("layername") == name
    )


def overview_rule(rules, max_denominator):
    for rule in rules.findall("rule"):
        if rule.get("scalemindenom") != "300000":
            continue
        if rule.get("scalemaxdenom") != max_denominator:
            continue
        return rule
    raise SystemExit("не найдено правило 1:300 000+")


def new_key():
    return "{" + str(uuid.uuid4()) + "}"


def symbol_map(layer):
    renderer = layer.find("renderer-v2")
    if renderer is None:
        raise SystemExit("у слоя нет renderer-v2")
    symbols = renderer.find("symbols")
    if symbols is None:
        raise SystemExit("у слоя нет symbols")
    return symbols


def clone_symbol(symbols, base):
    """Копия символа с новым именем и новыми id слоёв."""
    names = {int(symbol.get("name")) for symbol in symbols.findall("symbol")
             if (symbol.get("name") or "").isdigit()}
    name = str(max(names, default=-1) + 1)
    clone = copy.deepcopy(base)
    clone.set("name", name)
    for layer in clone.iter("layer"):
        layer.set("id", "{" + str(uuid.uuid4()) + "}")
    symbols.append(clone)
    return name


def add_band_rules(layer, bands):
    renderer = layer.find("renderer-v2")
    rules = renderer.find("rules")
    if rules is None:
        raise SystemExit("у слоя нет правил рендерера")
    symbols = symbol_map(layer)
    target = next(
        rule for rule in rules.findall("rule")
        if rule.get("scalemindenom") == "300000"
    )
    target.set("scalemaxdenom", str(bands[0][0]))
    target.set("label", f"300000 - {bands[0][0]}")
    base_symbol = next(
        symbol for symbol in symbols.findall("symbol")
        if symbol.get("name") == target.get("symbol")
    )
    for minimum, maximum, label in bands:
        values = THRESHOLDS[minimum]
        rule = next(
            (rule for rule in rules.findall("rule")
             if rule.get("scalemindenom") == str(minimum)
             and rule.get("scalemaxdenom") == (
                 None if maximum is None else str(maximum))),
            None,
        )
        if rule is None:
            rule = ET.Element("rule", {
                "scalemindenom": str(minimum),
                "key": new_key(),
                "label": label,
                "symbol": clone_symbol(symbols, base_symbol),
            })
            if maximum is not None:
                rule.set("scalemaxdenom", str(maximum))
            rules.append(rule)
        rule.set("filter", area_filter(values))


def add_label_condition(root, name, max_denominator, base, scale_limit,
                        values):
    layer = layer_element(root, name)
    rules = layer.find("labeling/rules")
    if rules is None:
        raise SystemExit(f"у слоя {name} нет правил подписей")
    rule = overview_rule(rules, max_denominator)
    condition = area_filter(values)
    if scale_limit is None:
        active = condition
    else:
        active = f"(@map_scale < {scale_limit} OR {condition})"
    expected = f"({base}) AND {active}" if base else active
    if rule.get("filter") != expected:
        rule.set("filter", expected)
        return True
    return False


def patch_root(root):
    changed = False
    water = layer_element(root, "water")
    before = ET.tostring(water)
    add_band_rules(water, BANDS)
    changed = ET.tostring(water) != before or changed

    intermittent = layer_element(root, "water intermittent")
    before = ET.tostring(intermittent)
    add_band_rules(intermittent, INTERMITTENT_BANDS)
    changed = ET.tostring(intermittent) != before or changed

    changed = add_label_condition(
        root, "water names continental", None, WATER_NAMES_BASE,
        3000000, THRESHOLDS[3000000],
    ) or changed
    changed = add_label_condition(
        root, "water intermittent", "1000000", "", 400000,
        THRESHOLDS[400000],
    ) or changed
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
        print("Вода уже ограничена по площади")
        return 0
    if args.dry_run:
        print("Будут добавлены фильтры воды по геодезической площади")
        return 0
    contents[project] = ET.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    print("Вода: добавлены фильтры по геодезической площади")
    return 0


if __name__ == "__main__":
    sys.exit(main())
