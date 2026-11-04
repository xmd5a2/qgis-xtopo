"""Выражения QGIS: латинская запись названия и читаемое запасное название.

`transliterate()` переводит название в латиницу, `readable_name()`
оставляет название как есть, если его письменность знакома читателю
выбранного языка, и переводит в латиницу иначе.
"""

import importlib.util
from pathlib import Path

from qgis.core import *
from qgis.gui import *


def _core_module():
    """Модуль ядра с таблицами транслитерации."""
    try:
        from qgis_xtopo.core import transliteration

        return transliteration
    except Exception:
        pass
    path = Path(__file__).resolve().parents[2] / "core" / "transliteration.py"
    spec = importlib.util.spec_from_file_location(
        "qgis_xtopo_transliteration", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@qgsfunction(args='auto', group='Custom')
def transliterate(name, feature, parent):
    if name is None:
        return None
    return _core_module().transliterate_text(str(name))


@qgsfunction(args='auto', group='Custom')
def readable_name(name, script, feature, parent):
    if name is None:
        return None
    return _core_module().readable_name(str(name), script)


@qgsfunction(args='auto', group='Custom')
def pick_name_part(name, script, translit, feature, parent):
    """Часть составного названия на языке карты (см. core/transliteration)."""
    if name is None:
        return None
    return _core_module().pick_name(str(name), script, str(translit) == '1')
