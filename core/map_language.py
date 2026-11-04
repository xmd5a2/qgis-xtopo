"""Язык подписей на карте через переменные проекта.

Шаблон карты (`automap.qgz`) берёт теги названий из переменных проекта
`name_tag_1..3`, а язык сокращений — из `name_pref_suf_lang`. Меняя эти
переменные и перерисовывая слои, можно переключить язык подписей на лету,
не перезагружая проект.

Язык подписей не связан с языком интерфейса: его выбирают отдельным
списком, а выбранное значение запоминается в настройках плагина.
Дополнительно можно включить транслитерацию: запасное название (когда
названия на выбранном языке нет) приводится к читаемому для этого языка
виду — см. `core/transliteration.py`.
"""

from qgis.core import (
    QgsExpressionContextUtils,
    QgsProject,
    QgsVectorLayer,
)

from . import i18n, transliteration

LOCAL_LANGUAGE = "local"
DEFAULT_MAP_LANGUAGE = "ru"
SETTINGS_KEY = "map_language"
SETTINGS_KEY_TRANSLIT = "map_transliteration"
SETTINGS_KEY_DETAIL = "level_of_detail"

# Уровень детализации подписей: переменная проекта, которую читают правила
# отрисовки шаблона. 3 — крупный масштаб (меньше мелких объектов),
# 4 — обычный, 5 — больше подробностей, 6 — все мелкие объекты.
DETAIL_VARIABLE = "level_of_detail"
DEFAULT_LEVEL_OF_DETAIL = 4
DETAIL_LEVELS = (3, 4, 5, 6)

# Порядок пунктов списка: местные названия, русский, английский, затем
# остальные языки по алфавиту. Третий элемент — письменность языка: по ней
# определяется, какие названия читатель выбранного языка прочитает.
MAP_LANGUAGES = (
    (LOCAL_LANGUAGE, "Местные названия", transliteration.LOCAL),
    ("ru", "Русский", transliteration.CYRL),
    ("en", "Английский", transliteration.LATIN),
    ("az", "Азербайджанский", transliteration.LATIN),
    ("sq", "Албанский", transliteration.LATIN),
    ("ar", "Арабский", transliteration.ARA),
    ("hy", "Армянский", transliteration.ARM),
    ("bg", "Болгарский", transliteration.CYRL),
    ("bs", "Боснийский", transliteration.LATIN),
    ("cy", "Валлийский", transliteration.LATIN),
    ("vi", "Вьетнамский", transliteration.LATIN),
    ("el", "Греческий", transliteration.GREK),
    ("ka", "Грузинский", transliteration.GEOR),
    ("da", "Датский", transliteration.LATIN),
    ("he", "Иврит", transliteration.HEBR),
    ("id", "Индонезийский", transliteration.LATIN),
    ("is", "Исландский", transliteration.LATIN),
    ("es", "Испанский", transliteration.LATIN),
    ("it", "Итальянский", transliteration.LATIN),
    ("kk", "Казахский", transliteration.CYRL),
    ("ca", "Каталанский", transliteration.LATIN),
    ("zh", "Китайский", transliteration.HANI),
    ("ko", "Корейский", transliteration.HANG),
    ("lv", "Латышский", transliteration.LATIN),
    ("lt", "Литовский", transliteration.LATIN),
    ("mk", "Македонский", transliteration.CYRL),
    ("ms", "Малайский", transliteration.LATIN),
    ("de", "Немецкий", transliteration.LATIN),
    ("ne", "Непальский", transliteration.LATIN),
    ("nl", "Нидерландский", transliteration.LATIN),
    ("no", "Норвежский", transliteration.LATIN),
    ("fa", "Персидский", transliteration.ARA),
    ("pl", "Польский", transliteration.LATIN),
    ("pt", "Португальский", transliteration.LATIN),
    ("ro", "Румынский", transliteration.LATIN),
    ("sr", "Сербский", transliteration.CYRL),
    ("sk", "Словацкий", transliteration.LATIN),
    ("sl", "Словенский", transliteration.LATIN),
    ("tg", "Таджикский", transliteration.LATIN),
    ("th", "Тайский", transliteration.THA),
    ("tr", "Турецкий", transliteration.LATIN),
    ("uz", "Узбекский", transliteration.LATIN),
    ("uk", "Украинский", transliteration.CYRL),
    ("fi", "Финский", transliteration.LATIN),
    ("fr", "Французский", transliteration.LATIN),
    ("hr", "Хорватский", transliteration.LATIN),
    ("cs", "Чешский", transliteration.LATIN),
    ("sv", "Шведский", transliteration.LATIN),
    ("et", "Эстонский", transliteration.LATIN),
    ("ja", "Японский", "jpan"),
)

