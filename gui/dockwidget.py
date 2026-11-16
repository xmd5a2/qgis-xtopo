import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import (
    QPointF, QRectF, QSettings, QSize, QThread, QTime, QTimer, QUrl,
    pyqtSignal, Qt,
)
from qgis.PyQt.QtGui import (
    QBrush, QDesktopServices, QColor, QFontMetrics, QIcon, QPainter, QPen,
    QPixmap,
)
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDockWidget, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QListView, QMessageBox, QPlainTextEdit,
    QProgressBar,
    QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..core import (
    dem_sources, disk_space, extracts, geofabrik, i18n, measure, net,
    performance, proxy, sonny, terrain_cache,
)
from ..core.bbox import parse_bbox
from ..core.config import (
    Config, default_projects_dir, default_queries, project_name_error,
)
from ..core.map_language import apply_map_language
from ..core.overpass_client import OverpassClient
from ..core.overpass_servers import DEFAULT_OVERPASS_SERVERS
from ..core.pbf_fast import UnsupportedPbf, probe_fast
from ..core.pbf_source import probe_dataset
from ..core.pipeline import run_project
from ..core.project import (
    fit_project_extent, open_project, project_file, project_file_path,
    set_canvas_magnification,
)
from ..core.resources import (
    plugin_build_date, plugin_version, query_names, read_query,
)
from ..core.transliteration import functions_available, patch_project_labeling
from ..core.ui import PLUGIN_DOCK_NAME, hide_docks, show_docks
from .extentdialog import ExtentDialog
from .projecticons import panels_icon
from .projectpanel import ProjectPanel
from .proxy_dialog import ProxyDialog
from .translation import translate_tree

MODE_EXAMPLES = {
    "local": "/usr/bin/osm3s_query --db-dir=/path/to/overpass_db",
    "ssh": 'ssh user@server "/usr/bin/osm3s_query --db-dir=/path/to/overpass_db"',
}

# Провайдеры файловых выгрузок: ключ и подпись в списке режимов.
EXTRACT_PROVIDERS = (
    ("geo2day", "geo2day — 1028 регионов, обновление ежедневно"),
    ("osmfr", "openstreetmap.fr — выгрузки по странам и регионам"),
    ("movisda", "movisda — выгрузки по точным границам областей"),
)

EXTRACT_MODES = tuple(key for key, _title in EXTRACT_PROVIDERS)

# Режимы, где данные берутся из готовых файлов выгрузок.
FILE_MODES = ("geofabrik",) + EXTRACT_MODES


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


def _proxy_state_icon(color, halo, state=None, size=16, ratio=1.0):
    """Значок прокси: контур глобуса и цветная точка состояния.

    Заливка стандартной иконки сети цветом состояния в некоторых стилях
    давала сплошное пятно: там глиф — плотная фигура, и после SourceIn
    от него оставался только цветной силуэт без деталей.  Поэтому контур
    рисуется сам цветом текста, а доступность прокси показывает
    маленькая точка в правом нижнем углу (``halo`` — цвет фона, чтобы
    точка не сливалась с контуром).
    """
    side = max(1, int(round(size * ratio)))
    pixmap = QPixmap(side, side)
    pixmap.fill(_enum(Qt, "GlobalColor", "transparent"))
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(_enum(QPainter, "RenderHint", "Antialiasing"))
    painter.scale(ratio, ratio)
    pen = QPen(color)
    pen.setWidthF(1.3)
    painter.setPen(pen)
    painter.setBrush(_enum(Qt, "BrushStyle", "NoBrush"))
    margin = 1.0
    painter.drawEllipse(QRectF(
        margin, margin, size - 2 * margin, size - 2 * margin,
    ))
    painter.drawEllipse(QRectF(
        size * 0.33, margin, size * 0.34, size - 2 * margin,
    ))
    painter.drawLine(QPointF(margin, size / 2), QPointF(size - margin, size / 2))
    if state in ("ok", "bad"):
        badge = QColor("#2e9e4f") if state == "ok" else QColor("#c0392b")
        center = QPointF(size - 3.9, size - 3.9)
        painter.setPen(_enum(Qt, "PenStyle", "NoPen"))
        painter.setBrush(halo)
        painter.drawEllipse(center, 3.4, 3.4)
        painter.setBrush(badge)
        painter.drawEllipse(center, 2.4, 2.4)
    painter.end()
    return QIcon(pixmap)


def _log_expand_icon(style):
    """Иконка кнопки «развернуть журнал».

    Раньше здесь была иконка открытой папки (SP_DialogOpenButton) — она
    читалась как «открыть файл», а не как «развернуть журнал». Теперь
    используется значок разворачивания окна.
    """
    for pixmap in (
        QStyle.StandardPixmap.SP_TitleBarMaxButton,
        QStyle.StandardPixmap.SP_ArrowUp,
        QStyle.StandardPixmap.SP_DialogOpenButton,
    ):
        icon = style.standardIcon(pixmap)
        if not icon.isNull():
            return icon
    return QStyle.StandardPixmap.SP_DialogOpenButton


def _short_status(text, limit=58):
    """Сокращает длинный текст статуса, оставляя конец пути.

    Длинный путь к проекту в строке статуса обрезался.  Полный текст
    остаётся в подсказке и в журнале.  Прогресс загрузки не сокращается:
    обрезка съела бы хвост с расчётным временем.
    """
    if text.startswith(("Загрузка карт:", "Downloading maps:")):
        return text
    if len(text) <= limit:
        return text
    head, separator, tail = text.rpartition(": ")
    if separator and len(head) + len(separator) < limit - 12:
        keep = limit - len(head) - len(separator) - 1
        return head + separator + "…" + tail[-keep:]
    return "…" + text[-(limit - 1):]


class _MiddleElideDelegate(QStyledItemDelegate):
    """Обрезает длинные строки списка многоточием посередине.

    Названия карт выгрузок не влезают в ширину панели; обрезка посередине
    оставляет видимым и начало названия, и конец строки со статусом
    («скачана»). Полный текст доступен в подсказке элемента.
    """

    _CHECK_WIDTH = 24  # место под галочку слева от текста

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        width = option.rect.width() - self._CHECK_WIDTH
        option.text = option.fontMetrics.elidedText(
            option.text, Qt.ElideMiddle, max(0, width)
        )
        widget = option.widget
        style = widget.style() if widget is not None else option.style
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, widget)

    def sizeHint(self, option, index):
        # Без ограничения ширины элементы растягиваются под полный текст,
        # появляется горизонтальная полоса прокрутки, а делегат рисует
        # строку целиком с обрезкой у края окна.
        hint = super().sizeHint(option, index)
        widget = option.widget
        if widget is not None:
            hint.setWidth(widget.viewport().width())
        return hint


# Фон строк карт, уже лежащих в локальном кэше: слабый светло-зелёный.
_DOWNLOADED_MAP_BG = QColor("#d8efdb")

# Измеренная суммарная скорость четырёх параллельных загрузок карт.
DOWNLOAD_RATE = 1_000_000

# Пороги предупреждения об оценке подготовки: область становится
# тяжёлой для конкретного ПК.
ESTIMATE_CAUTION_SECONDS = 10 * 60
ESTIMATE_CRITICAL_SECONDS = 20 * 60
ESTIMATE_CAUTION_COLOR = "#e67e22"
ESTIMATE_CRITICAL_COLOR = "#e74c3c"


def _checked_state():
    try:
        return Qt.CheckState.Checked, Qt.CheckState.Unchecked
    except AttributeError:
        return Qt.Checked, Qt.Unchecked


def _item_flag():
    try:
        return Qt.ItemFlag.ItemIsUserCheckable
    except AttributeError:
        return Qt.ItemIsUserCheckable


def _tooltip_role():
    try:
        return Qt.ItemDataRole.ToolTipRole
    except AttributeError:
        return Qt.ToolTipRole


def _user_role():
    try:
        return Qt.ItemDataRole.UserRole
    except AttributeError:
        return Qt.UserRole


def _top_right_corner():
    try:
        return Qt.Corner.TopRightCorner
    except AttributeError:
        return Qt.TopRightCorner


def _item_flags():
    try:
        return (
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
        )
    except AttributeError:
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable


def _no_flags():
    try:
        return Qt.ItemFlag.NoItemFlags
    except AttributeError:
        return Qt.NoItemFlags


def _spaced(value):
    return f"{value:,}".replace(",", " ")


def _duration(seconds):
    if seconds < 1:
        return i18n.tr("меньше 1 с")
    seconds = int(round(seconds))
    if seconds < 60:
        return i18n.trf("{} с", seconds)
    minutes = seconds // 60
    if minutes < 60:
        return i18n.trf("{} мин", minutes)
    return i18n.trf("{} ч {} мин", minutes // 60, minutes % 60)


def _estimate_warning(seconds, text):
    """Подсвечивает оценку подготовки для тяжёлой области.

    Больше 10 минут — оранжевым, больше 20 — красным и жирным: так
    видно, что область слишком велика для этого ПК.
    """
    if seconds > ESTIMATE_CRITICAL_SECONDS:
        return (
            f'<span style="color:{ESTIMATE_CRITICAL_COLOR}; '
            f'font-weight:bold;">{text}</span>'
        )
    if seconds > ESTIMATE_CAUTION_SECONDS:
        return f'<span style="color:{ESTIMATE_CAUTION_COLOR};">{text}</span>'
    return text


def _estimate_duration(seconds):
    return _estimate_warning(seconds, _duration(seconds))


PRIMARY_BUTTON_STYLE = (
    "QPushButton {"
    " font-weight: bold;"
    " padding: 4px 12px;"
    " border: 1px solid palette(highlight);"
    " border-radius: 3px;"
    " background-color: palette(highlight);"
    " color: palette(highlighted-text);"
    "}"
)


def _primary_button(button):
    """Emphasize the main action without depending on a fixed palette."""
    button.setStyleSheet(PRIMARY_BUTTON_STYLE)
    return button


def _set_primary(button, active):
    button.setStyleSheet(PRIMARY_BUTTON_STYLE if active else "")


LANGUAGE_BUTTON_STYLE = (
    "QToolButton {"
    " border: 1px solid rgba(128, 128, 128, 0.65);"
    " border-radius: 2px;"
    " padding: 0px 2px;"
    "}"
    "QToolButton:hover {"
    " border-color: palette(highlight);"
    "}"
)


class PipelineWorker(QThread):
    progress = pyqtSignal(str)
    percent = pyqtSignal(int)
    status = pyqtSignal(str)
    # Не называть сигнал finished: он перекрывает встроенный
    # QThread.finished, по которому поток удаляется после возврата из run().
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.cancel_event = threading.Event()

    def run(self):
        try:
            path = run_project(
                self.config, self.progress.emit, self.cancel_event,
                percent=self.percent.emit, status=self.status.emit,
            )
            self.completed.emit(str(path))
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def cancel(self):
        self.cancel_event.set()


class ServerCheckWorker(QThread):
    checked = pyqtSignal(str, bool, str)

    def __init__(self, endpoint, parent=None):
        super().__init__(parent)
        self.endpoint = endpoint
        self.cancel_event = threading.Event()

    def run(self):
        client = OverpassClient([self.endpoint], timeout=10)
        started = time.monotonic()
        try:
            client.check(self.endpoint, cancel=self.cancel_event)
        except InterruptedError:
            return
        except Exception as exc:
            self.checked.emit(self.endpoint, False, str(exc))
        else:
            self.checked.emit(self.endpoint, True, f"{time.monotonic() - started:.1f}")

    def cancel(self):
        self.cancel_event.set()


class PbfProbeWorker(QThread):
    result = pyqtSignal(int, int, int, float, bool, int, int, int, float, float)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, config, bbox, names, texts, paths=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.bbox = bbox
        self.names = names
        self.texts = texts
        self.paths = [Path(path) for path in (paths or [config.pbf_path])]
        self.cancel_event = threading.Event()

    def run(self):
        approximate = False
        sampled = 0
        total_blobs = 0
        total = in_bbox = matched = 0
        elapsed = 0.0
        extended_ratio = 0.0
        try:
            for position, path in enumerate(self.paths, 1):
                if self.cancel_event.is_set():
                    raise InterruptedError
                if len(self.paths) > 1:
                    self.progress.emit(
                        f"Оценка PBF {position}/{len(self.paths)}: {path.name}"
                    )
                probe_config = replace(self.config, pbf_path=str(path))
                try:
                    probe = probe_fast(
                        probe_config, self.bbox, self.names, self.texts,
                        cancel=self.cancel_event, progress=self.progress.emit,
                    )
                except UnsupportedPbf:
                    probe = probe_dataset(
                        probe_config, self.bbox, self.names, self.texts,
                        cancel=self.cancel_event, progress=self.progress.emit,
                    )
                else:
                    approximate = approximate or probe.approximate
                    sampled += probe.sampled_blobs
                    total_blobs += probe.total_blobs
                total += probe.total
                in_bbox += probe.in_bbox
                matched += probe.matched
                elapsed += probe.elapsed
                if getattr(probe, "extended_ratio", 0.0):
                    # Для нескольких выгрузок берём самую большую долю:
                    # столько данных придётся прочитать в худшем случае.
                    extended_ratio = max(extended_ratio, probe.extended_ratio)
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            # Скорость чтения нужна оценке подготовки: она показывает,
            # насколько диск и кэш быстрее или медленнее эталонных.
            source_bytes = 0
            for path in self.paths:
                try:
                    source_bytes += path.stat().st_size
                except OSError:
                    pass
            bytes_per_second = source_bytes / elapsed if elapsed > 0 else 0.0
            self.result.emit(
                total, in_bbox, matched, elapsed,
                approximate, sampled, total_blobs, len(self.paths),
                extended_ratio, bytes_per_second,
            )

    def cancel(self):
        self.cancel_event.set()


class DemAvailabilityWorker(QThread):
    """Проверка покрытия источников рельефа для выбранного охвата."""

    result = pyqtSignal(int, object, object)
    failed = pyqtSignal(int, str)

    def __init__(self, bbox, generation, force=False, parent=None):
        super().__init__(parent)
        self.bbox = bbox
        self.generation = generation
        self.force = force
        self.cancel_event = threading.Event()

    def run(self):
        try:
            sonny_cells = set(sonny.catalogue_tiles(sonny.catalogue_dir()))
            availability = dem_sources.check_availability(
                self.bbox, cancel=self.cancel_event, timeout=8,
                sonny_tiles=sonny_cells, force=self.force,
            )
        except InterruptedError:
            return
        except Exception as exc:  # noqa: BLE001 - сеть не должна ломать панель
            self.failed.emit(self.generation, str(exc))
        else:
            self.result.emit(self.generation, availability, sonny_cells)

    def cancel(self):
        self.cancel_event.set()


class SonnyCatalogueWorker(QThread):
    """Фоновое обновление списка тайлов рельефа Sonny DTM.

    Список покрытия нужен для проверки доступности, но он лежит в сети:
    обновление идёт в фоне, чтобы панель не подвисала при старте.
    """

    updated = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def run(self):
        try:
            sonny.refresh_catalogue(
                sonny.catalogue_dir(), cancel=self.cancel_event)
        except InterruptedError:
            return
        except Exception:  # noqa: BLE001 - сеть не должна ломать запуск
            return
        self.updated.emit(len(sonny.catalogue_tiles(sonny.catalogue_dir())))

    def cancel(self):
        self.cancel_event.set()


class ProxyCheckWorker(QThread):
    """Проверка доступности прокси без блокировки панели."""

    checked = pyqtSignal(int, bool)

    def __init__(self, config, generation, parent=None):
        super().__init__(parent)
        self.config = config
        self.generation = generation

    def run(self):
        self.checked.emit(self.generation, proxy.probe(self.config))


class MapIndexWorker(QThread):
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, config, bbox, parent=None, provider="geofabrik"):
        super().__init__(parent)
        self.config = config
        self.bbox = bbox
        self.provider = provider
        self.cancel_event = threading.Event()

    def run(self):
        try:
            if self.provider == "geofabrik":
                plan = geofabrik.analyse(
                    self.config, self.bbox,
                    cancel=self.cancel_event, progress=self.progress.emit,
                )
            else:
                plan = extracts.build_plan(
                    self.config, self.provider, self.bbox,
                    cancel=self.cancel_event, progress=self.progress.emit,
                )
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.result.emit(plan)

    def cancel(self):
        self.cancel_event.set()


