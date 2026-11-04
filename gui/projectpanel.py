import math
import re
import time
import zipfile
from pathlib import Path

from qgis.core import (
    QgsAbstractVectorLayerLabeling,
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRenderer,
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
    QgsReadWriteContext,
    QgsRectangle,
    QgsRuleBasedLabeling,
    QgsRuleBasedRenderer,
    Qgis,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import QEventLoop, QPoint, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor, QFontMetrics, QIcon, QPainter, QPalette, QPen, QPixmap,
)
from qgis.PyQt.QtPrintSupport import QPrintDialog, QPrinter
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QSlider, QStyle, QStyleOptionComboBox, QStylePainter,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)
from qgis.PyQt.QtXml import QDomDocument

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # QGIS 4 / Qt 6
    from qgis.PyQt.QtGui import QAction

from ..core import i18n, map_language
from ..core.project import fit_project_extent
from ..core.resources import template_path
from .atlas import (
    SheetEditMapTool, SheetMapTool, SheetMarker, SheetSizeLabels,
)
from .projecticons import terrain_icon
from .translation import translate_tree

PAGE_FORMATS = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A5": (148.0, 210.0),
}

MEMORY_BUDGET = 800 * 1024 * 1024

# Сколько кратных отметок перебирается при поиске общей линии у двух правил.
CONFLICT_SEARCH_LIMIT = 1000

# Краткая подпись в поле списка: полные названия видны в раскрытом списке.
MAP_LANGUAGE_SHORT = {
    code: ("Местные" if code == map_language.LOCAL_LANGUAGE else code.upper())
    for code, _label, _script in map_language.MAP_LANGUAGES
}


class MapLanguageCombo(QComboBox):
    """Список языков подписей: краткий код в поле, полные названия в списке."""

    @staticmethod
    def short_label(code):
        return i18n.tr(MAP_LANGUAGE_SHORT.get(code, code or ""))

    def short_text(self):
        return self.short_label(self.currentData())

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.short_text()
        painter.drawComplexControl(
            _enum(QStyle, "ComplexControl", "CC_ComboBox"), option
        )
        painter.drawControl(
            _enum(QStyle, "ControlElement", "CE_ComboBoxLabel"), option
        )


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

# Варианты окраски крутых склонов: это три слоя с общей подложкой
# slope_upscaled.tif, но разными цветовыми шкалами — для гор, предгорий
# и равнин. Показывается только один вариант, остальные скрыты.
#
# Привязка строго по полному имени слоя: в проекте есть и другие слои
# с суффиксом «var1», их нельзя путать с вариантом окраски уклонов.
STEEP_VARIANTS = (
    ("[TERRAIN] slope (steep) var1", "Горы"),
    ("[TERRAIN] slope (steep) var2", "Предгорья"),
    ("[TERRAIN] slope (steep) var3", "Равнины"),
)
MOUNTAIN_LAYER = "mountain area"
NATURE_RESERVE_LAYER = "nature_reserve"

LAYER_NAMES = (
    ISOLINES_LAYER,
    GLACIER_ISOLINES_LAYER,
    STEEP_LAYER,
    SLIGHT_LAYER,
    HILLSHADE_LAYER,
    MOUNTAIN_LAYER,
    NATURE_RESERVE_LAYER,
)

# Слои, у которых в панели есть шкала прозрачности.
OPACITY_LAYERS = (STEEP_LAYER, SLIGHT_LAYER, HILLSHADE_LAYER)

# Слой пересыхающей воды: стиль берётся из шаблона проекта.
WATER_LAYER_NAME = "water intermittent"

# Шкала затенения рельефа задаёт прозрачность теней: 50% — исходный вид
# шаблона, меньше — темнее (0% — тени непрозрачные), больше — светлее.
HILLSHADE_DEFAULT = 50

# Переменная проекта, на которую умножается ширина линий изогипс.
ISOLINES_WIDTH_VARIABLE = "isolines_width_mult"


def _instant_popup():
    """Режим кнопки, открывающей меню сразу по нажатию."""
    try:
        return QToolButton.ToolButtonPopupMode.InstantPopup
    except AttributeError:
        return QToolButton.InstantPopup


def _tool_button_icon_only():
    try:
        return Qt.ToolButtonStyle.ToolButtonIconOnly
    except AttributeError:
        return Qt.ToolButtonIconOnly


def _ramp_items(items, baseline, number):
    """Цветовая шкала для заданной прозрачности теней.

    number=50 — исходный вид, меньше — темнее (прозрачность теней меньше),
    больше — светлее.
    """
    factor = (100.0 - number) / float(HILLSHADE_DEFAULT)
    updated = []
    for item, base in zip(items, baseline):
        alpha = max(0, min(255, int(round(base * factor))))
        color = QColor(item.color)
        color.setAlpha(alpha)
        updated.append(
            QgsColorRampShader.ColorRampItem(item.value, color, item.label)
        )
    return updated


def _rule_descendants(rule):
    """Правило отрисовки или подписей вместе со всеми вложенными."""
    yield rule
    for child in rule.children():
        yield from _rule_descendants(child)


def _renderer_symbols(renderer):
    """Все символы отрисовщика, включая символы вложенных правил."""
    if renderer is None:
        return
    if isinstance(renderer, QgsRuleBasedRenderer):
        for rule in _rule_descendants(renderer.rootRule()):
            symbol = rule.symbol()
            if symbol is not None:
                yield symbol
        return
    try:
        symbol = renderer.symbol()
    except Exception:
        symbol = None
    if symbol is not None:
        yield symbol


def _symbol_layers(symbol):
    """Слои символа вместе с вложенными символами (например, штрихи)."""
    for index in range(symbol.symbolLayerCount()):
        layer = symbol.symbolLayer(index)
        if layer is None:
            continue
        yield layer
        try:
            sub = layer.subSymbol()
        except Exception:
            sub = None
        if sub is not None:
            yield from _symbol_layers(sub)


def _label_settings(labeling):
    """Пары «правило, настройки» для правила-based подписей слоя."""
    if not isinstance(labeling, QgsRuleBasedLabeling):
        return
    for rule in _rule_descendants(labeling.rootRule()):
        settings = rule.settings()
        if settings is not None:
            yield rule, settings


def _renderer_symbol_colors(renderer):
    """Цвета всех слоёв символов отрисовщика в порядке обхода."""
    colors = []
    for symbol in _renderer_symbols(renderer):
        for symbol_layer in _symbol_layers(symbol):
            color = symbol_layer.color()
            colors.append(QColor(color) if color.isValid() else None)
    return colors


def _labeling_styles(labeling):
    """Цвет и обводка подписей в порядке обхода."""
    styles = []
    for _rule, settings in _label_settings(labeling):
        fmt = settings.format()
        styles.append((QColor(fmt.color()), fmt.buffer().enabled()))
    return styles


def _isoline_symbol_colors(layer):
    """Цвета всех слоёв символов изогипс в порядке обхода."""
    return _renderer_symbol_colors(layer.renderer())


def _isoline_label_styles(layer):
    """Цвет и обводка подписей изогипс в порядке обхода."""
    return _labeling_styles(layer.labeling())


def label_color_for(color):
    """Заметно более тёмный оттенок цвета линии — для подписей."""
    source = QColor(color)
    hue, saturation, value, alpha = source.getHsvF()
    if hue < 0:
        hue, saturation = 0.0, 0.0
    return QColor.fromHsvF(
        hue,
        min(1.0, saturation + 0.1),
        max(0.0, value * 0.45),
        alpha,
    )


def _repaint_layer(layer):
    layer.triggerRepaint()
    try:
        layer.emitStyleChanged()
    except Exception:
        pass