PREF_SUFFIX_VARIABLE = "name_pref_suf_lang"
TAG_VARIABLE = "name_tag_{index}"
TRANS_VARIABLE = "name_translit"
LOCAL_VARIABLE = "name_local"
SCRIPT_VARIABLE = "name_script"
MAX_TAGS = 3

_map_language = DEFAULT_MAP_LANGUAGE
_transliteration = False
_detail_level = DEFAULT_LEVEL_OF_DETAIL


def normalize_detail(value):
    """Уровень детализации в пределах списка уровней шаблона."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LEVEL_OF_DETAIL
    if number < min(DETAIL_LEVELS):
        return min(DETAIL_LEVELS)
    if number > max(DETAIL_LEVELS):
        return max(DETAIL_LEVELS)
    return number


def _tags_for_code(code):
    """Предпочтительные теги названий для языка."""
    if code == LOCAL_LANGUAGE:
        return ("name",)
    if code == "ru":
        return ("name:ru", "name", "name:en")
    return (f"name:{code}", "name")


MAP_LANGUAGE_TAGS = {
    code: _tags_for_code(code) for code, _label, _script in MAP_LANGUAGES
}
LANGUAGE_CODES = tuple(code for code, _label, _script in MAP_LANGUAGES)
_language_labels = {code: label for code, label, _script in MAP_LANGUAGES}
_language_scripts = {code: script for code, _label, script in MAP_LANGUAGES}


def settings():
    """Настройки плагина (общие с языком интерфейса)."""
    return i18n.settings()


def normalize(code):
    code = str(code or "").strip()
    return code if code in MAP_LANGUAGE_TAGS else DEFAULT_MAP_LANGUAGE


def language():
    """Текущий язык подписей на карте."""
    return _map_language


def label(code):
    """Русское название языка (для перевода интерфейса)."""
    return _language_labels.get(normalize(code), code or "")


def tags_for(code=None):
    return MAP_LANGUAGE_TAGS[normalize(code)]


def script_for(code=None):
    """Письменность выбранного языка."""
    return _language_scripts.get(normalize(code), transliteration.LATIN)


def _pref_suffix(code):
    """Язык сокращений для подписей.

    Карта русская, поэтому сокращения типовых слов русские при любом
    языке названий — в том числе для местных названий: «ледник»
    сокращается до «л.», «брод» — до «бр.», высота подписывается «м».

    Значение должно быть непустым: пустая строка удаляется из переменных
    проекта, и тогда `@name_pref_suf_lang` становится NULL —
    пользовательские функции подписей (compose_peak_name,
    format_place_name) на NULL падают, и названия на карте пропадают.
    """
    return DEFAULT_MAP_LANGUAGE if code == LOCAL_LANGUAGE else code


def set_map_language(code, save=True):
    """Запоминает язык подписей; карта не перерисовывается."""
    global _map_language
    code = normalize(code)
    _map_language = code
    if save:
        try:
            settings().setValue(SETTINGS_KEY, code)
        except Exception:
            pass
    return code


def load_map_language(settings_object=None):
    """Восстанавливает язык подписей из настроек (по умолчанию русский)."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY, DEFAULT_MAP_LANGUAGE)
    except Exception:
        value = DEFAULT_MAP_LANGUAGE
    return set_map_language(value, save=False)


