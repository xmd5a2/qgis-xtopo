"""Размер подписей на карте через переменную проекта.

Шаблон умножает размер каждой подписи на `@text_size_mult`, поэтому одна
переменная проекта меняет текст во всех слоях.  Панель показывает размер
в процентах: 100 % — множитель 1, 50 % — 0.5, 200 % — 2.

Выбранный процент запоминается в настройках плагина, а в открытом проекте
сразу записывается в переменную.  Новые проекты получают значение при
сборке из шаблона.
"""

from qgis.core import (
    QgsExpressionContextUtils,
    QgsProject,
    QgsVectorLayer,
)

VARIABLE = "text_size_mult"
SETTINGS_KEY = "text_size_percent"
DEFAULT_PERCENT = 100
PERCENTS = (50, 75, 100, 125, 150, 200)

_percent = DEFAULT_PERCENT


def settings():
    """Настройки плагина (общие для всех его частей)."""
    from . import i18n

    return i18n.settings()


def normalize(value):
    """Ближайший доступный процент: в списке только фиксированные шаги."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PERCENT
    return min(PERCENTS, key=lambda item: (abs(item - number), item))


def multiplier(percent=None):
    """Значение переменной `text_size_mult` для процента."""
    value = normalize(_percent if percent is None else percent)
    return f"{value / 100:g}"


def percent_from_multiplier(value):
    """Процент для значения переменной из проекта."""
    try:
        return normalize(float(value) * 100)
    except (TypeError, ValueError):
        return DEFAULT_PERCENT


def percent():
    """Текущий выбранный процент."""
    return _percent


def set_percent(value, save=True):
    """Запоминает процент; карта не перерисовывается."""
    global _percent
    _percent = normalize(value)
    if save:
        try:
            settings().setValue(SETTINGS_KEY, str(_percent))
        except Exception:
            pass
    return _percent


def load_percent(settings_object=None):
    """Восстанавливает процент из настроек (по умолчанию 100)."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY, str(DEFAULT_PERCENT))
    except Exception:
        value = DEFAULT_PERCENT
    return set_percent(value, save=False)


def set_project_text_size(value, project=None):
    """Ставит размер подписей в переменную проекта и перерисовывает слои."""
    project = project or QgsProject.instance()
    value = normalize(value)
    text = multiplier(value)
    try:
        current = project.customVariables().get(VARIABLE)
    except Exception:
        current = None
    if str(current) != text:
        QgsExpressionContextUtils.setProjectVariable(
            project, VARIABLE, text
        )
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                try:
                    layer.triggerRepaint()
                except Exception:
                    pass
    return value