def apply_isoline_color(layer, color):
    """Перекрашивает линии, штрихи и подписи изогипс одним цветом.

    Альфа исходных слоёв сохраняется, поэтому невидимые служебные линии
    остаются невидимыми. Подписи получают затемнённый оттенок того же
    цвета и остаются без обводки.
    """
    if layer is None:
        return False
    pen = QColor(color)
    text = label_color_for(pen)
    changed = False
    for symbol in _renderer_symbols(layer.renderer()):
        for symbol_layer in _symbol_layers(symbol):
            old = symbol_layer.color()
            new = QColor(pen)
            if old.isValid():
                new.setAlpha(old.alpha())
            if old.isValid() and old == new:
                continue
            symbol_layer.setColor(new)
            changed = True
    for rule, settings in _label_settings(layer.labeling()):
        fmt = settings.format()
        enabled = fmt.buffer().enabled()
        if fmt.color() == text and not enabled:
            continue
        fmt.setColor(text)
        if enabled:
            buffer = fmt.buffer()
            buffer.setEnabled(False)
            fmt.setBuffer(buffer)
        settings.setFormat(fmt)
        rule.setSettings(settings)
        changed = True
    if changed:
        _repaint_layer(layer)
    return changed


def restore_isoline_style(layer, symbol_colors, label_styles):
    """Возвращает изогипсам цвета из шаблона проекта."""
    if layer is None:
        return False
    changed = False
    symbol_layers = []
    for symbol in _renderer_symbols(layer.renderer()):
        symbol_layers.extend(_symbol_layers(symbol))
    for symbol_layer, base in zip(symbol_layers, symbol_colors):
        if base is None or symbol_layer.color() == base:
            continue
        symbol_layer.setColor(QColor(base))
        changed = True
    settings_list = list(_label_settings(layer.labeling()))
    for (rule, settings), (base, buffer_enabled) in zip(
        settings_list, label_styles
    ):
        fmt = settings.format()
        if (
            fmt.color() == base
            and fmt.buffer().enabled() == buffer_enabled
        ):
            continue
        fmt.setColor(QColor(base))
        buffer = fmt.buffer()
        buffer.setEnabled(buffer_enabled)
        fmt.setBuffer(buffer)
        settings.setFormat(fmt)
        rule.setSettings(settings)
        changed = True
    if changed:
        _repaint_layer(layer)
    return changed


_TEMPLATE_ISOLINE_STYLES = {}


def _template_layer_objects(layer_name):
    """Отрисовщик и подписи слоя из шаблона automap.qgz."""
    path = template_path()
    try:
        with zipfile.ZipFile(path) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    document = QDomDocument()
    try:
        parsed = document.setContent(text)
    except Exception:
        return None
    if isinstance(parsed, tuple):
        parsed = parsed[0]
    if not parsed:
        return None
    maplayers = document.elementsByTagName("maplayer")
    context = QgsReadWriteContext()
    for index in range(maplayers.count()):
        element = maplayers.at(index).toElement()
        if element.firstChildElement("layername").text() != layer_name:
            continue
        renderer = QgsFeatureRenderer.load(
            element.firstChildElement("renderer-v2"), context
        )
        labeling = None
        labeling_element = element.firstChildElement("labeling")
        if not labeling_element.isNull():
            try:
                labeling = QgsAbstractVectorLayerLabeling.create(
                    labeling_element, context
                )
            except Exception:
                labeling = None
        return renderer, labeling
    return None


def template_isoline_styles(layer_name):
    """Исходные цвета линий и подписей изогипс из automap.qgz."""
    if layer_name in _TEMPLATE_ISOLINE_STYLES:
        return _TEMPLATE_ISOLINE_STYLES[layer_name]
    objects = _template_layer_objects(layer_name)
    if objects is None:
        return None
    renderer, labeling = objects
    styles = (
        _renderer_symbol_colors(renderer),
        _labeling_styles(labeling),
    )
    _TEMPLATE_ISOLINE_STYLES[layer_name] = styles
    return styles


def isoline_reset_styles(layer, captured):
    """Цвета для сброса: из шаблона, а если он не подходит — сохранённые."""
    styles = template_isoline_styles(layer.name())
    if styles is not None:
        colors, labels = styles
        if (
            len(colors) == len(_isoline_symbol_colors(layer))
            and len(labels) == len(_isoline_label_styles(layer))
        ):
            return styles
    return captured


def isoline_styles_differ(layer):
    """Текущие цвета изогипс отличаются от шаблона automap.qgz."""
    styles = template_isoline_styles(layer.name())
    if styles is None:
        return False
    return (
        _isoline_symbol_colors(layer) != styles[0]
        or _isoline_label_styles(layer) != styles[1]
    )


