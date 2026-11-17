"""Свободное место на диске и оценка потребности в нём.

Рельеф на области в сотни километров требует десятки гигабайт: уклон и
отмывка считаются вдвое крупнее DEM, и все производные растры
лежат рядом.  Без проверки запись обрывается сообщением GDAL
«На устройстве не осталось свободного места», из которого не видно,
что именно закончилось и сколько места нужно было.

Оценка всего этапа завышена намеренно: лучше предупредить о нехватке
места заранее, чем дождаться ошибки записи посреди многочасовой
подготовки.  Оценка отдельного файла, наоборот, близка к замеру:
завышенная отказала бы там, где файл на самом деле поместился бы.

Сообщения собираются из шаблонов: значения подставляются через
``str.format``, поэтому строки переводятся в ``resources/lang/en.json``
как обычно (см. ``core/i18n.py``).
"""

import shutil
from pathlib import Path

from . import i18n

MIB = 1024 * 1024
GIB = 1024 * MIB

# Запас на временные файлы и служебные данные проекта.
RESERVE = 256 * MIB

# Сжатый LZW float32 высот почти не жмётся: на замере DEM 16346×6674
# файл занимал 3,98 байта на пиксель при четырёх байтах без сжатия.
# Поэтому за растровые файлы берём размер float32 без сжатия, а за
# байтовый растр оттенков — байт на пиксель.  Это завышенная оценка:
# она нужна, чтобы предупредить о нехватке места заранее.
FLOAT_PIXEL = 4
BYTE_PIXEL = 1

# Размер одного файла считаем по верхней границе: float32 без сжатия —
# четыре байта на пиксель, оттенок — байт на пиксель.  LZW жмёт высоты
# слабо (на замере DEM 16346×6674 — 3,98 байта на пиксель), поэтому
# брать меньше нельзя: проверка идёт перед записью и заниженная оценка
# пропустит нехватку места.
FILE_FLOAT_PIXEL = 4
FILE_BYTE_PIXEL = 1


class InsufficientSpace(RuntimeError):
    """На томе, куда пишутся данные, не хватило места."""


def free_bytes(path):
    """Свободное место на томе, где лежит path; None — узнать не удалось."""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        pass
    # Каталога может ещё не быть: ищем ближайший существующий родитель.
    for parent in Path(str(path)).parents:
        try:
            return int(shutil.disk_usage(str(parent)).free)
        except OSError:
            continue
    return None


def human_size(size):
    """Размер словами: «12.3 ГБ»."""
    if size is None:
        return "?"
    if size >= GIB:
        return f"{size / GIB:.1f} {i18n.tr('ГБ')}"
    if size >= MIB:
        return f"{size / MIB:.0f} {i18n.tr('МБ')}"
    return f"{max(1, size // 1024)} {i18n.tr('КБ')}"


def relief_estimate(width, height, upscale):
    """Оценка места под уклон, отмывку и промежуточные растры.

    Ветки считаются параллельно, поэтому берём пик, когда крупные файлы
    лежат рядом: укрупнённый DEM, оттенок и его итоговая копия.  Без
    укрупнения (точный DEM) файлы остаются нативного размера.

    Размеры взяты по фактическим типам данных: DEM и уклон — float32
    (четыре байта на пиксель), оттенок и его итоговая копия — байт на
    пиксель.  Раньше итоговая копия оттенка считалась байтовой, а
    записывалась как float32 — оценка была вчетверо ниже реальности.
    """
    pixels = max(0, int(width)) * max(0, int(height))
    if not upscale:
        # DEM, его копия в 3857, ссылка на него же (на случай копии),
        # уклон и его итоговый растр, оттенок и его итоговая копия.
        return pixels * (
            3 * FLOAT_PIXEL + 2 * FLOAT_PIXEL + 2 * BYTE_PIXEL
        )
    big = pixels * 4
    return (
        # Сам DEM и его копия в EPSG:3857.
        pixels * 2 * FLOAT_PIXEL
        # Укрупнённый DEM, оттенок и его итоговая копия.
        + big * (FLOAT_PIXEL + 2 * BYTE_PIXEL)
        # Ветка уклона: DEM вдвое крупнее и сам уклон; либо уклон и его
        # итоговый растр — одновременно они не лежат.
        + big * 2 * FLOAT_PIXEL
    )


def space_warning(directory, needed, template):
    """Текст предупреждения о нехватке места; None — места хватает.

    Шаблон принимает два значения: сколько нужно и сколько свободно.
    """
    free = free_bytes(directory)
    if free is None:
        return None
    if free >= needed + RESERVE:
        return None
    return template.format(human_size(needed), human_size(free))


def require_space(directory, needed, template, subject=None):
    """Проверяет, что места хватает; иначе бросает InsufficientSpace.

    Места должно хватать заведомо, без запаса: иначе подготовка
    обрывается ошибкой записи в самый неподходящий момент.  Шаблон
    принимает размеры, а ``subject`` (имя файла) — первым значением.
    """
    free = free_bytes(directory)
    if free is None or free >= needed:
        return
    values = [human_size(needed), human_size(free)]
    if subject is not None:
        values.insert(0, subject)
    raise InsufficientSpace(template.format(*values))


def out_of_space(path):
    """Ошибка о закончившемся месте вместо сообщения GDAL."""
    free = free_bytes(path)
    return InsufficientSpace(
        f"Недостаточно места на диске при записи {path}: "
        f"свободно {human_size(free)}"
    )
