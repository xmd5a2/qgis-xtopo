"""Единицы длины на карте и единицы подготовки рельефа.

У плагина два независимых выбора:

* единицы высот при подготовке (`isolines_units`, вкладка «Рельеф»):
  в них строятся изолинии, поэтому в каталоге данных нет смеси метров и
  футов;
* единицы открытого проекта (переменные `map_units` и
  `map_units_data`): они записываются при подготовке вместе с проектом и
  определяют подписи высот, шкалу масштаба и вид списка на вкладке
  «Проект».  Переключать их можно и после подготовки: изолинии останутся
  в единицах подготовки, остальные подписи пересчитаются, а плагин
  предупредит, что для полного пересчёта нужна переподготовка.

Значения `m` — метры (по умолчанию), `ft` — футы.  OSM хранит высоты в
метрах, поэтому подписи пересчитываются при показе.
"""

from qgis.core import (
    QgsExpressionContextUtils,
    QgsProject,
    QgsVectorLayer,
)

# Единицы открытого проекта: что показывать и в чём подготовлены данные.
VARIABLE = "map_units"
DATA_VARIABLE = "map_units_data"
# Последний выбор на вкладке «Проект» (запасной вариант для старых
# проектов без переменных).
SETTINGS_KEY = "map_units"
# Единицы подготовки рельефа на вкладке «Рельеф».
PREP_SETTINGS_KEY = "isolines_units"

DEFAULT_UNITS = "m"
FEET = "ft"
UNITS = ("m", "ft")

# Метр в международном футе (1 фут = 0,3048 м).
FEET_PER_METER = 3.28084

# Подписи для интерфейса: полное название и единица на карте.
TITLES = {"m": "Метры", "ft": "Футы"}
SHORT_LABELS = {"m": "м", "ft": "ф"}

# Предупреждения при несовпадении единиц отображения и данных: изолинии
# не пересчитываются, поэтому остаются в единицах подготовки.
METRIC_DATA_WARNING = (
    "Данные подготовлены в метрах: изолинии останутся в метрах, "
    "остальные подписи и шкала переведены в футы. Для полностью "
    "имперской карты переподготовьте данные."
)
FEET_DATA_WARNING = (
    "Данные подготовлены в футах: изолинии останутся в футах, "
    "остальные подписи и шкала переведены в метры. Для полностью "
    "метрической карты переподготовьте данные."
)
PREPARED_WARNINGS = {"m": METRIC_DATA_WARNING, "ft": FEET_DATA_WARNING}

_units = DEFAULT_UNITS
_prep_units = DEFAULT_UNITS


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
    """Единицы карты по умолчанию (последний выбор на вкладке «Проект»)."""
    return _units


def prep_units():
    """Единицы высот, выбранные для подготовки рельефа."""
    return _prep_units


def feet_enabled():
    """Выбраны ли футы для отображения."""
    return _units == FEET


def title(code=None):
    """Русское название единиц (для перевода интерфейса)."""
    return TITLES.get(normalize(code), TITLES[DEFAULT_UNITS])


def short_label(code=None):
    """Короткая подпись единиц для компактного списка."""
    return SHORT_LABELS.get(normalize(code), SHORT_LABELS[DEFAULT_UNITS])


def prepared_warning(value=None):
    """Что сказать, если подписи переключены не в единицы данных."""
    return PREPARED_WARNINGS[normalize(value)]


def set_units(value, save=True):
    """Запоминает единицы карты по умолчанию."""
    global _units
    _units = normalize(value)
    if save:
        try:
            settings().setValue(SETTINGS_KEY, _units)
        except Exception:
            pass
    return _units


def load_units(settings_object=None):
    """Восстанавливает единицы карты по умолчанию (метры)."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY, DEFAULT_UNITS)
    except Exception:
        value = DEFAULT_UNITS
    return set_units(value, save=False)


def set_prep_units(value, save=True):
    """Запоминает единицы подготовки рельефа."""
    global _prep_units
    _prep_units = normalize(value)
    if save:
        try:
            settings().setValue(PREP_SETTINGS_KEY, _prep_units)
        except Exception:
            pass
    return _prep_units


def load_prep_units(settings_object=None):
    """Восстанавливает единицы подготовки рельефа (метры)."""
    try:
        source = settings_object or settings()
        value = source.value(PREP_SETTINGS_KEY, DEFAULT_UNITS)
    except Exception:
        value = DEFAULT_UNITS
    return set_prep_units(value, save=False)


def project_units(project=None):
    """Единицы, записанные в переменной проекта."""
    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(VARIABLE)
    except Exception:
        value = None
    return normalize(value)


def project_data_units(project=None):
    """Единицы, в которых подготовлены данные проекта.

    Старые проекты переменной не имеют: их изолинии и подписи считались
    в метрах, поэтому отсутствие значения означает метры.
    """
    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(DATA_VARIABLE)
    except Exception:
        value = None
    return normalize(value)


def display_units(project=None):
    """Единицы отображения проекта.

    Сохранённый выбор главнее единиц данных: метрические изолинии можно
    показывать на карте с подписями в футах, и наоборот.  Если выбора
    ещё нет, подписи показываются в единицах подготовленных данных.
    """
    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(VARIABLE)
    except Exception:
        value = None
    if str(value or "").strip() == "":
        return project_data_units(project)
    return normalize(value)


def _project_variable(project, name):
    try:
        return project.customVariables().get(name)
    except Exception:
        return None


def _repaint(project, iface=None):
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


def _apply_variables(project, values, iface=None):
    """Пишет переменные проекта, не оставляя проект изменённым."""
    updates = [
        (name, value)
        for name, value in values
        if str(_project_variable(project, name)) != value
    ]
    if not updates:
        return False
    try:
        was_dirty = project.isDirty()
    except Exception:
        was_dirty = False
    for name, value in updates:
        QgsExpressionContextUtils.setProjectVariable(project, name, value)
    _repaint(project, iface)
    try:
        project.setDirty(was_dirty)
    except Exception:
        pass
    return True


def set_project_units(value=None, project=None, iface=None):
    """Ставит единицы в переменную проекта и перерисовывает карту.

    Флаг изменений проекта восстанавливается: единицы — настройка
    отображения, как язык карты, и вопрос «сохранить проект?» при смене
    списка появляться не должен.
    """
    project = project or QgsProject.instance()
    value = normalize(_units if value is None else value)
    _apply_variables(project, [(VARIABLE, value)], iface)
    return value


def set_project_data_units(value, project=None):
    """Запоминает в проекте единицы, в которых подготовлены данные."""
    project = project or QgsProject.instance()
    value = normalize(value)
    _apply_variables(project, [(DATA_VARIABLE, value)])
    return value


def sync_project(project=None, iface=None):
    """Согласует переменные проекта с единицами отображения.

    Если переменной данных нет (проект собран до появления футов), это
    метры.  Выбранные единицы отображения сохраняются: изолинии остаются
    в единицах подготовки, а остальные подписи и шкала пересчитываются.
    Если выбор ещё не делали, он берётся из единиц данных.
    """
    project = project or QgsProject.instance()
    data = project_data_units(project)
    display = display_units(project)
    _apply_variables(
        project,
        [(DATA_VARIABLE, data), (VARIABLE, display)],
        iface,
    )
    return display