def level_of_detail():
    """Уровень детализации подписей: сколько подробностей видно на карте."""
    return _detail_level


def sync_level_of_detail(value):
    """Принимает уровень, сохранённый в проекте.

    Список панели мог сохранить другое значение раньше, чем открылся
    проект, — доверяем проекту.
    """
    global _detail_level
    _detail_level = normalize_detail(value)
    return _detail_level


def set_level_of_detail(value, save=True):
    """Запоминает уровень детализации; карта не перерисовывается."""
    global _detail_level
    value = normalize_detail(value)
    _detail_level = value
    if save:
        try:
            settings().setValue(SETTINGS_KEY_DETAIL, str(value))
        except Exception:
            pass
    return value


def load_level_of_detail(settings_object=None):
    """Восстанавливает уровень детализации из настроек."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY_DETAIL, str(DEFAULT_LEVEL_OF_DETAIL))
    except Exception:
        value = DEFAULT_LEVEL_OF_DETAIL
    return set_level_of_detail(value, save=False)


def set_project_detail(value, project=None):
    """Ставит уровень детализации в переменные проекта.

    Шаблон держит фильтры правил отрисовки на `@level_of_detail`: при
    значении 4 показываются подробности 1–4, при 3 — только до 3,
    при 5 — все, включая мелкие. Без переменной правила ложны, и модель
    карты (дороги, тропы, родники) не рисуется вовсе.
    """
    from qgis.core import QgsExpressionContextUtils, QgsProject, QgsVectorLayer

    project = project or QgsProject.instance()
    value = normalize_detail(value)
    QgsExpressionContextUtils.setProjectVariable(
        project, DETAIL_VARIABLE, str(value)
    )
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer):
            layer.triggerRepaint()
    return value


def transliteration_enabled():
    """Включена ли транслитерация запасных названий."""
    return _transliteration


def set_transliteration(enabled, save=True):
    """Запоминает включение транслитерации."""
    global _transliteration
    _transliteration = bool(enabled)
    if save:
        try:
            settings().setValue(SETTINGS_KEY_TRANSLIT, "1" if enabled else "0")
        except Exception:
            pass
    return _transliteration


def load_transliteration(settings_object=None):
    """Восстанавливает настройку транслитерации из настроек."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY_TRANSLIT, "0")
    except Exception:
        value = "0"
    return set_transliteration(str(value) == "1", save=False)


def apply_map_language(code=None, project=None, iface=None):
    """Выставляет предпочтительные теги названий и перерисовывает карту.

    Если проект не был изменён до вызова, флаг изменений снимается обратно:
    переключение языка не должно вызывать вопрос «сохранить проект?».
    """
    code = normalize(code or _map_language)
    tags = MAP_LANGUAGE_TAGS[code]
    translit = _transliteration
    project = project or QgsProject.instance()
    try:
        was_dirty = project.isDirty()
    except Exception:
        was_dirty = False
    for index in range(1, MAX_TAGS + 1):
        name = TAG_VARIABLE.format(index=index)
        if index <= len(tags):
            QgsExpressionContextUtils.setProjectVariable(
                project, name, tags[index - 1]
            )
        else:
            QgsExpressionContextUtils.removeProjectVariable(project, name)
    QgsExpressionContextUtils.setProjectVariable(
        project, PREF_SUFFIX_VARIABLE, _pref_suffix(code)
    )
    QgsExpressionContextUtils.setProjectVariable(
        project, TRANS_VARIABLE, "1" if translit else "0"
    )
    QgsExpressionContextUtils.setProjectVariable(
        project, LOCAL_VARIABLE, "1" if code == LOCAL_LANGUAGE else "0"
    )
    QgsExpressionContextUtils.setProjectVariable(
        project, SCRIPT_VARIABLE, script_for(code)
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
    return tags
