"""Безопасный доступ к настройкам подписей слоя.

Две особенности Python-обвязки QGIS делают прямой доступ к правилам
подписей опасным.

1. ``QgsRuleBasedLabeling.Rule.settings()`` отдаёт указатель на внутренний
   объект настроек.  Python считает такой объект своим и удаляет его при
   сборке мусора, а правило остаётся с освобождёнными настройками: после
   этого чтение цветов даёт мусор, а QGIS иногда падает.

2. ``QgsPalLayerSettings.format()`` возвращает **копию** формата, а
   ``QgsTextFormat.buffer()`` — ссылку на член этой копии.  Цепочка
   ``settings.format().buffer().enabled()`` читает уже освобождённую
   память: временный формат уничтожается сразу после вызова ``buffer()``.

Поэтому настройки читаются копией (``labeling.settings(key)``), формат
держится в переменной, а запись идёт обратно через
``labeling.setSettings(settings, key)``: правило забирает копию себе, а
прежние настройки удаляет сам.
"""

from qgis.core import QgsRuleBasedLabeling, QgsVectorLayerSimpleLabeling


def label_settings(labeling):
    """Пары «ключ правила, настройки» в порядке правил слоя.

    Настройки — копии: их можно свободно менять и отдавать обратно.
    Простая разметка (не rule-based) отдаёт одни настройки с пустым
    ключом.
    """
    if isinstance(labeling, QgsRuleBasedLabeling):
        for key in labeling.subProviders():
            yield key, labeling.settings(key)
    elif isinstance(labeling, QgsVectorLayerSimpleLabeling):
        yield "", labeling.settings()


def set_label_settings(labeling, key, settings):
    """Записывает настройки правила подписей.

    Возвращает False, если правило настроек не имеет: QGIS тогда
    игнорирует запись.
    """
    if isinstance(labeling, QgsVectorLayerSimpleLabeling):
        labeling.setSettings(settings)
        return True
    if key not in labeling.subProviders():
        return False
    labeling.setSettings(settings, key)
    return True


def settings_by_key(labeling, key):
    """Настройки правила по ключу (копия) или None, если правила нет."""
    if isinstance(labeling, QgsVectorLayerSimpleLabeling):
        return labeling.settings()
    if (
        isinstance(labeling, QgsRuleBasedLabeling)
        and key in labeling.subProviders()
    ):
        return labeling.settings(key)
    return None


def label_colors(settings):
    """Цвета подписей правила: текст, halo и признак включённого halo."""
    fmt = settings.format()
    buffer = fmt.buffer()
    return fmt.color(), buffer.color(), buffer.enabled()


def set_label_colors(settings, text_color=None, halo_color=None,
                     halo_enabled=None, bold=None):
    """Меняет цвета и жирность подписей в копии настроек.

    Формат держится в переменной: ``buffer()`` отдаёт ссылку на член
    формата, и при обращении по цепочке временный формат уже освобождён.
    """
    fmt = settings.format()
    buffer = fmt.buffer()
    changed = False
    if text_color is not None and fmt.color() != text_color:
        fmt.setColor(text_color)
        changed = True
    if halo_color is not None and buffer.color() != halo_color:
        buffer.setColor(halo_color)
        changed = True
    if halo_enabled is not None and buffer.enabled() != halo_enabled:
        buffer.setEnabled(halo_enabled)
        changed = True
    if bold is not None:
        font = fmt.font()
        if font.bold() != bool(bold):
            font.setBold(bool(bold))
            fmt.setFont(font)
            changed = True
    if not changed:
        return False
    fmt.setBuffer(buffer)
    settings.setFormat(fmt)
    return True
