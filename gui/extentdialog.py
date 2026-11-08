import math
from dataclasses import dataclass

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import QRectF, QSize, Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QShortcut,
    QVBoxLayout,
)

from ..core import i18n, nominatim
from ..core.bbox import BBox, parse_bbox


@dataclass(frozen=True)
class Basemap:
    """Подложка карты в окне выбора охвата."""

    key: str
    name: str
    template: str
    zmax: int
    attribution: str

    @property
    def uri(self):
        """URI слоя тайлов.

        Внутренние ``&`` адреса кодируются: иначе разбор URI разорвал бы
        шаблон Google на отдельные параметры.
        """
        url = self.template.replace("&", "%26")
        return f"type=xyz&url={url}&zmax={self.zmax}&zmin=0"


# Подложки предпросмотра.  В готовую карту они не попадают: там только
# данные OSM и рельеф.
BASEMAPS = (
    Basemap(
        "osm",
        "OpenStreetMap",
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        19,
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        " contributors",
    ),
    Basemap(
        "opentopo",
        "OpenTopoMap",
        "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        17,
        '© <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA), '
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        " contributors",
    ),
    Basemap(
        "arcgis",
        "ArcGIS Topo",
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        19,
        '© <a href="https://www.esri.com">Esri</a>, HERE, Garmin, USGS, '
        "Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), "
        "Esri (Thailand), TomTom",
    ),
    Basemap(
        "google_satellite",
        "Google Satellite",
        "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        20,
        '© <a href="https://www.google.com">Google</a>',
    ),
    Basemap(
        "google_hybrid",
        "Google Hybrid",
        "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        20,
        '© <a href="https://www.google.com">Google</a>',
    ),
)

BASEMAP_BY_KEY = {basemap.key: basemap for basemap in BASEMAPS}

OSM_TILE_URI = BASEMAPS[0].uri
MERCATOR_EXTENT = 20037508.342789244
MAX_LATITUDE = 85.0511287798

# Размер окна вокруг точки, если у найденного места нет границ.
PLACE_SPAN = 0.02

DRAW_TOOL_STYLE = (
    "QPushButton {"
    " border: 2px solid palette(highlight);"
    " border-radius: 4px;"
    " padding: 2px;"
    " background-color: palette(base);"
    "}"
    "QPushButton:checked {"
    " background-color: palette(highlight);"
    "}"
)


def _left_button():
    try:
        return Qt.MouseButton.LeftButton
    except AttributeError:
        return Qt.LeftButton


# Подписи размеров — общие для окна выбора охвата и разметки листов
# атласа, поэтому живут в core/measure.py.
from ..core.measure import format_length, frame_sizes_km  # noqa: E402,F401


def scale_bar(meters_per_pixel, max_length=140.0):
    """Длина полосы шкалы в пикселях и её подпись.

    Длина подбирается так, чтобы соответствовать «круглому»
    расстоянию: 1, 2 или 5 × 10ⁿ метров.
    """
    if not meters_per_pixel or meters_per_pixel <= 0:
        return 0.0, ""
    target = meters_per_pixel * max_length
    exponent = math.floor(math.log10(target)) if target > 0 else 0
    best = 0.0
    for factor in (1.0, 2.0, 5.0):
        value = factor * (10 ** exponent)
        if value <= target and value > best:
            best = value
    if best <= 0:
        best = target
    length = best / meters_per_pixel
    if length < 10 or length > max_length:
        return 0.0, ""
    if best >= 1000:
        return length, f"{best / 1000:g} км"
    return length, f"{best:g} м"


