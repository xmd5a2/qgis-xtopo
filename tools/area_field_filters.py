#!/usr/bin/env python3
"""Переводит фильтры рендерера с `$area` на поле `area_m2`.

`$area` провайдер не может протолкнуть в SQL, поэтому правило проверяет
площадь для каждого объекта в кадре.  Поле `area_m2` заполняет пайплайн
(`core/area_field.py`), и тот же фильтр уходит в SQL.

Скрипт правит только шаблон `automap.qgz` и идемпотентен: повторный
запуск ничего не меняет.  Готовые проекты не чинятся, они готовятся
заново из исправленного шаблона.

Запуск: `python3 tools/area_field_filters.py`
"""

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.area_field import AREA_LAYERS  # noqa: E402

TEMPLATE = ROOT / "automap.qgz"

_MAPLAYER = re.compile(r"<maplayer\b[^>]*>.*?</maplayer>", re.S)
_LAYERNAME = re.compile(r"<layername>(.*?)</layername>", re.S)
_RENDERER = re.compile(r"<renderer-v2\b.*?</renderer-v2>", re.S)


def patch_project(text):
    """Заменяет `$area` на `area_m2` в рендерерах нужных слоёв."""
    changed = []

    def patch_block(match):
        block = match.group(0)
        name_match = _LAYERNAME.search(block)
        if name_match is None:
            return block
        name = name_match.group(1)
        if name not in AREA_LAYERS:
            return block
        renderer = _RENDERER.search(block)
        if renderer is None:
            return block
        old = renderer.group(0)
        new = old.replace("$area", "&quot;area_m2&quot;")
        if new == old:
            return block
        changed.append(name)
        return block[:renderer.start()] + new + block[renderer.end():]

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
    print("Площадь вынесена в поле: %s" % ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
