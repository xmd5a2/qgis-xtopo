"""Просмотр листов экспорта в пониженном разрешении.

Листы рисуются тем же макетом и атласом, что и настоящий экспорт, но
с длинной стороной около 1280 пикселей.  Просмотрщик — обычное окно:
колесо мыши меняет масштаб, перетаскивание сдвигает лист, кнопки
листают, отдельная кнопка показывает сетку миниатюр.  Экспорт в PNG/PDF
запускается сигналом: саму запись и выбор файла делает панель проекта,
поэтому качество и настройки те же, что и без просмотра.
"""

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import i18n

THUMBNAIL_SIZE = QSize(150, 200)
MAX_ZOOM = 8.0
WHEEL_FACTOR = 1.2


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
    """Один лист: вписан в окно, с зумом и перетаскиванием."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._scale = 1.0
        self._fit_scale = 1.0
        self._offset = QPointF()
        self._drag_from = None
        self._user_zoomed = False
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

    def set_image(self, image):
        self._image = image if image is not None else QImage()
        self._reset_view()

    def reset_view(self):
        """Вписать лист в окно."""
        self._reset_view()

    def zoom_at(self, factor, anchor=None):
        """Увеличить/уменьшить изображение вокруг точки ``anchor``."""
        if self._image.isNull():
            return
        if anchor is None:
            anchor = QPointF(self.width() / 2.0, self.height() / 2.0)
        scale = min(
            max(self._scale * factor, self._fit_scale),
            self._fit_scale * MAX_ZOOM,
        )
        if abs(scale - self._scale) < 1e-6:
            return
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        image_point = (anchor - center - self._offset) / self._scale
        self._offset = anchor - center - image_point * scale
        self._scale = scale
        self._user_zoomed = True
        self.update()

    def _reset_view(self):
        self._user_zoomed = False
        self._offset = QPointF()
        self._fit_scale = self._fit_scale_for(self._image)
        self._scale = self._fit_scale
        self.update()

    def _fit_scale_for(self, image):
        if image.isNull() or not image.width() or not image.height():
            return 1.0
        width = max(1, self.width() - 16)
        height = max(1, self.height() - 16)
        return min(width / image.width(), height / image.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._user_zoomed:
            self._fit_scale = self._fit_scale_for(self._image)
            self._scale = self._fit_scale
            self._offset = QPointF()
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
            self._drag_from = _event_position(event)
            self.setCursor(_enum(Qt, "CursorShape", "ClosedHandCursor"))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            position = _event_position(event)
            self._offset += position - self._drag_from
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
        self._reset_view()
        event.accept()


class ExportPreviewDialog(QDialog):
    """Простый просмотрщик готовых листов и запуск экспорта."""

    exportRequested = pyqtSignal(str)

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self._images = [image for image in images if not image.isNull()]
        self._index = 0
        self.setWindowTitle(i18n.tr("Предпросмотр экспорта"))
        self.setWindowIcon(_theme_icon("mActionZoomIn.svg"))
        self.setAttribute(
            _enum(Qt, "WidgetAttribute", "WA_DeleteOnClose"), True
        )
        self.setMinimumSize(640, 480)
        self.resize(900, 720)
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

        self.stack = QStackedWidget(self)
        self.canvas = PreviewCanvas(self)
        self.thumbnails = QListWidget(self)
        self.thumbnails.setViewMode(
            _enum(QListView, "ViewMode", "IconMode")
        )
        self.thumbnails.setResizeMode(
            _enum(QListView, "ResizeMode", "Adjust")
        )
        self.thumbnails.setMovement(
            _enum(QListView, "Movement", "Static")
        )
        self.thumbnails.setWrapping(True)
        self.thumbnails.setIconSize(THUMBNAIL_SIZE)
        self.thumbnails.setGridSize(QSize(170, 236))
        self.thumbnails.setSpacing(4)
        self.thumbnails.setWordWrap(False)
        self.thumbnails.itemClicked.connect(self._thumbnail_clicked)
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.thumbnails)
        layout.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        bar.setSpacing(6)
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
        self.thumbnails_button = QPushButton("Миниатюры", self)
        self.thumbnails_button.setCheckable(True)
        self.thumbnails_button.setToolTip(
            i18n.tr("Показать листы сеткой миниатюр")
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
        bar.addWidget(self.previous_button)
        bar.addWidget(self.sheet_label)
        bar.addWidget(self.next_button)
        bar.addStretch(1)
        bar.addWidget(self.thumbnails_button)
        bar.addWidget(self.png_button)
        bar.addWidget(self.pdf_button)
        layout.addLayout(bar)

        self.previous_button.clicked.connect(self._previous)
        self.next_button.clicked.connect(self._next)
        self.thumbnails_button.toggled.connect(self._toggle_thumbnails)
        self.png_button.clicked.connect(
            lambda: self.exportRequested.emit("png")
        )
        self.pdf_button.clicked.connect(
            lambda: self.exportRequested.emit("pdf")
        )

    @property
    def sheet_count(self):
        return len(self._images)

    @property
    def current_index(self):
        return self._index

    def _fill_thumbnails(self):
        for number, image in enumerate(self._images, 1):
            thumbnail = image.scaled(
                THUMBNAIL_SIZE,
                _enum(Qt, "AspectRatioMode", "KeepAspectRatio"),
                _enum(Qt, "TransformationMode", "SmoothTransformation"),
            )
            item = QListWidgetItem(QIcon(QPixmap.fromImage(thumbnail)), "")
            item.setText(str(number))
            item.setData(_enum(Qt, "ItemDataRole", "UserRole"), number - 1)
            item.setTextAlignment(
                _enum(Qt, "AlignmentFlag", "AlignHCenter")
            )
            self.thumbnails.addItem(item)

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
        self.thumbnails_button.setChecked(False)

    def _toggle_thumbnails(self, checked):
        self.stack.setCurrentWidget(
            self.thumbnails if checked else self.canvas
        )
        if checked:
            self._show_index(self._index)
            item = self.thumbnails.item(self._index)
            if item is not None:
                self.thumbnails.scrollToItem(item)
