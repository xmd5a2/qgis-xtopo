#!/usr/bin/env python3
"""Ограничивает число подписей изолиний на обзорных масштабах.

Движок подписей перебирает и размещает тысячи подписей изолиний.  На
1:100 000 и мельче достаточно 500: карта читается так же, а кадр
рисуется заметно быстрее.  Крупные масштабы не трогаем.

Скрипт правит только шаблон `automap.qgz` и идемпотентен: повторный
запуск ничего не меняет.  Готовые проекты не чинятся, они готовятся
заново из исправленного шаблона.

Запуск: `python3 tools/isolines_label_limit.py`
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"

LAYERS = (
    "[TERRAIN] isolines regular",
    "[TERRAIN] isolines glacier",
)

# С какого знаменателя масштаба ограничиваем подписи и сколько оставляем.
MIN_SCALE = 100000
MAX_LABELS = 500

_RULE_SETTINGS = re.compile(
    r'<rule\b[^>]*\bscalemindenom="(\d+)"[^>]*>\s*'
    r'<settings\b[^>]*>'
)
_RENDERING = re.compile(r"<rendering\b[^>]*/>")
_LIMIT = re.compile(r'\blimitNumLabels="\d+"')
_MAX = re.compile(r'\bmaxNumLabels="\d+"')


def _patch_block(block):
    """Добавляет лимит в настройки правил обзорных масштабов."""
    changed = False
    position = 0
    while True:
        match = _RULE_SETTINGS.search(block, position)
        if match is None:
            break
        end = block.find("</settings>", match.end())
        if end < 0:
            break
        end += len("</settings>")
        if int(match.group(1)) >= MIN_SCALE:
            settings = block[match.start():end]
            rendering = _RENDERING.search(settings)
            if rendering is not None:
                tag = rendering.group(0)
                patched = _LIMIT.sub('limitNumLabels="1"', tag)
                patched = _MAX.sub(
                    f'maxNumLabels="{MAX_LABELS}"', patched)
                if patched != tag:
                    block = (
                        block[:match.start()]
                        + settings.replace(tag, patched, 1)
                        + block[end:]
                    )
                    changed = True
                    position = (
                        match.start() + len(settings)
                        - len(tag) + len(patched)
                    )
                    continue
        position = end
    return block, changed


def patch_project(text):
    """Правит подписи изолиний в тексте проекта."""
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
    print("Ограничены подписи: %s" % ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