class MeasuredCanvas(QgsMapCanvas):
    """Холст, подписывающий размеры сторон рамки охвата.

    Рядом с каждой осью рамки показывается длина стороны: по горизонтали —
    ширина, по вертикали — высота. Подписи рисуются поверх карты, поэтому
    отдельный слой или элемент управления не нужны. В правом нижнем углу
    рисуется шкала масштаба.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = None
        self._width_label = ""
        self._height_label = ""
        self._frame_provider = None

    def set_frame(self, rectangle, width_label="", height_label=""):
        """Задаёт рамку (в координатах холста) и подписи её сторон."""
        self._frame = QgsRectangle(rectangle) if rectangle is not None else None
        self._width_label = width_label or ""
        self._height_label = height_label or ""
        self.update()

    def set_frame_provider(self, provider):
        """Источник рамки и подписей.

        Поставщик вызывается при каждой отрисовке: подписи размеров
        сторон идут за рамкой даже при перемещении карты, когда сама
        рамка в координатах не меняется.
        """
        self._frame_provider = provider
        self.update()

    def _current_frame(self):
        if self._frame_provider is not None:
            try:
                rectangle, width_label, height_label = self._frame_provider()
            except Exception:
                return None, "", ""
            if rectangle is None:
                return None, "", ""
            return QgsRectangle(rectangle), width_label, height_label
        return self._frame, self._width_label, self._height_label

    def paintEvent(self, event):
        super().paintEvent(event)
        self._draw_scale_bar()
        frame, self._width_label, self._height_label = self._current_frame()
        if frame is None or not self._width_label or not self._height_label:
            return
        # QgsMapCanvas рисует карту во viewport(): сам виджет — только рамка,
        # и на нём QPainter не активируется.
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            transform = self.getCoordinateTransform()

            def device(x, y):
                point = transform.transform(QgsPointXY(x, y))
                return point.x(), point.y()

            left, top = device(frame.xMinimum(), frame.yMaximum())
            right, bottom = device(frame.xMaximum(), frame.yMinimum())
            height = metrics.height()
            gap = 3.0
            view = self.viewport()
            # Горизонтальная ось: ширина — одна подпись под нижней стороной
            # рамки (если не помещается — над верхней).
            if bottom + gap + height <= view.height():
                width_rect = QRectF(left, bottom + gap, right - left, height)
            else:
                width_rect = QRectF(left, top - height - gap, right - left, height)
            self._draw_label(painter, width_rect, self._width_label, font, metrics)
            # Вертикальная ось: высота — одна подпись вплотную к левой
            # стороне рамки, текст повёрнут (если не помещается — к правой).
            band = height + 2
            length = metrics.horizontalAdvance(self._height_label) + 6
            middle = (top + bottom) / 2
            if left - gap - band >= 0:
                side_rect = QRectF(left - gap - band, middle - length / 2, band, length)
            else:
                side_rect = QRectF(right + gap, middle - length / 2, band, length)
            self._draw_label(
                painter, side_rect, self._height_label, font, metrics, rotate=True
            )
        finally:
            painter.end()

    def _draw_scale_bar(self):
        """Шкала масштаба в правом нижнем углу карты.

        Длина полоски подбирается так, чтобы она соответствовала
        «круглому» расстоянию (1, 2 или 5 × 10ⁿ метров).
        """
        painter = QPainter(self.viewport())
        try:
            view = self.viewport()
            pixels = view.width()
            if pixels <= 0:
                return
            # Метров в пикселе: ширина видимой области (в CRS холста —
            # EPSG:3857, то есть в метрах) делим на ширину окна.
            # Прежде использовался знаменатель масштаба, а это другое
            # число, и полоса выходила в тысячи раз короче.
            extent = self.extent()
            span = extent.width()
            if not span or span <= 0:
                return
            meters_per_pixel = span / pixels
            metrics = painter.fontMetrics()
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            painter.setFont(font)
            length, text = scale_bar(meters_per_pixel)
            if not length:
                return
            margin = 10
            right = view.width() - margin
            bottom = view.height() - margin
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Классическая шкала: чередующиеся чёрные и белые четверти,
            # подпись — над полосой.  Раньше полоса сливалась с рамкой.
            bar_height = 6.0
            bar_top = bottom - bar_height
            segment = length / 4.0
            for index in range(4):
                left = right - length + index * segment
                rect = QRectF(left, bar_top, segment, bar_height)
                painter.setPen(QPen(QColor(25, 25, 25), 1))
                painter.setBrush(
                    QColor(25, 25, 25) if index % 2 == 0
                    else QColor(255, 255, 255)
                )
                painter.drawRect(rect)
            painter.setPen(QPen(QColor(25, 25, 25), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawText(
                QRectF(
                    right - length, bar_top - metrics.height() - 2,
                    length, metrics.height(),
                ),
                Qt.AlignCenter,
                text,
            )
        finally:
            painter.end()

    @staticmethod
    def _draw_label(painter, rect, text, font, metrics, rotate=False):
        """Подпись без подложки: тёмные буквы с белой обводкой.

        Обводка нужна, чтобы подпись читалась на любом фоне карты.
        """
        painter.save()
        if rotate:
            # Текст вдоль вертикальной оси: поворот на 90° против часовой
            # стрелки, подпись читается снизу вверх.
            painter.translate(rect.center())
            painter.rotate(-90)
            rect = QRectF(
                -rect.height() / 2, -rect.width() / 2, rect.height(), rect.width()
            )
        x = rect.left() + (rect.width() - metrics.horizontalAdvance(text)) / 2
        y = rect.top() + (rect.height() - metrics.height()) / 2 + metrics.ascent()
        path = QPainterPath()
        path.addText(x, y, font, text)
        pen = QPen(QColor(255, 255, 255), 3)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(25, 25, 25))
        painter.drawPath(path)
        painter.restore()


class RectangleMapTool(QgsMapTool):
    """Drag with the left mouse button to outline a rectangular extent."""

    rectangleDrawn = pyqtSignal(object)
    rectangleChanged = pyqtSignal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.start_point = None
        self.end_point = None
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(0, 120, 215))
        self.band.setFillColor(QColor(0, 120, 215, 40))
        self.band.setWidth(2)
        self.band.hide()

    def canvasPressEvent(self, event):
        if event.button() != _left_button():
            return
        self.start_point = self.toMapCoordinates(event.pos())
        self.end_point = self.start_point
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self._draw_band()

    def canvasMoveEvent(self, event):
        if self.start_point is None:
            return
        self.end_point = self.toMapCoordinates(event.pos())
        self._draw_band()
        # Подписи размеров сторон идут за рамкой, пока её рисуют.
        rectangle = self._rectangle()
        if rectangle.width() > 0 and rectangle.height() > 0:
            self.rectangleChanged.emit(rectangle)

    def canvasReleaseEvent(self, event):
        if self.start_point is None or event.button() != _left_button():
            return
        self.end_point = self.toMapCoordinates(event.pos())
        rectangle = self._rectangle()
        self.start_point = None
        self.end_point = None
        if rectangle.width() > 0 and rectangle.height() > 0:
            self.rectangleDrawn.emit(rectangle)

    def _rectangle(self):
        rectangle = QgsRectangle(self.start_point, self.end_point)
        rectangle.normalize()
        return rectangle

    def _draw_band(self):
        self.band.setToGeometry(QgsGeometry.fromRect(self._rectangle()), None)
        self.band.show()

    def show_rectangle(self, rectangle):
        """Показать готовый прямоугольник без рисования мышью."""
        if rectangle is None or rectangle.isEmpty():
            return
        self.band.setToGeometry(QgsGeometry.fromRect(rectangle), None)
        self.band.show()

    def clear(self):
        self.start_point = None
        self.end_point = None
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.band.hide()


class NominatimWorker(QThread):
    """Поиск мест по Nominatim в отдельном потоке."""

    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, query, parent=None):
        super().__init__(parent)
        self.query = query

    def run(self):
        try:
            places = nominatim.search(self.query)
        except Exception as exc:  # noqa: BLE001 - сеть не должна ломать окно
            self.failed.emit(str(exc))
        else:
            self.result.emit(places)


class ExtentDialog(QDialog):
    def __init__(self, bbox_text="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор охвата карты")
        self._fitted_to_screen = False
        self._fit_to_screen()
        self._bbox = None
        self._drawn_extent = None
        self._frame_labels = ("", "")
        self._search_worker = None
        self._wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        self._mercator = QgsCoordinateReferenceSystem("EPSG:3857")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Поиск места: например, «Кисловодск»"
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._start_search)
        self.search_button = QPushButton("Найти")
        self.search_button.clicked.connect(self._start_search)
        self.search_status = QLabel("")
        self.search_status.setMinimumWidth(120)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.search_status)
        self.basemap_combo = QComboBox()
        for basemap in BASEMAPS:
            self.basemap_combo.addItem(basemap.name, basemap.key)
        self.basemap_combo.setToolTip("Подложка карты")
        self.basemap_combo.currentIndexChanged.connect(self._basemap_changed)
        search_row.addWidget(self.basemap_combo)
        layout.addLayout(search_row)

        # Подпись под подложкой меняется вместе с ней.
        self.attribution = QLabel("")
        self.attribution.setOpenExternalLinks(True)

        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(110)
        self.search_results.itemSelectionChanged.connect(
            self._search_result_selected
        )
        self.search_results.hide()
        layout.addWidget(self.search_results)

        tools = QHBoxLayout()
        self.draw_button = QPushButton()
        self.draw_button.setCheckable(True)
        self.draw_button.setIcon(
            QgsApplication.getThemeIcon("mActionSelectRectangle.svg")
        )
        self.draw_button.setIconSize(QSize(22, 22))
        self.draw_button.setMinimumSize(46, 38)
        self.draw_button.setAccessibleName("Нарисовать охват")
        self.draw_button.setStyleSheet(DRAW_TOOL_STYLE)
        if self.draw_button.icon().isNull():
            self.draw_button.setText("Нарисовать охват")
        self.draw_button.setToolTip(
            "Нарисовать охват: зажмите левую кнопку мыши на карте "
            "и протяните прямоугольник"
        )
        self.draw_button.toggled.connect(self._toggle_draw)
        tools.addWidget(self.draw_button)
        self.reset_button = QPushButton("Сбросить")
        self.reset_button.setEnabled(False)
        self.reset_button.setToolTip("Убрать нарисованный прямоугольник")
        self.reset_button.clicked.connect(self._reset_drawn)
        tools.addWidget(self.reset_button)
        tools.addStretch(1)
        self.coords = QLabel("")
        tools.addWidget(self.coords)
        layout.addLayout(tools)

        self.canvas = MeasuredCanvas(self)
        self.canvas.setCanvasColor(QColor("white"))
        self.canvas.enableAntiAliasing(True)
        self.canvas.setDestinationCrs(self._mercator)
        self.layer = None
        self._basemap_changed(0)
        self._pan_tool = QgsMapToolPan(self.canvas)
        self._draw_tool = RectangleMapTool(self.canvas)
        self._draw_tool.rectangleDrawn.connect(self._rectangle_drawn)
        self._draw_tool.rectangleChanged.connect(self._rectangle_changed)
        self.canvas.setMapTool(self._pan_tool)
        initial, remembered = self._initial_extent(bbox_text)
        self.canvas.setExtent(
            self._padded_extent(initial) if remembered else initial
        )
        if remembered:
            self._drawn_extent = QgsRectangle(initial)
            self._draw_tool.show_rectangle(self._drawn_extent)
            self.reset_button.setEnabled(True)
        self.canvas.extentsChanged.connect(self._update_coords)
        self.canvas.extentsChanged.connect(self._update_measurement)
        self.canvas.set_frame_provider(self._frame_provider)
        self.canvas.viewport().installEventFilter(self)
        layout.addWidget(self.canvas, 1)

        hint = QLabel(
            "Перетаскивайте карту мышью, колесом меняйте масштаб. Найдите "
            "место в строке поиска или нажмите кнопку с иконкой прямоугольника "
            "и протяните рамку, чтобы задать область точнее. Затем нажмите "
            "«Использовать охват»."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        bottom.addWidget(self.attribution)
        bottom.addStretch(1)
        use_button = QPushButton("Использовать охват")
        use_button.setDefault(True)
        use_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        bottom.addWidget(use_button)
        bottom.addWidget(cancel_button)
        layout.addLayout(bottom)

        # Enter применяет охват и закрывает окно: канвас забирает фокус,
        # поэтому нужен явный shortcut.  Он живёт на канвасе, чтобы в
        # строке поиска Enter запускал поиск, а не закрывал окно.
        for key in (Qt.Key_Return, Qt.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.canvas)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(self.accept)

        # Esc отменяет выбор и закрывает окно: карта перехватывает его
        # для отмены рисования, поэтому shortcut задаётся явно.
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WindowShortcut)
        escape.activated.connect(self.reject)

        self._update_coords()
        self._update_measurement()

    def keyPressEvent(self, event):
        # Enter в строке поиска запускает поиск, а не применяет охват:
        # строка отдаёт клавишу диалогу, иначе окно закрылось бы.
        if QApplication.focusWidget() is self.search_input:
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _fit_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1100, 760)
            return
        available = screen.availableGeometry()
        width = min(available.width(), max(760, round(available.width() * 0.94)))
        height = min(available.height(), max(520, round(available.height() * 0.94)))
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def showEvent(self, event):
        if not self._fitted_to_screen:
            self._fitted_to_screen = True
            self._fit_to_screen()
        # Фокус у карты: Enter применяет охват, а в строке поиска
        # запускает поиск.
        self.canvas.setFocus()
        super().showEvent(event)

    def _toggle_draw(self, active):
        if active:
            self.canvas.setMapTool(self._draw_tool)
        else:
            self.canvas.setMapTool(self._pan_tool)

    def _basemap_changed(self, _index):
        """Сменить подложку карты."""
        basemap = BASEMAP_BY_KEY.get(self.basemap_combo.currentData())
        if basemap is None:
            return
        layer = QgsRasterLayer(basemap.uri, basemap.name, "wms")
        self.layer = layer
        self.canvas.setLayers([layer] if layer.isValid() else [])
        self.attribution.setText(basemap.attribution)

    def _start_search(self):
        """Запустить поиск места по Nominatim."""
        query = self.search_input.text().strip()
        if not query:
            return
        worker = self._search_worker
        if worker is not None:
            try:
                if worker.isRunning():
                    return
            except RuntimeError:
                # Поток уже удалён: можно искать снова.
                self._search_worker = None
        self.search_button.setEnabled(False)
        self.search_status.setText(i18n.tr("Поиск…"))
        # Поток — не ребёнок окна: окно может закрыться во время запроса,
        # и тогда поток должен спокойно доработать и удалиться сам.
        worker = NominatimWorker(query, QgsApplication.instance())
        worker.result.connect(self._search_done)
        worker.failed.connect(self._search_failed)
        worker.finished.connect(self._search_finished)
        worker.finished.connect(worker.deleteLater)
        self._search_worker = worker
        worker.start()

    def _search_done(self, places):
        """Результаты поиска показаны списком."""
        self.search_results.clear()
        for place in places:
            item = QListWidgetItem(place.name)
            item.setData(Qt.UserRole, place)
            self.search_results.addItem(item)
        self.search_results.setVisible(bool(places))
        if places:
            self.search_status.setText(
                i18n.trf("Найдено: {}", len(places))
            )
        else:
            self.search_status.setText(i18n.tr("Ничего не найдено"))

    def _search_failed(self, message):
        self.search_results.clear()
        self.search_results.hide()
        self.search_status.setText(
            i18n.trf("Ошибка поиска: {}", message)
        )

    def _search_finished(self):
        """Поиск закончен — кнопки снова доступны."""
        self.search_button.setEnabled(True)
        self._search_worker = None

    def _search_result_selected(self):
        """Выбран результат поиска — переходим к найденному месту."""
        items = self.search_results.selectedItems()
        if not items:
            return
        place = items[0].data(Qt.UserRole)
        if place is None:
            return
        self._show_place(place)

    def _show_place(self, place):
        """Перейти к найденному месту: центрировать и приблизить."""
        extent = self._place_extent(place)
        if extent is None:
            return
        # Рамка, нарисованная раньше, больше не задаёт охват: иначе
        # переход к месту ничего бы не менял.
        self._reset_drawn()
        self.canvas.setExtent(extent)
        self.canvas.refresh()
        self._update_coords()
        self._update_measurement()

    def _place_extent(self, place):
        """Прямоугольник найденного места в координатах холста."""
        bounds = place.bounds()
        if bounds is None:
            west = east = place.lon
            south = north = place.lat
        else:
            west, south, east, north = bounds
        transform = QgsCoordinateTransform(
            self._wgs84, self._mercator, QgsProject.instance()
        )
        extent = transform.transformBoundingBox(
            QgsRectangle(west, south, east, north)
        )
        extent.normalize()
        if extent.width() <= 0 or extent.height() <= 0:
            # Точка без границ: небольшое окно вокруг неё.
            center = transform.transform(QgsPointXY(place.lon, place.lat))
            half = PLACE_SPAN * MERCATOR_EXTENT / 180.0
            extent = QgsRectangle(
                center.x() - half, center.y() - half,
                center.x() + half, center.y() + half,
            )
        return self._padded_extent(extent)

    def _rectangle_drawn(self, rectangle):
        if rectangle is None or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._drawn_extent = QgsRectangle(rectangle)
        self._drawn_extent.normalize()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(True)
        self._update_coords()
        self._update_measurement()

    def _rectangle_changed(self, rectangle):
        """Рамка меняется прямо во время рисования — обновляем подписи."""
        if rectangle is None or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._drawn_extent = QgsRectangle(rectangle)
        self._drawn_extent.normalize()
        self._update_coords()
        self._update_measurement()

    def _reset_drawn(self):
        self._drawn_extent = None
        self._draw_tool.clear()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(False)
        self._update_coords()
        self._update_measurement()

    @staticmethod
    def _padded_extent(extent):
        """Отступ от краёв, чтобы границы прямоугольника не шли по краям."""
        margin = max(extent.width(), extent.height()) * 0.12
        padded = QgsRectangle(extent)
        padded.grow(margin)
        return padded

    def _initial_extent(self, bbox_text):
        """Прямоугольник карты и признак того, что охват был задан."""
        if bbox_text:
            try:
                bbox = parse_bbox(bbox_text)
            except ValueError:
                pass
            else:
                lat_min = max(-MAX_LATITUDE, bbox.lat_min)
                lat_max = min(MAX_LATITUDE, bbox.lat_max)
                transform = QgsCoordinateTransform(
                    self._wgs84, self._mercator, QgsProject.instance()
                )
                extent = transform.transformBoundingBox(
                    QgsRectangle(bbox.lon_min, lat_min, bbox.lon_max, lat_max)
                )
                extent.normalize()
                return extent, True
        return QgsRectangle(
            -MERCATOR_EXTENT, -MERCATOR_EXTENT, MERCATOR_EXTENT, MERCATOR_EXTENT
        ), False

    def _update_coords(self, *_args):
        try:
            bbox = self._extent_as_bbox()
        except Exception:
            self.coords.setText("")
            return
        self.coords.setText(
            f"{bbox.lon_min:.4f}, {bbox.lat_min:.4f}, "
            f"{bbox.lon_max:.4f}, {bbox.lat_max:.4f}"
        )

    def _extent_as_bbox(self):
        transform = QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(), self._wgs84, QgsProject.instance()
        )
        source = self._frame_rectangle()
        extent = transform.transformBoundingBox(source)
        lon_min = max(-180.0, extent.xMinimum())
        lat_min = max(-90.0, extent.yMinimum())
        lon_max = min(180.0, extent.xMaximum())
        lat_max = min(90.0, extent.yMaximum())
        if lon_min >= lon_max or lat_min >= lat_max:
            raise ValueError("Visible extent is outside the valid coordinates")
        return BBox(lon_min, lat_min, lon_max, lat_max)

    def bbox_csv(self):
        return self._bbox.as_csv()

    def _frame_rectangle(self):
        """Рамка охвата в координатах холста: нарисованная или текущая."""
        if self._drawn_extent is not None:
            return self._drawn_extent
        return self.canvas.extent()

    def _update_measurement(self, *_args):
        """Подписи размеров сторон рамки."""
        try:
            bbox = self._extent_as_bbox()
        except Exception:
            self._frame_labels = ("", "")
            self.canvas.update()
            return
        width_km, height_km = frame_sizes_km(bbox)
        self._frame_labels = (
            format_length(width_km),
            format_length(height_km),
        )
        self.canvas.update()

    def _frame_provider(self):
        """Рамка и подписи для отрисовки.

        Вызывается при каждой отрисовке холста: подписи размеров сторон
        идут за рамкой, даже когда карта перемещается, а сама рамка в
        координатах не меняется.
        """
        labels = getattr(self, "_frame_labels", ("", ""))
        return self._frame_rectangle(), labels[0], labels[1]

    def accept(self):
        try:
            self._bbox = self._extent_as_bbox()
        except Exception as exc:
            QMessageBox.warning(
                self, "QGIS-xtopo",
                i18n.trf("Не удалось определить охват: {}", exc),
            )
            return
        super().accept()

    def done(self, result):
        self._draw_tool.clear()
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        super().done(result)
