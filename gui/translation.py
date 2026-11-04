"""Перевод уже построенных виджетов при смене языка на лету.

Интерфейс плагина создаётся по-русски. При переключении языка обходим
дерево виджетов и переводим все подписи, заголовки, подсказки, пункты
списков и вкладок. Повторный обход безопасен: неизвестный текст остаётся
как есть, а уже переведённый не переводится второй раз.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QTabWidget,
    QWidget,
)

from ..core.i18n import tr

# Списки, пункты которых нельзя переводить: их текст является именем
# файла запроса и используется как ключ при чтении запроса.
SKIP_TRANSLATION_OBJECTS = {"queriesList"}


def _tooltip_role():
    try:
        return Qt.ItemDataRole.ToolTipRole
    except AttributeError:
        return Qt.ToolTipRole


def translate_widget(widget):
    """Переводит тексты одного виджета (если они есть)."""
    if widget is None:
        return
    if widget.objectName() in SKIP_TRANSLATION_OBJECTS:
        return
    if isinstance(widget, QLabel):
        text = widget.text()
        if text:
            widget.setText(tr(text))
    if isinstance(widget, QAbstractButton):
        text = widget.text()
        if text:
            widget.setText(tr(text))
    if isinstance(widget, QGroupBox):
        title = widget.title()
        if title:
            widget.setTitle(tr(title))
    if isinstance(widget, QComboBox):
        role = _tooltip_role()
        for index in range(widget.count()):
            text = widget.itemText(index)
            if text:
                widget.setItemText(index, tr(text))
            tooltip = widget.itemData(index, role)
            if isinstance(tooltip, str) and tooltip:
                widget.setItemData(index, tr(tooltip), role)
    if isinstance(widget, QTabWidget):
        for index in range(widget.count()):
            text = widget.tabText(index)
            if text:
                widget.setTabText(index, tr(text))
    if isinstance(widget, QListWidget):
        for index in range(widget.count()):
            item = widget.item(index)
            if item is not None and item.text():
                item.setText(tr(item.text()))
    if isinstance(widget, QProgressBar):
        text = widget.format()
        if text:
            widget.setFormat(tr(text))
    if isinstance(widget, QLineEdit):
        text = widget.placeholderText()
        if text:
            widget.setPlaceholderText(tr(text))
    if isinstance(widget, QPlainTextEdit):
        text = widget.placeholderText()
        if text:
            widget.setPlaceholderText(tr(text))
    tooltip = widget.toolTip()
    if tooltip:
        widget.setToolTip(tr(tooltip))
    if widget.isWindow():
        title = widget.windowTitle()
        if title:
            widget.setWindowTitle(tr(title))


def translate_tree(root):
    """Переводит root и всех его потомков-виджетов."""
    if root is None:
        return
    translate_widget(root)
    for widget in root.findChildren(QWidget):
        translate_widget(widget)
