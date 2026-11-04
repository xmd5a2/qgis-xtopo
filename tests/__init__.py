"""Тесты плагина.

Корень репозитория одновременно является корнем пакета плагина
(его каталог при установке в QGIS называется `qgis_xtopo`). Чтобы тесты
могли импортировать модули как `qgis_xtopo.core...`, корень регистрируется
в `sys.modules` под этим именем.
"""

import importlib.util
import sys
from pathlib import Path

PACKAGE_NAME = "qgis_xtopo"
ROOT = Path(__file__).resolve().parent.parent


def register_package(name=PACKAGE_NAME):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


register_package()

_APP = None


def qgis_app():
    """Единственный экземпляр QgsApplication на весь процесс тестов.

    QGIS не поддерживает повторный запуск приложения в одном процессе,
    поэтому все файлы тестов используют общий экземпляр.
    """
    global _APP
    if _APP is None:
        from qgis.core import QgsApplication

        _APP = QgsApplication([], False)
        _APP.initQgis()
    return _APP
