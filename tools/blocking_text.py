#!/usr/bin/env python3
"""Прячет подписи слоёв-блокировщиков дорог в шаблоне.

Слои группы «[blocking text]» — копии настоящих слоёв с нулевым
символом: они не рисуются, но подписывают те же объекты.  Текст копии
занимает место и не даёт чужим подписям ложиться на дорогу, но виден
и на карте, а у дорог стиль копии отличается от настоящего (серый halo
вместо цвета полосы) и приоритет одинаковый.  Из-за этого движок
подписей иногда оставлял копию вместо настоящей подписи: одно и то же
название дороги то ложилось на полосу, то оказывалось серым, а с
инверсией надписей — белым.

Скрипт делает текст копии прозрачным (место он по-прежнему занимает)
и понижает её приоритет ниже приоритета настоящих правил: побеждает
настоящая подпись, копия рисует пустоту.

Правка нужна один раз: уже подготовленные проекты не чинятся, они
готовятся заново из исправленного шаблона.

Скрипт идемпотентен: повторный запуск ничего не меняет.

Запуск: `python3 tools/blocking_text.py`
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

# Слои-блокировщики дорог: их подписи на карте не нужны.
LAYERS = (
    "[blocking] highway main",
    "[blocking] highway main minor",
    "[blocking] highway main bridge",
)

# Приоритет копии: ниже, чем у настоящих правил дорог (2, 4, 5),
# иначе копия может отнять размещение у настоящей подписи.
PRIORITY = 1

_COLOR = re.compile(r'(textColor|bufferColor)="([0-9a-zA-Z:.,]+)"')
_PRIORITY = re.compile(r'(<placement\b[^>]*?)priority="\d+"')
_LABELING = re.compile(r"<labeling\b[^>]*>")


def _transparent(match):
    """Цвет с нулевой прозрачностью."""
    name, value = match.group(1), match.group(2)
    parts = value.split(",")
    if len(parts) < 4:
        return match.group(0)
    parts[3] = "0"
    if len(parts) >= 8:
        parts[7] = "0"
    return '%s="%s"' % (name, ",".join(parts))


def _patch_block(block):
    """Прозрачный текст и пониженный приоритет у подписей слоя.

    У слоя две подписи: своя и та же, что лежит в менеджере стилей
    (стили «default» и «По умолчанию»).  Прозрачной делаем обе, иначе
    переключение стиля вернёт видимую подпись.
    """
    changed = False
    position = 0
    while True:
        match = _LABELING.search(block, position)
        if match is None:
            break
        end = block.find("</labeling>", match.start())
        if end < 0:
            break
        end += len("</labeling>")
        segment = block[match.start():end]
        patched = _COLOR.sub(_transparent, segment)
        patched = _PRIORITY.sub(r'\1priority="%d"' % PRIORITY, patched)
        if patched != segment:
            block = block[:match.start()] + patched + block[end:]
            changed = True
            position = match.start() + len(patched)
            continue
        position = end
    return block, changed


def patch_project(text):
    """Прячет подписи слоёв-блокировщиков дорог в тексте проекта.

    Возвращает (исправленный текст, список исправленных слоёв).
    """
    changed = []
    for name in LAYERS:
        marker = "<layername>%s</layername>" % name
        position = text.find(marker)
        if position < 0:
            continue
        start = text.rfind("<maplayer", 0, position)
        end = text.find("</maplayer>", position)
        if start < 0 or end < 0:
            continue
        end += len("</maplayer>")
        block, patched = _patch_block(text[start:end])
        if patched:
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
        print("Шаблон уже исправлен")
        return 0
    contents[project] = patched.encode("utf-8")

    temporary = TEMPLATE.with_suffix(".qgz.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    temporary.replace(TEMPLATE)
    print("Исправлены подписи: %s" % ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
