#!/usr/bin/env python3
"""Добавляет стрелки одностороннего движения в стиль дорог.

Стрелки встраиваются в символы правил, которые видны только до
предельного масштаба класса дороги.  Диапазон 0–50 000 делится на две
части: со стрелками и без них.  Поэтому на обзорных масштабах стрелок
нет, а `service` появляется только с 1:12 000.

Для `oneway=-1` стрелка разворачивается data-defined углом; сама
геометрия дороги не меняется.  Размер стрелки зависит от класса дороги:
толщина остаётся в пределах внешней обводки полосы.  Начало отсчёта
стрелок сдвигается по `$id`, поэтому на параллельных встречных дорогах
треугольники не оказываются в одной точке и не пересекаются.

Скрипт правит `automap.qgz` и идемпотентен: повторный запуск ничего не
меняет.  Готовые проекты не чинятся, они готовятся заново.

Запуск: `python3 tools/oneway_arrows.py`
"""

import re
import sys
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATE = ROOT / "automap.qgz"

# Слои, которые рисуют дороги вблизи.  Мосты пропущены: их геометрия
# растворяется, и направление после dissolve не гарантировано.
LAYERS = (
    "highway main",
    "highway main layer >=1",
    "highway main layer &lt;=-1",
    "highway main tunnel",
)

# Предельный масштаб и длина стрелки по подписи верхнего правила.
# `residential` включает unclassified/residential/living_street и живёт
# одним символом, поэтому для группы взят общий порог.
CLASS_THRESHOLDS = {
    "motorway": 50000,
    "trunk": 45000,
    "primary": 40000,
    "primary link": 40000,
    "primary_link": 40000,
    "secondary": 32000,
    "tertiary": 25000,
    "tertiary link": 25000,
    "tertiary_link": 25000,
    "residential": 16000,
    "service": 12000,
}

CLASS_SIZES = {
    "motorway": 1.8,
    "trunk": 1.8,
    "primary": 1.8,
    "primary link": 1.8,
    "primary_link": 1.8,
    "secondary": 1.8,
    "tertiary": 1.5,
    "tertiary link": 1.5,
    "tertiary_link": 1.5,
    "residential": 1.2,
    "service": 1.0,
}

MARKER_PASS = 12
MARKER_INTERVAL_MM = 20
MARKER_OFFSET_MM = 10
# Детерминированная фаза вдоль линии: 2–18 мм вместо постоянных 10 мм.
# Соседние встречные дороги получают разное положение стрелок, поэтому
# треугольники не накладываются друг на друга.
MARKER_PHASE = "10 + (($id * 7) % 17 - 8)"

_SYMBOL_OPEN = re.compile(r"<symbol\b")
_SYMBOL_CLOSE = re.compile(r"</symbol>")


