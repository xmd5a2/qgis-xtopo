"""Выбор варианта окраски крутых склонов по характеру рельефа.

У слоёв ``[TERRAIN] slope (steep) var1/var2/var3`` общая подложка
``slope_upscaled.tif``, но разные шкалы: горы 20–70°, предгорья 10–35°,
равнины 5–30°. Вариант выбирается по 90-му процентилю уклона: он
показывает, насколько круты самые крутые 10 % площади, поэтому шкала не
остаётся пустой на равнине и не упирается в последний цвет в горах.
Пользователь по-прежнему может выбрать вариант вручную.
"""

from pathlib import Path

# Порядок совпадает с STEEP_VARIANTS в gui/projectpanel.py.
MOUNTAINS = 1
FOOTHILLS = 2
PLAINS = 3

# 90-й процентиль уклона в градусах. У горных областей склоны круче 30°
# занимают заметную часть площади, у предгорий — 12–30°, у равнин меньше.
MOUNTAIN_P90 = 30.0
FOOTHILL_P90 = 12.0

SLOPE_FILE = Path("raster") / "slope_upscaled.tif"

# Горизонтальную выборку можно прореживать дёшево, а высоту — нет:
# у полосового TIFF (блок — одна строка на всю ширину) каждая строка
# читается целиком. 96 строк по 1200 пикселей дают ~115 тыс. точек, а
# процентиль отличается от полной выборки не больше чем на 0.2°.
SAMPLE_WIDTH = 1200
SAMPLE_ROWS = 96
MIN_SAMPLES = 100


def project_raster(project_file):
    """Путь к растру уклонов рядом с проектом или None, если его нет."""
    if not project_file:
        return None
    path = Path(project_file).parent / SLOPE_FILE
    return path if path.exists() else None


def slope_percentile(path, percent=90, width=SAMPLE_WIDTH, rows=SAMPLE_ROWS):
    """Процентиль уклона по выборке растра.

    Возвращает None, если файла нет, он не читается или данных слишком
    мало (например, в растре одни пустые ячейки).
    """
    if path is None:
        return None
    try:
        from osgeo import gdal
        import numpy as np

        gdal.UseExceptions()
        dataset = gdal.Open(str(path))
        if dataset is None:
            return None
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        values = band.ReadAsArray(
            buf_xsize=min(dataset.RasterXSize, width),
            buf_ysize=min(dataset.RasterYSize, rows),
        )
        dataset = None
        if values is None:
            return None
        array = np.asarray(values, dtype="float32").ravel()
        array = array[np.isfinite(array)]
        if nodata is not None:
            array = array[array != nodata]
        if array.size < MIN_SAMPLES:
            return None
        return float(np.percentile(array, percent))
    except Exception:  # noqa: BLE001 - без растра остаётся ручной выбор
        return None


def choose_variant(path):
    """Вариант окраски (1 — горы, 2 — предгорья, 3 — равнины) или None."""
    p90 = slope_percentile(path, 90)
    if p90 is None:
        return None
    if p90 >= MOUNTAIN_P90:
        return MOUNTAINS
    if p90 >= FOOTHILL_P90:
        return FOOTHILLS
    return PLAINS
