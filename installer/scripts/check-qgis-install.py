"""Проверка установки QGIS: работают ли GDAL, numpy и модули плагина.

Запускается интерпретатором Python из установки QGIS:

    python-qgis-ltr.bat check-qgis-install.py [каталог-репозитория]

Каталог репозитория нужен, чтобы импортировать модули плагина: корень
репозитория одновременно является пакетом ``qgis_xtopo``, но называется
``qgis-xtopo``, поэтому пакет регистрируется вручную — так же, как это
делает ``tests/__init__.py``.

Код возврата 0 — всё в порядке.
"""

import importlib.util
import sys
from pathlib import Path

PACKAGE_NAME = "qgis_xtopo"


def register_package(root):
    """Регистрирует корень репозитория как пакет qgis_xtopo."""
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    if not (root / "__init__.py").exists():
        print(f"Не найден корень репозитория: {root}")
        return 2
    register_package(root)

    from osgeo import gdal, ogr

    print("gdal", gdal.__version__)
    import numpy

    print("numpy", numpy.__version__)
    # Векторные драйверы нужны плагину для чтения OSM и записи GeoPackage.
    for name in ("OSM", "GPKG"):
        driver = ogr.GetDriverByName(name)
        if driver is None:
            print(f"Нет драйвера OGR: {name}")
            return 3
        print(f"драйвер {name}: ок")
    # Растровый драйвер — для рельефа (GeoTIFF, в том числе COG).
    if gdal.GetDriverByName("GTiff") is None:
        print("Нет драйвера GDAL: GTiff")
        return 3
    print("драйвер GTiff: ок")

    from qgis.core import Qgis

    print("qgis", Qgis.QGIS_VERSION)

    import qgis_xtopo.core.pipeline  # noqa: F401
    import qgis_xtopo.core.terrain  # noqa: F401

    print("модули плагина: ок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
