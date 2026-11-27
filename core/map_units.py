"""Единицы длины на карте через переменную проекта.

Функции подписей читают переменную `@map_units`: `m` — метры (по
умолчанию), `ft` — футы.  В OSM длины и высоты хранятся в метрах, поэтому
в футы пересчитываются только при показе.  Сейчас единицу измерения имеют
высоты вершин, перевалов и вулканов.

Выбранные единицы запоминаются в настройках плагина и записываются в
переменную открытого проекта при его открытии и при смене пункта списка.
Переключение не помечает проект изменённым.
"""

from qgis.core import (
    QgsExpressionContextUtils,
    QgsProject,
    QgsVectorLayer,
)

VARIABLE = "map_units"
SETTINGS_KEY = "map_units"
DEFAULT_UNITS = "m"
FEET = "ft"
UNITS = ("m", "ft")

# Метр в международном футе (1 фут = 0,3048 м).
FEET_PER_METER = 3.28084

# Подписи для интерфейса: полное название и единица на карте.
TITLES = {"m": "Метры", "ft": "Футы"}
SHORT_LABELS = {"m": "м", "ft": "ф"}

_units = DEFAULT_UNITS


def settings():
    """Настройки плагина (общие для всех его частей)."""
    from . import i18n

    return i18n.settings()


def normalize(value):
    """Единицы по значению из настроек или переменной проекта."""
    text = str(value or "").strip().lower()
    if text in ("ft", "f", "feet", "ф", "фут", "футы"):
        return FEET
    return DEFAULT_UNITS


def units():
    """Текущие единицы длины на карте."""
    return _units


def feet_enabled():
    """Выбраны ли футы."""
    return _units == FEET


def title(code=None):
    """Русское название единиц (для перевода интерфейса)."""
    return TITLES.get(normalize(code), TITLES[DEFAULT_UNITS])


def short_label(code=None):
    """Короткая подпись единиц для компактного списка."""
    return SHORT_LABELS.get(normalize(code), SHORT_LABELS[DEFAULT_UNITS])


def set_units(value, save=True):
    """Запоминает единицы; карта не перерисовывается."""
    global _units
    _units = normalize(value)
    if save:
        try:
            settings().setValue(SETTINGS_KEY, _units)
        except Exception:
            pass
    return _units


def load_units(settings_object=None):
    """Восстанавливает единицы из настроек (по умолчанию метры)."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY, DEFAULT_UNITS)
    except Exception:
        value = DEFAULT_UNITS
    return set_units(value, save=False)


def project_units(project=None):
    """Единицы, записанные в переменной проекта."""
    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(VARIABLE)
    except Exception:
        value = None
    return normalize(value)


def set_project_units(value=None, project=None, iface=None):
    """Ставит единицы в переменную проекта и перерисовывает карту.

    Флаг изменений проекта восстанавливается: единицы — настройка
    отображения, как язык карты, и вопрос «сохранить проект?» при смене
    списка появляться не должен.
    """
    project = project or QgsProject.instance()
    value = normalize(_units if value is None else value)
    try:
        was_dirty = project.isDirty()
    except Exception:
        was_dirty = False
    try:
        current = project.customVariables().get(VARIABLE)
    except Exception:
        current = None
    if str(current) != value:
        QgsExpressionContextUtils.setProjectVariable(
            project, VARIABLE, value
        )
        try:
            layers = list(project.mapLayers().values())
        except Exception:
            layers = []
        for layer in layers:
            if isinstance(layer, QgsVectorLayer):
                try:
                    layer.triggerRepaint()
                except Exception:
                    pass
        canvas = None
        if iface is not None:
            canvas = getattr(iface, "mapCanvas", None)
            if callable(canvas):
                try:
                    canvas = canvas()
                except Exception:
                    canvas = None
        if canvas is not None:
            try:
                canvas.refresh()
            except Exception:
                pass
    try:
        project.setDirty(was_dirty)
    except Exception:
        pass
    return value


def apply_units(project=None, iface=None):
    """Записывает выбранные единицы в открытый проект."""
    return set_project_units(units(), project=project, iface=iface)
