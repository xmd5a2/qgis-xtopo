import math
import re
import time
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLayoutExporter,
    QgsLayoutItem,
    QgsLayoutItemLabel,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRuleBasedRenderer,
    Qgis,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtPrintSupport import QPrintDialog, QPrinter
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # QGIS 4 / Qt 6
    from qgis.PyQt.QtGui import QAction

from ..core.project import fit_project_extent
from .atlas import SheetMapTool, SheetMarker
from .projecticons import terrain_icon

PAGE_FORMATS = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A5": (148.0, 210.0),
}

MEMORY_BUDGET = 800 * 1024 * 1024

# Сколько кратных отметок перебирается при поиске общей линии у двух правил.
CONFLICT_SEARCH_LIMIT = 1000


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)

ISOLINES_LAYER = "[TERRAIN] isolines regular"
GLACIER_ISOLINES_LAYER = "[TERRAIN] isolines glacier"
STEEP_LAYER = "[TERRAIN] slope (steep)"
SLIGHT_LAYER = "[TERRAIN] slope (slight)"
HILLSHADE_LAYER = "[TERRAIN] hillshade"
MOUNTAIN_LAYER = "mountain area"

LAYER_NAMES = (
    ISOLINES_LAYER,
    GLACIER_ISOLINES_LAYER,
    STEEP_LAYER,
    SLIGHT_LAYER,
    HILLSHADE_LAYER,
    MOUNTAIN_LAYER,
)

# Слои, у которых в панели есть шкала прозрачности.
OPACITY_LAYERS = (STEEP_LAYER, SLIGHT_LAYER, HILLSHADE_LAYER)


def _tool_button_icon_only():
    try:
        return Qt.ToolButtonStyle.ToolButtonIconOnly
    except AttributeError:
        return Qt.ToolButtonIconOnly


