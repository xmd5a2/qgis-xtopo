#!/usr/bin/env python3
"""Отсекает короткие тропы на обзорных масштабах.

На 1:200 000 и мельче тропы короче 70 м занимают меньше 0.25 мм и
почти не видны, но их отрисовка и регистрация подписей стоят времени.
Фильтр стоит на корневом правиле рендерера `track`: на крупных
масштабах он пропускает все тропы.

Скрипт правит только шаблон `automap.qgz` и идемпотентен: повторный
запуск ничего не меняет.  Готовые проекты не чинятся, они готовятся
заново из исправленного шаблона.

Запуск: `python3 tools/track_length_filter.py`
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "automap.qgz"
LAYER = "track"
FILTER = "@map_scale &lt;= 200000 OR $length &gt; 70"

_MAPLAYER = re.compile(r"<maplayer\b[^>]*>.*?</maplayer>", re.S)
_LAYERNAME = re.compile(r"<layername>(.*?)</layername>", re.S)
_RENDERER = re.compile(r"<renderer-v2\b.*?</renderer-v2>", re.S)
_RULES = re.compile(r"<rules\b[^>]*>")
_FILTER = re.compile(r'\s+filter="[^"]*"')


def _patch_rules(tag):
    tag = _FILTER.sub("", tag)
    return tag[:-1] + f' filter="{FILTER}">'


def patch_project(text):
    """Ставит фильтр длины на корень рендерера `track`."""
    changed = []

    def patch_block(match):
        block = match.group(0)
        name_match = _LAYERNAME.search(block)
        if name_match is None or name_match.group(1) != LAYER:
            return block
        renderer = _RENDERER.search(block)
        if renderer is None:
            return block
        rules = _RULES.search(renderer.group(0))
        if rules is None:
            return block
        old = rules.group(0)
        new = _patch_rules(old)
        if new == old:
            return block
        changed.append(LAYER)
        start = renderer.start() + rules.start()
        end = renderer.start() + rules.end()
        return block[:start] + new + block[end:]

    return _MAPLAYER.sub(patch_block, text), changed


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
    print("Короткие тропы отсечены на обзоре: %s" % ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