def color_swatch_icon(color, width=34, height=14):
    """Значок-образец выбранного цвета для маленькой кнопки."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(_enum(QPainter, "RenderHint", "Antialiasing"), True)
    border = QColor(120, 120, 120)
    painter.setPen(QPen(border, 1))
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(
        QRectF(0.5, 0.5, width - 1, height - 1), 2, 2
    )
    painter.end()
    return QIcon(pixmap)


def isolines_width_doubled(project=None):
    """Включена ли двойная ширина линий изогипс."""
    project = project or QgsProject.instance()
    try:
        value = project.customVariables().get(ISOLINES_WIDTH_VARIABLE)
    except Exception:
        return False
    try:
        return abs(float(value) - 2.0) < 1e-6
    except (TypeError, ValueError):
        return False


def set_isolines_width_doubled(enabled, project=None):
    """Включает или выключает двойную ширину линий изогипс.

    Шаблон проекта умножает ширину линий на переменную
    `@isolines_width_mult`, поэтому достаточно задать её в проекте.
    """
    project = project or QgsProject.instance()
    if enabled:
        QgsExpressionContextUtils.setProjectVariable(
            project, ISOLINES_WIDTH_VARIABLE, "2"
        )
    else:
        QgsExpressionContextUtils.removeProjectVariable(
            project, ISOLINES_WIDTH_VARIABLE
        )


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
        self._active_sheet_index = None
        self._sheet_tool = None
        self._edit_sheet_tool = None
        self._atlas_layer = None
        self._layout_original_layers = None
        self._layout_visible_states = None
        self._export_progress = None
        self._opacity_baseline = {}
        self._hillshade_baseline = []
        self._isoline_color = None
        self._isoline_baseline = {}
        self._baseline_project = None
        self._build_ui()
        self._connect_root()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_wrapped_labels()

    def _fit_wrapped_labels(self):
        """Подписи с переносом строк не должны обрезаться по высоте.

        QLabel с wordWrap не сообщает layout'у нужную высоту, если её
        не задать явно: текст переносится, но нижние строки срезаются.
        """
        for label in getattr(self, "_wrapped_labels", []):
            width = label.width()
            if width <= 0:
                width = label.sizeHint().width()
            height = label.heightForWidth(width)
            if height > 0:
                label.setMinimumHeight(height)

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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

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
        # Штатные инструменты QGIS: измерение и определение объектов.
        self.measure_action = QAction(
            QgsApplication.getThemeIcon("mActionMeasure.svg"),
            i18n.tr("Измерить"),
            self,
        )
        self.measure_action.setToolTip(i18n.tr("Измерить: длина, площадь, угол"))
        measure_menu = QMenu(self)
        for object_name, title in (
            ("mActionMeasure", i18n.tr("Расстояние")),
            ("mActionMeasureArea", i18n.tr("Площадь")),
            ("mActionMeasureAngle", i18n.tr("Угол")),
            ("mActionMeasureBearing", i18n.tr("Азимут")),
        ):
            action = QAction(title, measure_menu)
            action.triggered.connect(
                lambda checked=False, name=object_name:
                self._trigger_qgis_action(name)
            )
            measure_menu.addAction(action)
        self.measure_action.setMenu(measure_menu)
        self.toolbar.addAction(self.measure_action)
        measure_button = self.toolbar.widgetForAction(self.measure_action)
        if measure_button is not None:
            measure_button.setPopupMode(_instant_popup())
        self.identify_action = QAction(
            QgsApplication.getThemeIcon("mActionIdentify.svg"),
            i18n.tr("Определить объект"),
            self,
        )
        self.identify_action.setToolTip(
            i18n.tr("Определить объект: показать сведения об объекте под курсором")
        )
        self.identify_action.triggered.connect(
            lambda: self._trigger_qgis_action("mActionIdentify")
        )
        self.toolbar.addAction(self.identify_action)
        layout.addWidget(self.toolbar)

        self.map_language_row = QWidget(self)
        map_language_layout = QHBoxLayout(self.map_language_row)
        map_language_layout.setContentsMargins(0, 0, 0, 0)
        map_language_layout.setSpacing(6)
        self.map_language_label = QLabel("Язык карты", self.map_language_row)
        self.map_language_combo = MapLanguageCombo(self.map_language_row)
        self.map_language_combo.currentIndexChanged.connect(
            self._map_language_changed
        )
        self.transliteration_check = QCheckBox(
            "Транслитерация", self.map_language_row
        )
        self.transliteration_check.setToolTip(
            "Запасное название (когда нет name:xx) приводится к читаемому "
            "виду: незнакомая письменность переводится в латиницу"
        )
        self.transliteration_check.toggled.connect(
            self._transliteration_toggled
        )
        map_language_layout.addWidget(self.map_language_label)
        map_language_layout.addWidget(self.map_language_combo)
        map_language_layout.addWidget(self.transliteration_check, 1)
        layout.addWidget(self.map_language_row)

        # Уровень детализации — отдельной строкой: в одной строке с языком
        # и транслитерацией список не помещается в узкой панели.
        self.detail_row = QWidget(self)
        detail_layout = QHBoxLayout(self.detail_row)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        # Сколько подробностей показывать: тот же уровень, что задан в
        # шаблоне переменной проекта level_of_detail.
        self.detail_label = QLabel("Детализация", self.detail_row)
        self.detail_combo = QComboBox(self.detail_row)
        self.detail_combo.currentIndexChanged.connect(self._detail_changed)
        detail_layout.addWidget(self.detail_label)
        detail_layout.addWidget(self.detail_combo)
        detail_layout.addStretch(1)
        layout.addWidget(self.detail_row)
        self._fill_map_language_combo()
        self._fill_detail_combo()

        self.isolines_check = self._checkbox(
            "Контуры высот (изогипсы)", "isolines"
        )
        self.isolines_check.toggled.connect(
            lambda checked: self._layer_box_toggled(ISOLINES_LAYER, checked)
        )
        self.isolines_color_row = QWidget(self)
        isolines_color_layout = QHBoxLayout(self.isolines_color_row)
        isolines_color_layout.setContentsMargins(0, 0, 8, 0)
        isolines_color_layout.setSpacing(4)
        isolines_color_layout.addWidget(self.isolines_check, 1)
        self.isolines_width_button = QToolButton(self)
        self.isolines_width_button.setText("2×")
        self.isolines_width_button.setCheckable(True)
        self.isolines_width_button.setAutoRaise(True)
        bold = self.isolines_width_button.font()
        bold.setBold(True)
        metrics = QFontMetrics(bold)
        self.isolines_width_button.setFixedSize(
            QSize(max(24, metrics.horizontalAdvance("2×") + 14), 18)
        )
        self.isolines_width_button.setToolTip(
            "Двойная ширина линий изогипс"
        )
        self.isolines_width_button.toggled.connect(
            self._isoline_width_toggled
        )
        self.isolines_color_button = QToolButton(self)
        self.isolines_color_button.setAutoRaise(True)
        self.isolines_color_button.setFixedSize(QSize(38, 18))
        self.isolines_color_button.setIconSize(QSize(34, 14))
        self.isolines_color_button.setToolTip(
            "Цвет изогипс: выбрать произвольный цвет"
        )
        self.isolines_color_button.clicked.connect(self._pick_isoline_color)
        self.isolines_color_reset = QToolButton(self)
        self.isolines_color_reset.setIcon(
            QgsApplication.getThemeIcon("mActionReload.svg")
        )
        self.isolines_color_reset.setAutoRaise(True)
        self.isolines_color_reset.setFixedSize(QSize(18, 18))
        self.isolines_color_reset.setIconSize(QSize(14, 14))
        self.isolines_color_reset.setToolTip(
            "Сбросить цвет изогипс до исходного"
        )
        self.isolines_color_reset.setEnabled(False)
        self.isolines_color_reset.clicked.connect(self._reset_isoline_color)
        isolines_color_layout.addWidget(self.isolines_width_button)
        isolines_color_layout.addWidget(self.isolines_color_button)
        isolines_color_layout.addWidget(self.isolines_color_reset)
        layout.addWidget(self.isolines_color_row)
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
            "Раскр. уклонов", "steep"
        )
        self.steep_check.toggled.connect(self._steep_toggled)
        # Выбор варианта — кнопка с меню: в узкой панели выпадающий
        # список обрезал названия, а в меню помещаются все варианты.
        # В свёрнутом виде показывается первая буква выбранного варианта.
        self.steep_variant_button = QToolButton(self)
        self.steep_variant_button.setToolTip(
            "Вариант окраски уклонов: для гор, предгорий или равнин. "
            "Одновременно показывается только один вариант."
        )
        self.steep_variant_button.setPopupMode(_instant_popup())
        self.steep_variant_button.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon
        )
        self.steep_variant_button.setAutoRaise(True)
        # Системный индикатор меню у QToolButton нарисован мелким
        # треугольником и в узкой панели его почти не видно. Рисуем
        # стрелку сами: крупным глифом в тексте, а штатный индикатор
        # убираем, чтобы стрелка была одна.
        self.steep_variant_button.setStyleSheet(
            "QToolButton::menu-indicator { image: none; width: 0px; }"
        )
        self.steep_variant_button.setFixedHeight(
            self.steep_check.sizeHint().height()
        )
        self.steep_variant_menu = QMenu(self)
        self.steep_variant_button.setMenu(self.steep_variant_menu)
        self._steep_variant_name = None
        self.steep_opacity = self._layer_row(
            self.steep_check, STEEP_LAYER, extra=self.steep_variant_button
        )
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

        self.nature_reserve_check = self._checkbox(
            "Природоохранные зоны", "nature"
        )
        self.nature_reserve_check.toggled.connect(
            lambda checked: self._layer_box_toggled(
                NATURE_RESERVE_LAYER, checked
            )
        )
        self.nature_reserve_labels_check = QCheckBox("Подписи", self)
        self.nature_reserve_labels_check.toggled.connect(
            self._nature_reserve_labels_toggled
        )
        nature_reserve_row = QWidget(self)
        nature_reserve_layout = QHBoxLayout(nature_reserve_row)
        nature_reserve_layout.setContentsMargins(0, 0, 0, 0)
        nature_reserve_layout.setSpacing(8)
        nature_reserve_layout.addWidget(self.nature_reserve_check)
        nature_reserve_layout.addWidget(self.nature_reserve_labels_check)
        nature_reserve_layout.addStretch(1)
        layout.addWidget(nature_reserve_row)
        self._build_atlas_ui(layout)
        layout.addStretch(1)

        self._layer_boxes = [
            (self.steep_check, STEEP_LAYER),
            (self.slight_check, SLIGHT_LAYER),
            (self.hillshade_check, HILLSHADE_LAYER),
            (self.mountain_check, MOUNTAIN_LAYER),
            (self.nature_reserve_check, NATURE_RESERVE_LAYER),
        ]
        self._opacity_widgets = {
            STEEP_LAYER: self.steep_opacity,
            SLIGHT_LAYER: self.slight_opacity,
            HILLSHADE_LAYER: self.hillshade_opacity,
        }

    def _layer_row(self, check_box, layer_name, extra=None):
        """Строка слоя рельефа: галочка, шкала прозрачности, значение, сброс."""
        # Строки держим по 24 px: так панель остаётся в пределах 600 px.
        check_box.setFixedHeight(24)
        row = QWidget(self)
        line = QHBoxLayout(row)
        # Небольшой отступ справа: шкала и кнопка сброса не прилипают к краю.
        line.setContentsMargins(0, 0, 8, 0)
        line.setSpacing(4)
        hillshade = layer_name == HILLSHADE_LAYER
        initial = HILLSHADE_DEFAULT if hillshade else 100
        slider = QSlider(Qt.Horizontal, row)
        slider.setRange(0, 100)
        slider.setValue(initial)
        slider.setFixedWidth(70)
        if hillshade:
            slider.setToolTip(
                "Прозрачность теней рельефа: 50% — исходный вид, "
                "меньше — темнее (0% — тени непрозрачные), больше — светлее."
            )
        else:
            slider.setToolTip(
                i18n.trf(
                    "Прозрачность слоя «{}»",
                    layer_name.replace("[TERRAIN] ", ""),
                )
            )
        value = QLabel(f"{initial}%", row)
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
        if extra is not None:
            extra.setFixedHeight(24)
            line.addWidget(extra)
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
        group_layout.setSpacing(2)

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
        sheet_buttons.setSpacing(2)
        self.add_sheet_button = QPushButton(
            QgsApplication.getThemeIcon("mActionAdd.svg"), "Добавить"
        )
        self.add_sheet_button.setCheckable(True)
        self.add_sheet_button.setToolTip(
            "Добавить лист: протянуть рамку на карте"
        )
        self.edit_sheet_button = QPushButton(
            QgsApplication.getThemeIcon("mActionToggleEditing.svg"), "Изменить"
        )
        self.edit_sheet_button.setCheckable(True)
        self.edit_sheet_button.setToolTip(
            "Перетащите рамку листа или потяните за квадратный маркер угла"
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
            self.add_sheet_button, self.edit_sheet_button,
            self.remove_sheet_button, self.clear_sheets_button,
        ):
            button.setIconSize(QSize(16, 16))
            sheet_buttons.addWidget(button)
        group_layout.addLayout(sheet_buttons)

        self.sheets_label = QLabel("Листов: 0 — будет экспортирован текущий вид")
        self.sheets_label.setWordWrap(True)
        group_layout.addWidget(self.sheets_label)
        hint = QLabel(
            "«Добавить» — нарисовать рамку, «Изменить» — двигать её."
        )
        hint.setWordWrap(True)
        group_layout.addWidget(hint)
        # Подписи с переносом строк: без пересчёта высоты текст обрезается.
        self._wrapped_labels = [self.sheets_label, hint]

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
        self.edit_sheet_button.toggled.connect(self._toggle_edit_sheet_tool)
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
            if self._active_sheet_index is not None:
                text += f" · выбран {self._active_sheet_index + 1}"
        else:
            text = i18n.tr("Листов: 0 — будет экспортирован текущий вид")
        self.sheets_label.setText(i18n.tr(text))
        self._fit_wrapped_labels()
        self.edit_sheet_button.setEnabled(count > 0)
        self.remove_sheet_button.setEnabled(count > 0)
        self.remove_sheet_button.setToolTip(
            i18n.tr(
                "Удалить выбранный лист"
                if self._active_sheet_index is not None
                else "Удалить последний лист"
            )
        )
        self.clear_sheets_button.setEnabled(count > 0)
        if not count and self.edit_sheet_button.isChecked():
            self.edit_sheet_button.setChecked(False)

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
        if self._edit_sheet_tool is not None:
            self._edit_sheet_tool.set_aspect(self._page_aspect())
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

    def _toggle_edit_sheet_tool(self, active):
        if self._tool_syncing:
            return
        canvas = self._canvas()
        if active:
            if canvas is None or not self._sheets:
                self.edit_sheet_button.setChecked(False)
                return
            if self._edit_sheet_tool is None:
                self._edit_sheet_tool = SheetEditMapTool(
                    canvas, self._sheets, self._page_aspect()
                )
                self._edit_sheet_tool.sheetEdited.connect(self._sheet_edited)
                self._edit_sheet_tool.sheetActivated.connect(
                    self._sheet_activated
                )
            self._edit_sheet_tool.set_aspect(self._page_aspect())
            canvas.setMapTool(self._edit_sheet_tool)
            self.statusMessage.emit(
                "Перетащите рамку листа или потяните за маркер угла"
            )
        else:
            if self._edit_sheet_tool is not None and canvas is not None:
                canvas.unsetMapTool(self._edit_sheet_tool)
                self._edit_sheet_tool._clear_operation()
            self._set_active_sheet(None)

    def _sheet_activated(self, index):
        self._set_active_sheet(index)

    def _set_active_sheet(self, index):
        if index is not None and (index < 0 or index >= len(self._sheets)):
            return
        self._active_sheet_index = index
        self._update_sheet_colors()
        self._update_sheet_controls()

    def _sheet_band_colors(self, index):
        if index == self._active_sheet_index:
            return QColor(220, 140, 20), QColor(220, 140, 20, 45)
        return QColor(0, 120, 215), QColor(0, 120, 215, 30)

    def _update_sheet_colors(self):
        for index, (band, _marker, _labels) in enumerate(self._sheet_items):
            color, fill = self._sheet_band_colors(index)
            band.setColor(color)
            band.setFillColor(fill)
            band.update()

    def _sheet_edited(self, index, rectangle):
        if index < 0 or index >= len(self._sheets):
            return
        self._active_sheet_index = index
        _old_rectangle, crs = self._sheets[index]
        self._sheets[index] = (QgsRectangle(rectangle), crs)
        self._rebuild_sheet_items()
        self._remove_atlas_layer()
        self.statusMessage.emit(f"Лист {index + 1} изменён")

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
        color, fill = self._sheet_band_colors(number - 1)
        band.setColor(color)
        band.setFillColor(fill)
        band.setWidth(2)
        band.setToGeometry(QgsGeometry.fromRect(rectangle), crs)
        band.show()
        marker = SheetMarker(canvas, number)
        marker.set_map_point(rectangle.center())
        marker.show()
        # Размеры страницы вдоль границ прямоугольника.
        labels = SheetSizeLabels(canvas)
        labels.set_rectangle(rectangle, crs)
        labels.show()
        self._sheet_items.append((band, marker, labels))

    def _dispose_sheet_item(self, item):
        band, marker, labels = item
        band.reset(QgsWkbTypes.PolygonGeometry)
        band.hide()
        marker.hide()
        labels.hide()
        for graphics_item in (marker, labels, band):
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

    def _dispose_edit_sheet_tool(self):
        if self._edit_sheet_tool is None:
            return
        try:
            self._edit_sheet_tool.dispose()
        except Exception:
            pass
        self._edit_sheet_tool = None

    def _rebuild_sheet_items(self):
        for item in self._sheet_items:
            self._dispose_sheet_item(item)
        self._sheet_items = []
        for number, (rectangle, crs) in enumerate(self._sheets, 1):
            self._insert_sheet_items(rectangle, crs, number)
        if self._sheet_tool is not None:
            self._sheet_tool.set_number(len(self._sheets) + 1)
        if self._edit_sheet_tool is not None:
            self._edit_sheet_tool.handles.update()
        self._update_sheet_controls()

    def _remove_last_sheet(self):
        if not self._sheets:
            return
        index = (
            self._active_sheet_index
            if self._active_sheet_index is not None
            else len(self._sheets) - 1
        )
        self._sheets.pop(index)
        if self._active_sheet_index is not None:
            self._active_sheet_index = (
                min(index, len(self._sheets) - 1) if self._sheets else None
            )
        self._rebuild_sheet_items()
        self._remove_atlas_layer()
        self.statusMessage.emit(f"Лист {index + 1} удалён")

    def clear_atlas(self):
        if (
            self._sheets
            or self._sheet_items
            or self._atlas_layer is not None
            or self.add_sheet_button.isChecked()
            or self.edit_sheet_button.isChecked()
        ):
            self._clear_sheets()

    def reset_atlas_state(self):
        """Сбросить состояние атласа, не трогая очищаемый проект.

        Вызывается при закрытии проекта: слои и макеты всё равно удаляются
        вместе с проектом, а обращение к нему из сигнала «cleared» приводит
        к падению QGIS.
        """
        self.add_sheet_button.setChecked(False)
        self.edit_sheet_button.setChecked(False)
        self._sheets = []
        self._sheet_items = []
        self._active_sheet_index = None
        self._atlas_layer = None
        self._dispose_sheet_tool()
        self._dispose_edit_sheet_tool()
        self._update_sheet_controls()

    def _clear_sheets(self):
        if self.add_sheet_button.isChecked():
            self.add_sheet_button.setChecked(False)
        if self.edit_sheet_button.isChecked():
            self.edit_sheet_button.setChecked(False)
        self._sheets = []
        self._active_sheet_index = None
        self._rebuild_sheet_items()
        self._remove_atlas_layer()
        self._dispose_sheet_tool()
        self._dispose_edit_sheet_tool()
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
            title = i18n.trf("Экспорт атласа {}", extension.upper())
        else:
            title = i18n.trf("Экспорт {}", extension.upper())
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
            i18n.trf(
                "Экспорт при {} dpi может потребовать около {} ГБ памяти.",
                preferred,
                f"{estimate / (1024 ** 3):.1f}",
            )
        )
        box.setInformativeText(
            i18n.trf("Снизить разрешение до {} dpi?", safe)
        )
        lower = box.addButton(
            i18n.trf("Снизить до {} dpi", safe),
            _enum(QMessageBox, "ButtonRole", "AcceptRole"),
        )
        proceed = box.addButton(
            i18n.tr("Продолжить"),
            _enum(QMessageBox, "ButtonRole", "DestructiveRole"),
        )
        cancel = box.addButton(
            i18n.tr("Отмена"),
            _enum(QMessageBox, "ButtonRole", "RejectRole"),
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
            QMessageBox.warning(self, "QGIS-xtopo", i18n.tr(str(exc)))
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
        progress_connected = atlas.enabled()
        if progress_connected:
            self._export_progress = (extension.upper(), len(self._sheets))
            atlas.featureChanged.connect(self._atlas_export_feature)
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
            QMessageBox.critical(
                self, "QGIS-xtopo",
                i18n.trf("Ошибка экспорта: {}", exc),
            )
            return
        finally:
            if progress_connected:
                try:
                    atlas.featureChanged.disconnect(self._atlas_export_feature)
                except Exception:
                    pass
            self._export_progress = None
            QApplication.restoreOverrideCursor()
            self._remove_atlas_layer()
            self._restore_map_layers()
        self._export_result(result, error, path, extension, elapsed=time.monotonic() - started)

    def _atlas_export_feature(self, feature):
        if self._export_progress is None:
            return
        kind, total = self._export_progress
        try:
            number = int(feature.attribute("sheet"))
        except (KeyError, TypeError, ValueError):
            number = feature.id()
        self._notify(f"Экспорт {kind}: лист {number} из {total}…")
        QApplication.processEvents(
            _enum(QEventLoop, "ProcessEventsFlag", "ExcludeUserInputEvents")
        )

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
            QMessageBox.warning(self, "QGIS-xtopo", i18n.tr(str(exc)))
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
            QMessageBox.critical(
                self, "QGIS-xtopo",
                i18n.trf("Ошибка печати: {}", exc),
            )
            return
        finally:
            self._remove_atlas_layer()
            self._restore_map_layers()
        self._export_result(result, error, "", "print")

    def _export_result(self, result, error, path, kind, elapsed=None):
        if result == _enum(QgsLayoutExporter, "ExportResult", "Success"):
            if kind == "print":
                message = i18n.tr("Печать отправлена")
            else:
                message = i18n.trf("Экспорт: {}", path)
                if elapsed is not None:
                    message += i18n.trf(" ({} с)", f"{elapsed:.1f}")
            self._notify(message)
            return
        detail = error or i18n.trf("код {}", int(result))
        QMessageBox.critical(
            self, "QGIS-xtopo",
            i18n.trf("Не удалось выполнить экспорт: {}", detail),
        )

    def _notify(self, message):
        """Сообщение в журнал и всплывающее уведомление QGIS."""
        message = i18n.tr(message)
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

    def retranslate(self):
        """Переводит подписи панели на текущий язык."""
        self._update_sheet_controls()
        self._update_isoline_controls()
        translate_tree(self)
        self._fill_map_language_combo()
        self._fill_detail_combo()
        # Название варианта в кнопке и в меню зависит от языка.
        self._update_steep_variants()

    def _fill_map_language_combo(self):
        """Заполняет список языков подписей, сохраняя выбор."""
        combo = self.map_language_combo
        current = combo.currentData() or map_language.language()
        combo.blockSignals(True)
        try:
            combo.clear()
            for code, label, _script in map_language.MAP_LANGUAGES:
                combo.addItem(i18n.tr(label), code)
                combo.setItemData(
                    combo.count() - 1,
                    " → ".join(map_language.tags_for(code)),
                    _enum(Qt, "ItemDataRole", "ToolTipRole"),
                )
            index = combo.findData(current)
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(False)
        combo.setToolTip(i18n.tr("Язык подписей на карте"))
        metrics = QFontMetrics(combo.font())
        width = max(
            metrics.horizontalAdvance(MapLanguageCombo.short_label(code))
            for code, _label, _script in map_language.MAP_LANGUAGES
        )
        combo.setFixedWidth(max(56, width + 34))
        combo.setFixedHeight(24)
        self._update_transliteration_state()

    def _map_language_changed(self, _index=None):
        code = self.map_language_combo.currentData()
        if not code:
            return
        map_language.set_map_language(code)
        self._update_transliteration_state()
        if self.is_xtopo_project():
            map_language.apply_map_language(code, iface=self.iface)

    def _update_transliteration_state(self):
        """Транслитерация нужна там, где запасное название может быть
        записано в незнакомой читателю письменности.

        Для местных названий запасного варианта нет, поэтому галочка
        недоступна.
        """
        code = self.map_language_combo.currentData() or map_language.language()
        available = code != map_language.LOCAL_LANGUAGE
        self.transliteration_check.setEnabled(available)
        if not available and self.transliteration_check.isChecked():
            self.transliteration_check.setChecked(False)
            map_language.set_transliteration(False)
        elif available and map_language.transliteration_enabled():
            self.transliteration_check.blockSignals(True)
            self.transliteration_check.setChecked(True)
            self.transliteration_check.blockSignals(False)

    def _transliteration_toggled(self, checked):
        map_language.set_transliteration(bool(checked))
        if self.is_xtopo_project():
            map_language.apply_map_language(iface=self.iface)

    DETAIL_TITLES = {
        3: "3 — главное",
        4: "4 — обычный",
        5: "5 — подробнее",
        6: "6 — всё",
    }
    DETAIL_TIPS = {
        3: "Только главные объекты: без мелких родников, троп и остановок",
        4: "Обычная подробность карты",
        5: "Больше мелких объектов: тропы, родники, остановки",
        6: "Все мелкие объекты, какие есть в данных",
    }

    def _fill_detail_combo(self):
        """Заполняет список уровней детализации, сохраняя выбор."""
        combo = self.detail_combo
        current = combo.currentData()
        if current is None:
            current = map_language.level_of_detail()
        combo.blockSignals(True)
        try:
            combo.clear()
            # Переводим сами: панель проекта переводится не всегда (её
            # retranslate откладывается до открытия проекта), а список
            # пересобирается при каждой смене выбора.
            for level in map_language.DETAIL_LEVELS:
                combo.addItem(i18n.tr(self.DETAIL_TITLES[level]), level)
                combo.setItemData(
                    combo.count() - 1,
                    i18n.tr(self.DETAIL_TIPS[level]),
                    _enum(Qt, "ItemDataRole", "ToolTipRole"),
                )
            index = combo.findData(current)
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(False)
        combo.setToolTip(
            i18n.tr(
                "Уровень детализации: какие мелкие объекты показывать "
                "(названия, дороги, тропы, родники)"
            )
        )
        metrics = combo.fontMetrics()
        text_width = max(
            (metrics.horizontalAdvance(combo.itemText(i))
             for i in range(combo.count())),
            default=0,
        )
        combo.setFixedWidth(max(120, text_width + 40))
        combo.setFixedHeight(22)

    def _detail_changed(self, _index=None):
        level = self.detail_combo.currentData()
        if level is None:
            return
        map_language.set_level_of_detail(level)
        # Открытый проект правится на месте (признак карты проекта — слой
        # BBOX, а не имя файла: несохранённый проект тоже бывает собран
        # плагином); новый получит значение из шаблона при сборке.
        project = QgsProject.instance()
        if self.is_xtopo_project(project):
            map_language.set_project_detail(level, project)
            self._repaint_canvas()

    def _sync_detail_combo(self):
        """Показывает в списке уровень детализации открытого проекта."""
        project = QgsProject.instance()
        try:
            value = project.customVariables().get(
                map_language.DETAIL_VARIABLE
            )
        except Exception:
            value = None
        if value is None or not self.is_xtopo_project(project):
            return
        level = map_language.normalize_detail(value)
        combo = self.detail_combo
        if combo.currentData() == level:
            return
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(max(0, combo.findData(level)))
        finally:
            combo.blockSignals(False)
        map_language.sync_level_of_detail(level)

    def _repaint_canvas(self):
        canvas = self._canvas()
        if canvas is not None:
            try:
                canvas.refresh()
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
            self._hillshade_baseline = []
            self._isoline_color = None
            self._isoline_baseline = {}
            self._capture_hillshade_baseline()
        self._connect_style_layer()
        self._rebuild_rules()
        self._sync_from_project()
        self._sync_isoline_color()
        # Уровень детализации для проекта берётся из списка панели: проект,
        # собранный старым шаблоном, может не иметь переменной вовсе.
        before = self.detail_combo.currentData()
        self._sync_detail_combo()
        if before == self.detail_combo.currentData():
            map_language.set_project_detail(
                self.detail_combo.currentData() or map_language.level_of_detail()
            )

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

    def _trigger_qgis_action(self, object_name):
        """Включает штатный инструмент QGIS по имени его действия.

        Инструменты измерения и определения объектов живут в самом QGIS
        (QgsMapToolMeasure, QgsMapToolIdentify) и в Python-обёртках QGIS
        3.44 недоступны, поэтому вызывается готовое действие главного
        окна: пользователь получает ровно инструмент QGIS.
        """
        iface = self.iface
        if iface is None:
            return False
        try:
            window = iface.mainWindow()
        except Exception:
            return False
        for action in window.findChildren(QAction):
            if action.objectName() == object_name:
                action.trigger()
                return True
        return False

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
        if (
            self._edit_sheet_tool is not None
            and new_tool is not self._edit_sheet_tool
            and self.edit_sheet_button.isChecked()
        ):
            self._tool_syncing = True
            self.edit_sheet_button.setChecked(False)
            self._tool_syncing = False
            self._set_active_sheet(None)

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
            self._update_steep_variants()
            available = {name: self._layer(name) is not None for name in LAYER_NAMES}
            visible = {name: self._layer_visible(name) for name in LAYER_NAMES}
            # Раскраска уклонов — семейство слоёв: в проекте может быть
            # несколько вариантов окраски, показывается только один.
            steep_names = self._steep_variant_names()
            available[STEEP_LAYER] = bool(steep_names)
            self._sync_steep_state()
            active = self._active_steep_layer()
            visible[STEEP_LAYER] = (
                self._layer_visible(active) if active else False
            )
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
            nature = self._layer(NATURE_RESERVE_LAYER)
            self.nature_reserve_labels_check.setEnabled(
                nature is not None
                and nature.labeling() is not None
                and self.nature_reserve_check.isChecked()
            )
            self.nature_reserve_labels_check.setChecked(
                nature is not None and nature.labelsEnabled()
            )
            self._sync_opacity_sliders()
            self._update_steep_variant_state()
            self._update_rule_checks(available[ISOLINES_LAYER])
            self._sync_glacier_rules()
            self._update_isoline_controls()
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
            self._sync_glacier_rules()
            self._update_rule_checks(True)
        finally:
            self._syncing = False

    def _sync_glacier_rules(self):
        """Приводит правила изолиний ледников к состоянию обычных изолиний.

        Галочки «Кратность» относятся к обоим слоям: изолинии ледников
        рисуются теми же интервалами, что и остальные изолинии.
        """
        states = self._rule_states()
        if not states:
            return False
        layer = self._layer(GLACIER_ISOLINES_LAYER)
        renderer = layer.renderer() if layer is not None else None
        if not isinstance(renderer, QgsRuleBasedRenderer):
            return False
        changed = False
        for rule in renderer.rootRule().children():
            label = rule.label().strip()
            if label not in states:
                continue
            if rule.active() != states[label]:
                rule.setActive(states[label])
                changed = True
        if not changed:
            return False
        layer.triggerRepaint()
        try:
            layer.emitStyleChanged()
        except Exception:
            pass
        return True

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

    def _steep_variant_hint(self, name):
        """Порог уклона, с которого показывается вариант окраски.

        У каждого варианта своя классификация: «Горы» показывает крутые
        склоны, «Предгорья» — более пологие. Подсказка объясняет, почему
        на равнинной карте «Горы» могут выглядеть почти пустыми.

        Возвращает градусы (число) или None, если порог неизвестен.
        """
        layer = self._layer(name)
        if layer is None:
            return None
        try:
            renderer = layer.renderer()
            items = renderer.shader().rasterShaderFunction().colorRampItemList()
            minimum = float(items[0].value)
        except Exception:
            return None
        return int(round(minimum))

    def _steep_variant_hint_text(self, name):
        """Подсказка о пороге уклона на языке интерфейса."""
        minimum = self._steep_variant_hint(name)
        if minimum is None:
            return ""
        return i18n.trf("склоны круче {}°", minimum)

    def _steep_variant_names(self):
        """Слои-варианты крутых склонов, присутствующие в проекте.

        Сопоставление строго по полному имени слоя: в проекте встречаются
        и другие слои с суффиксом «var1», они вариантами не являются.
        """
        known = dict(STEEP_VARIANTS)
        known[STEEP_LAYER] = ""
        names = []
        try:
            layers = list(QgsProject.instance().mapLayers().values())
        except Exception:
            layers = []
        for layer in layers:
            name = layer.name()
            if name in known:
                names.append(name)
        order = {
            name: index for index, (name, _) in enumerate(STEEP_VARIANTS)
        }
        names.sort(key=lambda name: (order.get(name, len(order)), name))
        return names

    def _steep_variant_label(self, name):
        """Подпись варианта уклонов."""
        for layer_name, label in STEEP_VARIANTS:
            if layer_name == name:
                return label
        if name == STEEP_LAYER:
            return "Общий"
        return name

    def _active_steep_layer(self):
        """Выбранный вариант крутых склонов (имя слоя)."""
        names = self._steep_variant_names()
        if not names:
            return None
        if self._steep_variant_name in names:
            return self._steep_variant_name
        return names[0]

    def _update_steep_variants(self):
        """Приводит список вариантов уклонов в соответствие с проектом."""
        names = self._steep_variant_names()
        current = self._steep_variant_name
        if current not in names:
            current = names[0] if names else None
        self._steep_variant_name = current
        self.steep_variant_menu.clear()
        for name in names:
            label = self._steep_variant_label(name)
            action = self.steep_variant_menu.addAction(i18n.tr(label))
            action.setData(name)
            action.setCheckable(True)
            action.setChecked(name == current)
            hint = self._steep_variant_hint_text(name)
            if hint:
                action.setToolTip(f"{i18n.tr(label)}: {hint}")
            action.triggered.connect(
                lambda checked=False, layer_name=name:
                self._steep_variant_changed(layer_name)
            )
        self._update_steep_variant_button()
        self._update_steep_variant_state()

    def _update_steep_variant_button(self):
        """Текст кнопки: первая буква выбранного варианта."""
        name = self._steep_variant_name
        if not name:
            self.steep_variant_button.setText("")
            return
        label = self._steep_variant_label(name)
        # Стрелка вниз — крупным глифом прямо в тексте: штатный
        # индикатор меню у QToolButton слишком мелкий и в узкой панели
        # его почти не видно.
        self.steep_variant_button.setText(
            "(%s) ▼" % i18n.tr(label)[:1].upper()
        )
        hint = self._steep_variant_hint_text(name)
        tooltip = i18n.tr(
            "Вариант окраски уклонов: для гор, предгорий или равнин. "
            "Одновременно показывается только один вариант."
        )
        if hint:
            tooltip += " " + i18n.trf("Выбрано: {} — {}.", i18n.tr(label), hint)
        self.steep_variant_button.setToolTip(tooltip)

    def _update_steep_variant_state(self):
        """Вариант окраски уклонов доступен только при включённой опции.

        Пока «Раскр. уклонов» выключено, выбор заблокирован:
        выбирать окраску, которая не показывается, бессмысленно.
        """
        enabled = (
            self.steep_check.isChecked()
            and len(self._steep_variant_names()) > 1
        )
        self.steep_variant_button.setEnabled(enabled)
        self.steep_variant_button.setVisible(
            bool(self._steep_variant_names())
        )

    def _apply_steep_visibility(self, checked):
        """Показывает выбранный вариант уклонов, остальные скрывает."""
        active = self._active_steep_layer()
        for name in self._steep_variant_names():
            self._set_layer_visible(name, bool(checked) and name == active)

    def _set_steep_variant(self, name):
        """Выбирает вариант уклонов без обработчика (по состоянию проекта)."""
        if name not in self._steep_variant_names():
            return
        self._steep_variant_name = name
        for action in self.steep_variant_menu.actions():
            action.setChecked(action.data() == name)
        self._update_steep_variant_button()

    def _sync_steep_state(self):
        """Держит включённым только один вариант окраски уклонов."""
        names = self._steep_variant_names()
        if not names:
            return
        active = self._active_steep_layer()
        visible = [name for name in names if self._layer_visible(name)]
        if active is not None and active not in visible and len(visible) == 1:
            # Другой вариант включили в дереве слоёв QGIS — он и выбран.
            active = visible[0]
            self._set_steep_variant(active)
            self._apply_steep_opacity()
        if len(visible) > 1:
            for name in visible:
                if name != active:
                    self._set_layer_visible(name, False)

    def _apply_steep_opacity(self):
        """Переносит прозрачность со шкалы на выбранный вариант уклонов."""
        widget = self._opacity_widgets.get(STEEP_LAYER)
        slider = widget.property("slider") if widget is not None else None
        active = self._active_steep_layer()
        layer = self._layer(active) if active else None
        if slider is None or layer is None:
            return
        try:
            layer.setOpacity(slider.value() / 100.0)
        except Exception:
            return
        layer.triggerRepaint()
        try:
            layer.emitStyleChanged()
        except Exception:
            pass

    def _steep_toggled(self, checked):
        """Галочка «Раскр. уклонов» управляет выбранным вариантом."""
        if self._syncing:
            return
        self._syncing = True
        try:
            self._apply_steep_visibility(checked)
        finally:
            self._syncing = False
        self._update_steep_variant_state()
        self._sync_from_project()

    def _steep_variant_changed(self, name):
        """Выбор варианта уклонов из меню: показываем новый, остальные прячем."""
        if self._syncing:
            return
        self._steep_variant_name = name
        self._update_steep_variant_button()
        self._syncing = True
        try:
            self._apply_steep_visibility(self.steep_check.isChecked())
        finally:
            self._syncing = False
        self._apply_steep_opacity()
        self._sync_from_project()

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

    def _nature_reserve_labels_toggled(self, checked):
        if self._syncing:
            return
        layer = self._layer(NATURE_RESERVE_LAYER)
        if layer is None:
            return
        layer.setLabelsEnabled(bool(checked))
        layer.triggerRepaint()
        try:
            layer.emitStyleChanged()
        except Exception:
            pass
        self._sync_from_project()

    def _opacity_changed(self, layer_name, number, label):
        """Шкала прозрачности слоя рельефа."""
        label.setText(f"{number}%")
        if self._syncing:
            return
        if layer_name == HILLSHADE_LAYER:
            self._apply_hillshade_transparency(number)
            return
        target = self._opacity_target(layer_name)
        layer = self._layer(target) if target else None
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

    def _opacity_target(self, layer_name):
        """Слой, к которому применяется прозрачность.

        Для крутых склонов это выбранный вариант окраски.
        """
        if layer_name == STEEP_LAYER:
            return self._active_steep_layer()
        return layer_name

    def _reset_opacity(self, layer_name):
        """Возвращает слою прозрачность из шаблона проекта."""
        widget = self._opacity_widgets.get(layer_name)
        slider = widget.property("slider") if widget is not None else None
        if slider is None:
            return
        if layer_name == HILLSHADE_LAYER:
            slider.setValue(HILLSHADE_DEFAULT)
            return
        target = self._opacity_target(layer_name)
        value = self._opacity_baseline.get(target)
        if value is None:
            value = self._opacity_baseline.get(layer_name)
        if value is None:
            return
        slider.setValue(value)

    def _capture_isoline_baseline(self):
        """Запоминает цвета линий и подписей изогипс из шаблона."""
        for name in (ISOLINES_LAYER, GLACIER_ISOLINES_LAYER):
            if name in self._isoline_baseline:
                continue
            layer = self._layer(name)
            if layer is None:
                continue
            self._isoline_baseline[name] = (
                _isoline_symbol_colors(layer),
                _isoline_label_styles(layer),
            )

    def _template_isoline_color(self):
        """Исходный цвет линий изогипс из шаблона automap.qgz."""
        styles = template_isoline_styles(ISOLINES_LAYER)
        colors = styles[0] if styles is not None else []
        if not colors:
            data = self._isoline_baseline.get(ISOLINES_LAYER)
            colors = data[0] if data else []
        for color in colors:
            if color is not None and color.alpha() > 0:
                return QColor(color)
        return None

    def _isoline_swatch_color(self):
        if self._isoline_color is not None:
            return QColor(self._isoline_color)
        layer = self._layer(ISOLINES_LAYER)
        if layer is not None:
            for color in _isoline_symbol_colors(layer):
                if color is not None and color.alpha() > 0:
                    return QColor(color)
        template = self._template_isoline_color()
        return template if template is not None else QColor(64, 64, 64)

    def _reset_available(self, enabled):
        """Сброс нужен, если цвет выбран или проект уже перекрашен."""
        if not enabled:
            return False
        if self._isoline_color is not None:
            return True
        layer = self._layer(ISOLINES_LAYER)
        return layer is not None and isoline_styles_differ(layer)

    def _update_isoline_controls(self):
        """Образец цвета, кнопки сброса и двойной ширины."""
        if not self._isoline_baseline and self._isoline_color is None:
            self._capture_isoline_baseline()
        layer = self._layer(ISOLINES_LAYER) or self._layer(
            GLACIER_ISOLINES_LAYER
        )
        enabled = layer is not None and self.isolines_check.isChecked()
        self.isolines_width_button.setEnabled(enabled)
        self.isolines_width_button.blockSignals(True)
        self.isolines_width_button.setChecked(isolines_width_doubled())
        self.isolines_width_button.blockSignals(False)
        self._style_isoline_width_button(
            self.isolines_width_button.isChecked()
        )
        self.isolines_color_button.setEnabled(enabled)
        self.isolines_color_button.setIcon(
            color_swatch_icon(self._isoline_swatch_color())
        )
        self.isolines_color_reset.setEnabled(
            self._reset_available(enabled)
        )

    def _isoline_width_toggled(self, checked):
        """Двойная ширина линий изогипс через переменную проекта."""
        self._style_isoline_width_button(checked)
        if self._syncing:
            return
        set_isolines_width_doubled(bool(checked))
        for name in (ISOLINES_LAYER, GLACIER_ISOLINES_LAYER):
            layer = self._layer(name)
            if layer is not None:
                _repaint_layer(layer)

    def _style_isoline_width_button(self, checked):
        """Жирная надпись «2×» — заметный признак включённой ширины."""
        font = self.isolines_width_button.font()
        if font.bold() == bool(checked):
            return
        font.setBold(bool(checked))
        self.isolines_width_button.setFont(font)

    def _pick_isoline_color(self):
        color = QColorDialog.getColor(
            self._isoline_swatch_color(), self, "Цвет изогипс"
        )
        if not color.isValid():
            return
        self._set_isoline_color(color)

    def _set_isoline_color(self, color):
        self._capture_isoline_baseline()
        self._isoline_color = QColor(color)
        for name in (ISOLINES_LAYER, GLACIER_ISOLINES_LAYER):
            apply_isoline_color(self._layer(name), self._isoline_color)
        self._update_isoline_controls()

    def _reset_isoline_color(self):
        self._isoline_color = None
        self._capture_isoline_baseline()
        for name, captured in self._isoline_baseline.items():
            layer = self._layer(name)
            if layer is None:
                continue
            colors, labels = isoline_reset_styles(layer, captured)
            restore_isoline_style(layer, colors, labels)
        self._update_isoline_controls()

    def _sync_isoline_color(self):
        """Возвращает выбранный цвет после перезагрузки слоёв проекта."""
        self._capture_isoline_baseline()
        if self._isoline_color is not None:
            for name in (ISOLINES_LAYER, GLACIER_ISOLINES_LAYER):
                apply_isoline_color(self._layer(name), self._isoline_color)
        self._update_isoline_controls()

    def _hillshade_ramp(self):
        """Отрисовщик, шейдер и цветовая шкала слоя затенения рельефа."""
        layer = self._layer(HILLSHADE_LAYER)
        if layer is None:
            return None, None, None
        try:
            renderer = layer.renderer()
            shader = renderer.shader()
            function = shader.rasterShaderFunction()
        except Exception:
            return None, None, None
        if not isinstance(function, QgsColorRampShader):
            return None, None, None
        return renderer, shader, function

    def _capture_hillshade_baseline(self):
        """Запоминает прозрачность теней из шаблона проекта."""
        _, _, function = self._hillshade_ramp()
        if function is None:
            return
        try:
            alphas = [item.color.alpha() for item in function.colorRampItemList()]
        except Exception:
            return
        if alphas:
            self._hillshade_baseline = alphas

    def _apply_hillshade_transparency(self, number):
        """Затемняет или осветляет тени рельефа.

        Прозрачность слоя ограничена единицей, поэтому плотность теней
        задаётся прозрачностью цветовой шкалы: 50% — исходный вид,
        меньше — темнее, больше — светлее.
        """
        renderer, shader, function = self._hillshade_ramp()
        if renderer is None:
            return False
        try:
            items = function.colorRampItemList()
        except Exception:
            return False
        baseline = self._hillshade_baseline
        if not baseline or len(baseline) != len(items):
            return False
        updated = _ramp_items(items, baseline, number)
        # Меняем цветовую шкалу на месте: шейдер и функция принадлежат
        # отрисовщику, повторная установка привела бы к двойному удалению.
        function.setColorRampItemList(updated)
        layer = self._layer(HILLSHADE_LAYER)
        if layer is not None:
            layer.triggerRepaint()
            try:
                layer.emitStyleChanged()
            except Exception:
                pass
        return True

    def _hillshade_transparency(self):
        """Текущая прозрачность теней по цветовой шкале слоя."""
        _, _, function = self._hillshade_ramp()
        if function is None:
            return None
        baseline = self._hillshade_baseline
        try:
            items = function.colorRampItemList()
        except Exception:
            return None
        if not baseline or len(baseline) != len(items):
            return None
        ratios = [
            item.color.alpha() / base
            for item, base in zip(items, baseline)
            if base
        ]
        if not ratios:
            return None
        factor = sum(ratios) / len(ratios)
        value = int(round(100.0 - HILLSHADE_DEFAULT * factor))
        return max(0, min(100, value))

    def _sync_opacity_sliders(self):
        """Показывает в шкалах прозрачность слоёв из проекта."""
        for layer_name, widget in getattr(self, "_opacity_widgets", {}).items():
            slider = widget.property("slider")
            label = widget.property("value_label")
            check_box = widget.property("check_box")
            reset_button = widget.property("reset_button")
            if slider is None or label is None:
                continue
            layer = self._layer(self._opacity_target(layer_name) or "")
            value = 100
            if layer_name == HILLSHADE_LAYER:
                if not self._hillshade_baseline:
                    self._capture_hillshade_baseline()
                value = self._hillshade_transparency()
                if value is None:
                    value = HILLSHADE_DEFAULT
            elif layer is not None:
                try:
                    value = int(round(layer.opacity() * 100))
                except Exception:
                    value = 100
            target = self._opacity_target(layer_name) or layer_name
            self._opacity_baseline.setdefault(target, value)
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