class ProjectPanel(QWidget):
    """Map settings for a project prepared by QGIS-xtopo."""

    statusMessage = pyqtSignal(str)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._syncing = False
        self._tool_syncing = False
        self._root = None
        self._style_layer = None
        self._pan_tool = None
        self._canvas_object = None
        self._layer_boxes = []
        self._rule_checks = []
        self._rule_available = {}
        self._sheets = []
        self._sheet_items = []
        self._sheet_tool = None
        self._atlas_layer = None
        self._layout_original_layers = None
        self._layout_visible_states = None
        self._opacity_baseline = {}
        self._baseline_project = None
        self._build_ui()
        self._connect_root()

    @staticmethod
    def is_xtopo_project(project=None):
        project = project or QgsProject.instance()
        try:
            layers = list(project.mapLayers().values())
        except Exception:
            return False
        for layer in layers:
            if layer.name() != "BBOX":
                continue
            source = (layer.source() or "").split("|", 1)[0]
            if Path(source).name == "crop.geojson" and Path(source).exists():
                return True
        file_name = project.fileName()
        if file_name:
            crop = Path(file_name).parent / "crop.geojson"
            names = {layer.name() for layer in layers}
            if crop.exists() and names.intersection(LAYER_NAMES):
                return True
        return False

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(3)

        self.toolbar = QToolBar(self)
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setToolButtonStyle(_tool_button_icon_only())
        self.fit_action = QAction(
            QgsApplication.getThemeIcon("mActionZoomFullExtent.svg"),
            "Вписать карту в экран",
            self,
        )
        self.fit_action.setToolTip("Вписать карту в экран")
        self.fit_action.triggered.connect(self.fit_to_screen)
        self.pan_action = QAction(
            QgsApplication.getThemeIcon("mActionPan.svg"),
            "Прокрутка карты",
            self,
        )
        self.pan_action.setToolTip("Прокрутка карты: перетаскивание мышью")
        self.pan_action.setCheckable(True)
        self.pan_action.toggled.connect(self._toggle_pan)
        self.toolbar.addAction(self.fit_action)
        self.toolbar.addAction(self.pan_action)
        layout.addWidget(self.toolbar)

        self.isolines_check = self._checkbox(
            "Контуры высот (изогипсы)", "isolines"
        )
        self.isolines_check.toggled.connect(
            lambda checked: self._layer_box_toggled(ISOLINES_LAYER, checked)
        )
        layout.addWidget(self.isolines_check)
        self.rules_title = QLabel("Кратность:", self)
        self.rules_title.setStyleSheet("padding-left: 20px;")
        self.rules_title.setVisible(False)
        layout.addWidget(self.rules_title)
        self.rules_container = QWidget(self)
        self.rules_layout = QGridLayout(self.rules_container)
        self.rules_layout.setContentsMargins(20, 0, 0, 0)
        self.rules_layout.setHorizontalSpacing(6)
        self.rules_layout.setVerticalSpacing(2)
        layout.addWidget(self.rules_container)

        self.steep_check = self._checkbox(
            "Крутые склоны", "steep"
        )
        self.steep_check.toggled.connect(
            lambda checked: self._layer_box_toggled(STEEP_LAYER, checked)
        )
        self.steep_opacity = self._layer_row(self.steep_check, STEEP_LAYER)
        layout.addWidget(self.steep_opacity)

        self.slight_check = self._checkbox(
            "Равнинные участки", "slight"
        )
        self.slight_check.toggled.connect(
            lambda checked: self._layer_box_toggled(SLIGHT_LAYER, checked)
        )
        self.slight_opacity = self._layer_row(self.slight_check, SLIGHT_LAYER)
        layout.addWidget(self.slight_opacity)

        self.hillshade_check = self._checkbox(
            "Затенение рельефа", "hillshade1"
        )
        self.hillshade_check.toggled.connect(
            lambda checked: self._layer_box_toggled(HILLSHADE_LAYER, checked)
        )
        self.hillshade_opacity = self._layer_row(
            self.hillshade_check, HILLSHADE_LAYER
        )
        layout.addWidget(self.hillshade_opacity)

        self.mountain_check = self._checkbox(
            "Подписи горных массивов", "labels"
        )
        self.mountain_check.toggled.connect(
            lambda checked: self._layer_box_toggled(MOUNTAIN_LAYER, checked)
        )
        layout.addWidget(self.mountain_check)
        self._build_atlas_ui(layout)
        layout.addStretch(1)

        self._layer_boxes = [
            (self.steep_check, STEEP_LAYER),
            (self.slight_check, SLIGHT_LAYER),
            (self.hillshade_check, HILLSHADE_LAYER),
            (self.mountain_check, MOUNTAIN_LAYER),
        ]
        self._opacity_widgets = {
            STEEP_LAYER: self.steep_opacity,
            SLIGHT_LAYER: self.slight_opacity,
            HILLSHADE_LAYER: self.hillshade_opacity,
        }

    def _layer_row(self, check_box, layer_name):
        """Строка слоя рельефа: галочка, шкала прозрачности, значение, сброс."""
        row = QWidget(self)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(4)
        slider = QSlider(Qt.Horizontal, row)
        slider.setRange(0, 100)
        slider.setValue(100)
        slider.setFixedWidth(70)
        slider.setToolTip(
            "Прозрачность слоя «%s»" % layer_name.replace("[TERRAIN] ", "")
        )
        value = QLabel("100%", row)
        value.setMinimumWidth(34)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        reset = QToolButton(row)
        reset.setIcon(QgsApplication.getThemeIcon("mActionReload.svg"))
        reset.setToolTip("Сбросить прозрачность до исходного значения")
        reset.setAutoRaise(True)
        reset.setFixedSize(QSize(18, 18))
        reset.setIconSize(QSize(14, 14))
        reset.setEnabled(False)
        reset.clicked.connect(
            lambda _checked=False, ln=layer_name: self._reset_opacity(ln)
        )
        line.addWidget(check_box, 1)
        line.addWidget(slider)
        line.addWidget(value)
        line.addWidget(reset)
        slider.valueChanged.connect(
            lambda number: self._opacity_changed(layer_name, number, value)
        )
        row.setProperty("slider", slider)
        row.setProperty("value_label", value)
        row.setProperty("check_box", check_box)
        row.setProperty("reset_button", reset)
        return row

    def _checkbox(self, text, icon_kind):
        box = QCheckBox(text, self)
        icon = terrain_icon(icon_kind, self.palette())
        if not icon.isNull():
            box.setIcon(icon)
            box.setIconSize(QSize(16, 16))
        return box

    def _build_atlas_ui(self, layout):
        self.atlas_group = QGroupBox("Атлас", self)
        group_layout = QVBoxLayout(self.atlas_group)
        group_layout.setContentsMargins(4, 4, 4, 4)
        group_layout.setSpacing(4)

        options = QHBoxLayout()
        options.setSpacing(4)
        options.addWidget(QLabel("Формат:"))
        self.page_format = QComboBox()
        for code in ("A4", "A3", "A5"):
            self.page_format.addItem(code, code)
        options.addWidget(self.page_format)
        options.addSpacing(6)
        options.addWidget(QLabel("Ориентация:"))
        self.page_orientation = QComboBox()
        self.page_orientation.addItem("Альбомная", "landscape")
        self.page_orientation.addItem("Портретная", "portrait")
        options.addWidget(self.page_orientation)
        options.addStretch(1)
        group_layout.addLayout(options)

        quality = QHBoxLayout()
        quality.setSpacing(4)
        quality.addWidget(QLabel("Разрешение, dpi:"))
        self.export_dpi = QComboBox()
        for value in (96, 150, 300):
            self.export_dpi.addItem(str(value), value)
        self.export_dpi.setCurrentIndex(self.export_dpi.findData(300))
        self.export_dpi.setToolTip(
            "Больше dpi — выше качество и нагрузка на память. "
            "При нехватке памяти используйте 150 или 96."
        )
        quality.addWidget(self.export_dpi)
        quality.addStretch(1)
        group_layout.addLayout(quality)

        sheet_buttons = QHBoxLayout()
        self.add_sheet_button = QPushButton(
            QgsApplication.getThemeIcon("mActionAdd.svg"), "Добавить лист"
        )
        self.add_sheet_button.setCheckable(True)
        self.add_sheet_button.setToolTip(
            "Нарисовать на карте прямоугольник листа с пропорциями формата"
        )
        self.remove_sheet_button = QPushButton(
            QgsApplication.getThemeIcon("mActionRemove.svg"), "Удалить"
        )
        self.remove_sheet_button.setToolTip("Удалить последний лист")
        self.clear_sheets_button = QPushButton(
            QgsApplication.getThemeIcon("mActionDeleteSelected.svg"),
            "Очистить",
        )
        self.clear_sheets_button.setToolTip("Удалить все листы")
        for button in (
            self.add_sheet_button, self.remove_sheet_button,
            self.clear_sheets_button,
        ):
            button.setIconSize(QSize(16, 16))
            sheet_buttons.addWidget(button)
        sheet_buttons.addStretch(1)
        group_layout.addLayout(sheet_buttons)

        self.sheets_label = QLabel("Листов: 0 — будет экспортирован текущий вид")
        self.sheets_label.setWordWrap(True)
        group_layout.addWidget(self.sheets_label)
        hint = QLabel(
            "Нажмите «Добавить лист» и протяните рамку на карте. "
            "Номер листа рисуется в его центре."
        )
        hint.setWordWrap(True)
        group_layout.addWidget(hint)

        export_buttons = QHBoxLayout()
        self.export_png_button = QPushButton(
            QgsApplication.getThemeIcon("mActionFileSave.svg"), "PNG"
        )
        self.export_pdf_button = QPushButton(
            QgsApplication.getThemeIcon("mActionExport.svg"), "PDF"
        )
        self.print_button = QPushButton(
            QgsApplication.getThemeIcon("mActionFilePrint.svg"), "Печать"
        )
        for button in (
            self.export_png_button, self.export_pdf_button, self.print_button,
        ):
            button.setIconSize(QSize(16, 16))
            export_buttons.addWidget(button)
        group_layout.addLayout(export_buttons)
        layout.addWidget(self.atlas_group)

        self.page_format.currentIndexChanged.connect(self._atlas_page_changed)
        self.page_orientation.currentIndexChanged.connect(
            self._atlas_page_changed
        )
        self.add_sheet_button.toggled.connect(self._toggle_sheet_tool)
        self.remove_sheet_button.clicked.connect(self._remove_last_sheet)
        self.clear_sheets_button.clicked.connect(self._clear_sheets)
        self.export_png_button.clicked.connect(
            lambda: self._export("png")
        )
        self.export_pdf_button.clicked.connect(
            lambda: self._export("pdf")
        )
        self.print_button.clicked.connect(self._print_atlas)
        self._update_sheet_controls()

    def _update_sheet_controls(self):
        count = len(self._sheets)
        if count:
            text = f"Листов: {count}"
        else:
            text = "Листов: 0 — будет экспортирован текущий вид"
        self.sheets_label.setText(text)
        self.remove_sheet_button.setEnabled(count > 0)
        self.clear_sheets_button.setEnabled(count > 0)

    def _page_size_mm(self):
        width, height = PAGE_FORMATS.get(
            self.page_format.currentData(), PAGE_FORMATS["A4"]
        )
        landscape = self.page_orientation.currentData() == "landscape"
        if landscape and width < height:
            width, height = height, width
        elif not landscape and width > height:
            width, height = height, width
        return width, height

    def _page_aspect(self):
        width, height = self._page_size_mm()
        return width / height

    def _atlas_page_changed(self, _index=None):
        if self._sheet_tool is not None:
            self._sheet_tool.set_aspect(self._page_aspect())
        if self._sheets:
            aspect = self._page_aspect()
            for index, (rectangle, crs) in enumerate(self._sheets):
                center = rectangle.center()
                area = rectangle.width() * rectangle.height()
                width = math.sqrt(max(area, 1e-9) * aspect)
                height = width / aspect
                self._sheets[index] = (
                    QgsRectangle(
                        center.x() - width / 2,
                        center.y() - height / 2,
                        center.x() + width / 2,
                        center.y() + height / 2,
                    ),
                    crs,
                )
            self._rebuild_sheet_items()
        self._remove_atlas_layer()

    def _canvas(self):
        if self.iface is None:
            return None
        canvas = getattr(self.iface, "mapCanvas", None)
        if callable(canvas):
            return canvas()
        return canvas

    def _freeze_canvas(self):
        canvas = self._canvas()
        if canvas is not None and hasattr(canvas, "freeze"):
            try:
                canvas.freeze(True)
            except Exception:
                pass
            if hasattr(canvas, "stopRendering"):
                try:
                    canvas.stopRendering()
                except Exception:
                    pass
        return canvas

    def _unfreeze_canvas(self, canvas):
        if canvas is not None and hasattr(canvas, "freeze"):
            try:
                canvas.freeze(False)
            except Exception:
                pass

    def _toggle_sheet_tool(self, active):
        if self._tool_syncing:
            return
        canvas = self._canvas()
        if active:
            if canvas is None:
                self.add_sheet_button.setChecked(False)
                return
            if self._sheet_tool is None:
                self._sheet_tool = SheetMapTool(canvas, self._page_aspect())
                self._sheet_tool.sheetDrawn.connect(self._sheet_drawn)
            self._sheet_tool.set_aspect(self._page_aspect())
            self._sheet_tool.set_number(len(self._sheets) + 1)
            canvas.setMapTool(self._sheet_tool)
        elif self._sheet_tool is not None and canvas is not None:
            canvas.unsetMapTool(self._sheet_tool)
            self._sheet_tool._clear_preview()

    def _sheet_drawn(self, rectangle):
        crs = QgsCoordinateReferenceSystem(
            self._canvas().mapSettings().destinationCrs()
        )
        self._sheets.append((QgsRectangle(rectangle), crs))
        number = len(self._sheets)
        self._insert_sheet_items(QgsRectangle(rectangle), crs, number)
        if self._sheet_tool is not None:
            self._sheet_tool.set_number(number + 1)
        self._update_sheet_controls()
        self._remove_atlas_layer()
        self.statusMessage.emit(f"Лист {number} добавлен")

    def _insert_sheet_items(self, rectangle, crs, number):
        canvas = self._canvas()
        if canvas is None:
            return
        band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        band.setColor(QColor(0, 120, 215))
        band.setFillColor(QColor(0, 120, 215, 30))
        band.setWidth(2)
        band.setToGeometry(QgsGeometry.fromRect(rectangle), crs)
        band.show()
        marker = SheetMarker(canvas, number)
        marker.set_map_point(rectangle.center())
        marker.show()
        self._sheet_items.append((band, marker))

    def _dispose_sheet_item(self, item):
        band, marker = item
        band.reset(QgsWkbTypes.PolygonGeometry)
        band.hide()
        marker.hide()
        for graphics_item in (marker, band):
            try:
                scene = graphics_item.scene()
                if scene is not None:
                    scene.removeItem(graphics_item)
            except Exception:
                pass

    def _dispose_sheet_tool(self):
        if self._sheet_tool is None:
            return
        try:
            self._sheet_tool.dispose()
        except Exception:
            pass
        self._sheet_tool = None

    def _rebuild_sheet_items(self):
        for item in self._sheet_items:
            self._dispose_sheet_item(item)
        self._sheet_items = []
        for number, (rectangle, crs) in enumerate(self._sheets, 1):
            self._insert_sheet_items(rectangle, crs, number)
        if self._sheet_tool is not None:
            self._sheet_tool.set_number(len(self._sheets) + 1)
        self._update_sheet_controls()

    def _remove_last_sheet(self):
        if not self._sheets:
            return
        self._sheets.pop()
        item = self._sheet_items.pop()
        self._dispose_sheet_item(item)
        if self._sheet_tool is not None:
            self._sheet_tool.set_number(len(self._sheets) + 1)
        self._update_sheet_controls()
        self._remove_atlas_layer()
        self.statusMessage.emit("Последний лист удалён")

    def clear_atlas(self):
        if (
            self._sheets
            or self._sheet_items
            or self._atlas_layer is not None
            or self.add_sheet_button.isChecked()
        ):
            self._clear_sheets()

    def reset_atlas_state(self):
        """Сбросить состояние атласа, не трогая очищаемый проект.

        Вызывается при закрытии проекта: слои и макеты всё равно удаляются
        вместе с проектом, а обращение к нему из сигнала «cleared» приводит
        к падению QGIS.
        """
        self.add_sheet_button.setChecked(False)
        self._sheets = []
        self._sheet_items = []
        self._atlas_layer = None
        self._dispose_sheet_tool()
        self._update_sheet_controls()

    def _clear_sheets(self):
        if self.add_sheet_button.isChecked():
            self.add_sheet_button.setChecked(False)
        self._sheets = []
        self._rebuild_sheet_items()
        self._remove_atlas_layer()
        self._dispose_sheet_tool()
        self.statusMessage.emit("Листы атласа очищены")

    def _remove_atlas_layer(self):
        layout = QgsProject.instance().layoutManager().layoutByName("Detailed")
        if layout is not None:
            try:
                # Пока карта управляется атласом, она держит ссылку на слой
                # покрытия: слой нельзя удалять, не отключив эту связь.
                for item in layout.items():
                    if isinstance(item, QgsLayoutItemMap) and item.atlasDriven():
                        item.setAtlasDriven(False)
                layout.atlas().setEnabled(False)
                layout.atlas().setCoverageLayer(None)
            except Exception:
                pass
        if self._atlas_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(self._atlas_layer.id())
            except Exception:
                pass
            self._atlas_layer = None

    def _visible_layers_in_order(self):
        """Visible layers in legend order (topmost first), as QgsMapSettings expects."""
        project = QgsProject.instance()
        try:
            return list(project.mapThemeCollection().masterVisibleLayers())
        except Exception:
            root = project.layerTreeRoot()
            if root is None:
                return list(project.mapLayers().values())
            return [
                node.layer()
                for node in root.findLayers()
                if node.layer() is not None
            ]

    def _layers_for_extent(self, map_crs, extent):
        project = QgsProject.instance()
        layers = []
        for layer in self._visible_layers_in_order():
            if layer is None or not layer.isValid():
                continue
            try:
                if layer.crs() != map_crs:
                    transform = QgsCoordinateTransform(
                        map_crs, layer.crs(), project
                    )
                    layer_extent = transform.transformBoundingBox(extent)
                else:
                    layer_extent = extent
                if not layer.extent().intersects(layer_extent):
                    continue
                if isinstance(layer, QgsVectorLayer):
                    request = QgsFeatureRequest()
                    request.setFilterRect(layer_extent)
                    request.setLimit(1)
                    if next(layer.getFeatures(request), None) is None:
                        continue
            except Exception:
                pass
            layers.append(layer)
        return layers

    def _configure_map_layers(self, map_item, extent):
        if self._layout_original_layers is None:
            self._layout_original_layers = (
                map_item, list(map_item.layers())
            )
        filtered = self._layers_for_extent(map_item.crs(), extent)
        map_item.setLayers(filtered)

    def _restore_map_layers(self):
        self._layout_visible_states = None
        if self._layout_original_layers is None:
            return
        map_item, layers = self._layout_original_layers
        self._layout_original_layers = None
        try:
            map_item.setLayers(layers)
        except Exception:
            pass
        self._sync_from_project()

    def _create_atlas_layer(self, map_crs):
        self._remove_atlas_layer()
        crs_id = map_crs.authid() or "EPSG:3857"
        layer = QgsVectorLayer(
            f"Polygon?crs={crs_id}&field=sheet:integer",
            "Xtopo atlas sheets",
            "memory",
        )
        provider = layer.dataProvider()
        sheet_index = layer.fields().indexOf("sheet")
        for number, (rectangle, crs) in enumerate(self._sheets, 1):
            if crs != map_crs:
                transform = QgsCoordinateTransform(
                    crs, map_crs, QgsProject.instance()
                )
                rectangle = transform.transformBoundingBox(rectangle)
            feature = QgsFeature(layer.fields())
            feature.setAttribute(sheet_index, number)
            feature.setGeometry(QgsGeometry.fromRect(rectangle))
            provider.addFeature(feature)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer, False)
        self._atlas_layer = layer
        return layer

    def _configure_layout(self, filename_prefix=None):
        layout = QgsProject.instance().layoutManager().layoutByName("Detailed")
        if layout is None:
            raise RuntimeError("В проекте нет макета печати «Detailed»")
        width, height = self._page_size_mm()
        units = _enum(QgsUnitTypes, "LayoutUnit", "LayoutMillimeters")
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(width, height, units))
        map_item = next(
            (item for item in layout.items() if isinstance(item, QgsLayoutItemMap)),
            None,
        )
        if map_item is None:
            raise RuntimeError("В макете «Detailed» нет карты")
        map_item.attemptMove(QgsLayoutPoint(0, 0, units))
        map_item.attemptResize(QgsLayoutSize(width, height, units))
        scale_bar = next(
            (
                item for item in layout.items()
                if isinstance(item, QgsLayoutItemScaleBar)
            ),
            None,
        )
        if scale_bar is not None:
            scale_bar.attemptMove(
                QgsLayoutPoint(max(0.0, 5.0), max(0.0, height - 5.589), units)
            )
        number_item = next(
            (
                item for item in layout.items()
                if isinstance(item, QgsLayoutItemLabel)
                and "@atlas_featurenumber" in item.text()
            ),
            None,
        )
        if number_item is not None:
            # В макете номер листа стоит в левом верхнем углу страницы.
            number_item.setReferencePoint(
                _enum(QgsLayoutItem, "ReferencePoint", "UpperLeft")
            )
            number_item.attemptMove(QgsLayoutPoint(0.0, 0.0, units))
        copyright_item = next(
            (
                item for item in layout.items()
                if isinstance(item, QgsLayoutItemLabel)
                and "OpenStreetMap" in item.text()
            ),
            None,
        )
        if copyright_item is not None:
            # В макете копирайт прижат к правому нижнему углу страницы:
            # опорная точка элемента — его правый нижний угол.
            copyright_item.setReferencePoint(
                _enum(QgsLayoutItem, "ReferencePoint", "LowerRight")
            )
            copyright_item.attemptMove(
                QgsLayoutPoint(width, height, units)
            )
        atlas = layout.atlas()
        if self._sheets:
            union = QgsRectangle()
            for rectangle, crs in self._sheets:
                if crs != map_item.crs():
                    transform = QgsCoordinateTransform(
                        crs, map_item.crs(), QgsProject.instance()
                    )
                    rectangle = transform.transformBoundingBox(rectangle)
                if union.isEmpty():
                    union = QgsRectangle(rectangle)
                else:
                    union.combineExtentWith(rectangle)
        else:
            canvas = self._canvas()
            if canvas is not None:
                union = canvas.extent()
                canvas_crs = canvas.mapSettings().destinationCrs()
                if canvas_crs != map_item.crs():
                    transform = QgsCoordinateTransform(
                        canvas_crs, map_item.crs(), QgsProject.instance()
                    )
                    union = transform.transformBoundingBox(union)
            else:
                union = map_item.extent()
        self._configure_map_layers(map_item, union)
        if self._sheets:
            layer = self._create_atlas_layer(map_item.crs())
            atlas.setCoverageLayer(layer)
            atlas.setEnabled(True)
            atlas.setSortFeatures(True)
            atlas.setSortExpression("sheet")
            atlas.setSortAscending(True)
            atlas.setHideCoverage(True)
            # Выражение имени файла задаётся здесь, а не перед экспортом:
            # смена выражения во время рендера атласа ломает память в QGIS.
            prefix = (filename_prefix or "sheet").replace("'", "''")
            atlas.setFilenameExpression(
                f"'{prefix}_' || @atlas_featurenumber"
            )
            map_item.setAtlasDriven(True)
            map_item.setAtlasScalingMode(
                _enum(QgsLayoutItemMap, "AtlasScalingMode", "Auto")
            )
            map_item.setAtlasMargin(0.0)
        else:
            self._remove_atlas_layer()
            atlas.setEnabled(False)
            atlas.setCoverageLayer(None)
            map_item.setAtlasDriven(False)
            map_item.zoomToExtent(union)
        return layout, atlas

    def _default_export_path(self, extension):
        project_path = QgsProject.instance().fileName()
        if project_path:
            base = Path(project_path).parent
            name = Path(project_path).stem
        else:
            base = Path.home()
            name = "map"
        return str(base / f"{name}.{extension}")

    def _export(self, extension):
        atlas_export = bool(self._sheets)
        if atlas_export:
            title = f"Экспорт атласа {extension.upper()}"
        else:
            title = f"Экспорт {extension.upper()}"
        dialog = QFileDialog(self, title)
        dialog.setAcceptMode(_enum(QFileDialog, "AcceptMode", "AcceptSave"))
        dialog.setFileMode(_enum(QFileDialog, "FileMode", "AnyFile"))
        dialog.setNameFilter(f"{extension.upper()} (*.{extension})")
        default = self._default_export_path(extension)
        dialog.setDirectory(str(Path(default).parent))
        dialog.selectFile(default)
        if atlas_export:
            # При экспорте атласа файлы получают суффиксы (_1, _2, ...),
            # базовый файл не перезаписывается — подтверждение не нужно.
            dialog.setOption(
                _enum(QFileDialog, "FileDialogOption", "DontConfirmOverwrite"),
                True,
            )
        accepted = _enum(QDialog, "DialogCode", "Accepted")
        if dialog.exec() != accepted:
            return
        files = dialog.selectedFiles()
        if not files or not files[0]:
            return
        self._export_to(files[0], extension)

    def _layout_layer_count(self, layout):
        map_item = next(
            (item for item in layout.items() if isinstance(item, QgsLayoutItemMap)),
            None,
        )
        if map_item is not None and map_item.layers():
            return len(map_item.layers())
        root = QgsProject.instance().layerTreeRoot()
        if root is None:
            return len(QgsProject.instance().mapLayers())
        return sum(
            1 for layer in QgsProject.instance().mapLayers().values()
            if root.findLayer(layer.id()) is not None
            and root.findLayer(layer.id()).itemVisibilityChecked()
        )

    def _estimate_export_memory(self, layout, dpi):
        width, height = self._page_size_mm()
        pixels = (width / 25.4 * dpi) * (height / 25.4 * dpi)
        # The map is painted directly onto the page image; full-size per-layer
        # buffers are only needed for raster layers and layers with blending.
        return (1 + self._rasterized_layer_count(layout)) * pixels * 4

    def _rasterized_layer_count(self, layout):
        map_item = next(
            (item for item in layout.items() if isinstance(item, QgsLayoutItemMap)),
            None,
        )
        if map_item is not None and map_item.layers():
            layers = map_item.layers()
        else:
            root = QgsProject.instance().layerTreeRoot()
            layers = [
                layer
                for layer in QgsProject.instance().mapLayers().values()
                if root is not None
                and (node := root.findLayer(layer.id())) is not None
                and node.itemVisibilityChecked()
            ]
        count = 0
        for layer in layers:
            if isinstance(layer, QgsRasterLayer):
                count += 1
            elif layer.blendMode() != 0 or layer.opacity() < 1.0:
                count += 1
        return count

    def _safe_export_dpi(self, layout, preferred):
        for dpi in (preferred, 150, 96):
            if self._estimate_export_memory(layout, dpi) <= MEMORY_BUDGET:
                return dpi
        return 96

    def _confirm_export_dpi(self, layout, preferred):
        estimate = self._estimate_export_memory(layout, preferred)
        if estimate <= MEMORY_BUDGET:
            return preferred
        safe = self._safe_export_dpi(layout, preferred)
        if safe >= preferred:
            return preferred
        box = QMessageBox(self)
        box.setIcon(_enum(QMessageBox, "Icon", "Warning"))
        box.setWindowTitle("QGIS-xtopo")
        box.setText(
            f"Экспорт при {preferred} dpi может потребовать около "
            f"{estimate / (1024 ** 3):.1f} ГБ памяти."
        )
        box.setInformativeText(f"Снизить разрешение до {safe} dpi?")
        lower = box.addButton(
            f"Снизить до {safe} dpi",
            _enum(QMessageBox, "ButtonRole", "AcceptRole"),
        )
        proceed = box.addButton(
            "Продолжить", _enum(QMessageBox, "ButtonRole", "DestructiveRole")
        )
        cancel = box.addButton(
            "Отмена", _enum(QMessageBox, "ButtonRole", "RejectRole")
        )
        box.setDefaultButton(lower)
        if hasattr(box, "exec"):
            box.exec()
        else:
            box.exec_()
        clicked = box.clickedButton()
        if clicked is cancel:
            return None
        if clicked is lower:
            self.export_dpi.setCurrentIndex(
                self.export_dpi.findData(safe)
            )
            return safe
        return preferred

    def _export_to(self, path, extension):
        canvas = self._freeze_canvas()
        try:
            self._export_to_impl(path, extension)
        finally:
            self._unfreeze_canvas(canvas)

    def _export_to_impl(self, path, extension):
        try:
            layout, atlas = self._configure_layout(Path(path).stem)
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", str(exc))
            return
        self._export_layout(layout, atlas, path, extension)

    def _export_layout(self, layout, atlas, path, extension):
        dpi = self._confirm_export_dpi(
            layout, int(self.export_dpi.currentData() or 150)
        )
        if dpi is None:
            self._remove_atlas_layer()
            self._restore_map_layers()
            return
        self.statusMessage.emit(f"Экспорт {extension.upper()}…")
        self._notify(f"Экспорт {extension.upper()}…")
        started = time.monotonic()
        QApplication.setOverrideCursor(
            _enum(Qt, "CursorShape", "WaitCursor")
        )
        try:
            if extension == "png":
                settings = QgsLayoutExporter.ImageExportSettings()
                settings.dpi = dpi
                exporter = QgsLayoutExporter(layout)
                if atlas.enabled():
                    base = str(Path(path).with_suffix(""))
                    result, error = exporter.exportToImage(
                        atlas, base, "png", settings
                    )
                else:
                    result = exporter.exportToImage(path, settings)
                    error = ""
            else:
                settings = QgsLayoutExporter.PdfExportSettings()
                settings.dpi = dpi
                exporter = QgsLayoutExporter(layout)
                if atlas.enabled():
                    result, error = exporter.exportToPdf(
                        atlas, path, settings
                    )
                else:
                    result = exporter.exportToPdf(path, settings)
                    error = ""
        except Exception as exc:
            QMessageBox.critical(self, "QGIS-xtopo", f"Ошибка экспорта: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
            self._remove_atlas_layer()
            self._restore_map_layers()
        self._export_result(result, error, path, extension, elapsed=time.monotonic() - started)

    def _print_atlas(self):
        canvas = self._freeze_canvas()
        try:
            self._print_atlas_impl()
        finally:
            self._unfreeze_canvas(canvas)

    def _print_atlas_impl(self):
        try:
            layout, atlas = self._configure_layout()
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", str(exc))
            return
        self._print_layout(layout, atlas)

    def _print_layout(self, layout, atlas):
        dpi = self._confirm_export_dpi(
            layout, int(self.export_dpi.currentData() or 150)
        )
        if dpi is None:
            self._remove_atlas_layer()
            self._restore_map_layers()
            return
        try:
            mode = getattr(QPrinter, "HighResolution", None)
            if mode is None:
                mode = QPrinter.PrinterMode.HighResolution
            printer = QPrinter(mode)
            dialog = QPrintDialog(printer, self)
            if dialog.exec() != _enum(QDialog, "DialogCode", "Accepted"):
                return
            settings = QgsLayoutExporter.PrintExportSettings()
            settings.dpi = dpi
            exporter = QgsLayoutExporter(layout)
            if atlas.enabled():
                result, error = exporter.print(atlas, printer, settings)
            else:
                result = exporter.print(printer, settings)
                error = ""
        except Exception as exc:
            QMessageBox.critical(self, "QGIS-xtopo", f"Ошибка печати: {exc}")
            return
        finally:
            self._remove_atlas_layer()
            self._restore_map_layers()
        self._export_result(result, error, "", "print")

    def _export_result(self, result, error, path, kind, elapsed=None):
        if result == _enum(QgsLayoutExporter, "ExportResult", "Success"):
            if kind == "print":
                message = "Печать отправлена"
            else:
                message = f"Экспорт: {path}"
                if elapsed is not None:
                    message += f" ({elapsed:.1f} с)"
            self._notify(message)
            return
        detail = error or f"код {int(result)}"
        QMessageBox.critical(
            self, "QGIS-xtopo", f"Не удалось выполнить экспорт: {detail}"
        )

    def _notify(self, message):
        """Сообщение в журнал и всплывающее уведомление QGIS."""
        self.statusMessage.emit(message)
        iface = self.iface
        if iface is None:
            return
        try:
            bar = iface.messageBar()
            bar.pushMessage(
                "QGIS-xtopo",
                message,
                _enum(Qgis, "MessageLevel", "Info"),
                8,
            )
        except Exception:
            pass

    def refresh(self):
        self._connect_root()
        own = self.is_xtopo_project()
        self.setEnabled(own)
        self._update_tool_actions(own)
        if not own:
            self._style_layer = None
            return
        file_name = QgsProject.instance().fileName()
        if file_name != self._baseline_project:
            self._baseline_project = file_name
            self._opacity_baseline = {}
        self._connect_style_layer()
        self._rebuild_rules()
        self._sync_from_project()

    def focus_first(self):
        widget = self.toolbar.widgetForAction(self.fit_action)
        if widget is not None:
            widget.setFocus()

    def _canvas(self):
        if self.iface is None:
            return None
        canvas = getattr(self.iface, "mapCanvas", None)
        if callable(canvas):
            return canvas()
        return canvas

    def _update_tool_actions(self, own):
        canvas = self._canvas() if own else None
        self._connect_canvas(canvas)
        self.fit_action.setEnabled(canvas is not None)
        self.pan_action.setEnabled(canvas is not None)
        if canvas is None and self.pan_action.isChecked():
            self.pan_action.setChecked(False)

    def _connect_canvas(self, canvas):
        if canvas is self._canvas_object:
            return
        if self._canvas_object is not None:
            try:
                self._canvas_object.mapToolSet.disconnect(
                    self._map_tool_changed
                )
            except Exception:
                pass
        self._canvas_object = canvas
        if canvas is not None:
            try:
                canvas.mapToolSet.connect(self._map_tool_changed)
            except Exception:
                pass

    def fit_to_screen(self):
        canvas = self._canvas()
        if canvas is None:
            return
        if not fit_project_extent(self.iface, zoom=1.0) and hasattr(self.iface, "zoomFull"):
            try:
                self.iface.zoomFull()
            except Exception:
                pass

    def _toggle_pan(self, checked):
        if self._tool_syncing:
            return
        canvas = self._canvas()
        if canvas is None:
            return
        if checked:
            if self._pan_tool is None:
                self._pan_tool = QgsMapToolPan(canvas)
            canvas.setMapTool(self._pan_tool)
        elif self._pan_tool is not None:
            canvas.unsetMapTool(self._pan_tool)

    def _map_tool_changed(self, new_tool, _old_tool):
        if (
            self._pan_tool is not None
            and new_tool is not self._pan_tool
            and self.pan_action.isChecked()
        ):
            self._tool_syncing = True
            self.pan_action.setChecked(False)
            self._tool_syncing = False
        if (
            self._sheet_tool is not None
            and new_tool is not self._sheet_tool
            and self.add_sheet_button.isChecked()
        ):
            self._tool_syncing = True
            self.add_sheet_button.setChecked(False)
            self._tool_syncing = False

    def _connect_root(self):
        root = QgsProject.instance().layerTreeRoot()
        if root is self._root:
            return
        if self._root is not None:
            try:
                self._root.visibilityChanged.disconnect(
                    self._on_tree_visibility
                )
            except Exception:
                pass
        self._root = root
        if root is not None:
            root.visibilityChanged.connect(self._on_tree_visibility)

    def _connect_style_layer(self):
        layer = self._layer(ISOLINES_LAYER)
        if layer is self._style_layer:
            return
        if self._style_layer is not None:
            try:
                for name in ("rendererChanged", "styleChanged"):
                    signal = getattr(self._style_layer, name, None)
                    if signal is None:
                        continue
                    try:
                        signal.disconnect(self._on_layer_style_changed)
                    except Exception:
                        pass
            except RuntimeError:
                pass
        self._style_layer = layer
        if layer is None:
            return
        for name in ("rendererChanged", "styleChanged"):
            signal = getattr(layer, name, None)
            if signal is None:
                continue
            try:
                signal.connect(self._on_layer_style_changed)
            except Exception:
                pass

    def _on_tree_visibility(self, _node):
        self._sync_from_project()

    def _on_layer_style_changed(self, *_args):
        self._sync_from_project()

    def _layer(self, name):
        for layer in QgsProject.instance().mapLayersByName(name):
            if layer is not None:
                return layer
        return None

    def _layer_node(self, layer):
        root = QgsProject.instance().layerTreeRoot()
        return root.findLayer(layer.id()) if root is not None else None

    def _layer_visible(self, name):
        layer = self._layer(name)
        if layer is None:
            return False
        node = self._layer_node(layer)
        if node is not None:
            return node.itemVisibilityChecked()
        return False

    def _set_layer_visible(self, name, visible):
        layer = self._layer(name)
        if layer is None:
            return
        node = self._layer_node(layer)
        if node is not None:
            node.setItemVisibilityChecked(bool(visible))

    def _sync_from_project(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            available = {name: self._layer(name) is not None for name in LAYER_NAMES}
            visible = {name: self._layer_visible(name) for name in LAYER_NAMES}
            for box, name in self._layer_boxes:
                box.setEnabled(available[name])
                box.setChecked(visible[name])
            isolines_available = (
                available[ISOLINES_LAYER] or available[GLACIER_ISOLINES_LAYER]
            )
            isolines_visible = (
                visible[ISOLINES_LAYER] or visible[GLACIER_ISOLINES_LAYER]
            )
            self.isolines_check.setEnabled(isolines_available)
            self.isolines_check.setChecked(isolines_visible)
            self._sync_opacity_sliders()
            self._update_rule_checks(available[ISOLINES_LAYER])
        finally:
            self._syncing = False

    def _update_rule_checks(self, layer_available):
        active = self._rule_states()
        enabled = layer_available and self.isolines_check.isChecked()
        for box, label in self._rule_checks:
            box.setEnabled(
                enabled and self._rule_available.get(label, True)
            )
            if label in active:
                box.setChecked(active[label])

    def _rule_states(self):
        layer = self._layer(ISOLINES_LAYER)
        if layer is None:
            return {}
        renderer = layer.renderer()
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return {}
        return {
            rule.label().strip(): rule.active()
            for rule in renderer.rootRule().children()
        }

    def _rebuild_rules(self):
        while self.rules_layout.count():
            item = self.rules_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rule_checks = []
        layer = self._layer(ISOLINES_LAYER)
        renderer = layer.renderer() if layer is not None else None
        if not isinstance(renderer, QgsRuleBasedRenderer):
            self.rules_container.setVisible(False)
            self.rules_title.setVisible(False)
            return
        rules = []
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            try:
                value = int(label)
            except ValueError:
                continue
            rules.append((value, label, rule.active()))
        if not rules:
            self.rules_container.setVisible(False)
            self.rules_title.setVisible(False)
            return
        rules.sort()
        self._rule_available = self._available_rule_labels(layer, rules)
        for index, (value, label, active) in enumerate(rules):
            box = QCheckBox(f"{value} м", self.rules_container)
            box.setChecked(active)
            box.toggled.connect(
                lambda checked, rule_label=label: self._rule_toggled(
                    rule_label, checked
                )
            )
            self.rules_layout.addWidget(box, index // 4, index % 4)
            self._rule_checks.append((box, label))
        self.rules_title.setVisible(True)
        self.rules_container.setVisible(True)

    def _available_rule_labels(self, layer, rules):
        labels = [label for _value, label, _active in rules]
        root = layer.renderer().rootRule()
        expressions = {}
        for rule in root.children():
            label = rule.label().strip()
            if label not in labels:
                continue
            text = rule.filterExpression().strip()
            if not text:
                expressions[label] = None
                continue
            expression = QgsExpression(text)
            if expression.hasParserError():
                expressions[label] = None
                continue
            expressions[label] = expression
        found = {
            label: expressions.get(label, None) is None for label in labels
        }
        pending = {
            label: expression
            for label, expression in expressions.items()
            if expression is not None
        }
        if not pending:
            return found
        request = QgsFeatureRequest()
        geometry_needed = any(
            "$" in expression.expression() for expression in pending.values()
        )
        if not geometry_needed:
            flags = getattr(QgsFeatureRequest, "NoGeometry", None)
            if flags is None:
                flags = QgsFeatureRequest.Flag.NoGeometry
            request.setFlags(flags)
        columns = set()
        for expression in pending.values():
            columns.update(expression.referencedColumns())
        fields = layer.fields()
        indexes = [
            fields.indexOf(name)
            for name in columns
            if fields.indexOf(name) >= 0
        ]
        if indexes:
            request.setSubsetOfAttributes(indexes)
        context = layer.createExpressionContext()
        for feature in layer.getFeatures(request):
            context.setFeature(feature)
            for label, expression in list(pending.items()):
                try:
                    matches = bool(expression.evaluate(context))
                except Exception:
                    matches = False
                if matches:
                    found[label] = True
                    del pending[label]
            if not pending:
                break
        return found

    def _rule_toggled(self, label, checked):
        if self._syncing:
            return
        layer = self._layer(ISOLINES_LAYER)
        if layer is None:
            return
        renderer = layer.renderer()
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return
        values = self._rule_values()
        if not values:
            return
        try:
            value = int(label)
        except ValueError:
            return
        deps = self._rule_dependencies()
        available = self._available_values(values)
        states = self._rule_states()
        current = {
            item for item, name in values.items() if states.get(name)
        }
        conflicts = self._rule_conflicts(values)
        target = self._rule_target(
            checked, value, values, deps, available, current, conflicts
        )
        self._syncing = True
        try:
            self._set_rule_states(renderer, values, target)
            self._update_rule_checks(True)
        finally:
            self._syncing = False

    def _rule_dependencies(self):
        """Кратности, линии которых правило не рисует: взяты из фильтра.

        Например, правило «10 м» не рисует линии кратности 25, 50, 100 и
        1000 — значит, оно зависит от этих правил.
        """
        layer = self._layer(ISOLINES_LAYER)
        renderer = layer.renderer() if layer is not None else None
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return {}
        deps = {}
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            try:
                value = int(label)
            except ValueError:
                continue
            excluded = set()
            for match in re.finditer(
                r'\(\s*"ELEV"\s*%\s*(\d+)\s*\)\s*!=\s*0',
                rule.filterExpression() or "",
            ):
                excluded.add(int(match.group(1)))
            deps[value] = excluded
        return deps

    def _rule_filters(self):
        """Разбор фильтров правил: {кратность: (базовая, исключения)}.

        Правило «200 м» рисует отметки, кратные 200, но не кратные 1000, а
        правило «1000 м» — отметки, кратные 1000. Общих линий у них нет,
        поэтому конфликт есть не у всех кратных, а вычисляется по фильтрам.
        """
        layer = self._layer(ISOLINES_LAYER)
        renderer = layer.renderer() if layer is not None else None
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return {}
        filters = {}
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            try:
                value = int(label)
            except ValueError:
                continue
            expression = rule.filterExpression() or ""
            base = None
            for match in re.finditer(
                r'\(\s*"ELEV"\s*%\s*(\d+)\s*\)\s*=\s*0', expression
            ):
                base = int(match.group(1))
            excluded = {
                int(match.group(1))
                for match in re.finditer(
                    r'\(\s*"ELEV"\s*%\s*(\d+)\s*\)\s*!=\s*0', expression
                )
            }
            filters[value] = (base, excluded)
        return filters

    @staticmethod
    def _rules_conflict(first, second):
        """True, если два правила рисуют хотя бы одну общую линию."""
        (base_first, excluded_first), (base_second, excluded_second) = first, second
        if not base_first or not base_second:
            return False
        excluded = excluded_first | excluded_second
        step = base_first * base_second // math.gcd(base_first, base_second)
        for multiplier in range(1, CONFLICT_SEARCH_LIMIT + 1):
            elevation = step * multiplier
            if elevation % base_first or elevation % base_second:
                continue
            if any(elevation % value == 0 for value in excluded):
                continue
            return True
        return False

    def _rule_conflicts(self, values=None):
        """Пары кратностей, которые нельзя включать одновременно.

        При конфликте остаётся более мелкая кратность: её правило рисует и
        линии более крупной кратности, поэтому пропусков не будет.
        """
        filters = self._rule_filters()
        if not filters:
            return []
        conflicts = []
        items = sorted(filters)
        for index, first in enumerate(items):
            for second in items[index + 1:]:
                if self._rules_conflict(filters[first], filters[second]):
                    conflicts.append((first, second))
        return conflicts

    def _available_values(self, values):
        """Кратности, для которых в данных есть линии."""
        return {
            value for value, label in values.items()
            if self._rule_available.get(label, True)
        }

    def _close_rule_set(self, target, deps, available, add):
        """Привести набор к правилам без пропусков в линиях.

        Правило, которое не рисует линии отключённой кратности, оставило бы
        пропуск: при снятии кратности такое правило выключается, при включении
        — включается недостающая кратность.
        """
        changed = True
        while changed:
            changed = False
            for value in sorted(target):
                missing = (deps.get(value, set()) & available) - target
                if not missing:
                    continue
                if add:
                    target |= missing
                else:
                    target.discard(value)
                changed = True
        return target

    def _rule_values(self):
        """Кратности, доступные в слое изолиний: {значение: подпись правила}."""
        layer = self._layer(ISOLINES_LAYER)
        renderer = layer.renderer() if layer is not None else None
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return {}
        values = {}
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            try:
                values[int(label)] = label
            except ValueError:
                continue
        return values

    def _rule_target(self, checked, value, values, deps, available, current,
                     conflicts=None):
        """Набор включённых кратностей после действия пользователя."""
        if checked:
            # Базовая кратность задаёт набор кратных, как на вкладке «Рельеф».
            target = {
                item for item in values
                if item % value == 0 and item in available
            }
            target = self._close_rule_set(target, deps, available, add=True)
        else:
            # Снятая кратность убирает и более мелкие: их фильтр исключает
            # эти линии, иначе в изолиниях появляются пропуски.
            target = set(current)
            target.discard(value)
            target = self._close_rule_set(target, deps, available, add=False)
        if conflicts is None:
            conflicts = self._rule_conflicts(values)
        for smaller, larger in conflicts:
            if smaller in target and larger in target:
                target.discard(larger)
        return target

    def _set_rule_states(self, renderer, values, target):
        labels = {values[value] for value in target}
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            if label not in values.values():
                continue
            rule.setActive(label in labels)
        layer = self._layer(ISOLINES_LAYER)
        if layer is not None:
            layer.triggerRepaint()
            try:
                layer.emitStyleChanged()
            except Exception:
                pass

    def _layer_box_toggled(self, name, checked):
        if self._syncing:
            return
        self._syncing = True
        try:
            self._set_layer_visible(name, checked)
            if name == ISOLINES_LAYER:
                # Контуры высот управляют и изолиниями ледников.
                self._set_layer_visible(GLACIER_ISOLINES_LAYER, checked)
        finally:
            self._syncing = False
        self._sync_from_project()

    def _opacity_changed(self, layer_name, number, label):
        """Шкала прозрачности слоя рельефа."""
        label.setText(f"{number}%")
        if self._syncing:
            return
        layer = self._layer(layer_name)
        if layer is None:
            return
        try:
            layer.setOpacity(number / 100.0)
        except Exception:
            return
        layer.triggerRepaint()
        try:
            layer.emitStyleChanged()
        except Exception:
            pass

    def _reset_opacity(self, layer_name):
        """Возвращает слою прозрачность из шаблона проекта."""
        value = self._opacity_baseline.get(layer_name)
        widget = self._opacity_widgets.get(layer_name)
        slider = widget.property("slider") if widget is not None else None
        if value is None or slider is None:
            return
        slider.setValue(value)

    def _sync_opacity_sliders(self):
        """Показывает в шкалах прозрачность слоёв из проекта."""
        for layer_name, widget in getattr(self, "_opacity_widgets", {}).items():
            slider = widget.property("slider")
            label = widget.property("value_label")
            check_box = widget.property("check_box")
            reset_button = widget.property("reset_button")
            if slider is None or label is None:
                continue
            layer = self._layer(layer_name)
            value = 100
            if layer is not None:
                try:
                    value = int(round(layer.opacity() * 100))
                except Exception:
                    value = 100
            self._opacity_baseline.setdefault(layer_name, value)
            slider.blockSignals(True)
            slider.setValue(max(0, min(100, value)))
            slider.blockSignals(False)
            label.setText(f"{value}%")
            enabled = layer is not None and (
                check_box is None or check_box.isChecked()
            )
            slider.setEnabled(enabled)
            label.setEnabled(enabled)
            if reset_button is not None:
                reset_button.setEnabled(
                    enabled and layer_name in self._opacity_baseline
                )
