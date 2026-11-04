"""Язык интерфейса плагина: состояние, каталог переводов, слушатели.

Исходные строки интерфейса написаны по-русски. Английские переводы лежат
в `resources/lang/en.json` в виде словаря «русская строка → английская».
Для строк с подстановками (счётчики, размеры, имена) ключом служит шаблон
с `{}`; такие шаблоны переводятся и в уже сформированном тексте, например
«Листов: 3» → «Sheets: 3».

Перевод работает в обе стороны: при русском интерфейсе английский текст
превращается обратно в русский. Это позволяет переключать язык на лету,
обновляя существующие виджеты (см. `gui/translation.py`).
"""

import json
import re

from qgis.PyQt.QtCore import QSettings

LANGUAGES = ("ru", "en")
DEFAULT_LANGUAGE = "ru"
SETTINGS_GROUP = ("qgis-xtopo", "qgis-xtopo")
SETTINGS_KEY = "language"

_PLACEHOLDER = re.compile(r"\{[^{}]*\}")

_language = DEFAULT_LANGUAGE
_catalog = None
_listeners = []


def settings():
    """Настройки плагина (общие для всех его частей)."""
    return QSettings(*SETTINGS_GROUP)


def _catalog_path():
    from .resources import RESOURCES_DIR

    return RESOURCES_DIR / "lang" / "en.json"


def catalog():
    """Словарь переводов «русский → английский»."""
    global _catalog
    if _catalog is not None:
        return _catalog
    pairs = {}
    try:
        text = _catalog_path().read_text(encoding="utf-8")
        loaded = json.loads(text)
    except (OSError, ValueError):
        loaded = {}
    if isinstance(loaded, dict):
        for source, target in loaded.items():
            if (
                isinstance(source, str)
                and isinstance(target, str)
                and source
                and target
            ):
                pairs[source] = target
    _catalog = pairs
    return pairs


def _template_pattern(text):
    """Регулярное выражение для шаблона с `{}` (или None)."""
    parts = _PLACEHOLDER.split(text)
    if len(parts) == 1:
        return None
    body = "(.+?)".join(re.escape(part) for part in parts)
    return re.compile("^" + body + "$", re.DOTALL)


def _fill(template, values):
    parts = _PLACEHOLDER.split(template)
    if len(parts) - 1 != len(values):
        return None
    result = [parts[0]]
    for value, part in zip(values, parts[1:]):
        result.append(value)
        result.append(part)
    return "".join(result)


_LOOKUPS = {}


def _lookup_data(direction):
    """Словарь и шаблоны для направления перевода."""
    key = "en" if direction == "en" else "ru"
    cached = _LOOKUPS.get(key)
    if cached is not None:
        return cached
    pairs = catalog()
    if key == "en":
        exact = dict(pairs)
        source_targets = pairs.items()
    else:
        exact = {}
        for source, target in pairs.items():
            exact.setdefault(target, source)
        source_targets = ((target, source) for source, target in pairs.items())
    templates = []
    for source, target in source_targets:
        pattern = _template_pattern(source)
        if pattern is not None:
            templates.append((len(source), pattern, target))
    # Более длинные шаблоны проверяются раньше: они конкретнее.
    templates.sort(key=lambda item: item[0], reverse=True)
    cached = (exact, [(pattern, target) for _length, pattern, target in templates])
    _LOOKUPS[key] = cached
    return cached


def tr(text):
    """Перевод строки на текущий язык; неизвестные строки не меняются."""
    if not isinstance(text, str) or not text:
        return text
    exact, templates = _lookup_data(_language)
    translated = exact.get(text)
    if translated is not None:
        return translated
    for pattern, target in templates:
        match = pattern.match(text)
        if match is None:
            continue
        filled = _fill(target, match.groups())
        if filled is not None:
            return filled
    return text


def trf(text, *args, **values):
    """Перевод шаблона и подстановка значений."""
    return tr(text).format(*args, **values)


def language():
    return _language


def set_language(code, save=True):
    """Устанавливает язык; при изменении уведомляет слушателей."""
    global _language
    code = str(code or "").strip().lower()
    if code not in LANGUAGES:
        code = DEFAULT_LANGUAGE
    changed = code != _language
    _language = code
    if save:
        try:
            settings().setValue(SETTINGS_KEY, code)
        except Exception:
            pass
    if changed:
        for callback in list(_listeners):
            try:
                callback(code)
            except Exception:
                pass
    return code


def load_language(settings_object=None):
    """Восстанавливает язык из QSettings (по умолчанию русский)."""
    try:
        source = settings_object or settings()
        value = source.value(SETTINGS_KEY, DEFAULT_LANGUAGE)
    except Exception:
        value = DEFAULT_LANGUAGE
    return set_language(value, save=False)


def cycle_language():
    """Следующий язык из списка; список закольцован."""
    try:
        index = LANGUAGES.index(_language)
    except ValueError:
        index = -1
    code = LANGUAGES[(index + 1) % len(LANGUAGES)]
    return set_language(code)


def add_listener(callback):
    if callback not in _listeners:
        _listeners.append(callback)


def remove_listener(callback):
    try:
        _listeners.remove(callback)
    except ValueError:
        pass


def reset_cache():
    """Сбрасывает загруженный каталог (для тестов)."""
    global _catalog
    _catalog = None
    _LOOKUPS.clear()