class WrappedLabel(QLabel):
    """Подпись, высота которой следует за переносами строк.

    QLabel с wordWrap в QFormLayout может получить меньше высоты, чем
    нужно тексту, и тогда последняя строка обрезается.  Минимальная
    высота считается по фактической ширине.
    """

    def _wrapped_height(self, hint):
        width = self.width()
        if width > 0 and self.wordWrap():
            hint.setHeight(max(hint.height(), self.heightForWidth(width)))
        return hint

    def sizeHint(self):
        return self._wrapped_height(super().sizeHint())

    def minimumSizeHint(self):
        return self._wrapped_height(super().minimumSizeHint())

    def setText(self, text):
        super().setText(text)
        self._sync_height()
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None and parent.layout() is not None:
            # Смена текста меняет нужную высоту: без этого QFormLayout
            # мог оставить старую высоту и обрезать последнюю строку.
            parent.layout().activate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self):
        width = self.width()
        if width > 0 and self.wordWrap():
            height = self.heightForWidth(width)
            if height > 0:
                self.setMinimumHeight(height)


class XtopoDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        details = " ".join(
            part for part in (plugin_version(), plugin_build_date()) if part
        )
        title = f"QGIS-xtopo {details}".strip()
        super().__init__(title, parent)
        self.iface = iface
        self.worker = None
        self.server_workers = []
        self.pbf_probe = None
        self.map_index_worker = None
        self.dem_availability_worker = None
        self._dem_availability = {}
        self._dem_bbox = None
        self._dem_source_syncing = False
        self._dem_sonny_tiles = set()
        self._dem_check_message = ""
        self._dem_check_detail = ""
        # Сообщение о замене тайлов GLO-30 на SRTM после обработки.
        self._terrain_fallback = ""
        self._dem_availability_generation = 0
        self.sonny_catalogue_worker = None
        self._dem_check_timer = QTimer(self)
        self._dem_check_timer.setSingleShot(True)
        self._dem_check_timer.timeout.connect(self._start_dem_availability)
        self._map_index_plan = None
        self._map_index_plan_bbox = ""
        self._map_index_stats_maps = ()
        self._pbf_info_state = None
        self._map_index_timer = QTimer(self)
        self._map_index_timer.setSingleShot(True)
        self._map_index_timer.timeout.connect(self._start_map_index)
        self._pbf_probe_timer = QTimer(self)
        self._pbf_probe_timer.setSingleShot(True)
        self._pbf_probe_timer.timeout.connect(self._start_pbf_probe)
        self._servers_checked = False
        self._mode_values = {}
        self._active_mode = "geofabrik"
        self._own_project_open = False
        self._own_marker = False
        self._hidden_panels = []
        self._running = False
        self._narrowed = False
        self._minimum_width = 0
        self._project_timer = QTimer(self)
        self._project_timer.setSingleShot(True)
        self._project_timer.setInterval(0)
        self._project_timer.timeout.connect(self._refresh_project_state)
        self.proxy_check = None
        self._proxy_generation = 0
        self._proxy_ok = False
        self._proxy_known = False
        self._proxy_recheck = False
        self._shutting_down = False
        self._proxy_icon_key = ""
        self._proxy_timer = QTimer(self)
        self._proxy_timer.setInterval(30_000)
        self._proxy_timer.timeout.connect(self._check_proxy)
        self.settings = QSettings("qgis-xtopo", "qgis-xtopo")
        proxy.apply()
        i18n.add_listener(self._language_changed)
        self.destroyed.connect(self.detach_language_listener)
        app = QgsApplication.instance()
        self._application = app
        if app is not None:
            app.aboutToQuit.connect(self.shutdown_workers)
        self.setObjectName("QgisXtopoDockWidget")
        self._build_ui()
        self._restore_settings()
        self._connect_project_signals()
        self._refresh_project_state()
        self.retranslate()
        if not self._own_project_open:
            # Проекта нет — возвращаем панели, скрытые в прошлой сессии.
            self.restore_panels()

    def refresh_sonny_catalogue(self, force=False):
        """Обновляет список тайлов рельефа Sonny DTM в фоне.

        Список нужен для проверки доступности источников.  Он лежит в
        сети, поэтому обновление идёт отдельным потоком: панель при
        этом не подвисает.  Свежий список не трогаем, если не просят
        принудительно.
        """
        age = sonny.catalogue_age_days()
        if not force and age is not None and age < sonny.CACHE_DAYS:
            return
        if self.sonny_catalogue_worker is not None:
            try:
                if self.sonny_catalogue_worker.isRunning():
                    return
            except RuntimeError:
                self.sonny_catalogue_worker = None
        worker = SonnyCatalogueWorker(self)
        worker.updated.connect(self._sonny_catalogue_updated)
        worker.finished.connect(lambda: self._sonny_worker_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.sonny_catalogue_worker = worker
        worker.start()

    def _sonny_worker_finished(self, worker):
        if self.sonny_catalogue_worker is worker:
            self.sonny_catalogue_worker = None

    def _sonny_catalogue_updated(self, count):
        """Список тайлов обновлён — проверку доступности надо повторить."""
        self._dem_sonny_tiles = set(sonny.catalogue_tiles())
        if self.bbox.text().strip():
            self._schedule_dem_availability()

    def showEvent(self, event):
        super().showEvent(event)
        # Пока панель скрыта, статус прокси не проверяем: без настроенного
        # прокси это бесполезно, а с недоступным — лишние сетевые попытки.
        self._check_proxy()
        self._proxy_timer.start()

    def hideEvent(self, event):
        self._proxy_timer.stop()
        super().hideEvent(event)

    def shutdown_workers(self, final=True):
        """Останавливает фоновые потоки до удаления панели.

        QThread нельзя уничтожать, пока он работает: QGIS падает с
        «QThread: Destroyed while thread is still running».  Поэтому при
        закрытии, выгрузке плагина и завершении QGIS сначала отменяем
        все задачи, затем ждём потоки; не оставшиеся — принудительно
        завершаем.  ``final=False`` — панель прячется, но может снова
        открыться и запускать проверки.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        for timer in (
                self._pbf_probe_timer, self._map_index_timer,
                self._dem_check_timer, self._project_timer,
                self._proxy_timer):
            try:
                timer.stop()
            except RuntimeError:
                pass
        workers = []
        for name in (
                "worker", "pbf_probe", "map_index_worker",
                "dem_availability_worker", "sonny_catalogue_worker",
                "proxy_check"):
            worker = getattr(self, name, None)
            if worker is not None:
                workers.append((name, worker))
        for worker in list(self.server_workers):
            workers.append(("server", worker))
        for _name, worker in workers:
            try:
                cancel = getattr(worker, "cancel", None)
                if callable(cancel) and worker.isRunning():
                    cancel()
            except RuntimeError:
                pass
        timeouts = {
            "worker": 30000,
            "pbf_probe": 10000,
            "map_index_worker": 15000,
            "dem_availability_worker": 15000,
            "sonny_catalogue_worker": 30000,
            "proxy_check": 5000,
            "server": 15000,
        }
        for name, worker in workers:
            try:
                if not worker.isRunning():
                    continue
                if not worker.wait(timeouts.get(name, 5000)):
                    worker.terminate()
                    worker.wait(2000)
            except RuntimeError:
                pass
        self.server_workers = []
        for name in (
                "worker", "pbf_probe", "map_index_worker",
                "dem_availability_worker", "sonny_catalogue_worker",
                "proxy_check"):
            setattr(self, name, None)
        if not final:
            self._shutting_down = False

    def detach_language_listener(self):
        self.shutdown_workers()
        if self._application is not None:
            try:
                self._application.aboutToQuit.disconnect(
                    self.shutdown_workers)
            except (TypeError, RuntimeError):
                pass
            self._application = None
        i18n.remove_listener(self._language_changed)

    def _fit_language_button(self):
        """Высота угловых кнопок — по строке заголовков вкладок.

        Угловой виджет стоит внутри полосы вкладок. Если кнопки выше неё,
        они выходят за рамку вкладок, поэтому обе получают одинаковую
        высоту с небольшим отступом сверху и снизу.
        """
        bar = self.tabs.tabBar()
        height = bar.sizeHint().height()
        if height <= 0:
            height = bar.height()
        height = max(16, height - 8)
        metrics = QFontMetrics(self.language_button.font())
        width = max(30, metrics.horizontalAdvance("RU") + 16)
        self.language_button.setFixedSize(QSize(width, height))
        if hasattr(self, "proxy_button"):
            self.proxy_button.setFixedSize(QSize(height, height))
        corner = self.tabs.cornerWidget(_top_right_corner())
        if corner is not None:
            # Полоса вкладок прижимает угол к низу, поэтому снизу оставляем
            # поле в раскладке, а не в высоте виджета.
            corner.setFixedHeight(height + 4)

    def _narrow_minimum_width(self):
        """Минимальная ширина панели на 15% меньше естественной."""
        hint = self.minimumSizeHint()
        width = hint.width()
        if width <= 0:
            return
        target = int(width * 0.85)
        # Ширину задаёт содержимое панели: без этого подсказка ширины
        # остаётся прежней и панель нельзя сжать.
        self.container.setMinimumWidth(target)
        self.tabs.setMinimumWidth(target)
        self.setMinimumWidth(target)
        self._minimum_width = target
        return target

    def _collapse_to_minimum(self):
        """По умолчанию панель занимает минимальную ширину."""
        target = self._narrow_minimum_width()
        if not target:
            return
        iface = self.iface
        if iface is not None:
            try:
                iface.mainWindow().resizeDocks(
                    [self], [target], Qt.Horizontal
                )
                return
            except Exception:
                pass
        self.resize(target, self.height())

    def _build_ui(self):
        container = QWidget(self)
        self.container = container
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.language_button = QToolButton()
        self.language_button.setText(i18n.language().upper())
        self.language_button.setAutoRaise(True)
        bold = self.language_button.font()
        bold.setBold(True)
        self.language_button.setFont(bold)
        self.language_button.setStyleSheet(LANGUAGE_BUTTON_STYLE)
        self.language_button.setToolTip("Язык интерфейса")
        self.language_button.clicked.connect(self._switch_language)
        self.proxy_button = QToolButton()
        self.proxy_button.setAutoRaise(True)
        off_color = self.palette().color(self.foregroundRole())
        halo = self.palette().window().color()
        ratio = self.devicePixelRatioF()
        self._proxy_icons = {
            "off": _proxy_state_icon(off_color, halo, ratio=ratio),
            "ok": _proxy_state_icon(off_color, halo, "ok", ratio=ratio),
            "bad": _proxy_state_icon(off_color, halo, "bad", ratio=ratio),
        }
        self.proxy_button.setIconSize(QSize(16, 16))
        self.proxy_button.setIcon(self._proxy_icons["off"])
        self.proxy_button.clicked.connect(self._open_proxy_dialog)
        corner = QWidget(self.tabs)
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 4, 4)
        corner_layout.setSpacing(2)
        corner_layout.addWidget(self.proxy_button)
        corner_layout.addWidget(self.language_button)
        self.tabs.setCornerWidget(corner, _top_right_corner())
        self._update_proxy_tooltip()
        self._fit_language_button()

        data_tab = QWidget()
        data_layout = QVBoxLayout(data_tab)
        data_layout.setContentsMargins(4, 4, 4, 4)
        data_layout.setSpacing(4)

        project_group = QGroupBox("Проект")
        project_form = QFormLayout(project_group)
        self.projects_dir = QLineEdit()
        browse_projects = QPushButton("…")
        browse_projects.setMaximumWidth(32)
        browse_projects.clicked.connect(self._browse_projects)
        row = QHBoxLayout()
        row.addWidget(self.projects_dir)
        row.addWidget(browse_projects)
        project_form.addRow("Каталог проектов:", row)
        self.disk_info = WrappedLabel("")
        self.disk_info.setWordWrap(True)
        self.disk_info.setToolTip(
            i18n.tr(
                "Свободное место на диске, где хранится проект: карты OSM, "
                "растры рельефа и слои. Для рельефа на сотни километров "
                "нужно несколько гигабайт."
            )
        )
        project_form.addRow(self.disk_info)
        self.project_name = QLineEdit("automap")
        self.project_name.setPlaceholderText("буквы, цифры, _ - +")
        self.project_name.textChanged.connect(self._project_name_changed)
        self.projects_dir.textChanged.connect(self._project_path_changed)
        # Свободное место пересчитываем только при смене каталога: имя
        # проекта на том не влияет.
        self.projects_dir.textChanged.connect(self._update_disk_info)
        project_form.addRow("Имя проекта:", self.project_name)
        self.project_name_hint = WrappedLabel("")
        self.project_name_hint.setWordWrap(True)
        self.project_name_hint.setStyleSheet("color: #b00020;")
        self.project_name_hint.setVisible(False)
        project_form.addRow(self.project_name_hint)

        self.bbox = QLineEdit()
        self.bbox.textChanged.connect(self._bbox_text_changed)
        self.choose_extent = _primary_button(QPushButton("Выбрать…"))
        self.choose_extent.setToolTip(
            "Открыть карту OpenStreetMap и выбрать охват без создания проекта"
        )
        self.choose_extent.clicked.connect(self._bbox_from_picker)
        row = QHBoxLayout()
        row.addWidget(self.bbox)
        row.addWidget(self.choose_extent)
        project_form.addRow("Охват:", row)
        self.area_label = project_form.labelForField(row)
        data_layout.addWidget(project_group)

        source_group = QGroupBox("Источник OSM")
        source_form = QFormLayout(source_group)
        self.source_mode = QComboBox()
        self.source_mode.addItem("Карты с Geofabrik (предпочтительно)", "geofabrik")
        for key, title in EXTRACT_PROVIDERS:
            self.source_mode.addItem(title, key)
        self.source_mode.addItem("Вырез по рамке (SliceOSM)", "slice")
        self.source_mode.addItem("Публичный Overpass API", "overpass")
        self.source_mode.addItem("Локальный PBF/OSM экстракт", "pbf")
        self.source_mode.addItem("Локальный Overpass", "local")
        self.source_mode.addItem("Overpass по SSH", "ssh")
        self.source_mode.currentIndexChanged.connect(self._source_changed)
        source_form.addRow("Режим:", self.source_mode)

        self.overpass_server = QComboBox()
        role = _tooltip_role()
        for server in DEFAULT_OVERPASS_SERVERS:
            self.overpass_server.addItem(f"{server.name} — не проверен", server.url)
            self.overpass_server.setItemData(
                self.overpass_server.count() - 1, server.url, role
            )
        self.overpass_server.addItem("Свой URL…", "")
        self.overpass_server.currentIndexChanged.connect(self._server_changed)
        self.recheck_servers = QPushButton("Проверить")
        self.recheck_servers.setMaximumWidth(90)
        self.recheck_servers.clicked.connect(lambda: self._check_servers(force=True))
        server_widget = QWidget()
        server_row = QHBoxLayout(server_widget)
        server_row.setContentsMargins(0, 0, 0, 0)
        server_row.addWidget(self.overpass_server, 1)
        server_row.addWidget(self.recheck_servers)
        source_form.addRow("Сервер Overpass:", server_widget)
        self.server_label = source_form.labelForField(server_widget)

        self.source_value = QLineEdit()
        self.source_value.setPlaceholderText("URL Overpass, путь к PBF или команда")
        self.source_value.textChanged.connect(self._source_path_changed)
        browse_source = QPushButton("…")
        browse_source.setMaximumWidth(32)
        browse_source.clicked.connect(self._browse_source)
        self.browse_source = browse_source
        self.download_pbf_button = QPushButton("Geofabrik")
        self.download_pbf_button.clicked.connect(self._open_geofabrik)
        row = QHBoxLayout()
        row.addWidget(self.source_value)
        row.addWidget(browse_source)
        row.addWidget(self.download_pbf_button)
        source_form.addRow("Адрес/файл:", row)
        self.source_label = source_form.labelForField(row)
        self.source_hint = WrappedLabel("")
        self.source_hint.setWordWrap(True)
        source_form.addRow(self.source_hint)
        self.map_index_group = QGroupBox("")
        maps_layout = QVBoxLayout(self.map_index_group)
        maps_layout.setContentsMargins(4, 4, 4, 4)
        # Заголовок группы с датой индекса и кнопкой обновления.
        maps_header = QHBoxLayout()
        self.map_index_title = QLabel(i18n.tr("Карты Geofabrik"))
        maps_header.addWidget(self.map_index_title)
        # Статус занимает свободное место в строке заголовка: отдельная
        # строка добавляла бы вертикальный промежуток перед списком.
        self.map_index_status = QLabel("")
        self.map_index_status.setWordWrap(False)
        self.map_index_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.map_index_status.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        maps_header.addWidget(self.map_index_status, 1)
        self.map_index_refresh = QToolButton()
        self.map_index_refresh.setIcon(
            QgsApplication.getThemeIcon("mActionReload.svg")
        )
        self.map_index_refresh.setAutoRaise(True)
        self.map_index_refresh.setFixedSize(QSize(18, 18))
        self.map_index_refresh.setIconSize(QSize(14, 14))
        self.map_index_refresh.setToolTip(
            i18n.tr("Обновить индекс выгрузок")
        )
        self.map_index_refresh.clicked.connect(self._refresh_map_index)
        maps_header.addWidget(self.map_index_refresh)
        maps_layout.addLayout(maps_header)
        self.map_index_list = QListWidget()
        self.map_index_list.setMaximumHeight(150)
        # Элементы всегда по ширине окна списка: иначе длинные названия
        # дают горизонтальную прокрутку, а делегат обрезает строку у края
        # вместо многоточия посередине.
        self.map_index_list.setResizeMode(QListView.Adjust)
        self.map_index_list.setItemDelegate(
            _MiddleElideDelegate(self.map_index_list)
        )
        self.map_index_list.itemChanged.connect(self._map_selection_changed)
        maps_layout.addWidget(self.map_index_list)
        self.map_index_summary = WrappedLabel("")
        self.map_index_summary.setWordWrap(True)
        maps_layout.addWidget(self.map_index_summary)
        self.map_index_group.setVisible(False)
        source_form.addRow(self.map_index_group)
        self.pbf_info = WrappedLabel("")
        self.pbf_info.setWordWrap(True)
        self.pbf_info.setVisible(False)
        source_form.addRow(self.pbf_info)
        data_layout.addWidget(source_group)
        data_layout.addStretch(1)

        layers_tab = QWidget()
        layers_layout = QVBoxLayout(layers_tab)
        layers_layout.setContentsMargins(4, 4, 4, 4)
        layers_layout.setSpacing(4)
        queries_group = QGroupBox("Слои")
        queries_layout = QVBoxLayout(queries_group)
        self.queries = QListWidget()
        self.queries.setObjectName("queriesList")
        self.queries.setMinimumHeight(120)
        self._fill_queries(query_names())
        self.queries.itemChanged.connect(self._queries_changed)
        queries_layout.addWidget(self.queries)
        buttons = QHBoxLayout()
        all_button = QPushButton("Все")
        none_button = QPushButton("Ничего")
        common_button = QPushButton("По умолчанию")
        all_button.clicked.connect(lambda: self._set_queries(True))
        none_button.clicked.connect(lambda: self._set_queries(False))
        common_button.clicked.connect(self._select_default_queries)
        buttons.addWidget(all_button)
        buttons.addWidget(none_button)
        buttons.addWidget(common_button)
        queries_layout.addLayout(buttons)
        layers_layout.addWidget(queries_group)

        terrain_tab = QWidget()
        terrain_layout = QVBoxLayout(terrain_tab)
        terrain_layout.setContentsMargins(4, 4, 4, 4)
        terrain_layout.setSpacing(4)
        terrain_group = QGroupBox("Рельеф")
        terrain_form = QFormLayout(terrain_group)
        self.generate_terrain = QCheckBox("Обрабатывать рельеф")
        self.generate_terrain.setChecked(True)
        self.generate_terrain.toggled.connect(self._terrain_changed)
        terrain_form.addRow(self.generate_terrain)
        self.dem_source = QComboBox()
        for item in dem_sources.SOURCES:
            self.dem_source.addItem(i18n.tr(item.title), item.key)
        self.dem_source.currentIndexChanged.connect(self._dem_source_changed)
        self.dem_refresh = QToolButton()
        self.dem_refresh.setIcon(
            QgsApplication.getThemeIcon("mActionReload.svg")
        )
        self.dem_refresh.setAutoRaise(True)
        self.dem_refresh.setFixedSize(QSize(18, 18))
        self.dem_refresh.setIconSize(QSize(14, 14))
        self.dem_refresh.setToolTip(
            i18n.tr("Проверить доступность заново")
        )
        self.dem_refresh.clicked.connect(self._refresh_dem_availability)
        self.dem_source_row = QWidget()
        source_row = QHBoxLayout(self.dem_source_row)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.setSpacing(4)
        source_row.addWidget(self.dem_source, 1)
        source_row.addWidget(self.dem_refresh)
        terrain_form.addRow("Источник:", self.dem_source_row)
        self.dem_source_label = terrain_form.labelForField(
            self.dem_source_row
        )
        self.dem_info = WrappedLabel()
        self.dem_info.setWordWrap(True)
        try:
            self.dem_info.setTextFormat(Qt.TextFormat.PlainText)
        except AttributeError:
            self.dem_info.setTextFormat(Qt.PlainText)
        terrain_form.addRow(self.dem_info)
        self.dem_warning = WrappedLabel()
        self.dem_warning.setWordWrap(True)
        try:
            self.dem_warning.setTextFormat(Qt.TextFormat.PlainText)
        except AttributeError:
            self.dem_warning.setTextFormat(Qt.PlainText)
        self.dem_warning.setStyleSheet("color: #b35c00;")
        self.dem_warning.setVisible(False)
        terrain_form.addRow(self.dem_warning)
        self.dem_dir = QLineEdit()
        self.dem_dir.textChanged.connect(self._dem_dir_changed)
        self.browse_dem = QPushButton("…")
        self.browse_dem.setMaximumWidth(32)
        self.browse_dem.clicked.connect(self._browse_dem)
        self.dem_dir_row = QWidget()
        dem_row = QHBoxLayout(self.dem_dir_row)
        dem_row.setContentsMargins(0, 0, 0, 0)
        dem_row.addWidget(self.dem_dir)
        dem_row.addWidget(self.browse_dem)
        terrain_form.addRow("Каталог DEM:", self.dem_dir_row)
        self.dem_dir_label = terrain_form.labelForField(self.dem_dir_row)
        self.dem_dir_row.setVisible(False)
        if self.dem_dir_label is not None:
            self.dem_dir_label.setVisible(False)
        self.hillshade_slope = QCheckBox("Затенение рельефа и уклоны")
        self.hillshade_slope.setChecked(True)
        self.hillshade_slope.toggled.connect(
            lambda _checked: self._terrain_changed(
                self.generate_terrain.isChecked()
            )
        )
        terrain_form.addRow(self.hillshade_slope)
        self.resample_method = QComboBox()
        self.resample_method.addItem("cubicspline — мягче", "cubicspline")
        self.resample_method.addItem("lanczos — резче", "lanczos")
        self.resample_method.setToolTip(
            "Terrain resample method: влияет на детальность затенения "
            "рельефа и уклонов.\n"
            "cubicspline — мягче, меньше деталей; "
            "lanczos — резче, возможны артефакты"
        )
        # Список не должен занимать всю ширину панели: из-за этого
        # подпись «Масштабирование» не помещалась.
        try:
            self.resample_method.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToContents
            )
        except AttributeError:
            self.resample_method.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.resample_method.setMaximumWidth(170)
        terrain_form.addRow("Масштабирование:", self.resample_method)
        self.resample_label = terrain_form.labelForField(
            self.resample_method
        )
        separator = QFrame()
        try:
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setFrameShadow(QFrame.Shadow.Sunken)
        except AttributeError:
            separator.setFrameShape(QFrame.HLine)
            separator.setFrameShadow(QFrame.Sunken)
        terrain_form.addRow(separator)
        self.isolines = QCheckBox("Контуры высот")
        self.isolines.setChecked(True)
        self.isolines.toggled.connect(
            lambda _checked: self._terrain_changed(
                self.generate_terrain.isChecked()
            )
        )
        terrain_form.addRow(self.isolines)
        self.isolines_steps = {}
        self.isolines_steps_widget = QWidget()
        steps_layout = QGridLayout(self.isolines_steps_widget)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.setHorizontalSpacing(8)
        steps_layout.setVerticalSpacing(2)
        for index, value in enumerate((5, 10, 25, 50, 100, 200)):
            box = QCheckBox(f"{value} м")
            box.setToolTip(
                "Отметка включает эту кратность и все кратные ей. "
                "Снятие убирает одну галочку — набор можно уточнять по "
                "одной. Контуры строятся с минимальной из отмеченных; "
                "последнюю галочку снять нельзя."
            )
            box.toggled.connect(
                lambda checked, step=value: self._isolines_step_toggled(
                    step, checked
                )
            )
            steps_layout.addWidget(box, index // 3, index % 3)
            self.isolines_steps[value] = box
        terrain_form.addRow("Кратность, м:", self.isolines_steps_widget)
        self.isolines_steps_label = terrain_form.labelForField(
            self.isolines_steps_widget
        )
        self.smooth_isolines = QCheckBox("Сглаживать контуры")
        terrain_form.addRow(self.smooth_isolines)
        terrain_layout.addWidget(terrain_group)
        terrain_layout.addStretch(1)
        self._set_isolines_step(10)
        self._terrain_changed(self.generate_terrain.isChecked())

        self.tabs.addTab(data_tab, "Данные")
        self.tabs.addTab(terrain_tab, "Рельеф")
        self.tabs.addTab(layers_tab, "Слои")
        self.project_panel = ProjectPanel(self.iface, self)
        self.project_panel.statusMessage.connect(self._append_log)
        self.project_tab_index = self.tabs.addTab(self.project_panel, "Проект")
        self.tabs.setTabEnabled(self.project_tab_index, False)
        layout.addWidget(self.tabs, 1)

        buttons = QGridLayout()
        self.prepare_button = _primary_button(QPushButton("Подготовить данные"))
        self.prepare_button.clicked.connect(self._prepare)
        self.open_button = QPushButton("Открыть проект")
        self.open_button.clicked.connect(self._open_project)
        self.close_button = QPushButton("Закрыть проект")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self._close_project)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.prepare_button, 0, 0)
        buttons.addWidget(self.cancel_button, 0, 1)
        buttons.addWidget(self.open_button, 1, 0)
        buttons.addWidget(self.close_button, 1, 1)
        layout.addLayout(buttons)

        self.status = WrappedLabel("Готово")
        self.status.setWordWrap(True)
        # Высота следует за переносами: длинный прогресс с ETA не
        # обрезается, а при коротком статусе строка занимает одну строку.
        self.status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(70)
        self.log.setMaximumHeight(140)
        self.log.setVisible(False)
        self.log_window = None
        self.log_window_text = None
        self.log_toggle = QCheckBox("Журнал")
        self.log_toggle.toggled.connect(self.log.setVisible)
        self.log_window_button = QPushButton()
        self.log_window_button.setIcon(_log_expand_icon(self.style()))
        self.log_window_button.setFixedSize(24, 24)
        self.log_window_button.setToolTip("Открыть журнал в отдельном окне")
        self.log_window_button.clicked.connect(self._open_log_window)
        # Кнопка живёт вне вкладок: вкладки блокируются при открытом
        # проекте, а переключить панели нужно именно в этот момент.
        self.show_panels_button = QPushButton()
        self.show_panels_button.setIcon(panels_icon(self.palette()))
        self.show_panels_button.setFixedSize(24, 24)
        self.show_panels_button.setToolTip(
            "Скрыть или показать стандартные панели и панели инструментов "
            "QGIS, а также строку уведомлений. При открытии проекта всё это "
            "скрывается автоматически."
        )
        self.show_panels_button.clicked.connect(self._show_panels)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.log_toggle)
        status_row.addWidget(self.log_window_button)
        status_row.addWidget(self.show_panels_button)
        layout.addLayout(status_row)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        layout.addLayout(progress_row)
        layout.addWidget(self.log)

        self.setWidget(container)

    def _fill_queries(self, names):
        checked, unchecked = _checked_state()
        flag = _item_flag()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | flag)
            item.setCheckState(checked)
            # Имя файла запроса хранится отдельно от подписи: подпись могут
            # перевести, а имя файла нужно для чтения запроса как есть.
            item.setData(_user_role(), name)
            self.queries.addItem(item)

    def _set_queries(self, value):
        checked, unchecked = _checked_state()
        state = checked if value else unchecked
        for index in range(self.queries.count()):
            self.queries.item(index).setCheckState(state)

    def _select_default_queries(self):
        defaults = set(default_queries())
        checked, unchecked = _checked_state()
        for index in range(self.queries.count()):
            item = self.queries.item(index)
            name = item.data(_user_role()) or item.text()
            item.setCheckState(checked if name in defaults else unchecked)

    def _selected_queries(self):
        checked, _ = _checked_state()
        names = []
        for index in range(self.queries.count()):
            item = self.queries.item(index)
            if item.checkState() != checked:
                continue
            names.append(str(item.data(_user_role()) or item.text()))
        return names

    def _browse_projects(self):
        path = QFileDialog.getExistingDirectory(
            self, i18n.tr("Каталог проектов"), self.projects_dir.text()
        )
        if path:
            self.projects_dir.setText(path)

    def _project_name_changed(self, text):
        error = project_name_error(text)
        self.project_name_hint.setText(i18n.tr(error))
        self.project_name_hint.setVisible(bool(error))
        self._project_path_changed()

    def _project_path_changed(self, *_args):
        """Имя или каталог изменились: обновляем доступность кнопок."""
        self._update_action_buttons()

    def _update_disk_info(self, *_args):
        """Свободное место на томе, где лежат данные проекта."""
        directory = self.projects_dir.text().strip() or default_projects_dir()
        free = disk_space.free_bytes(directory)
        if free is None:
            self.disk_info.setText("")
            self.disk_info.setVisible(False)
            return
        self.disk_info.setText(
            i18n.trf("Свободно на диске: {}", disk_space.human_size(free))
        )
        self.disk_info.setVisible(True)
        # Мало места — предупреждаем заранее, до долгой подготовки.
        if free < disk_space.GIB:
            self.disk_info.setStyleSheet("color: #b00020;")
        elif free < 5 * disk_space.GIB:
            self.disk_info.setStyleSheet("color: #b35c00;")
        else:
            self.disk_info.setStyleSheet("")

    def _project_file_path(self):
        """Путь к файлу проекта по указанным каталогу и имени.

        None, если имя недопустимо или проект ещё не создан.
        """
        directory = self.projects_dir.text().strip() or default_projects_dir()
        return project_file_path(directory, self.project_name.text())

    def _browse_source(self):
        if self.source_mode.currentData() != "pbf":
            return
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("OSM экстракт"), "",
            i18n.tr(
                "OSM files (*.pbf *.osm *.osm.bz2 *.osm.gz)"
                ";;All files (*)"
            ),
        )
        if path:
            self.source_value.setText(path)

    def _open_geofabrik(self):
        provider = self._file_provider()
        url = (
            "https://download.geofabrik.de/"
            if provider == "geofabrik"
            else extracts.provider(provider).homepage
        )
        QDesktopServices.openUrl(QUrl(url))

    def _browse_dem(self):
        path = QFileDialog.getExistingDirectory(
            self, i18n.tr("Каталог DEM"), self.dem_dir.text()
        )
        if path:
            self.dem_dir.setText(path)

    def _bbox_from_picker(self):
        """Открывает окно выбора охвата. True, если охват выбран."""
        dialog = ExtentDialog(self.bbox.text().strip(), self)
        translate_tree(dialog)
        if dialog.exec():
            self.bbox.setText(dialog.bbox_csv())
            return True
        return False

    def _apply_example(self, mode):
        example = MODE_EXAMPLES.get(mode)
        if example and not self.source_value.text().strip():
            self.source_value.setText(example)
            self.source_value.setCursorPosition(0)

    def _source_changed(self, _index):
        mode = self.source_mode.currentData()
        if self._active_mode != mode:
            if self._active_mode not in FILE_MODES:
                current = self.source_value.text()
                if current or self._active_mode not in self._mode_values:
                    self._mode_values[self._active_mode] = current
            self._active_mode = mode
            if mode in FILE_MODES or mode == "slice":
                self.source_value.clear()
            elif mode != "overpass":
                self.source_value.setText(self._mode_values.get(mode, ""))

        labels = {
            "geofabrik": "",
            **{key: "" for key, _title in EXTRACT_PROVIDERS},
            "slice": "",
            "overpass": "Сервер:",
            "pbf": "Файл PBF:",
            "local": "Команда:",
            "ssh": "SSH-команда:",
        }
        if self.source_label is not None:
            self.source_label.setText(
                i18n.tr(labels.get(mode, "Адрес/файл:"))
            )

        is_file = mode in FILE_MODES
        is_extracts = mode in EXTRACT_MODES
        self.source_value.setVisible(not (is_file or mode == "slice"))
        self.source_label.setVisible(not (is_file or mode == "slice"))
        self.download_pbf_button.setEnabled(mode == "pbf")
        self.download_pbf_button.setVisible(not (is_file or mode == "slice"))
        self.browse_source.setEnabled(mode == "pbf")
        self.browse_source.setVisible(not (is_file or mode == "slice"))
        self.browse_source.setToolTip(
            i18n.tr("Выбрать файл .pbf или .osm") if mode == "pbf" else ""
        )
        self.map_index_group.setVisible(is_file)
        if not is_file:
            self._map_index_timer.stop()
            self._map_index_plan = None
            self.map_index_status.clear()
            self.map_index_list.clear()
            self.map_index_summary.clear()
            self._pbf_info_state = None
            self._reset_map_index_stamp()
            if self.map_index_worker is not None and self.map_index_worker.isRunning():
                self.map_index_worker.cancel()

        is_overpass = mode == "overpass"
        if mode != "pbf":
            self._pbf_probe_timer.stop()
            if self.pbf_probe is not None and self.pbf_probe.isRunning():
                self.pbf_probe.cancel()
        if mode not in ("pbf",) + FILE_MODES:
            self.pbf_info.setVisible(False)
        self.overpass_server.parentWidget().setVisible(is_overpass)
        self.recheck_servers.setVisible(is_overpass)
        if self.server_label is not None:
            self.server_label.setVisible(is_overpass)
        if is_overpass:
            remembered = self._mode_values.get("overpass", "")
            self._server_changed(self.overpass_server.currentIndex())
            if not self.overpass_server.currentData() and remembered:
                self.source_value.setText(remembered)
            self.source_hint.setText(
                i18n.tr(
                    "Адрес выбранного Overpass-сервера. Проверка серверов "
                    "выполняется в фоне. Для подготовки данных используются "
                    "только ответившие серверы. Для больших областей "
                    "используйте PBF."
                )
            )
            if self.isVisible():
                self._check_servers()
        elif mode == "pbf":
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText(
                i18n.tr("Путь к .pbf или .osm")
            )
            self.source_hint.setText(
                i18n.tr(
                    "Файл .pbf или .osm. Загружаются только объекты внутри "
                    "охвата."
                )
            )
            self.pbf_info.setVisible(True)
            self._schedule_pbf_probe()
        elif is_file:
            self.source_value.setEnabled(False)
            self.source_value.setPlaceholderText(
                i18n.tr("Определяется по охвату автоматически")
            )
            self.source_value.clear()
            if is_extracts:
                self.source_hint.setText(
                    i18n.tr(
                        "Файловые выгрузки с другого хоста: список карт "
                        "определяется по охвату, отмеченные карты "
                        "скачиваются автоматически при нажатии "
                        "«Подготовить данные»."
                    )
                )
            else:
                self.source_hint.setText(
                    i18n.tr(
                        "Список карт определяется по охвату из онлайн-индекса "
                        "Geofabrik. Отмеченные карты скачиваются автоматически "
                        "при нажатии «Подготовить данные». Снимите галочку с "
                        "лишней карты, чтобы не качать её целиком."
                    )
                )
            self.pbf_info.setVisible(True)
            self.map_index_status.setText(
                i18n.tr("Определение карт по охвату…")
            )
            self._schedule_map_index()
        elif mode == "slice":
            self.source_value.setEnabled(False)
            self.source_value.setPlaceholderText(
                i18n.tr("Вырез по охвату")
            )
            self.source_value.clear()
            self.source_hint.setText(
                i18n.tr(
                    "Сервис SliceOSM собирает файл PBF по указанной рамке: "
                    "данные свежее минуты, но вырез готовится на стороне "
                    "сервиса и занимает от нескольких секунд до минут."
                )
            )
            self.pbf_info.setVisible(False)
        elif mode == "local":
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText(
                "/usr/bin/osm3s_query --db-dir=..."
            )
            self._apply_example(mode)
            self.source_hint.setText(
                i18n.tr(
                    "Команда osm3s_query и параметры. Если osm3s_query нет "
                    "в PATH, укажите полный путь к файлу. --db-dir — каталог "
                    "базы Overpass. Запрос передаётся через stdin."
                )
            )
        else:
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText('ssh user@server "..."')
            self._apply_example(mode)
            self.source_hint.setText(
                i18n.tr(
                    "Команда ssh для удалённого Overpass. Укажите полный путь "
                    "к osm3s_query на сервере, если его нет в PATH. Запрос "
                    "передаётся через stdin."
                )
            )
        # Высота подсказки зависит от переносов: после смены текста
        # заставляем QFormLayout пересчитать строку, иначе последняя
        # строка остаётся обрезанной до следующего изменения размера.
        self._fit_source_hint()

    def _fit_source_hint(self):
        """Пересчитывает высоту подсказок после смены текста."""
        for label in (self.source_hint, self.pbf_info):
            label.updateGeometry()
        parent = self.source_hint.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().activate()

    def _source_path_changed(self, _text):
        if self.source_mode.currentData() == "pbf":
            self._schedule_pbf_probe()

    def _bbox_text_changed(self, _text):
        mode = self.source_mode.currentData()
        if mode == "pbf":
            self._schedule_pbf_probe()
        elif mode in FILE_MODES:
            self._pbf_probe_timer.stop()
            if self.pbf_probe is not None and self.pbf_probe.isRunning():
                self.pbf_probe.cancel()
            self.pbf_info.setText("")
            self._schedule_map_index()
        self._schedule_dem_availability()

    def _queries_changed(self, _item):
        mode = self.source_mode.currentData()
        if mode == "pbf":
            self._schedule_pbf_probe()
        elif mode in FILE_MODES:
            self._pbf_probe_timer.start(700)

    def _file_provider(self):
        """Провайдер файловых выгрузок для текущего режима."""
        mode = self.source_mode.currentData()
        return mode if mode in FILE_MODES else ""

    def _provider_title(self, provider):
        if provider == "geofabrik":
            return i18n.tr("Карты Geofabrik")
        item = extracts.provider(provider)
        return i18n.tr("Выгрузки {}").format(item.title)

    def _schedule_pbf_probe(self):
        self._pbf_probe_timer.start(700)

    def _clear_map_index_view(self, status):
        """Сбрасывает список карт и связанную оценку PBF."""
        self._map_index_plan = None
        self._map_index_plan_bbox = ""
        self._map_index_stats_maps = ()
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
        widget = self.map_index_list
        widget.blockSignals(True)
        try:
            widget.clear()
        finally:
            widget.blockSignals(False)
        self.map_index_summary.clear()
        self.source_value.clear()
        self._pbf_info_state = None
        self.pbf_info.clear()
        self.map_index_status.setText(status)
        self.map_index_status.setToolTip(status)

    def _set_map_index_busy(self, busy):
        """Пока индекс обновляется, список и кнопка обновления недоступны."""
        self.map_index_refresh.setEnabled(not busy)
        self.map_index_list.setEnabled(not busy)

    def _schedule_map_index(self):
        self._clear_map_index_view(i18n.tr("Определение карт по охвату…"))
        self._reset_map_index_stamp()
        self._map_index_timer.start(700)

    def _maps_config(self, bbox):
        provider = self._file_provider()
        return Config(
            qgis_projects_dir=self.projects_dir.text().strip() or default_projects_dir(),
            project_name=self.project_name.text().strip() or "automap",
            bbox=bbox.as_csv(),
            array_queries=[],
            source_mode="geofabrik" if provider == "geofabrik" else "extracts",
            extract_provider=provider if provider != "geofabrik" else "geo2day",
        )

    def _start_map_index(self, refreshing=False):
        self._map_index_timer.stop()
        if self.source_mode.currentData() not in FILE_MODES or not self.isVisible():
            return
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:
            self._map_index_plan = None
            self.map_index_status.setText(
                i18n.trf("Определение карт недоступно: {}", exc)
            )
            return
        if self.map_index_worker is not None and self.map_index_worker.isRunning():
            self.map_index_worker.cancel()
            self.map_index_worker.wait(1000)
        config = self._maps_config(bbox)
        provider = self._file_provider()
        worker = MapIndexWorker(config, bbox, self, provider=provider)
        self.map_index_worker = worker
        worker.progress.connect(self._map_index_progress)
        worker.result.connect(lambda plan, w=worker: self._map_index_ready(w, plan))
        worker.failed.connect(lambda message, w=worker: self._map_index_failed(w, message))
        worker.finished.connect(lambda w=worker: self._map_index_done(w))
        if refreshing:
            message = i18n.tr("Обновление индекса…")
        elif provider == "geofabrik":
            message = i18n.tr("Загрузка индекса Geofabrik…")
        else:
            message = i18n.trf("Загрузка индекса {}…", provider)
        self.map_index_status.setText(message)
        self.map_index_status.setToolTip(message)
        self._set_map_index_busy(True)
        worker.start()

    def _map_index_done(self, worker):
        if worker is self.map_index_worker:
            self.map_index_worker = None
            self._set_map_index_busy(False)
        worker.deleteLater()

    def _map_index_progress(self, message):
        # Подробности (сколько мегабайт, сколько каталогов) уходят в
        # подсказку: в заголовке для них слишком мало места.
        self.map_index_status.setToolTip(i18n.tr(message))

    def _map_index_ready(self, worker, plan):
        if worker is not self.map_index_worker:
            return
        if worker.provider != self._file_provider():
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.source_mode.currentData() not in FILE_MODES:
            return
        try:
            current = parse_bbox(self.bbox.text().strip()).as_csv()
        except Exception:
            return
        if current != worker.bbox.as_csv():
            return
        self._map_index_plan = plan
        self._map_index_plan_bbox = worker.bbox.as_csv()
        self._apply_map_index_plan(plan, worker.provider)
        self._start_map_index_stats(plan)

    def _map_index_failed(self, worker, message):
        if worker is not self.map_index_worker:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self._map_index_plan = None
        text = i18n.trf("Не удалось определить карты: {}", message)
        self.map_index_status.setText(i18n.tr("Ошибка загрузки индекса"))
        self.map_index_status.setToolTip(text)

    def _apply_map_index_plan(self, plan, provider="geofabrik"):
        self._fill_map_list(plan)
        selected = self._selected_maps()
        self.source_value.setText(", ".join(item.id for item in selected))
        base = []
        if plan.state.warning:
            base.append(i18n.tr(plan.state.warning))
        self._set_map_index_stamp(plan)
        details = []
        total_line = []
        if not plan.maps:
            if provider == "geofabrik":
                base.append(
                    i18n.tr("Для этого охвата карты Geofabrik не найдены.")
                )
            else:
                base.append(
                    i18n.trf(
                        "Для выбранного охвата выгрузки не найдены ({})",
                        provider,
                    )
                )
        else:
            total = 0
            for item, status in plan.items():
                size = (
                    i18n.trf("{} МБ", f"{status.size / 1048576:.1f}")
                    if status.size
                    else i18n.tr("размер неизвестен")
                )
                state = i18n.tr(
                    "доступна" if status.available else "недоступна"
                )
                local = i18n.tr(" · скачана") if status.local else ""
                details.append(
                    f"• {item.name} ({item.id}) — {size} — {state}{local}"
                )
                if status.size:
                    total += status.size
            if total:
                total_line.append(
                    i18n.trf("Всего: {} МБ", f"{total / 1048576:.1f}")
                )
        self.map_index_status.setText("\n".join(base))
        self.map_index_status.setToolTip(
            "\n".join(base + details + total_line) if details else "\n".join(base)
        )
        self._update_map_summary()

    def _set_map_index_stamp(self, plan):
        """Дату индекса выбранного хоста показываем в заголовке группы."""
        stamp = ""
        state = getattr(plan, "state", None)
        if state is not None:
            if state.last_modified:
                try:
                    from email.utils import parsedate_to_datetime

                    stamp = parsedate_to_datetime(state.last_modified).strftime("%d.%m.%Y")
                except Exception:
                    stamp = state.last_modified
            elif state.fetched_at:
                stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(state.fetched_at))
        self._reset_map_index_stamp(stamp)

    def _reset_map_index_stamp(self, stamp=""):
        title = self._provider_title(self._file_provider())
        if stamp:
            title = i18n.trf("{} · индекс {}", title, stamp)
        self.map_index_title.setText(title)

    def _refresh_map_index(self):
        """Перезагрузить индекс выгрузок из сети."""
        if self.source_mode.currentData() not in FILE_MODES:
            return
        provider = self._file_provider()
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:
            text = i18n.trf("Не удалось обновить индекс: {}", exc)
            self.map_index_status.setText(text)
            self.map_index_status.setToolTip(text)
            return
        # Кнопка обновления — повод попробовать сеть заново: после
        # недавней ошибки предохранитель иначе сразу отклонит запрос.
        net.reset()
        try:
            if provider == "geofabrik":
                geofabrik.invalidate_index(self._maps_config(bbox))
            else:
                extracts.invalidate_cache(self._maps_config(bbox), provider)
        except Exception as exc:
            text = i18n.trf("Не удалось обновить индекс: {}", exc)
            self.map_index_status.setText(text)
            self.map_index_status.setToolTip(text)
            return
        self._clear_map_index_view(i18n.tr("Обновление индекса…"))
        self._reset_map_index_stamp()
        self._start_map_index(refreshing=True)

    def _map_list_item(self, item, status, checked):
        size = (
            i18n.trf("{} МБ", f"{status.size / 1048576:.1f}")
            if status.size
            else i18n.tr("размер неизвестен")
        )
        if status.local:
            state = i18n.tr("скачана")
        elif not status.available:
            state = i18n.tr("недоступна")
        else:
            state = ""
        parts = [item.name]
        parent = self._map_parent_name(item)
        if parent:
            parts.append(parent)
        text = " — ".join(parts + [size])
        if state:
            text += f" · {state}"
        entry = QListWidgetItem(text)
        entry.setToolTip(text)
        if status.local:
            # Скачанные в кэш карты подсвечиваем зелёным — сразу видно,
            # что их можно не скачивать повторно.
            entry.setBackground(QBrush(_DOWNLOADED_MAP_BG))
        entry.setData(_user_role(), item.id)
        entry.setFlags(_item_flags())
        checked_state, unchecked_state = _checked_state()
        entry.setCheckState(checked_state if checked else unchecked_state)
        return entry

    def _map_parent_name(self, item):
        """Название родительского региона карты (например, Россия)."""
        if self._map_index_plan is None:
            return ""
        name = self._map_index_plan.parent_names.get(item.id, "")
        return "" if name == item.name else name

    def _fill_map_list(self, plan):
        widget = self.map_index_list
        widget.blockSignals(True)
        widget.clear()
        try:
            for item, status in zip(plan.maps, plan.statuses):
                widget.addItem(self._map_list_item(item, status, True))
            if plan.extra:
                header = QListWidgetItem(
                    i18n.tr("Другие карты для этой области")
                )
                header.setFlags(_no_flags())
                widget.addItem(header)
                for item, status in zip(plan.extra, plan.extra_statuses):
                    widget.addItem(self._map_list_item(item, status, False))
        finally:
            widget.blockSignals(False)
        self._map_index_stats_maps = ()
        stored = self._stored_map_selection()
        if stored:
            self._restore_map_selection(set(stored))

    def _stored_map_selection(self):
        """Сохранённый список карт для текущего провайдера."""
        provider = self._file_provider() or "geofabrik"
        value = self.settings.value(f"maps_{provider}", [])
        if isinstance(value, str):
            value = [part for part in value.split(",") if part]
        return [str(item) for item in value or []]

    def _store_map_selection(self):
        provider = self._file_provider() or "geofabrik"
        if not self.map_index_list.count():
            return
        self.settings.setValue(f"maps_{provider}", self._selected_map_ids())

    def _selected_map_ids(self):
        checked_state = _checked_state()[0]
        selected = []
        for row in range(self.map_index_list.count()):
            entry = self.map_index_list.item(row)
            identifier = entry.data(_user_role())
            if identifier and entry.checkState() == checked_state:
                selected.append(str(identifier))
        return selected

    def _selected_maps(self):
        if self._map_index_plan is None:
            return []
        chosen = set(self._selected_map_ids())
        return [
            item
            for item in self._map_index_plan.maps + self._map_index_plan.extra
            if item.id in chosen
        ]

    def _selected_map_statuses(self):
        if self._map_index_plan is None:
            return []
        chosen = set(self._selected_map_ids())
        return [
            status
            for status in (
                self._map_index_plan.statuses + self._map_index_plan.extra_statuses
            )
            if status.map.id in chosen
        ]

    def _update_map_summary(self):
        statuses = self._selected_map_statuses()
        total = sum(status.size or 0 for status in statuses)
        text = i18n.trf("Выбрано карт: {}", len(statuses))
        if total:
            text += i18n.trf(", объём {} МБ", f"{total / 1048576:.1f}")
        self.map_index_summary.setText(text)

    def _map_selection_changed(self, _item):
        if self._map_index_plan is None:
            return
        selected = self._selected_maps()
        self.source_value.setText(", ".join(item.id for item in selected))
        self._update_map_summary()
        self._store_map_selection()
        pending, total = self._pending_map_bytes(self._map_index_plan)
        self._show_pbf_estimate(pending, total)
        self._start_map_index_stats(self._map_index_plan)

    def _start_map_index_stats(self, plan):
        maps = self._selected_maps()
        if not maps:
            self._pbf_info_state = None
            self.pbf_info.setText("")
            self._map_index_stats_maps = ()
            return
        current = tuple(item.id for item in maps)
        if current == getattr(self, "_map_index_stats_maps", ()):
            return
        self._map_index_stats_maps = current
        try:
            bbox = parse_bbox(self.bbox.text().strip())
            config = self._maps_config(bbox)
            paths = geofabrik.source_paths(config, maps)
        except Exception:
            pending, total = self._pending_map_bytes(plan)
            self._show_pbf_estimate(pending, total)
            return
        names = self._selected_queries()
        texts = {name: read_query(name) for name in names}
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(2000)
        self._pbf_info_state = None
        self.pbf_info.setText(i18n.tr("Оценка объединённой карты…"))
        self.pbf_probe = PbfProbeWorker(
            config, bbox, names, texts, paths=paths, parent=self
        )
        self.pbf_probe.result.connect(self._pbf_probed)
        self.pbf_probe.failed.connect(self._pbf_probe_failed)
        self.pbf_probe.progress.connect(self._pbf_probe_progress)
        self.pbf_probe.start()

    def _start_pbf_probe(self):
        self._pbf_probe_timer.stop()
        mode = self.source_mode.currentData()
        if mode not in ("pbf",) + FILE_MODES:
            return
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:
            self.pbf_info.setText(
                i18n.trf("Оценка PBF недоступна: {}", exc)
            )
            return
        if mode in FILE_MODES:
            plan = self._map_index_plan
            if plan is None:
                self._schedule_map_index()
                return
            maps = self._selected_maps()
            if not maps:
                self.pbf_info.setText("")
                return
            config = self._maps_config(bbox)
            try:
                paths = geofabrik.source_paths(config, maps)
            except Exception:
                pending, total = self._pending_map_bytes(plan)
                self._show_pbf_estimate(pending, total)
                return
            source_config = config
        else:
            text = self.source_value.text().strip()
            if not text:
                self.pbf_info.setText(
                    i18n.tr(
                        "Выберите файл, чтобы оценить количество объектов "
                        "и время подготовки."
                    )
                )
                return
            path = Path(text).expanduser()
            if not path.exists():
                self.pbf_info.setText(
                    i18n.trf("Файл не найден: {}", path)
                )
                return
            paths = [path]
            source_config = Config(
                qgis_projects_dir=self.projects_dir.text().strip() or default_projects_dir(),
                project_name=self.project_name.text().strip() or "automap",
                bbox=bbox.as_csv(),
                array_queries=[],
                source_mode="pbf",
                pbf_path=str(path),
            )
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(2000)
        names = self._selected_queries()
        texts = {name: read_query(name) for name in names}
        self._pbf_info_state = None
        self.pbf_info.setText(i18n.tr("Быстрая оценка PBF…"))
        self.pbf_probe = PbfProbeWorker(
            source_config, bbox, names, texts, paths=paths, parent=self
        )
        self.pbf_probe.result.connect(self._pbf_probed)
        self.pbf_probe.failed.connect(self._pbf_probe_failed)
        self.pbf_probe.progress.connect(self._pbf_probe_progress)
        self.pbf_probe.start()

    def _pbf_probe_progress(self, message):
        self._pbf_info_state = None
        self.pbf_info.setText(i18n.tr(message))

    def _pbf_probed(self, total, in_bbox, matched, elapsed, approximate,
                    sampled, total_blobs, file_count, extended_ratio=0.0,
                    bytes_per_second=0.0):
        if approximate:
            prefix = "≈ "
        else:
            prefix = ""
        estimate = self._estimate_seconds(
            total, matched, extended_ratio, file_count, bytes_per_second
        )
        if self.source_mode.currentData() in FILE_MODES:
            plan = self._map_index_plan
            pending, _size = self._pending_map_bytes(plan) if plan else (0, 0)
            self._pbf_info_state = (
                "geo", total, in_bbox, estimate, pending, prefix
            )
        else:
            path = Path(self.source_value.text().strip()).expanduser()
            try:
                size = path.stat().st_size / 1048576
            except OSError:
                size = 0.0
            self._pbf_info_state = (
                "pbf", total, in_bbox, elapsed, estimate, size,
                file_count, prefix,
            )
        self._render_pbf_info()

    def _estimate_seconds(self, total, matched, extended_ratio, file_count=1,
                          bytes_per_second=0.0):
        """Оценка времени подготовки в секундах.

        Читать приходится не весь файл, а только область с запасом
        фильтра: долю этой области в выгрузке даёт быстрая оценка.
        Дальше добавляется время обработки слоёв.  Скорость чтения берётся
        из замера PBF-пробы, скорость процессора — из короткого бенчмарка,
        а параллелизм — из фактического ограничения по памяти.  Рельеф
        считается отдельно: он идёт параллельно слоям, но тоже занимает
        время и зависит от площади охвата.
        """
        layers = len(self.queries) if hasattr(self, "queries") else 0
        cpu = performance.cpu_score(self.settings)
        return performance.estimate_preparation_seconds(
            total,
            matched,
            extended_ratio,
            layers,
            bytes_per_second=bytes_per_second,
            cpu=cpu,
            terrain_seconds=self._terrain_estimate_seconds(cpu=cpu),
        )

    def _terrain_estimate_config(self, bbox):
        """Настройки рельефа из панели — для проверки кэша и оценки."""
        manual = self.dem_source.currentData() == dem_sources.MANUAL_SOURCE
        return Config(
            qgis_projects_dir=(
                self.projects_dir.text().strip() or default_projects_dir()
            ),
            project_name=self.project_name.text().strip() or "automap",
            bbox=bbox.as_csv(),
            array_queries=[],
            generate_terrain=self.generate_terrain.isChecked(),
            terrain_source=(
                self.dem_source.currentData() or dem_sources.DEFAULT_SOURCE
            ),
            download_terrain_tiles=not manual,
            get_terrain_tiles=manual,
            terrain_src_dir=(
                self.dem_dir.text().strip() if manual else ""
            ),
            generate_terrain_hillshade_slope=(
                self.hillshade_slope.isChecked()
            ),
            terrain_resample_method=(
                self.resample_method.currentData() or "cubicspline"
            ),
            generate_terrain_isolines=self.isolines.isChecked(),
            isolines_step=self._current_isolines_step(),
            smooth_isolines=self.smooth_isolines.isChecked(),
        )

    def _terrain_estimate_seconds(self, cpu=None):
        """Сколько примерно займёт растровый рельеф для текущего охвата."""
        if not self.generate_terrain.isChecked():
            return 0.0
        try:
            bbox = parse_bbox(self.bbox.text().strip())
            config = self._terrain_estimate_config(bbox)
            width, height = measure.frame_sizes_km(bbox)
            cached = terrain_cache.dem_cached(config, bbox)
        except Exception:
            return 0.0
        return performance.estimate_terrain_seconds(
            width * height,
            source=self._terrain_estimate_source(config),
            cached=cached,
            hillshade=config.generate_terrain_hillshade_slope,
            cpu=cpu,
        )

    def _terrain_estimate_source(self, config):
        """Набор DEM, который фактически определит скорость рельефа."""
        source = config.resolved_terrain_source()
        if source != dem_sources.AUTO_SOURCE:
            return source
        try:
            keys = dem_sources.auto_source_keys(
                self._dem_availability, self._dem_sonny_tiles
            )
        except Exception:
            return source
        # Если доступен 3DEP, автоматический режим берёт его на
        # покрытых тайлах: это самые тяжёлые растры.
        return "3dep" if "3dep" in keys else source

    def _show_pbf_estimate(self, pending, total):
        """Оценка по объёму карт (до точного подсчёта объектов)."""
        self._pbf_info_state = ("estimate", pending, total)
        self._render_pbf_info()

    def _render_pbf_info(self):
        """Собирает подпись оценки PBF на текущем языке."""
        state = getattr(self, "_pbf_info_state", None)
        if state is None:
            return
        kind = state[0]
        if kind == "estimate":
            _, pending, total = state
            self.pbf_info.setText(self._map_download_estimate(pending, total))
            return
        if kind == "geo":
            _, total, in_bbox, estimate, pending, prefix = state
            counts = i18n.trf(
                "объектов: {} · в охвате: {}",
                f"{prefix}{_spaced(total)}",
                f"{prefix}{_spaced(in_bbox)}",
            )
            parts = []
            if pending:
                parts.append(
                    i18n.trf(
                        "Скачивание ≈ {}",
                        _duration(pending / DOWNLOAD_RATE),
                    )
                )
            parts.append(
                i18n.trf("подготовка ≈ {}", _estimate_duration(estimate))
            )
            text = ", ".join(parts)
            self.pbf_info.setText(
                f"{text[0].upper()}{text[1:]} · {counts}."
            )
            return
        _, total, in_bbox, elapsed, estimate, size, file_count, prefix = state
        counts = i18n.trf(
            "объектов: {} · в охвате: {}",
            f"{prefix}{_spaced(total)}",
            f"{prefix}{_spaced(in_bbox)}",
        )
        title = (
            i18n.trf("PBF ({} файлов)", file_count)
            if file_count > 1
            else i18n.trf("PBF {} МБ", f"{size:.1f}")
        )
        self.pbf_info.setText(
            i18n.trf(
                "{} · {}. Проверка ≈ {} с, подготовка ≈ {} с.",
                title,
                counts,
                f"{elapsed:.1f}",
                _estimate_warning(estimate, f"{estimate:.0f}"),
            )
        )

    def _pending_map_bytes(self, plan=None):
        try:
            bbox = parse_bbox(self.bbox.text().strip())
            config = self._maps_config(bbox)
        except Exception:
            return 0, 0
        pending = total = 0
        for status in self._selected_map_statuses():
            size = status.size or 0
            total += size
            if not geofabrik.local_ready(config, status.map):
                pending += size
        return pending, total

    def _map_download_estimate(self, pending, total):
        parts = []
        if pending:
            parts.append(
                i18n.trf(
                    "Скачивание ≈ {}",
                    _duration(pending / DOWNLOAD_RATE),
                )
            )
        if total:
            # Грубая оценка до скачивания PBF: около 0,45 с на мегабайт
            # на эталонной машине, с поправкой на процессор и параллелизм.
            cpu = performance.cpu_score(self.settings)
            workers = performance.layer_worker_count(
                len(self.queries) if hasattr(self, "queries") else 0
            )
            parts.append(
                i18n.trf(
                    "подготовка ≈ {}",
                    _estimate_duration(
                        performance.estimate_map_seconds(
                            total / 1048576,
                            cpu=cpu,
                            workers=workers,
                            terrain_seconds=self._terrain_estimate_seconds(
                                cpu=cpu
                            ),
                        )
                    ),
                )
            )
        if not parts:
            return i18n.tr("Оценка времени появится после проверки карт.")
        text = ", ".join(parts)
        return i18n.trf(
            "{}. Точная статистика появится после загрузки карт.",
            f"{text[0].upper()}{text[1:]}",
        )

    def _pbf_probe_failed(self, message):
        self._pbf_info_state = None
        self.pbf_info.setText(
            i18n.trf("Не удалось оценить PBF: {}", message)
        )

    def _server_changed(self, _index):
        if self.source_mode.currentData() != "overpass":
            return
        url = self.overpass_server.currentData()
        if url:
            self.source_value.setText(url)
            self.source_value.setEnabled(False)
            self.source_value.setPlaceholderText(
                i18n.tr("URL выбранного сервера")
            )
            self.source_hint.setText(
                i18n.tr(
                    "Проверка серверов выполняется в фоне. При подготовке "
                    "данных плагин использует только доступные серверы."
                )
            )
        else:
            self.source_value.setEnabled(True)
            self.source_value.clear()
            self.source_value.setPlaceholderText(
                "https://example.org/api/interpreter"
            )
            self.source_hint.setText(
                i18n.tr("Укажите URL своего Overpass API.")
            )

    def _check_servers(self, force=False):
        if not force and self._servers_checked:
            return
        if any(worker.isRunning() for worker in self.server_workers):
            return
        self._servers_checked = True
        self.recheck_servers.setEnabled(False)
        self.server_workers = []
        for server in DEFAULT_OVERPASS_SERVERS:
            self._set_server_status(server.url, i18n.tr("проверка…"))
        for server in DEFAULT_OVERPASS_SERVERS:
            worker = ServerCheckWorker(server.url, self)
            worker.checked.connect(self._server_checked)
            worker.finished.connect(lambda w=worker: self._server_worker_done(w))
            self.server_workers.append(worker)
        for worker in self.server_workers:
            worker.start()

    def _server_worker_done(self, worker):
        if worker in self.server_workers:
            self.server_workers.remove(worker)
        worker.deleteLater()
        if not self.server_workers:
            self.recheck_servers.setEnabled(True)

    def _set_server_status(self, url, status):
        index = self.overpass_server.findData(url)
        if index < 0:
            return
        name = next(
            (server.name for server in DEFAULT_OVERPASS_SERVERS if server.url == url), url
        )
        self.overpass_server.setItemText(index, f"{name} — {status}")

    def _server_checked(self, url, ok, detail):
        index = self.overpass_server.findData(url)
        if index < 0:
            return
        if ok:
            self._set_server_status(
                url, i18n.trf("доступен ({} с)", detail)
            )
            tooltip = i18n.trf("Тестовый запрос выполнен за {} с", detail)
        else:
            self._set_server_status(url, i18n.tr("недоступен"))
            tooltip = i18n.tr(detail)
        self.overpass_server.setItemData(index, tooltip, _tooltip_role())
        if not ok and self.overpass_server.currentIndex() == index:
            self.source_hint.setText(
                i18n.tr(
                    "Выбранный сервер не отвечает. При подготовке данных "
                    "плагин попробует остальные доступные серверы."
                )
            )

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_language_button()
        if not self._narrowed:
            self._narrowed = True
            # После показа панели известна её естественная ширина: по
            # умолчанию панель занимает минимальную ширину.
            QTimer.singleShot(0, self._collapse_to_minimum)
        QTimer.singleShot(0, self._fit_source_hint)
        mode = self.source_mode.currentData()
        if mode == "overpass":
            self._check_servers()
        elif mode in FILE_MODES:
            self._schedule_map_index()

    def _terrain_changed(self, enabled):
        manual = self.dem_source.currentData() == dem_sources.MANUAL_SOURCE
        for widget in (
            self.dem_source, self.hillshade_slope, self.isolines,
            self.smooth_isolines, self.dem_info, self.dem_warning,
        ):
            widget.setEnabled(enabled)
        for label in (
            self.dem_source_label, self.resample_label,
            self.isolines_steps_label,
        ):
            if label is not None:
                label.setEnabled(enabled)
        # Кнопка обновления недоступна и без рельефа, и пока идёт
        # проверка доступности.
        self.dem_refresh.setEnabled(enabled and not self._dem_worker_running())
        self.dem_dir.setEnabled(enabled and manual)
        self.browse_dem.setEnabled(enabled and manual)
        self.dem_dir_row.setVisible(enabled and manual)
        if self.dem_dir_label is not None:
            self.dem_dir_label.setVisible(enabled and manual)
        self.resample_method.setEnabled(
            enabled and self.hillshade_slope.isChecked()
        )
        steps_enabled = enabled and self.isolines.isChecked()
        for box in self.isolines_steps.values():
            box.setEnabled(steps_enabled)
        self._update_dem_info()
        self._update_dem_warning()

    def _dem_worker_running(self):
        worker = self.dem_availability_worker
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # Объект уже удалён QThread — проверки нет.
            return False

    def _dem_source_changed(self, _index):
        # Предупреждение о замене тайлов относилось к прежнему источнику.
        self._terrain_fallback = ""
        self._terrain_changed(self.generate_terrain.isChecked())
        self._update_dem_warning()

    def _set_dem_source(self, key):
        """Выбирает источник без пометки «пользователь выбрал вручную»."""
        index = self.dem_source.findData(key)
        if index < 0:
            return False
        self._dem_source_syncing = True
        try:
            self.dem_source.setCurrentIndex(index)
        finally:
            self._dem_source_syncing = False
        return True

    def _dem_dir_changed(self, _text):
        self._update_dem_info()

    def _dem_manual_availability(self):
        value = self.dem_dir.text().strip()
        if not value:
            message = "каталог не выбран"
            detail = "Свой каталог: каталог не выбран"
        else:
            path = Path(value).expanduser()
            if not path.exists():
                message = "каталог не найден"
                detail = f"Каталог DEM не найден: {path}"
            elif not path.is_dir():
                message = "путь не каталог"
                detail = f"Указанный путь не является каталогом: {path}"
            else:
                message = "каталог выбран"
                detail = f"Каталог: {path}"
        return dem_sources.Availability(
            available=True, checked=True, message=message, detail=detail,
        )

    def _update_dem_info(self):
        key = self.dem_source.currentData()
        if not key:
            self.dem_info.clear()
            self.dem_info.setToolTip("")
            return
        if key == dem_sources.MANUAL_SOURCE:
            availability = self._dem_manual_availability()
        else:
            availability = dem_sources.display_availability(
                key, self._dem_bbox, self._dem_availability
            )
            if availability is None:
                availability = dem_sources.Availability(
                    available=True, checked=False,
                    message=self._dem_check_message or "не проверено",
                    detail=self._dem_check_detail,
                )
        text, tooltip = dem_sources.describe(key, availability)
        self.dem_info.setText(text)
        self.dem_info.setToolTip(tooltip)

    def _update_dem_warning(self):
        key = self.dem_source.currentData()
        warning = ""
        if key and self._dem_bbox is not None and self._dem_availability:
            warning = dem_sources.source_warning(
                key, self._dem_bbox, self._dem_availability,
                self._dem_sonny_tiles,
            )
        if self._terrain_fallback:
            warning = f"{warning} {self._terrain_fallback}".strip()
        self.dem_warning.setText(warning)
        # Без обработки рельефа предупреждение о системах высот не нужно:
        # блок затенён целиком.
        self.dem_warning.setVisible(
            bool(warning) and self.generate_terrain.isChecked()
        )
        self.dem_warning.setToolTip(warning)

    def _schedule_dem_availability(self):
        self._dem_availability_generation += 1
        self._dem_availability = {}
        self._dem_bbox = None
        # Сообщение о прошлой обработке относится к прежнему охвату.
        self._terrain_fallback = ""
        if not self.bbox.text().strip():
            self._dem_check_timer.stop()
            self._dem_check_message = "не проверено"
            self._dem_check_detail = (
                "Охват не выбран — доступность не проверена"
            )
            self._update_dem_info()
            self._update_dem_warning()
            return
        self._dem_check_message = "проверка…"
        self._dem_check_detail = "Проверка доступности…"
        self._update_dem_info()
        self._update_dem_warning()
        self._dem_check_timer.start(600)

    def _refresh_dem_availability(self):
        """Ручная повторная проверка доступности источников."""
        self._dem_check_timer.stop()
        self._dem_availability_generation += 1
        self._dem_availability = {}
        self._dem_bbox = None
        self._dem_check_message = "проверка…"
        self._dem_check_detail = "Проверка доступности…"
        if not sonny.catalogue_tiles(sonny.catalogue_dir()):
            # Список Sonny лежит в сети: если его ещё нет, пробуем
            # загрузить — после обновления проверка повторится сама.
            self.refresh_sonny_catalogue(force=True)
        self._update_dem_info()
        self._update_dem_warning()
        self._start_dem_availability(force=True)

    def _start_dem_availability(self, force=False):
        self._dem_check_timer.stop()
        if self.iface is None:
            self._dem_bbox = None
            self._dem_check_message = "не проверено"
            self._dem_check_detail = ""
            self._update_dem_info()
            self._update_dem_warning()
            return
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:  # noqa: BLE001 - в поле может быть что угодно
            self._dem_availability = {}
            self._dem_bbox = None
            self._dem_check_message = "не проверено"
            self._dem_check_detail = f"Охват не задан: {exc}"
            self._update_dem_info()
            self._update_dem_warning()
            return
        self._dem_bbox = bbox
        if self.dem_availability_worker is not None:
            try:
                running = self.dem_availability_worker.isRunning()
            except RuntimeError:
                running = False
                self.dem_availability_worker = None
            if running:
                self.dem_availability_worker.cancel()
        generation = self._dem_availability_generation
        worker = DemAvailabilityWorker(bbox, generation, force, self)
        worker.result.connect(self._dem_availability_ready)
        worker.failed.connect(self._dem_availability_failed)
        worker.finished.connect(lambda: self._dem_worker_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.dem_availability_worker = worker
        self.dem_refresh.setEnabled(False)
        worker.start()

    def _dem_worker_finished(self, worker):
        if self.dem_availability_worker is worker:
            self.dem_availability_worker = None
            # При выключенной обработке рельефа кнопка остаётся
            # недоступной вместе с остальным блоком.
            self.dem_refresh.setEnabled(
                self.generate_terrain.isChecked()
            )

    def _dem_availability_ready(self, generation, availability, sonny_cells):
        if generation != self._dem_availability_generation:
            return
        self._dem_availability = availability
        self._dem_sonny_tiles = set(sonny_cells)
        self._dem_check_message = ""
        self._dem_check_detail = ""
        self._apply_dem_availability(availability)
        self._update_dem_info()
        self._update_dem_warning()

    def _dem_availability_failed(self, generation, message):
        if generation != self._dem_availability_generation:
            return
        self._dem_availability = {}
        self._dem_check_message = "не удалось проверить"
        self._dem_check_detail = (
            f"Не удалось проверить доступность: {message}. "
            "Повторите проверку кнопкой обновления."
        )
        self._update_dem_info()
        self._update_dem_warning()

    def _apply_dem_availability(self, availability):
        current = self.dem_source.currentData()
        for index in range(self.dem_source.count()):
            key = self.dem_source.itemData(index)
            state = availability.get(key)
            if state is None:
                enabled = True
                tooltip = ""
            else:
                enabled = state.available or not state.checked
                tooltip = state.detail or state.message
            model = self.dem_source.model()
            item = model.item(index) if hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(enabled)
            self.dem_source.setItemData(index, tooltip, _tooltip_role())
        if current:
            state = availability.get(current)
            if state is not None and state.checked and not state.available:
                self._set_dem_source(self._fallback_dem_source(availability))

    def _fallback_dem_source(self, availability):
        for key in (
            dem_sources.AUTO_SOURCE, "cop30", "srtm", "nasadem", "alos",
            "3dep", dem_sources.SONNY_SOURCE, dem_sources.MANUAL_SOURCE,
        ):
            state = availability.get(key)
            if key == dem_sources.MANUAL_SOURCE:
                return key
            if state is None or state.available or not state.checked:
                return key
        return dem_sources.MANUAL_SOURCE

    def _current_isolines_step(self):
        checked = [
            value for value, box in self.isolines_steps.items()
            if box.isChecked()
        ]
        return min(checked) if checked else 10

    def _isolines_step_toggled(self, value, checked):
        if checked:
            # Отметка включает кратность и все кратные ей (как раньше).
            self._apply_isolines_step(value)
            return
        # Снятие убирает только эту галочку: набор кратностей можно
        # уточнять по одной отметке. Последнюю снять нельзя.
        others = any(
            box.isChecked()
            for other, box in self.isolines_steps.items()
            if other != value
        )
        if others:
            return
        box = self.isolines_steps[value]
        box.blockSignals(True)
        box.setChecked(True)
        box.blockSignals(False)

    def _apply_isolines_step(self, step):
        step = max(1, int(step))
        for value, box in self.isolines_steps.items():
            enabled = value % step == 0
            box.blockSignals(True)
            box.setChecked(enabled)
            box.blockSignals(False)

    def _set_isolines_step(self, step):
        values = sorted(self.isolines_steps)
        if step not in values:
            step = min(values, key=lambda value: (abs(value - step), value))
        self._apply_isolines_step(step)

    def _restore_settings(self):
        self.projects_dir.setText(
            self.settings.value("projects_dir", default_projects_dir())
        )
        self._update_disk_info()
        self.project_name.setText(self.settings.value("project_name", "automap"))
        self.bbox.setText(self.settings.value("bbox", ""))
        saved_value = self.settings.value("source_value", "")
        for mode in ("overpass", "pbf", "local", "ssh"):
            stored = self.settings.value(f"source_value_{mode}", "")
            if stored:
                self._mode_values[mode] = str(stored)
        saved_server = self.settings.value("server_url", "")
        server_index = self.overpass_server.findData(saved_server) if saved_server else -1
        if server_index < 0 and saved_value:
            server_index = self.overpass_server.findData(saved_value)
        self.overpass_server.setCurrentIndex(max(0, server_index))
        saved_mode = str(
            self.settings.value("source_mode", "geofabrik") or "geofabrik"
        )
        if saved_mode == "extracts":
            # Старые настройки хранили хост отдельно от режима.
            saved_mode = str(
                self.settings.value("extract_provider", "geo2day") or "geo2day"
            )
        self.source_mode.setCurrentIndex(
            max(0, self.source_mode.findData(saved_mode))
        )
        self._source_changed(0)
        mode = self.source_mode.currentData()
        if mode in FILE_MODES or mode == "slice":
            self.source_value.clear()
            self.source_value.setEnabled(False)
        elif mode == "overpass" and self.overpass_server.currentData():
            self.source_value.setText(self.overpass_server.currentData())
        else:
            value = saved_value
            if mode != "overpass" and value.strip().lower().startswith(
                ("http://", "https://")
            ):
                value = ""
            if MODE_EXAMPLES.get(mode) and not value.strip():
                value = MODE_EXAMPLES[mode]
            self.source_value.setText(value)
            self.source_value.setEnabled(True)
        self._mode_values[mode] = self.source_value.text()
        self.dem_dir.setText(self.settings.value("dem_dir", ""))
        saved_source = str(self.settings.value("terrain_source", "") or "")
        if not saved_source:
            saved_source = (
                dem_sources.MANUAL_SOURCE
                if self.dem_dir.text().strip()
                else dem_sources.DEFAULT_SOURCE
            )
        if (saved_source == dem_sources.MANUAL_SOURCE
                and not self.dem_dir.text().strip()):
            saved_source = dem_sources.DEFAULT_SOURCE
        source_index = self.dem_source.findData(saved_source)
        # Загрузка сохранённого источника не считается ручным выбором:
        # иначе автовыбор 3DEP не сработает для сохранённого охвата.
        self._dem_source_syncing = True
        try:
            self.dem_source.setCurrentIndex(max(0, source_index))
        finally:
            self._dem_source_syncing = False
        saved_steps = str(self.settings.value("isolines_steps", "") or "")
        checked = []
        for item in saved_steps.split(","):
            item = item.strip()
            if item.isdigit():
                checked.append(int(item))
        if checked:
            known = set(self.isolines_steps)
            checked = [value for value in checked if value in known]
            for value, box in self.isolines_steps.items():
                box.blockSignals(True)
                box.setChecked(value in checked)
                box.blockSignals(False)
        else:
            try:
                step = int(self.settings.value("isolines_step", 10))
            except (TypeError, ValueError):
                step = 10
            self._set_isolines_step(step)
        method = str(self.settings.value("terrain_resample_method", "cubicspline"))
        self.resample_method.setCurrentIndex(
            max(0, self.resample_method.findData(method))
        )
        self.generate_terrain.setChecked(
            self.settings.value("generate_terrain", "true") == "true"
        )
        self._terrain_changed(self.generate_terrain.isChecked())
        hidden = self.settings.value("hidden_panels", [])
        if isinstance(hidden, str):
            hidden = [hidden] if hidden else []
        self._hidden_panels = [str(name) for name in hidden or []]

    def _save_settings(self):
        self.settings.setValue("projects_dir", self.projects_dir.text())
        self.settings.setValue("project_name", self.project_name.text())
        self.settings.setValue("bbox", self.bbox.text())
        mode = self.source_mode.currentData()
        self.settings.setValue("source_mode", mode)
        self.settings.setValue("server_url", self.overpass_server.currentData())
        value = (
            "" if (mode in FILE_MODES or mode == "slice") else self.source_value.text()
        )
        self.settings.setValue("source_value", value)
        values = dict(self._mode_values)
        values[mode] = value
        for name, value in values.items():
            self.settings.setValue(f"source_value_{name}", value)
        self.settings.setValue("dem_dir", self.dem_dir.text())
        self.settings.setValue(
            "terrain_source",
            self.dem_source.currentData() or dem_sources.DEFAULT_SOURCE,
        )
        self.settings.setValue("generate_terrain", str(self.generate_terrain.isChecked()).lower())
        self.settings.setValue("isolines_step", str(self._current_isolines_step()))
        checked_steps = ",".join(
            str(value)
            for value, box in sorted(self.isolines_steps.items())
            if box.isChecked()
        )
        self.settings.setValue("isolines_steps", checked_steps)
        self.settings.setValue(
            "terrain_resample_method",
            self.resample_method.currentData() or "cubicspline",
        )
        if self._hidden_panels:
            self.settings.setValue("hidden_panels", list(self._hidden_panels))
        else:
            self.settings.remove("hidden_panels")

    def _connect_project_signals(self):
        project = QgsProject.instance()
        for signal_name, slot in (
            ("aboutToBeCleared", self._stop_canvas_rendering),
            ("cleared", self._project_closed),
            ("readProject", self._project_read),
        ):
            signal = getattr(project, signal_name, None)
            if signal is None:
                continue
            try:
                signal.connect(slot)
            except Exception:
                pass
        if self.iface is None:
            return
        for signal_name in ("projectRead", "newProjectCreated"):
            signal = getattr(self.iface, signal_name, None)
            if signal is None:
                continue
            try:
                signal.connect(self._schedule_project_refresh)
            except Exception:
                pass

    def _project_read(self, document):
        self._own_marker = False
        self.log_toggle.setChecked(False)
        try:
            properties = document.documentElement().firstChildElement(
                "properties"
            )
            if not properties.isNull():
                self._own_marker = not properties.firstChildElement(
                    "qgis_xtopo"
                ).isNull()
        except Exception:
            pass
        self._schedule_project_refresh()

    def _project_closed(self):
        self._own_marker = False
        self.project_panel.reset_atlas_state()
        # Проект закрыт — возвращаем стандартные панели QGIS.
        self.restore_panels()
        self._schedule_project_refresh()

    def _schedule_project_refresh(self, *_args):
        self._project_timer.start()

    def _apply_panel_visibility(self):
        """Прячет стандартные панели QGIS при открытии проекта."""
        return self._hide_panels()

    def _hide_panels(self):
        """Прячет стандартные панели QGIS и запоминает их имена."""
        hidden = hide_docks(self.iface, keep=(PLUGIN_DOCK_NAME,))
        if hidden:
            self._hidden_panels = sorted(set(self._hidden_panels) | set(hidden))
            self._save_settings()
        return hidden

    def restore_panels(self):
        """Возвращает панели QGIS, скрытые плагином."""
        names = list(self._hidden_panels)
        restored = show_docks(self.iface, names) if names else []
        if names:
            self._hidden_panels = []
            self._save_settings()
        return restored

    def _show_panels(self):
        # Кнопка переключает видимость: скрытые панели возвращает,
        # видимые скрывает. Автоматическое скрытие при открытии проекта
        # при этом не отключается: оно всегда включено.
        if self._hidden_panels:
            self.restore_panels()
        else:
            self._hide_panels()

    def _refresh_project_state(self):
        was_own = self._own_project_open
        own = self._own_marker or ProjectPanel.is_xtopo_project()
        self._own_project_open = own
        for index in range(self.tabs.count()):
            is_project = index == self.project_tab_index
            self.tabs.setTabEnabled(index, is_project if own else not is_project)
        self.project_panel.refresh()
        if own:
            # Панели скрываем при каждом открытии проекта, а не только при
            # первом: кнопка переключает видимость только сейчас,
            # а следующее открытие снова оставит на экране панель плагина.
            self._apply_panel_visibility()
            if not was_own:
                self.log_toggle.setChecked(False)
                set_canvas_magnification(self.iface)
                self._patch_transliteration()
                apply_map_language(iface=self.iface)
                # Скрытие панелей меняет размер холста, поэтому охват
                # вписываем после пересчёта размера интерфейса.
                QTimer.singleShot(0, lambda: fit_project_extent(self.iface))
            self.tabs.setCurrentWidget(self.project_panel)
            self.project_panel.focus_first()
        else:
            self.tabs.setCurrentIndex(0)
        self._update_action_buttons()

    def _patch_transliteration(self):
        """Добавляет в подписи проекта запасной вариант с транслитерацией.

        Если функция `readable_name` в QGIS не зарегистрирована, ветка из
        подписей убирается: иначе QGIS не нарисовал бы ни одной подписи.
        """
        project = QgsProject.instance()
        try:
            was_dirty = project.isDirty()
        except Exception:
            was_dirty = False
        available = functions_available()
        patch_project_labeling(project, available)
        try:
            project.setDirty(was_dirty)
        except Exception:
            pass
        if not available:
            self._append_log(
                "WARNING: Функция readable_name не зарегистрирована — "
                "подписи показаны без транслитерации. Перезапустите QGIS."
            )

    def _switch_language(self):
        i18n.cycle_language()

    def _open_proxy_dialog(self):
        dialog = ProxyDialog(self)
        if dialog.exec_() != ProxyDialog.Accepted:
            return
        config = dialog.config()
        proxy.save(config)
        proxy.apply(config)
        # Прошлый результат проверки относился к прежней конфигурации.
        self._proxy_generation += 1
        self._proxy_known = False
        self._proxy_ok = False
        self._update_proxy_tooltip()
        self._check_proxy()

    def _check_proxy(self):
        """Запускает фоновую проверку доступности прокси."""
        config = proxy.load()
        if not config.enabled:
            self._proxy_known = True
            self._proxy_ok = False
            self._update_proxy_tooltip()
            return
        if self.proxy_check is not None and self.proxy_check.isRunning():
            # Проверка уже идёт; когда закончит — запустим заново с
            # актуальной конфигурацией.
            self._proxy_recheck = True
            return
        worker = ProxyCheckWorker(config, self._proxy_generation, self)
        worker.checked.connect(self._proxy_checked)
        worker.finished.connect(lambda w=worker: self._proxy_check_done(w))
        worker.finished.connect(worker.deleteLater)
        self.proxy_check = worker
        worker.start()

    def _proxy_checked(self, generation, ok):
        if generation != self._proxy_generation:
            return
        self._proxy_ok = bool(ok)
        self._proxy_known = True
        self._update_proxy_tooltip()

    def _proxy_check_done(self, worker):
        if worker is self.proxy_check:
            self.proxy_check = None
        if self._proxy_recheck:
            self._proxy_recheck = False
            self._check_proxy()

    def _update_proxy_tooltip(self):
        config = proxy.load()
        if config.enabled:
            address = f"{config.ptype.upper()} {config.address()}"
            if not self._proxy_known:
                state = i18n.tr("проверка…")
                icon_key = "off"
            elif self._proxy_ok:
                state = i18n.tr("доступен")
                icon_key = "ok"
            else:
                state = i18n.tr("недоступен")
                icon_key = "bad"
            text = i18n.trf("Прокси: {} — {}", address, state)
        else:
            text = i18n.tr("Прокси не настроен")
            icon_key = "off"
        self.proxy_button.setToolTip(text)
        if icon_key != self._proxy_icon_key:
            self._proxy_icon_key = icon_key
            self.proxy_button.setIcon(self._proxy_icons[icon_key])

    def _language_changed(self, _code=None):
        self.retranslate()

    def retranslate(self):
        """Переводит интерфейс на текущий язык без пересборки панели."""
        translate_tree(self)
        self.language_button.setText(i18n.language().upper())
        self.language_button.setToolTip(i18n.tr("Язык интерфейса"))
        self._update_proxy_tooltip()
        self._fit_language_button()
        self._retranslate_dynamic()
        self.project_panel.retranslate()

    def _retranslate_dynamic(self):
        """Пересобирает динамические подписи на текущем языке."""
        self._render_pbf_info()
        self._update_dem_info()
        self._update_dem_warning()
        # Размеры пересобираются заново: единицы измерения переводятся.
        self._update_disk_info()
        plan = self._map_index_plan
        if plan is None:
            return
        chosen = set(self._selected_map_ids())
        self._apply_map_index_plan(plan, self._file_provider())
        self._restore_map_selection(chosen)

    def _restore_map_selection(self, chosen):
        checked, unchecked = _checked_state()
        widget = self.map_index_list
        widget.blockSignals(True)
        try:
            for row in range(widget.count()):
                entry = widget.item(row)
                identifier = entry.data(_user_role())
                if not identifier:
                    continue
                entry.setCheckState(
                    checked if identifier in chosen else unchecked
                )
        finally:
            widget.blockSignals(False)
        self._update_map_summary()

    def _update_action_buttons(self):
        can_create = not self._running and not self._own_project_open
        self.prepare_button.setEnabled(can_create)
        # Открыть можно только существующий проект: путь берётся из полей
        # «Каталог проектов» и «Имя проекта».
        path = self._project_file_path()
        self.open_button.setEnabled(
            can_create and path is not None and path.exists()
        )
        self.close_button.setEnabled(self._own_project_open and not self._running)
        self.choose_extent.setEnabled(can_create)
        _set_primary(self.prepare_button, can_create)
        _set_primary(self.choose_extent, can_create)

    def _build_config(self):
        mode = self.source_mode.currentData()
        provider = self._file_provider()
        value = self.source_value.text().strip()
        project_name = self.project_name.text().strip()
        if not project_name:
            raise ValueError("Укажите имя проекта")
        name_error = project_name_error(project_name)
        if name_error:
            raise ValueError(name_error)
        if mode in ("local", "ssh"):
            if not value:
                raise ValueError("Укажите команду Overpass")
            if "/path/to/overpass_db" in value:
                raise ValueError(
                    "Замените /path/to/overpass_db на реальный путь к базе Overpass"
                )
            if mode == "ssh" and "user@server" in value:
                raise ValueError("Замените user@server на адрес вашего сервера")
        if mode in FILE_MODES and self._map_index_plan is not None:
            title = self._provider_title(self._file_provider())
            if not self._map_index_plan.maps:
                raise ValueError(f"Для этого охвата карты не найдены ({title})")
            if not self._selected_map_ids():
                raise ValueError(f"Выберите хотя бы одну карту ({title})")
        endpoints = [server.url for server in DEFAULT_OVERPASS_SERVERS]
        endpoint = endpoints[0]
        if mode == "overpass":
            if not self.overpass_server.currentData() and not value:
                raise ValueError("Укажите URL сервера Overpass")
            endpoint = (self.overpass_server.currentData() or value).strip()
            if endpoint in endpoints:
                endpoints.remove(endpoint)
            endpoints.insert(0, endpoint)
        source_key = self.dem_source.currentData() or dem_sources.DEFAULT_SOURCE
        manual = source_key == dem_sources.MANUAL_SOURCE
        dem_dir = self.dem_dir.text().strip() if manual else ""
        if manual and dem_dir:
            dem_path = Path(dem_dir).expanduser()
            if not dem_path.exists():
                raise ValueError(f"Каталог DEM не найден: {dem_path}")
            if not dem_path.is_dir():
                raise ValueError(
                    f"Указанный путь не является каталогом: {dem_path}"
                )
        config = Config(
            qgis_projects_dir=self.projects_dir.text().strip(),
            project_name=project_name,
            bbox=self.bbox.text().strip(),
            array_queries=self._selected_queries(),
            source_mode=(
                "extracts" if provider and provider != "geofabrik" else mode
            ),
            pbf_path=value if mode == "pbf" else "",
            geofabrik_maps=(
                self._selected_map_ids() if mode in FILE_MODES else []
            ),
            extract_provider=(
                provider if provider and provider != "geofabrik" else "geo2day"
            ),
            overpass_endpoint=endpoint,
            overpass_endpoints=endpoints,
            overpass_endpoint_external=endpoint,
            overpass_endpoint_local=value if mode == "local" else "",
            overpass_endpoint_ssh=value if mode == "ssh" else "",
            generate_terrain=self.generate_terrain.isChecked(),
            terrain_source=source_key,
            download_terrain_tiles=not manual,
            get_terrain_tiles=manual,
            terrain_src_dir=dem_dir,
            generate_terrain_hillshade_slope=self.hillshade_slope.isChecked(),
            terrain_resample_method=(
                self.resample_method.currentData() or "cubicspline"
            ),
            generate_terrain_isolines=self.isolines.isChecked(),
            isolines_step=self._current_isolines_step(),
            smooth_isolines=self.smooth_isolines.isChecked(),
        )
        parse_bbox(config.bbox)
        config.validate()
        return config

    def _prepare(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.bbox.text().strip():
            # Охват не задан: сначала предлагаем выбрать его на карте.
            if not self._bbox_from_picker():
                self._set_status("Охват не выбран")
                return
        try:
            config = self._build_config()
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", i18n.tr(str(exc)))
            return
        self._pbf_probe_timer.stop()
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(5000)
        self._map_index_timer.stop()
        if self.map_index_worker is not None and self.map_index_worker.isRunning():
            self.map_index_worker.cancel()
            self.map_index_worker.wait(1000)
        self._save_settings()
        # Новая обработка — прошлое сообщение о замене тайлов неактуально.
        self._terrain_fallback = ""
        self._update_dem_warning()
        self.log.clear()
        if self.log_window_text is not None:
            self.log_window_text.clear()
        self.log_toggle.setChecked(True)
        self._set_status("Подготовка...")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._set_running(True)
        self._append_log("Старт подготовки")
        self.worker = PipelineWorker(config, self)
        self.worker.progress.connect(self._append_log)
        self.worker.percent.connect(self._pipeline_percent)
        self.worker.status.connect(self._set_status)
        self.worker.completed.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        # Встроенный finished приходит после возврата из run(); только
        # по нему можно безопасно удалять QThread.
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def _set_running(self, running):
        self._running = running
        self.cancel_button.setEnabled(running)
        self.tabs.setEnabled(not running)
        self.progress.setVisible(running)
        self._update_action_buttons()

    def _set_status(self, message):
        message = i18n.tr(message)
        self.status.setToolTip(message)
        self.status.setText(_short_status(message))

    def _open_log_window(self):
        if self.log_window is None:
            self.log_window = QDialog(self)
            self.log_window.setWindowTitle("Журнал QGIS-xtopo")
            self.log_window.resize(850, 500)
            window_layout = QVBoxLayout(self.log_window)
            self.log_window_text = QPlainTextEdit(self.log_window)
            self.log_window_text.setReadOnly(True)
            self.log_window_text.setMaximumBlockCount(2000)
            self.log_window_text.setPlainText(self.log.toPlainText())
            window_layout.addWidget(self.log_window_text)
            translate_tree(self.log_window)
        self.log_window.show()
        self._scroll_log_to_end(self.log_window_text)
        self.log_window.raise_()
        self.log_window.activateWindow()

    @staticmethod
    def _scroll_log_to_end(view):
        """Показывает последнюю строку, не меняя масштаб текста."""
        if view is None:
            return
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_log(self, message):
        if message.lstrip().casefold().startswith(("результаты", "results")):
            return
        message = i18n.tr(message)
        timestamp = QTime.currentTime().toString("HH:mm:ss")
        line = f"[{timestamp}] {message}"
        self.log.appendPlainText(line)
        if self.log_window_text is not None:
            self.log_window_text.appendPlainText(line)
        # Прокрутка к последней строке: ensureCursorVisible() при заполненном
        # лимите блоков оставляет новую строку за нижним краем.
        self._scroll_log_to_end(self.log)
        self._scroll_log_to_end(self.log_window_text)
        self._set_status(message)

    def _pipeline_percent(self, value):
        self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def _finished(self, path):
        worker = self.worker
        self.worker = None
        fallback = (
            getattr(worker.config, "terrain_fallback", "") if worker else ""
        )
        if fallback:
            self._terrain_fallback = fallback
            self._append_log(fallback)
        self._update_dem_warning()
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._set_running(False)
        self._append_log(f"Готово: {path}")
        # После подготовки на диске осталось меньше места.
        self._update_disk_info()
        # В строке статуса путь не помещается: показываем короткий текст,
        # полный остаётся в журнале и в подсказке.
        self._set_status("Готово")
        try:
            open_project(path, self.iface, repair=False)
        except Exception as exc:
            QMessageBox.warning(
                self, "QGIS-xtopo",
                i18n.trf("Проект создан, но не открылся: {}", exc),
            )
        if self.source_mode.currentData() in FILE_MODES:
            self._schedule_map_index()

    def _failed(self, message):
        self.worker = None
        self._set_running(False)
        self._set_status("Ошибка")
        self.log_toggle.setChecked(True)
        self._append_log(f"Ошибка: {message}")
        QMessageBox.critical(self, "QGIS-xtopo", i18n.tr(message))

    def _cancelled(self):
        self.worker = None
        self._set_running(False)
        self._set_status("Отмена")
        self._append_log("Подготовка отменена")

    def _cancel(self):
        cancelled = False
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            cancelled = True
        if self.map_index_worker is not None and self.map_index_worker.isRunning():
            self.map_index_worker.cancel()
            cancelled = True
        if cancelled:
            self._set_status("Отмена...")

    def _open_project(self):
        try:
            config = self._build_config()
            path = project_file(config)
            if not path.exists():
                QMessageBox.warning(
                    self, "QGIS-xtopo",
                    i18n.trf("Проект не найден: {}", path),
                )
                return
            open_project(path, self.iface)
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", i18n.tr(str(exc)))

    def _stop_canvas_rendering(self, *_args):
        """Останавливает фоновую отрисовку карты перед очисткой проекта.

        QgsMapCanvas рисует карту отдельным потоком.  Если в это время
        вызвать QgsProject.clear(), поток обращается к выражениям проекта,
        пока главный поток удаляет слои, и QGIS падает.  Ждём завершения
        прерванной отрисовки до очистки проекта.
        """
        if self.iface is None:
            return
        canvas = getattr(self.iface, "mapCanvas", None)
        if canvas is None:
            return
        if callable(canvas):
            try:
                canvas = canvas()
            except Exception:
                return
        for name in ("stopRendering", "waitWhileRendering"):
            method = getattr(canvas, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass

    def _close_project(self):
        if not self._own_project_open:
            return
        self._stop_canvas_rendering()
        if self.iface is not None and hasattr(self.iface, "newProject"):
            self.iface.newProject(True)
        else:
            QgsProject.instance().clear()
        self._schedule_project_refresh()

    def closeEvent(self, event):
        # Панель может быть просто скрыта и открыта снова, поэтому
        # останавливаем потоки без постоянного запрета на их запуск.
        self.shutdown_workers(final=False)
        self._save_settings()
        super().closeEvent(event)