def escape(text):
    """Экранирование значения XML-атрибута."""
    return (
        str(text).replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
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


def symbol_region(text, name):
    """Границы внешнего `<symbol name="...">` с учётом вложенных символов."""
    pattern = re.compile(rf'<symbol[^>]*\bname="{re.escape(name)}"')
    match = pattern.search(text)
    if match is None:
        return None
    position = match.end()
    depth = 1
    while depth:
        opening = _SYMBOL_OPEN.search(text, position)
        closing = _SYMBOL_CLOSE.search(text, position)
        if closing is None:
            return None
        if opening is not None and opening.start() < closing.start():
            depth += 1
            position = opening.end()
        else:
            depth -= 1
            position = closing.end()
    return match.start(), position


def _element_region(text, tag, start):
    """Границы элемента с вложенными элементами того же имени."""
    opening = re.compile(rf"<{tag}\b")
    closing = re.compile(rf"</{tag}>")
    position = opening.search(text, start).end()
    depth = 1
    while depth:
        next_open = opening.search(text, position)
        next_close = closing.search(text, position)
        if next_close is None:
            return None
        if next_open is not None and next_open.start() < next_close.start():
            depth += 1
            position = next_open.end()
        else:
            depth -= 1
            position = next_close.end()
    return start, position


def _marker_region(symbol):
    """Границы уже добавленного MarkerLine со стрелкой."""
    for match in re.finditer(
            r'<layer\b[^>]*class="MarkerLine"[^>]*>', symbol):
        region = _element_region(symbol, "layer", match.start())
        if region is None:
            continue
        block = symbol[region[0]:region[1]]
        if "oneway_arrow.svg" in block:
            return region
    return None


def _marker_xml(layer_name, symbol_name, size):
    """Символьный слой MarkerLine с чёрным вытянутым треугольником."""
    seed = f"qgis-xtopo/oneway/{layer_name}/{symbol_name}"
    return """\
<layer class="MarkerLine" enabled="1" locked="0" pass="{pass_}" id="{{{uuid}}}">
  <Option type="Map">
    <Option name="average_angle_length" value="4" type="QString"/>
    <Option name="average_angle_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
    <Option name="average_angle_unit" value="MM" type="QString"/>
    <Option name="interval" value="{interval}" type="QString"/>
    <Option name="interval_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
    <Option name="interval_unit" value="MM" type="QString"/>
    <Option name="offset" value="0" type="QString"/>
    <Option name="offset_along_line" value="{offset}" type="QString"/>
    <Option name="offset_along_line_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
    <Option name="offset_along_line_unit" value="MM" type="QString"/>
    <Option name="offset_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
    <Option name="offset_unit" value="MM" type="QString"/>
    <Option name="place_on_every_part" value="true" type="bool"/>
    <Option name="placements" value="Interval" type="QString"/>
    <Option name="ring_filter" value="0" type="QString"/>
    <Option name="rotate" value="1" type="QString"/>
  </Option>
  <data_defined_properties>
    <Option type="Map">
      <Option name="name" value="" type="QString"/>
      <Option name="properties" type="Map">
        <Option name="offsetAlongLine" type="Map">
          <Option name="active" value="true" type="bool"/>
          <Option name="expression" value="{phase}" type="QString"/>
          <Option name="type" value="3" type="int"/>
        </Option>
      </Option>
      <Option name="type" value="collection" type="QString"/>
    </Option>
  </data_defined_properties>
  <symbol alpha="1" clip_to_extent="1" force_rhr="0" frame_rate="10" is_animated="0" name="@oneway@0" type="marker">
    <data_defined_properties>
      <Option type="Map">
        <Option name="name" value="" type="QString"/>
        <Option name="properties"/>
        <Option name="type" value="collection" type="QString"/>
      </Option>
    </data_defined_properties>
    <layer class="SvgMarker" enabled="1" locked="0" pass="0" id="{{{marker_uuid}}}">
      <Option type="Map">
        <Option name="angle" value="0" type="QString"/>
        <Option name="color" value="0,0,0,255,rgb:0,0,0,1" type="QString"/>
        <Option name="fixedAspectRatio" value="0" type="QString"/>
        <Option name="horizontal_anchor_point" value="1" type="QString"/>
        <Option name="name" value="../icons/oneway_arrow.svg" type="QString"/>
        <Option name="offset" value="0,0" type="QString"/>
        <Option name="offset_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
        <Option name="offset_unit" value="MM" type="QString"/>
        <Option name="outline_color" value="0,0,0,255,rgb:0,0,0,1" type="QString"/>
        <Option name="outline_width" value="0" type="QString"/>
        <Option name="outline_width_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
        <Option name="outline_width_unit" value="MM" type="QString"/>
        <Option name="parameters"/>
        <Option name="scale_method" value="diameter" type="QString"/>
        <Option name="size" value="{size}" type="QString"/>
        <Option name="size_map_unit_scale" value="3x:0,0,0,0,0,0" type="QString"/>
        <Option name="size_unit" value="MM" type="QString"/>
        <Option name="vertical_anchor_point" value="1" type="QString"/>
      </Option>
      <data_defined_properties>
        <Option type="Map">
          <Option name="name" value="" type="QString"/>
          <Option name="properties" type="Map">
            <Option name="enabled" type="Map">
              <Option name="active" value="true" type="bool"/>
              <Option name="expression" value="{visibility}" type="QString"/>
              <Option name="type" value="3" type="int"/>
            </Option>
            <Option name="angle" type="Map">
              <Option name="active" value="true" type="bool"/>
              <Option name="expression" value="{angle}" type="QString"/>
              <Option name="type" value="3" type="int"/>
            </Option>
          </Option>
          <Option name="type" value="collection" type="QString"/>
        </Option>
      </data_defined_properties>
    </layer>
  </symbol>
</layer>""".format(
        pass_=MARKER_PASS,
        uuid=uuid.uuid5(uuid.NAMESPACE_URL, seed + "/line"),
        marker_uuid=uuid.uuid5(uuid.NAMESPACE_URL, seed + "/marker"),
        interval=MARKER_INTERVAL_MM,
        offset=MARKER_OFFSET_MM,
        phase=escape(MARKER_PHASE),
        size=size,
        visibility=escape(
            "coalesce(try(\"oneway\", ''), '') IN ('yes', '-1')"
        ),
        angle=escape(
            "if(coalesce(try(\"oneway\", ''), '') = '-1', 0, 180)"
        ),
    )


def _strip_marker(symbol):
    """Убирает прежнюю стрелку из символа."""
    region = _marker_region(symbol)
    if region is None:
        return symbol
    start, end = region
    if symbol[end:end + 1] == "\n":
        end += 1
    return symbol[:start] + symbol[end:]


def _add_marker(symbol, layer_name, symbol_name, size):
    """Добавляет или обновляет стрелку в символе."""
    marker = _marker_xml(layer_name, symbol_name, size)
    region = _marker_region(symbol)
    if region is not None:
        start, end = region
        return symbol[:start] + marker + symbol[end:]
    closing = symbol.rfind("</symbol>")
    if closing < 0:
        return symbol
    return symbol[:closing] + marker + "\n" + symbol[closing:]


def _symbol_names(renderer):
    root = ElementTree.fromstring(renderer)
    symbols = root.find("symbols")
    if symbols is None:
        return []
    return [item.get("name") for item in symbols.findall("symbol")
            if item.get("name")]


def _rule_leaves(rule, parent_max=None):
    """Листья правила и их действующий scalemaxdenom."""
    maximum = rule.get("scalemaxdenom")
    effective = int(maximum) if maximum else parent_max
    children = rule.findall("rule")
    if not children:
        yield rule, effective
        return
    for child in children:
        yield from _rule_leaves(child, effective)


def _set_attr(tag, name, value):
    pattern = re.compile(rf'\b{name}="[^"]*"')
    if pattern.search(tag):
        return pattern.sub(f'{name}="{value}"', tag, count=1)
    return tag[:-2] + f' {name}="{escape(value)}"/>'


def _remove_attr(tag, name):
    return re.sub(rf'\s+{name}="[^"]*"', "", tag)


def _rule_match(block, key):
    return re.search(
        rf'<rule\b[^>]*\bkey="{re.escape(key)}"[^>]*/>', block
    )


def _split_leaf(block, key, threshold, new_symbol):
    """Делит лист 0–50k на 0–threshold и threshold–50k."""
    match = _rule_match(block, key)
    if match is None:
        return block
    original = match.group(0)
    first = _remove_attr(original, "scalemindenom")
    first = _set_attr(first, "scalemaxdenom", str(threshold))
    first = _set_attr(first, "symbol", new_symbol)
    first = _set_attr(first, "label", f"0 - {threshold}")
    second = _set_attr(original, "scalemindenom", str(threshold))
    second = _set_attr(second, "scalemaxdenom", "50000")
    second = _set_attr(second, "label", f"{threshold} - 50000")
    second = _set_attr(second, "key", "{" + str(uuid.uuid4()) + "}")
    line_start = block.rfind("\n", 0, match.start()) + 1
    indent = block[line_start:match.start()]
    replacement = (
        indent + first + "\n" + indent + second
    )
    return block[:line_start] + replacement + block[match.end():]


def _add_symbol(block, name, symbol):
    """Добавляет новый символ в конец блока symbols."""
    if re.search(rf'<symbol[^>]*\bname="{re.escape(name)}"', block):
        return block
    closing = block.find("</symbols>")
    if closing < 0:
        return block
    return block[:closing] + symbol + "\n" + block[closing:]


def _rename_symbol(symbol, name):
    """Меняет имя верхнего символа, не трогая вложенные."""
    return re.sub(
        r'^(<symbol\b[^>]*\bname=")[^"]*(")',
        lambda match: match.group(1) + name + match.group(2),
        symbol,
        count=1,
    )


def patch_layer(block, layer_name):
    """Добавляет стрелки и делит правила по масштабам."""
    match = re.search(r"<renderer-v2\b.*?</renderer-v2>", block, re.S)
    if match is None:
        return block, []
    renderer_start, renderer_end = match.start(), match.end()
    renderer = block[renderer_start:renderer_end]
    root = ElementTree.fromstring(renderer)
    rules = root.find("rules")
    if rules is None:
        return block, []

    already_split = any(
        name.startswith("oneway-") for name in _symbol_names(renderer)
    )
    arrow_symbols = set()
    if already_split:
        for branch in rules.findall("rule"):
            threshold = CLASS_THRESHOLDS.get(branch.get("label") or "")
            if threshold is None:
                continue
            for leaf, effective in _rule_leaves(branch):
                if effective == threshold and leaf.get("symbol"):
                    arrow_symbols.add(leaf.get("symbol"))
    changed = []

    # Снимаем прежние стрелки.  После первого разделения правил
    # трогаем только символы без стрелок: маркеры в `oneway-*` ниже
    # заменяются на месте, и повторный запуск ничего не меняет.
    for name in _symbol_names(renderer):
        if already_split and name in arrow_symbols:
            continue
        region = symbol_region(renderer, name)
        if region is None:
            continue
        start, end = region
        symbol = renderer[start:end]
        cleaned = _strip_marker(symbol)
        if cleaned != symbol:
            renderer = renderer[:start] + cleaned + renderer[end:]

    for branch in rules.findall("rule"):
        label = branch.get("label") or ""
        threshold = CLASS_THRESHOLDS.get(label)
        if threshold is None:
            continue
        size = CLASS_SIZES.get(label, 3.6)
        for leaf, effective in _rule_leaves(branch):
            symbol_name = leaf.get("symbol")
            key = leaf.get("key")
            if (not symbol_name or not key or effective is None
                    or effective > 50000):
                continue
            if already_split:
                if effective != threshold:
                    continue
                region = symbol_region(renderer, symbol_name)
                if region is None:
                    continue
                start, end = region
                symbol = _add_marker(
                    renderer[start:end], layer_name, symbol_name, size
                )
                if symbol != renderer[start:end]:
                    renderer = renderer[:start] + symbol + renderer[end:]
                    changed.append(symbol_name)
                continue
            if threshold == 50000:
                region = symbol_region(renderer, symbol_name)
                if region is None:
                    continue
                start, end = region
                symbol = _add_marker(
                    renderer[start:end], layer_name, symbol_name, size
                )
                if symbol != renderer[start:end]:
                    renderer = renderer[:start] + symbol + renderer[end:]
                    changed.append(symbol_name)
                continue
            new_name = f"oneway-{symbol_name}"
            region = symbol_region(renderer, symbol_name)
            if region is None:
                continue
            start, end = region
            base = renderer[start:end]
            new_symbol = _add_marker(
                base, layer_name, new_name, size
            )
            new_symbol = _rename_symbol(new_symbol, new_name)
            renderer = _add_symbol(renderer, new_name, new_symbol)
            renderer = _split_leaf(renderer, key, threshold, new_name)
            changed.append(new_name)

    block = block[:renderer_start] + renderer + block[renderer_end:]
    return block, changed


def patch_project(text):
    """Правит все дорожные слои. Возвращает (текст, изменения)."""
    changed = {}
    for name in LAYERS:
        region = layer_region(text, name)
        if region is None:
            continue
        start, end = region
        block, symbols = patch_layer(text[start:end], name)
        if symbols:
            text = text[:start] + block + text[end:]
            changed[name] = symbols
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
        print("Стрелки oneway уже добавлены")
        return 0
    contents[project] = patched.encode("utf-8")

    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    for name, symbols in changed.items():
        print(f"{name}: символы {', '.join(symbols)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
