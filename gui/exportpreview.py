"""Просмотр листов экспорта в пониженном разрешении.

Листы рисуются тем же макетом и атласом, что и настоящий экспорт, но
с длинной стороной около 1280 пикселей.  Просмотрщик — полноэкранное
окно: колесо мыши плавно меняет масштаб, перетаскивание сдвигает лист,
справа показывается вертикальная лента миниатюр, а PNG/PDF запускают
обычный экспорт с настройками панели.
"""

import math

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import i18n

THUMBNAIL_WIDTH = 200
MAX_ZOOM = 8.0
WHEEL_FACTOR = 1.2
ANIMATION_INTERVAL_MS = 16
ANIMATION_EASING = 0.45


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


def _event_position(event):
    """Положение курсора в событии мыши для Qt 5 и Qt 6."""
    position = getattr(event, "position", None)
    if callable(position):
        return position()
    return QPointF(event.pos())


def _theme_icon(name):
    try:
        return QgsApplication.getThemeIcon(name)
    except Exception:
        return QIcon()


class PreviewCanvas(QWidget):
    """Один лист: вписан в окно, с плавным зумом и перетаскиванием."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._scale = 1.0
        self._fit_scale = 1.0
        self._offset = QPointF()
        self._target_scale = 1.0
        self._target_offset = QPointF()
        self._drag_from = None
        self._user_zoomed = False
        self._timer = QTimer(self)
        self._timer.setInterval(ANIMATION_INTERVAL_MS)
        self._timer.timeout.connect(self._step_animation)
        self.setMinimumSize(320, 240)
        self.setFocusPolicy(_enum(Qt, "FocusPolicy", "StrongFocus"))
        self.setCursor(_enum(Qt, "CursorShape", "OpenHandCursor"))
        self.setToolTip(
            i18n.tr(
                "Колесо — масштаб, перетаскивание — сдвиг, "
                "двойной щелчок — вписать лист"
            )
        )

    @property
    def image(self):
        return self._image

    @property
    def scale(self):
        return self._scale

    @property
    def offset(self):
        return QPointF(self._offset)

    @property
    def is_animating(self):
        return self._timer.isActive()

    def set_image(self, image):
        self._image = image if image is not None else QImage()
        self._reset_view()

    def fit_to_window(self, animate=True):
        """Вписать лист в окно, при желании плавно."""
        self._user_zoomed = False
        self._fit_scale = self._fit_scale_for(self._image)
        if animate:
            self._animate_to(self._fit_scale, QPointF())
        else:
            self._set_view(self._fit_scale, QPointF())

    def reset_view(self):
        """Вписать лист в окно без анимации."""
        self.fit_to_window(animate=False)

    def zoom_at(self, factor, anchor=None, animate=True):
        """Увеличить/уменьшить изображение вокруг точки ``anchor``."""
        if self._image.isNull():
            return
        if anchor is None:
            anchor = QPointF(self.width() / 2.0, self.height() / 2.0)
        animating = self._timer.isActive()
        base_scale = self._target_scale if animating else self._scale
        base_offset = self._target_offset if animating else self._offset
        scale = min(
            max(base_scale * factor, self._fit_scale),
            self._fit_scale * MAX_ZOOM,
        )
        if scale <= self._fit_scale + 1e-9:
            target_scale = self._fit_scale
            target_offset = QPointF()
            self._user_zoomed = False
        else:
            center = QPointF(self.width() / 2.0, self.height() / 2.0)
            image_point = (anchor - center - base_offset) / base_scale
            target_scale = scale
            target_offset = anchor - center - image_point * scale
            self._user_zoomed = True
        if animate:
            self._animate_to(target_scale, target_offset)
        else:
            self._set_view(target_scale, target_offset)

    def _animate_to(self, scale, offset):
        self._target_scale = scale
        self._target_offset = QPointF(offset)
        if not self._timer.isActive():
            self._timer.start()

    def _set_view(self, scale, offset):
        self._timer.stop()
        self._scale = scale
        self._offset = QPointF(offset)
        self._target_scale = scale
        self._target_offset = QPointF(offset)
        self.update()

    def _step_animation(self):
        scale_delta = self._target_scale - self._scale
        offset_delta = self._target_offset - self._offset
        close_scale = abs(scale_delta) < 1e-3
        close_offset = math.hypot(offset_delta.x(), offset_delta.y()) < 0.5
        if close_scale and close_offset:
            self._set_view(self._target_scale, self._target_offset)
            return
        self._scale += scale_delta * ANIMATION_EASING
        self._offset += offset_delta * ANIMATION_EASING
        self.update()

    def _reset_view(self):
        self._user_zoomed = False
        self._offset = QPointF()
        self._fit_scale = self._fit_scale_for(self._image)
        self._set_view(self._fit_scale, QPointF())

    def _fit_scale_for(self, image):
        if image.isNull() or not image.width() or not image.height():
            return 1.0
        width = max(1, self.width() - 16)
        height = max(1, self.height() - 16)
        return min(width / image.width(), height / image.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_scale = self._fit_scale_for(self._image)
        if not self._user_zoomed:
            self._set_view(self._fit_scale, QPointF())
        elif self._scale < self._fit_scale:
            self.fit_to_window(animate=False)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(42, 42, 42))
        if self._image.isNull():
            return
        painter.setRenderHint(
            _enum(QPainter, "RenderHint", "SmoothPixmapTransform")
        )
        width = self._image.width() * self._scale
        height = self._image.height() * self._scale
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        left = center.x() + self._offset.x() - width / 2.0
        top = center.y() + self._offset.y() - height / 2.0
        painter.drawImage(
            QRectF(left, top, width, height),
            self._image,
            QRectF(self._image.rect()),
        )

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom_at(WHEEL_FACTOR ** (delta / 120.0),
                         _event_position(event))
        event.accept()

    def mousePressEvent(self, event):
        if (
            event.button() == _enum(Qt, "MouseButton", "LeftButton")
            and not self._image.isNull()
        ):
            self._timer.stop()
            self._drag_from = _event_position(event)
            self._target_scale = self._scale
            self._target_offset = QPointF(self._offset)
            self.setCursor(_enum(Qt, "CursorShape", "ClosedHandCursor"))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            position = _event_position(event)
            self._offset += position - self._drag_from
            self._target_offset = QPointF(self._offset)
            self._drag_from = position
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.setCursor(_enum(Qt, "CursorShape", "OpenHandCursor"))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.fit_to_window()
        event.accept()


class ExportPreviewDialog(QDialog):
    """Полноэкранный просмотрщик листов и запуск экспорта."""

    exportRequested = pyqtSignal(str)

    def __init__(self, images, parent=None):
        super().__init__(parent)
        flags = _enum(Qt, "WindowType", "Window")
        for name in (
            "WindowTitleHint",
            "WindowSystemMenuHint",
            "WindowMinimizeButtonHint",
            "WindowMaximizeButtonHint",
            "WindowCloseButtonHint",
        ):
            flags |= _enum(Qt, "WindowType", name)
        self.setWindowFlags(flags)
        self._images = [image for image in images if not image.isNull()]
        self._index = 0
        self.setWindowTitle(i18n.tr("Предпросмотр экспорта"))
        self.setWindowIcon(_theme_icon("mActionZoomIn.svg"))
        self.setAttribute(
            _enum(Qt, "WidgetAttribute", "WA_DeleteOnClose"), True
        )
        self.setMinimumSize(800, 560)
        self._build_ui()
        self._fill_thumbnails()
        if self._images:
            self._show_index(0)
        else:
            self.sheet_label.setText("0 / 0")
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.canvas = PreviewCanvas(self)
        body.addWidget(self.canvas, 1)

        self.thumbnail_panel = QWidget(self)
        self.thumbnail_panel.setFixedWidth(232)
        panel_layout = QVBoxLayout(self.thumbnail_panel)
        panel_layout.setContentsMargins(4, 4, 4, 4)
        self.thumbnails = QListWidget(self.thumbnail_panel)
        self.thumbnails.setViewMode(
            _enum(QListView, "ViewMode", "IconMode")
        )
        self.thumbnails.setFlow(
            _enum(QListView, "Flow", "TopToBottom")
        )
        self.thumbnails.setResizeMode(
            _enum(QListView, "ResizeMode", "Adjust")
        )
        self.thumbnails.setMovement(
            _enum(QListView, "Movement", "Static")
        )
        self.thumbnails.setWrapping(False)
        self.thumbnails.setUniformItemSizes(True)
        self.thumbnails.setSpacing(4)
        self.thumbnails.setWordWrap(False)
        self.thumbnails.setHorizontalScrollBarPolicy(
            _enum(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")
        )
        self.thumbnails.setStyleSheet(
            "QListWidget { border: none; background: transparent; }"
            "QListWidget::item { border: 1px solid transparent; padding: 2px; }"
            "QListWidget::item:selected {"
            " border: 1px solid #0078d7;"
            " background: transparent;"
            " color: palette(text);"
            "}"
        )
        self.thumbnails.itemClicked.connect(self._thumbnail_clicked)
        panel_layout.addWidget(self.thumbnails)
        body.addWidget(self.thumbnail_panel)
        layout.addLayout(body, 1)

        self._control_row = QHBoxLayout()
        self._control_row.setContentsMargins(8, 6, 8, 6)
        self._control_row.setSpacing(6)

        self._left_controls = QWidget(self)
        left_layout = QHBoxLayout(self._left_controls)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.zoom_out_button = QPushButton("−", self)
        self.zoom_out_button.setFixedWidth(34)
        self.zoom_out_button.setToolTip(i18n.tr("Уменьшить"))
        self.zoom_in_button = QPushButton("+", self)
        self.zoom_in_button.setFixedWidth(34)
        self.zoom_in_button.setToolTip(i18n.tr("Увеличить"))
        self.fit_button = QPushButton("Вписать в окно", self)
        self.fit_button.setToolTip(i18n.tr("Вписать лист в окно"))
        left_layout.addWidget(self.zoom_out_button)
        left_layout.addWidget(self.zoom_in_button)
        left_layout.addWidget(self.fit_button)
        left_layout.addStretch(1)

        self.navigation = QWidget(self)
        navigation = QHBoxLayout(self.navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.setSpacing(6)
        self.previous_button = QPushButton("←", self)
        self.previous_button.setFixedWidth(34)
        self.previous_button.setToolTip(i18n.tr("Предыдущий лист"))
        self.next_button = QPushButton("→", self)
        self.next_button.setFixedWidth(34)
        self.next_button.setToolTip(i18n.tr("Следующий лист"))
        self.sheet_label = QLabel("", self)
        self.sheet_label.setAlignment(
            _enum(Qt, "AlignmentFlag", "AlignCenter")
        )
        self.sheet_label.setMinimumWidth(64)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.sheet_label)
        navigation.addWidget(self.next_button)

        self._right_controls = QWidget(self)
        right_layout = QHBoxLayout(self._right_controls)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self.thumbnails_button = QPushButton("Миниатюры", self)
        self.thumbnails_button.setCheckable(True)
        self.thumbnails_button.setToolTip(
            i18n.tr("Показать или скрыть панель миниатюр")
        )
        self.png_button = QPushButton(
            _theme_icon("mActionFileSave.svg"), "PNG", self
        )
        self.pdf_button = QPushButton(
            _theme_icon("mActionExport.svg"), "PDF", self
        )
        for button in (self.png_button, self.pdf_button):
            button.setIconSize(QSize(16, 16))
            button.setToolTip(i18n.tr("Экспортировать листы в {}…").format(
                button.text()
            ))
        right_layout.addStretch(1)
        right_layout.addWidget(self.thumbnails_button)
        right_layout.addWidget(self.png_button)
        right_layout.addWidget(self.pdf_button)

        self._control_row.addWidget(self._left_controls, 1)
        self._control_row.addWidget(self.navigation)
        self._control_row.addWidget(self._right_controls, 1)
        layout.addLayout(self._control_row)
        self._balance_control_groups()

        no_focus = _enum(Qt, "FocusPolicy", "NoFocus")
        for button in (
            self.zoom_out_button, self.zoom_in_button, self.fit_button,
            self.thumbnails_button, self.png_button, self.pdf_button,
            self.previous_button, self.next_button,
        ):
            button.setFocusPolicy(no_focus)
        self.thumbnails.setFocusPolicy(no_focus)

        self.zoom_out_button.clicked.connect(
            lambda: self.canvas.zoom_at(1.0 / WHEEL_FACTOR)
        )
        self.zoom_in_button.clicked.connect(
            lambda: self.canvas.zoom_at(WHEEL_FACTOR)
        )
        self.fit_button.clicked.connect(
            lambda: self.canvas.fit_to_window()
        )
        self.previous_button.clicked.connect(self._previous)
        self.next_button.clicked.connect(self._next)
        self.thumbnails_button.toggled.connect(self._toggle_thumbnails)
        self.png_button.clicked.connect(
            lambda: self.exportRequested.emit("png")
        )
        self.pdf_button.clicked.connect(
            lambda: self.exportRequested.emit("pdf")
        )

    def _balance_control_groups(self):
        """Одинаковая ширина боковых групп держит навигацию по центру."""
        side = max(
            self._left_controls.sizeHint().width(),
            self._right_controls.sizeHint().width(),
        )
        self._left_controls.setMinimumWidth(side)
        self._right_controls.setMinimumWidth(side)

    @property
    def sheet_count(self):
        return len(self._images)

    @property
    def current_index(self):
        return self._index

    def showEvent(self, event):
        super().showEvent(event)
        self.canvas.setFocus()

    def keyPressEvent(self, event):
        key = event.key()
        if key == _enum(Qt, "Key", "Key_Left"):
            self._previous()
        elif key == _enum(Qt, "Key", "Key_Right"):
            self._next()
        elif key == _enum(Qt, "Key", "Key_Up"):
            self._previous()
        elif key == _enum(Qt, "Key", "Key_Down"):
            self._next()
        elif key == _enum(Qt, "Key", "Key_Home"):
            self._show_index(0)
        elif key == _enum(Qt, "Key", "Key_End"):
            self._show_index(len(self._images) - 1)
        elif key == _enum(Qt, "Key", "Key_Space"):
            self._next()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _fill_thumbnails(self):
        if not self._images:
            return
        first = self._images[0]
        height = 200
        if first.width():
            height = max(80, round(
                THUMBNAIL_WIDTH * first.height() / first.width()
            ))
        icon_size = QSize(THUMBNAIL_WIDTH, height)
        self.thumbnails.setIconSize(icon_size)
        self.thumbnails.setGridSize(QSize(216, height + 26))
        for number, image in enumerate(self._images, 1):
            thumbnail = image.scaled(
                icon_size,
                _enum(Qt, "AspectRatioMode", "KeepAspectRatio"),
                _enum(Qt, "TransformationMode", "SmoothTransformation"),
            )
            pixmap = QPixmap.fromImage(thumbnail)
            icon = QIcon()
            icon.addPixmap(pixmap, _enum(QIcon, "Mode", "Normal"))
            icon.addPixmap(pixmap, _enum(QIcon, "Mode", "Selected"))
            item = QListWidgetItem(icon, "")
            item.setText(str(number))
            item.setData(_enum(Qt, "ItemDataRole", "UserRole"), number - 1)
            item.setTextAlignment(
                _enum(Qt, "AlignmentFlag", "AlignHCenter")
            )
            self.thumbnails.addItem(item)
        many = len(self._images) > 1
        self.navigation.setVisible(many)
        self.thumbnails_button.setVisible(many)
        self.thumbnail_panel.setVisible(many)
        self.thumbnails_button.blockSignals(True)
        self.thumbnails_button.setChecked(many)
        self.thumbnails_button.blockSignals(False)
        self._balance_control_groups()

    def _show_index(self, index):
        if not self._images:
            return
        index = max(0, min(int(index), len(self._images) - 1))
        if index != self._index or self.canvas.image.isNull():
            self._index = index
            self.canvas.set_image(self._images[self._index])
        self.sheet_label.setText(
            f"{self._index + 1} / {len(self._images)}"
        )
        self.previous_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index < len(self._images) - 1)
        self.thumbnails.blockSignals(True)
        try:
            self.thumbnails.setCurrentRow(self._index)
        finally:
            self.thumbnails.blockSignals(False)

    def _previous(self):
        self._show_index(self._index - 1)

    def _next(self):
        self._show_index(self._index + 1)

    def _thumbnail_clicked(self, item):
        index = item.data(_enum(Qt, "ItemDataRole", "UserRole"))
        if index is None:
            return
        self._show_index(int(index))

    def _toggle_thumbnails(self, checked):
        self.thumbnail_panel.setVisible(checked and len(self._images) > 1)
        if checked:
            self._show_index(self._index)
            item = self.thumbnails.item(self._index)
            if item is not None:
                self.thumbnails.scrollToItem(item)
