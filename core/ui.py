"""Управление видимостью элементов главного окна QGIS.

Панели QGIS — обычные QDockWidget главного окна, панели инструментов —
QToolBar, а уведомления и ошибки показывает QgsMessageBar под панелями
инструментов. Плагин прячет всё это при открытии проекта, чтобы на экране
осталась только собственная панель: лишние элементы интерфейса только
смущают пользователя, не готовящего данные вручную.
"""

# objectName панели плагина — её скрывать нельзя.
PLUGIN_DOCK_NAME = "QgisXtopoDockWidget"

# Псевдоимя строки уведомлений в списках скрытых элементов: своего
# objectName у неё нет, поэтому отмечаем её этим именем.
MESSAGE_BAR_NAME = "<messageBar>"


def _main_window(iface):
    """Главное окно QGIS или None, если оно недоступно."""
    if iface is None:
        return None
    getter = getattr(iface, "mainWindow", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _docks(iface):
    """Все панели главного окна QGIS."""
    main_window = _main_window(iface)
    if main_window is None:
        return []
    try:
        from qgis.PyQt.QtWidgets import QDockWidget
    except ImportError:
        return []
    try:
        return list(main_window.findChildren(QDockWidget))
    except Exception:
        return []


def _toolbars(iface):
    """Все панели инструментов главного окна QGIS."""
    main_window = _main_window(iface)
    if main_window is None:
        return []
    try:
        from qgis.PyQt.QtWidgets import QToolBar
    except ImportError:
        return []
    try:
        return list(main_window.findChildren(QToolBar))
    except Exception:
        return []


def _message_bar(iface):
    """Строка уведомлений QGIS (QgsMessageBar) или None."""
    if iface is None:
        return None
    getter = getattr(iface, "messageBar", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _widgets(iface):
    """Скрываемые виджеты: панели и панели инструментов."""
    return _docks(iface) + _toolbars(iface)


def dock_names(iface):
    """Имена всех панелей и панелей инструментов главного окна."""
    return [widget.objectName() for widget in _widgets(iface)]


def visible_dock_names(iface, keep=()):
    """Имена видимых элементов, кроме перечисленных в keep."""
    return [
        widget.objectName()
        for widget in _widgets(iface)
        if widget.objectName()
        and widget.objectName() not in keep
        and not widget.isHidden()
    ]


def hide_docks(iface, keep=(PLUGIN_DOCK_NAME,)):
    """Скрывает видимые панели, панели инструментов и строку уведомлений.

    Элементы из keep не трогают. Возвращает имена скрытых элементов;
    строка уведомлений отмечается как MESSAGE_BAR_NAME.
    """
    hidden = []
    for widget in _widgets(iface):
        name = widget.objectName()
        if not name or name in keep:
            continue
        if widget.isHidden():
            continue
        try:
            widget.setVisible(False)
        except Exception:
            continue
        if widget.isHidden():
            hidden.append(name)
    bar = _message_bar(iface)
    if bar is not None and not bar.isHidden():
        try:
            bar.setVisible(False)
        except Exception:
            pass
        if bar.isHidden():
            hidden.append(MESSAGE_BAR_NAME)
    return hidden


def show_docks(iface, names):
    """Показывает элементы с указанными именами.

    Возвращает имена элементов, которые стали видимыми.
    """
    wanted = set(names or ())
    if not wanted:
        return []
    restored = []
    bar = _message_bar(iface)
    if bar is not None and MESSAGE_BAR_NAME in wanted and bar.isHidden():
        try:
            bar.setVisible(True)
        except Exception:
            pass
        if not bar.isHidden():
            restored.append(MESSAGE_BAR_NAME)
    for widget in _widgets(iface):
        name = widget.objectName()
        if name not in wanted or not widget.isHidden():
            continue
        try:
            widget.setVisible(True)
        except Exception:
            continue
        if not widget.isHidden():
            restored.append(name)
    return restored
