#!/usr/bin/env python3
"""Переносит подавление значков из выражений шаблона в данные.

Правила слоёв POI в `automap.qgz` ищут подавителя через
`overlay_nearest`: выражение вызывается на каждый объект слоя, и
радиус поиска растёт вместе с масштабом.  На мелком масштабе слой
`peak` рисуется десятки секунд.

Пороги подавления считаются при подготовке данных (`core/poi_icons.py`)
и кладутся в поле `hide` слоя, а правило читает поле.  Скрипт
правит фильтры правил в `automap.qgz`; фильтр подписи меняется тем
же способом — подпись скрывается вместе со значком.

Скрипт идемпотентен: повторный запуск на уже исправленном шаблоне
ничего не меняет.  Если правило содержит условие подавления, которого
нет в коде (шаблон правили в другое время), скрипт об этом сообщает:
такой шаблон надо пересобрать из исходного.

Запуск: `python3 tools/suppression_to_data.py`
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import poi_icons  # noqa: E402

# Слои, у которых подавление отключено вручную: их правила не содержат
# условия подавления, и скрипт их не трогает.
NO_SUPPRESSION_LAYERS = {"places main"}


def escape(text):
    """Экранирование значения XML-атрибута, как это делает QGIS."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def layer_regions(text):
    """Границы элементов <maplayer> шаблона."""
    regions = []
    position = 0
    while True:
        start = text.find("<maplayer", position)
        if start < 0:
            break
        end = text.find("</maplayer>", start)
        if end < 0:
            break
        end += len("</maplayer>")
        regions.append((start, end))
        position = end
    return regions


def has_condition(filter_text, suppression):
    """Есть ли в фильтре условие подавления с таким выражением."""
    condition = f"NOT ({suppression})"
    return filter_text == condition or filter_text.endswith(
        f") AND {condition}"
    )


def collect_patches(layers, bands):
    """Список правок: имя слоя, старый фильтр, новый фильтр.

    Возвращает (правки, расхождения).  Расхождение — правило, у которого
    в фильтре нет условия подавления, которое есть в коде: значит,
    шаблон правили в другое время и его надо пересобрать.
    """
    patches = []
    problems = []
    for layer in layers:
        for leaf, label in _rules(layer):
            old = _suppression(layer, leaf, label, bands, old=True)
            if old == "false":
                continue
            new = _suppression(layer, leaf, label, bands, old=False)
            if has_condition(leaf.filter, old):
                base = poi_icons.base_filter(leaf.filter, old)
                patches.append((
                    layer.name, leaf.filter,
                    poi_icons.data_filter(base, new),
                ))
            elif has_condition(leaf.filter, new):
                # Уже исправлено: фильтр должен совпадать с тем, что
                # даёт код, — иначе правило считает не то.
                base = poi_icons.base_filter(leaf.filter, new)
                if base is None or leaf.filter != poi_icons.data_filter(
                        base, new):
                    problems.append((layer.name, leaf.filter))
            elif layer.name not in NO_SUPPRESSION_LAYERS:
                problems.append((layer.name, leaf.filter))
    return patches, problems


def _rules(layer):
    """Правила значка и правила подписи слоя."""
    for leaf in poi_icons.leaves(layer.root):
        yield leaf, None
    for label in layer.labels:
        yield label, label


def _suppression(layer, leaf, label, bands, old):
    """Условие подавления правила: прежнее или новое."""
    if label is None:
        if old:
            return poi_icons.suppression_expression(layer, leaf, bands)
        return poi_icons.suppression_data_expression(layer, leaf, bands)
    if old:
        return poi_icons.layer_suppression_expression(
            layer, bands, label.minimum, label.maximum,
        )
    return poi_icons.layer_suppression_data_expression(
        layer, bands, label.minimum, label.maximum,
    )


def patch_filter(region, old_filter, new_filter):
    """Заменяет фильтр правила на проверку данных.

    Ищем по тексту старого фильтра: ключи правил в шаблоне
    повторяются между слоями и внутри одного слоя (правило скопировано
    вместе с диапазоном масштаба), а текст фильтра у таких правил
    разный.  Совпадение текста означает и совпадение нового фильтра,
    поэтому заменяются все вхождения.
    """
    marker = f'filter="{escape(old_filter)}"'
    if marker not in region:
        raise ValueError(f"фильтр правила не найден: {old_filter[:60]}")
    return region.replace(
        marker, f'filter="{escape(new_filter)}"'
    )


def main():
    template = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "automap.qgz"
    with zipfile.ZipFile(template) as archive:
        names = archive.namelist()
        qgs = next(name for name in names if name.endswith(".qgs"))
        text = archive.read(qgs).decode("utf-8")
        others = {
            name: archive.read(name) for name in names if name != qgs
        }

    layers = poi_icons.read_template_poi()
    bands = poi_icons.canonical_bands(layers)
    patches, problems = collect_patches(layers, bands)
    if problems:
        for name, filter_text in problems[:5]:
            print(
                f"правило слоя {name} не содержит условия подавления "
                f"из кода: {filter_text[:80]}",
                file=sys.stderr,
            )
        raise SystemExit(
            f"расхождений с кодом: {len(problems)}; шаблон правили "
            "в другое время — пересоберите его из исходного"
        )

    by_layer = {}
    for name, old_filter, new_filter in patches:
        # Один и тот же старый фильтр встречается и у правила значка,
        # и у правила подписи: тогда и новый фильтр один.
        key = (name, old_filter)
        if key in by_layer and by_layer[key] != new_filter:
            raise SystemExit(
                f"у слоя {name} разные новые фильтры для одного старого"
            )
        by_layer[key] = new_filter
    grouped = {}
    for (name, old_filter), new_filter in by_layer.items():
        grouped.setdefault(name, []).append((old_filter, new_filter))

    regions = layer_regions(text)
    pieces = []
    position = 0
    changed = 0
    expected = sum(len(items) for items in grouped.values())
    for start, end in regions:
        region = text[start:end]
        name_match = re.search(r"<layername>(.*?)</layername>", region)
        if not name_match:
            continue
        name = name_match.group(1)
        for old_filter, new_filter in grouped.get(name, ()):
            region = patch_filter(region, old_filter, new_filter)
            changed += 1
        pieces.append(text[position:start])
        pieces.append(region)
        position = end
    pieces.append(text[position:])
    new_text = "".join(pieces)

    if changed != expected:
        raise SystemExit(
            f"правил исправлено {changed}, ожидалось {expected}"
        )
    if f"'{poi_icons.AUX_LAYER_NAME} " in new_text:
        raise SystemExit("в шаблоне остались ссылки на служебные слои")

    temporary = template.with_suffix(".qgz.new")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(qgs, new_text.encode("utf-8"))
        for name, data in others.items():
            archive.writestr(name, data)
    temporary.replace(template)
    print(f"исправлено правил: {changed}")


if __name__ == "__main__":
    main()
