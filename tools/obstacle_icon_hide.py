#!/usr/bin/env python3
"""Ставит значкам на объектах подавление по данным слоя.

Значок моста рисуется на линии, а значок кладбища — в центре
площадки: фильтр правила скрыл бы вместе со значком всю линию или
площадку.  Поэтому решение принимает свойство ``enabled`` символьного
слоя, а данные берутся из поля ``icon_hide`` слоя: в нём перечислены
диапазоны масштаба, где у объекта есть сосед того же слоя рядом.

Скрипт правит ``automap.qgz`` в репозитории (или файл, переданный
аргументом).  Он идемпотентен: повторный запуск ничего не меняет.

Запуск: ``python3 tools/obstacle_icon_hide.py``
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import poi_icons  # noqa: E402

# Символьный слой, который рисует значок объекта: у линии — маркер в
# середине, у площадки — метка в центре.
ICON_LAYER_CLASSES = ("MarkerLine", "CentroidFill")

DATA_DEFINED = """<data_defined_properties>
<Option type="Map">
<Option name="name" value="" type="QString" />
<Option name="properties" type="Map">
<Option name="enabled" type="Map">
<Option name="active" value="true" type="bool" />
<Option name="expression" value="{expression}" type="QString" />
<Option name="type" value="3" type="int" />
</Option>
</Option>
<Option name="type" value="collection" type="QString" />
</Option>
</data_defined_properties>"""

EMPTY_DATA_DEFINED = """<data_defined_properties>
<Option type="Map">
<Option name="name" value="" type="QString" />
<Option name="properties" />
<Option name="type" value="collection" type="QString" />
</Option>
</data_defined_properties>"""


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


def symbol_region(text, name):
    """Границы элемента <symbol name="...">."""
    pattern = re.compile(rf'<symbol name="{re.escape(name)}"[^>]*>')
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"символ не найден: {name}")
    end = text.find("</symbol>", match.end())
    if end < 0:
        raise ValueError(f"символ не закрыт: {name}")
    return match.start(), end + len("</symbol>")


def patch_symbol(text, name, expression):
    """Ставит свойство enabled символьному слою со значком."""
    start, end = symbol_region(text, name)
    symbol = text[start:end]
    for class_name in ICON_LAYER_CLASSES:
        match = re.search(
            rf'<layer[^>]*class="{class_name}"[^>]*>', symbol,
        )
        if match is None:
            continue
        layer_start = match.start()
        layer_end = symbol.find("</layer>", layer_start)
        if layer_end < 0:
            raise ValueError(f"слой символа не закрыт: {name}")
        layer = symbol[layer_start:layer_end]
        block = re.search(
            r"<data_defined_properties>.*?</data_defined_properties>",
            layer, re.S,
        )
        if block is not None:
            current = re.search(
                r'<Option name="expression" value="([^"]*)"', block.group(0),
            )
            if current is not None and current.group(1) == escape(expression):
                # Свойство уже такое же — шаблон не трогаем.
                return text
        replacement = DATA_DEFINED.format(expression=escape(expression))
        if block is None:
            # Свойства у слоя нет — добавляем их после списка опций.
            options = re.search(r"<Option type=\"Map\">.*?</Option>", layer, re.S)
            if options is None:
                raise ValueError(f"у слоя символа нет опций: {name}")
            insert = options.end()
            layer = layer[:insert] + replacement + layer[insert:]
        else:
            layer = (
                layer[:block.start()] + replacement + layer[block.end():]
            )
        symbol = symbol[:layer_start] + layer + symbol[layer_end:]
        return text[:start] + symbol + text[end:]
    raise ValueError(f"у символа {name} нет слоя со значком")


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
    obstacles = {
        layer.name: layer for layer in poi_icons.read_template_obstacles()
    }

    pieces = []
    position = 0
    changed = 0
    for start, end in layer_regions(text):
        region = text[start:end]
        name_match = re.search(r"<layername>(.*?)</layername>", region)
        if not name_match:
            continue
        name = name_match.group(1)
        layer = obstacles.get(name)
        if layer is None or name not in poi_icons.SELF_ICON_LAYERS:
            continue
        for symbol, indexes in poi_icons.icon_symbol_bands(layer, bands).items():
            expression = poi_icons.icon_enabled_expression_for(indexes)
            region = patch_symbol(region, symbol, expression)
            changed += 1
        pieces.append(text[position:start])
        pieces.append(region)
        position = end
    pieces.append(text[position:])
    new_text = "".join(pieces)

    temporary = template.with_suffix(".qgz.new")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(qgs, new_text.encode("utf-8"))
        for name, data in others.items():
            archive.writestr(name, data)
    temporary.replace(template)
    print(f"значков с подавлением по данным: {changed}")


if __name__ == "__main__":
    main()
