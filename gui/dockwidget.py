import threading
import time
from dataclasses import replace
from pathlib import Path

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings, QThread, QTime, QTimer, QUrl, pyqtSignal, Qt
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from ..core import geofabrik
from ..core.bbox import parse_bbox
from ..core.config import (
    Config, default_projects_dir, default_queries, project_name_error,
)
from ..core.overpass_client import OverpassClient
from ..core.overpass_servers import DEFAULT_OVERPASS_SERVERS
from ..core.pbf_fast import UnsupportedPbf, probe_fast
from ..core.pbf_source import probe_dataset
from ..core.pipeline import run_project
from ..core.project import open_project, project_file, set_canvas_magnification
from ..core.resources import query_names, read_query
from .extentdialog import ExtentDialog
from .projectpanel import ProjectPanel

MODE_EXAMPLES = {
    "local": "/usr/bin/osm3s_query --db-dir=/path/to/overpass_db",
    "ssh": 'ssh user@server "/usr/bin/osm3s_query --db-dir=/path/to/overpass_db"',
}

# Measured aggregate throughput of four parallel Geofabrik downloads.
DOWNLOAD_RATE = 1_000_000


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
        return "меньше 1 с"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


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


class PipelineWorker(QThread):
    progress = pyqtSignal(str)
    percent = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(str)
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
            self.finished.emit(str(path))
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
    result = pyqtSignal(int, int, int, float, bool, int, int, int)
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
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.result.emit(
                total, in_bbox, matched, elapsed,
                approximate, sampled, total_blobs, len(self.paths),
            )

    def cancel(self):
        self.cancel_event.set()


class GeofabrikPlanWorker(QThread):
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, config, bbox, parent=None):
        super().__init__(parent)
        self.config = config
        self.bbox = bbox
        self.cancel_event = threading.Event()

    def run(self):
        try:
            plan = geofabrik.analyse(
                self.config, self.bbox,
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


class XtopoDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("QGIS-xtopo", parent)
        self.iface = iface
        self.worker = None
        self.server_workers = []
        self.pbf_probe = None
        self.geofabrik_worker = None
        self._geofabrik_plan = None
        self._geofabrik_plan_bbox = ""
        self._geofabrik_stats_maps = ()
        self._geofabrik_timer = QTimer(self)
        self._geofabrik_timer.setSingleShot(True)
        self._geofabrik_timer.timeout.connect(self._start_geofabrik)
        self._pbf_probe_timer = QTimer(self)
        self._pbf_probe_timer.setSingleShot(True)
        self._pbf_probe_timer.timeout.connect(self._start_pbf_probe)
        self._servers_checked = False
        self._mode_values = {}
        self._active_mode = "geofabrik"
        self._own_project_open = False
        self._own_marker = False
        self._running = False
        self._narrowed = False
        self._project_timer = QTimer(self)
        self._project_timer.setSingleShot(True)
        self._project_timer.setInterval(0)
        self._project_timer.timeout.connect(self._refresh_project_state)
        self.settings = QSettings("qgis-xtopo", "qgis-xtopo")
        self.setObjectName("QgisXtopoDockWidget")
        self._build_ui()
        self._restore_settings()
        self._connect_project_signals()
        self._refresh_project_state()

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

    def _build_ui(self):
        container = QWidget(self)
        self.container = container
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

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
        self.project_name = QLineEdit("automap")
        self.project_name.setPlaceholderText("буквы, цифры, _ - +")
        self.project_name.textChanged.connect(self._project_name_changed)
        project_form.addRow("Имя проекта:", self.project_name)
        self.project_name_hint = QLabel("")
        self.project_name_hint.setWordWrap(True)
        self.project_name_hint.setStyleSheet("color: #b00020;")
        self.project_name_hint.setVisible(False)
        project_form.addRow(self.project_name_hint)

        self.bbox = QLineEdit()
        self.bbox.setPlaceholderText("lon_min,lat_min,lon_max,lat_max или ссылка на OpenStreetMap")
        self.bbox.textChanged.connect(self._bbox_text_changed)
        choose_extent = _primary_button(QPushButton("Выбрать…"))
        choose_extent.setToolTip(
            "Открыть карту OpenStreetMap и выбрать охват без создания проекта"
        )
        choose_extent.clicked.connect(self._bbox_from_picker)
        row = QHBoxLayout()
        row.addWidget(self.bbox)
        row.addWidget(choose_extent)
        project_form.addRow("Охват:", row)
        self.area_label = project_form.labelForField(row)
        data_layout.addWidget(project_group)

        source_group = QGroupBox("Источник OSM")
        source_form = QFormLayout(source_group)
        self.source_mode = QComboBox()
        self.source_mode.addItem("Карты с Geofabrik PBF", "geofabrik")
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
        self.source_hint = QLabel("")
        self.source_hint.setWordWrap(True)
        source_form.addRow(self.source_hint)
        self.geofabrik_info = QLabel("")
        self.geofabrik_info.setWordWrap(True)
        self.geofabrik_info.setVisible(False)
        source_form.addRow(self.geofabrik_info)
        self.geofabrik_group = QGroupBox("Карты Geofabrik")
        maps_layout = QVBoxLayout(self.geofabrik_group)
        maps_layout.setContentsMargins(4, 4, 4, 4)
        self.geofabrik_maps = QListWidget()
        self.geofabrik_maps.setMaximumHeight(150)
        self.geofabrik_maps.itemChanged.connect(self._geofabrik_selection_changed)
        maps_layout.addWidget(self.geofabrik_maps)
        self.geofabrik_summary = QLabel("")
        self.geofabrik_summary.setWordWrap(True)
        maps_layout.addWidget(self.geofabrik_summary)
        self.geofabrik_group.setVisible(False)
        source_form.addRow(self.geofabrik_group)
        self.pbf_info = QLabel("")
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
        self.generate_terrain.toggled.connect(self._terrain_changed)
        terrain_form.addRow(self.generate_terrain)
        self.download_terrain = QCheckBox("Скачать SRTM 30 м автоматически")
        terrain_form.addRow(self.download_terrain)
        self.dem_dir = QLineEdit()
        self.dem_dir.textChanged.connect(self._dem_dir_changed)
        browse_dem = QPushButton("…")
        browse_dem.setMaximumWidth(32)
        browse_dem.clicked.connect(self._browse_dem)
        row = QHBoxLayout()
        row.addWidget(self.dem_dir)
        row.addWidget(browse_dem)
        terrain_form.addRow("Каталог DEM:", row)
        self.dem_hint = QLabel(
            "Если каталог указан, используется рельеф из него — "
            "скачивание SRTM отключается."
        )
        self.dem_hint.setWordWrap(True)
        terrain_form.addRow(self.dem_hint)
        self.hillshade_slope = QCheckBox("Затенение рельефа и уклоны")
        self.hillshade_slope.setChecked(True)
        self.hillshade_slope.toggled.connect(
            lambda _checked: self._terrain_changed(
                self.generate_terrain.isChecked()
            )
        )
        terrain_form.addRow(self.hillshade_slope)
        self.resample_method = QComboBox()
        self.resample_method.addItem(
            "cubicspline — мягче, меньше деталей", "cubicspline"
        )
        self.resample_method.addItem(
            "lanczos — резче, возможны артефакты", "lanczos"
        )
        self.resample_method.setToolTip(
            "Terrain resample method: влияет на детальность затенения "
            "рельефа и уклонов"
        )
        terrain_form.addRow("Метод масштабирования:", self.resample_method)
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
                "Минимальная кратность контуров. Остальные отметки "
                "включаются автоматически."
            )
            box.toggled.connect(
                lambda checked, step=value: self._isolines_step_toggled(
                    step, checked
                )
            )
            steps_layout.addWidget(box, index // 3, index % 3)
            self.isolines_steps[value] = box
        terrain_form.addRow("Кратность, м:", self.isolines_steps_widget)
        self.smooth_isolines = QCheckBox("Сглаживать контуры")
        terrain_form.addRow(self.smooth_isolines)
        terrain_layout.addWidget(terrain_group)
        terrain_layout.addStretch(1)
        self._set_isolines_step(10)
        self._terrain_changed(False)

        self.tabs.addTab(data_tab, "Данные")
        self.tabs.addTab(layers_tab, "Слои")
        self.tabs.addTab(terrain_tab, "Рельеф")
        self.project_panel = ProjectPanel(self.iface, self)
        self.project_panel.statusMessage.connect(self._append_log)
        self.project_tab_index = self.tabs.addTab(self.project_panel, "Проект")
        self.tabs.setTabEnabled(self.project_tab_index, False)
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.prepare_button = _primary_button(QPushButton("Подготовить данные"))
        self.prepare_button.clicked.connect(self._prepare)
        self.open_button = QPushButton("Открыть проект")
        self.open_button.clicked.connect(self._open_project)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.prepare_button)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.status = QLabel("Готово")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.status.setFixedHeight(
            self.status.fontMetrics().lineSpacing() * 2 + 6
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(70)
        self.log.setMaximumHeight(140)
        self.log.setVisible(False)
        self.log_toggle = QCheckBox("Журнал")
        self.log_toggle.toggled.connect(self.log.setVisible)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.log_toggle)
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
            item.setCheckState(checked if item.text() in defaults else unchecked)

    def _selected_queries(self):
        checked, _ = _checked_state()
        return [
            self.queries.item(index).text()
            for index in range(self.queries.count())
            if self.queries.item(index).checkState() == checked
        ]

    def _browse_projects(self):
        path = QFileDialog.getExistingDirectory(self, "Каталог проектов", self.projects_dir.text())
        if path:
            self.projects_dir.setText(path)

    def _project_name_changed(self, text):
        error = project_name_error(text)
        self.project_name_hint.setText(error)
        self.project_name_hint.setVisible(bool(error))

    def _browse_source(self):
        if self.source_mode.currentData() != "pbf":
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "OSM экстракт", "",
            "OSM files (*.pbf *.osm *.osm.bz2 *.osm.gz);;All files (*)",
        )
        if path:
            self.source_value.setText(path)

    def _open_geofabrik(self):
        QDesktopServices.openUrl(QUrl("https://download.geofabrik.de/"))

    def _browse_dem(self):
        path = QFileDialog.getExistingDirectory(self, "Каталог DEM", self.dem_dir.text())
        if path:
            self.dem_dir.setText(path)

    def _bbox_from_picker(self):
        """Открывает окно выбора охвата. True, если охват выбран."""
        dialog = ExtentDialog(self.bbox.text().strip(), self)
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
            if self._active_mode != "geofabrik":
                current = self.source_value.text()
                if current or self._active_mode not in self._mode_values:
                    self._mode_values[self._active_mode] = current
            self._active_mode = mode
            if mode == "geofabrik":
                self.source_value.clear()
            elif mode != "overpass":
                self.source_value.setText(self._mode_values.get(mode, ""))

        labels = {
            "geofabrik": "Карты:",
            "overpass": "Сервер:",
            "pbf": "Файл PBF:",
            "local": "Команда:",
            "ssh": "SSH-команда:",
        }
        if self.source_label is not None:
            self.source_label.setText(labels.get(mode, "Адрес/файл:"))

        is_geofabrik = mode == "geofabrik"
        self.download_pbf_button.setEnabled(mode == "pbf")
        self.download_pbf_button.setVisible(not is_geofabrik)
        self.browse_source.setEnabled(mode == "pbf")
        self.browse_source.setVisible(not is_geofabrik)
        self.browse_source.setToolTip(
            "Выбрать файл .pbf или .osm" if mode == "pbf" else ""
        )
        self.geofabrik_info.setVisible(is_geofabrik)
        self.geofabrik_group.setVisible(is_geofabrik)
        if not is_geofabrik:
            self._geofabrik_timer.stop()
            self._geofabrik_plan = None
            self.geofabrik_info.clear()
            self.geofabrik_maps.clear()
            self.geofabrik_summary.clear()
            self._reset_geofabrik_index_stamp()
            if self.geofabrik_worker is not None and self.geofabrik_worker.isRunning():
                self.geofabrik_worker.cancel()

        is_overpass = mode == "overpass"
        if mode != "pbf":
            self._pbf_probe_timer.stop()
            if self.pbf_probe is not None and self.pbf_probe.isRunning():
                self.pbf_probe.cancel()
        if mode not in ("pbf", "geofabrik"):
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
                "Адрес выбранного Overpass-сервера. Проверка серверов выполняется "
                "в фоне. Для подготовки данных используются только ответившие "
                "серверы. Для больших областей используйте PBF."
            )
            if self.isVisible():
                self._check_servers()
        elif mode == "pbf":
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText("Путь к .pbf или .osm")
            self.source_hint.setText(
                "Файл .pbf или .osm. Загружаются только объекты внутри bbox."
            )
            self.pbf_info.setVisible(True)
            self._schedule_pbf_probe()
        elif is_geofabrik:
            self.source_value.setEnabled(False)
            self.source_value.setPlaceholderText("Определяется по bbox автоматически")
            self.source_value.clear()
            self.source_hint.setText(
                "Список карт определяется по bbox из онлайн-индекса Geofabrik. "
                "Отмеченные карты скачиваются автоматически при нажатии "
                "«Подготовить данные». Снимите галочку с лишней карты, чтобы "
                "не качать её целиком."
            )
            self.pbf_info.setVisible(True)
            self.geofabrik_info.setText("Определение карт по bbox…")
            self._schedule_geofabrik()
        elif mode == "local":
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText("/usr/bin/osm3s_query --db-dir=...")
            self._apply_example(mode)
            self.source_hint.setText(
                "Команда osm3s_query и параметры. Если osm3s_query нет в PATH, "
                "укажите полный путь к файлу. --db-dir — каталог базы Overpass. "
                "Запрос передаётся через stdin."
            )
        else:
            self.source_value.setEnabled(True)
            self.source_value.setPlaceholderText('ssh user@server "..."')
            self._apply_example(mode)
            self.source_hint.setText(
                "Команда ssh для удалённого Overpass. Укажите полный путь к "
                "osm3s_query на сервере, если его нет в PATH. Запрос передаётся "
                "через stdin."
            )

    def _source_path_changed(self, _text):
        if self.source_mode.currentData() == "pbf":
            self._schedule_pbf_probe()

    def _bbox_text_changed(self, _text):
        mode = self.source_mode.currentData()
        if mode == "pbf":
            self._schedule_pbf_probe()
        elif mode == "geofabrik":
            self._pbf_probe_timer.stop()
            if self.pbf_probe is not None and self.pbf_probe.isRunning():
                self.pbf_probe.cancel()
            self.pbf_info.setText("")
            self._schedule_geofabrik()

    def _queries_changed(self, _item):
        mode = self.source_mode.currentData()
        if mode == "pbf":
            self._schedule_pbf_probe()
        elif mode == "geofabrik":
            self._pbf_probe_timer.start(700)

    def _schedule_pbf_probe(self):
        self._pbf_probe_timer.start(700)

    def _schedule_geofabrik(self):
        self._geofabrik_plan = None
        self._geofabrik_plan_bbox = ""
        self._geofabrik_stats_maps = ()
        self.geofabrik_maps.blockSignals(True)
        self.geofabrik_maps.clear()
        self.geofabrik_maps.blockSignals(False)
        self.geofabrik_summary.clear()
        self._reset_geofabrik_index_stamp()
        if self.source_mode.currentData() == "geofabrik":
            self.source_value.clear()
            self.geofabrik_info.setText("Определение карт по bbox…")
        self._geofabrik_timer.start(700)

    def _geofabrik_config(self, bbox):
        return Config(
            qgis_projects_dir=self.projects_dir.text().strip() or default_projects_dir(),
            project_name=self.project_name.text().strip() or "automap",
            bbox=bbox.as_csv(),
            array_queries=[],
            source_mode="geofabrik",
        )

    def _start_geofabrik(self):
        self._geofabrik_timer.stop()
        if self.source_mode.currentData() != "geofabrik" or not self.isVisible():
            return
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:
            self._geofabrik_plan = None
            self.geofabrik_info.setText(f"Определение карт недоступно: {exc}")
            return
        if self.geofabrik_worker is not None and self.geofabrik_worker.isRunning():
            self.geofabrik_worker.cancel()
            self.geofabrik_worker.wait(1000)
        config = self._geofabrik_config(bbox)
        worker = GeofabrikPlanWorker(config, bbox, self)
        self.geofabrik_worker = worker
        worker.progress.connect(self._geofabrik_progress)
        worker.result.connect(lambda plan, w=worker: self._geofabrik_ready(w, plan))
        worker.failed.connect(lambda message, w=worker: self._geofabrik_failed(w, message))
        worker.finished.connect(lambda w=worker: self._geofabrik_done(w))
        self.geofabrik_info.setText("Загрузка индекса Geofabrik…")
        worker.start()

    def _geofabrik_done(self, worker):
        if worker is self.geofabrik_worker:
            self.geofabrik_worker = None
        worker.deleteLater()

    def _geofabrik_progress(self, message):
        self.geofabrik_info.setText(message)

    def _geofabrik_ready(self, worker, plan):
        if worker is not self.geofabrik_worker:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.source_mode.currentData() != "geofabrik":
            return
        try:
            current = parse_bbox(self.bbox.text().strip()).as_csv()
        except Exception:
            return
        if current != worker.bbox.as_csv():
            return
        self._geofabrik_plan = plan
        self._geofabrik_plan_bbox = worker.bbox.as_csv()
        self._apply_geofabrik_plan(plan)
        self._start_geofabrik_stats(plan)

    def _geofabrik_failed(self, worker, message):
        if worker is not self.geofabrik_worker:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self._geofabrik_plan = None
        self.geofabrik_info.setText(f"Не удалось определить карты: {message}")

    def _apply_geofabrik_plan(self, plan):
        self._fill_geofabrik_list(plan)
        selected = self._geofabrik_selected_maps()
        self.source_value.setText(", ".join(item.id for item in selected))
        base = []
        if plan.state.warning:
            base.append(plan.state.warning)
        self._set_geofabrik_index_stamp(plan)
        details = []
        total_line = []
        if not plan.maps:
            base.append("Для этого bbox карты Geofabrik не найдены.")
        else:
            total = 0
            for item, status in plan.items():
                size = f"{status.size / 1048576:.1f} МБ" if status.size else "размер неизвестен"
                state = "доступна" if status.available else "недоступна"
                local = " · уже скачана" if status.local else ""
                details.append(f"• {item.name} ({item.id}) — {size} — {state}{local}")
                if status.size:
                    total += status.size
            if total:
                total_line.append(f"Всего: {total / 1048576:.1f} МБ")
        self.geofabrik_info.setText("\n".join(base))
        self.geofabrik_info.setVisible(bool(self.geofabrik_info.text()))
        self.geofabrik_info.setToolTip(
            "\n".join(base + details + total_line) if details else "\n".join(base)
        )
        self._update_geofabrik_summary()

    def _set_geofabrik_index_stamp(self, plan):
        """Дату индекса Geofabrik показываем в заголовке группы карт."""
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
        self._reset_geofabrik_index_stamp(stamp)

    def _reset_geofabrik_index_stamp(self, stamp=""):
        title = "Карты Geofabrik"
        if stamp:
            title = f"{title} · индекс {stamp}"
        self.geofabrik_group.setTitle(title)

    def _geofabrik_item(self, item, status, checked):
        size = f"{status.size / 1048576:.1f} МБ" if status.size else "размер неизвестен"
        if status.local:
            state = "уже скачана"
        elif not status.available:
            state = "недоступна"
        else:
            state = ""
        parts = [item.name]
        parent = self._geofabrik_parent_name(item)
        if parent:
            parts.append(parent)
        text = " — ".join(parts + [size])
        if state:
            text += f" · {state}"
        entry = QListWidgetItem(text)
        entry.setData(_user_role(), item.id)
        entry.setFlags(_item_flags())
        checked_state, unchecked_state = _checked_state()
        entry.setCheckState(checked_state if checked else unchecked_state)
        return entry

    def _geofabrik_parent_name(self, item):
        """Название родительского региона карты (например, Россия)."""
        if self._geofabrik_plan is None:
            return ""
        name = self._geofabrik_plan.parent_names.get(item.id, "")
        return "" if name == item.name else name

    def _fill_geofabrik_list(self, plan):
        widget = self.geofabrik_maps
        widget.blockSignals(True)
        widget.clear()
        try:
            for item, status in zip(plan.maps, plan.statuses):
                widget.addItem(self._geofabrik_item(item, status, True))
            if plan.extra:
                header = QListWidgetItem("Другие карты для этой области")
                header.setFlags(_no_flags())
                widget.addItem(header)
                for item, status in zip(plan.extra, plan.extra_statuses):
                    widget.addItem(self._geofabrik_item(item, status, False))
        finally:
            widget.blockSignals(False)
        self._geofabrik_stats_maps = ()

    def _geofabrik_selected_ids(self):
        checked_state = _checked_state()[0]
        selected = []
        for row in range(self.geofabrik_maps.count()):
            entry = self.geofabrik_maps.item(row)
            identifier = entry.data(_user_role())
            if identifier and entry.checkState() == checked_state:
                selected.append(str(identifier))
        return selected

    def _geofabrik_selected_maps(self):
        if self._geofabrik_plan is None:
            return []
        chosen = set(self._geofabrik_selected_ids())
        return [
            item
            for item in self._geofabrik_plan.maps + self._geofabrik_plan.extra
            if item.id in chosen
        ]

    def _geofabrik_selected_statuses(self):
        if self._geofabrik_plan is None:
            return []
        chosen = set(self._geofabrik_selected_ids())
        return [
            status
            for status in (
                self._geofabrik_plan.statuses + self._geofabrik_plan.extra_statuses
            )
            if status.map.id in chosen
        ]

    def _update_geofabrik_summary(self):
        statuses = self._geofabrik_selected_statuses()
        total = sum(status.size or 0 for status in statuses)
        text = f"Выбрано карт: {len(statuses)}"
        if total:
            text += f", объём {total / 1048576:.1f} МБ"
        self.geofabrik_summary.setText(text)

    def _geofabrik_selection_changed(self, _item):
        if self._geofabrik_plan is None:
            return
        selected = self._geofabrik_selected_maps()
        self.source_value.setText(", ".join(item.id for item in selected))
        self._update_geofabrik_summary()
        pending, total = self._geofabrik_pending(self._geofabrik_plan)
        self.pbf_info.setText(self._geofabrik_estimate(pending, total))
        self._start_geofabrik_stats(self._geofabrik_plan)

    def _start_geofabrik_stats(self, plan):
        maps = self._geofabrik_selected_maps()
        if not maps:
            self.pbf_info.setText("")
            self._geofabrik_stats_maps = ()
            return
        current = tuple(item.id for item in maps)
        if current == getattr(self, "_geofabrik_stats_maps", ()):
            return
        self._geofabrik_stats_maps = current
        try:
            bbox = parse_bbox(self.bbox.text().strip())
            config = self._geofabrik_config(bbox)
            paths = geofabrik.source_paths(config, maps)
        except Exception:
            pending, total = self._geofabrik_pending(plan)
            self.pbf_info.setText(self._geofabrik_estimate(pending, total))
            return
        names = self._selected_queries()
        texts = {name: read_query(name) for name in names}
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(2000)
        self.pbf_info.setText("Оценка объединённой карты…")
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
        if mode not in ("pbf", "geofabrik"):
            return
        try:
            bbox = parse_bbox(self.bbox.text().strip())
        except Exception as exc:
            self.pbf_info.setText(f"Оценка PBF недоступна: {exc}")
            return
        if mode == "geofabrik":
            plan = self._geofabrik_plan
            if plan is None:
                self._schedule_geofabrik()
                return
            maps = self._geofabrik_selected_maps()
            if not maps:
                self.pbf_info.setText("")
                return
            config = self._geofabrik_config(bbox)
            try:
                paths = geofabrik.source_paths(config, maps)
            except Exception:
                pending, total = self._geofabrik_pending(plan)
                self.pbf_info.setText(self._geofabrik_estimate(pending, total))
                return
            source_config = config
        else:
            text = self.source_value.text().strip()
            if not text:
                self.pbf_info.setText(
                    "Выберите файл, чтобы оценить количество объектов и время подготовки."
                )
                return
            path = Path(text).expanduser()
            if not path.exists():
                self.pbf_info.setText(f"Файл не найден: {path}")
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
        self.pbf_info.setText("Быстрая оценка PBF…")
        self.pbf_probe = PbfProbeWorker(
            source_config, bbox, names, texts, paths=paths, parent=self
        )
        self.pbf_probe.result.connect(self._pbf_probed)
        self.pbf_probe.failed.connect(self._pbf_probe_failed)
        self.pbf_probe.progress.connect(self._pbf_probe_progress)
        self.pbf_probe.start()

    def _pbf_probe_progress(self, message):
        self.pbf_info.setText(message)

    def _pbf_probed(self, total, in_bbox, matched, elapsed, approximate,
                    sampled, total_blobs, file_count):
        if approximate:
            prefix = "≈ "
        else:
            prefix = ""
        # GDAL reads roughly 45 thousand objects per second on an average
        # desktop; layer processing adds about 3 ms per matched object
        # (calibrated on a small PBF extract).
        estimate = total / 45000.0 + matched * 0.003
        counts = (
            f"объектов: {prefix}{_spaced(total)} · в bbox: "
            f"{prefix}{_spaced(in_bbox)}"
        )
        if self.source_mode.currentData() == "geofabrik":
            plan = self._geofabrik_plan
            pending, _size = self._geofabrik_pending(plan) if plan else (0, 0)
            parts = []
            if pending:
                parts.append(f"Скачивание ≈ {_duration(pending / DOWNLOAD_RATE)}")
            parts.append(f"подготовка ≈ {_duration(estimate)}")
            text = ", ".join(parts)
            self.pbf_info.setText(
                f"{text[0].upper()}{text[1:]} · {counts}."
            )
            return
        path = Path(self.source_value.text().strip()).expanduser()
        try:
            size = path.stat().st_size / 1048576
        except OSError:
            size = 0.0
        title = f"PBF ({file_count} файлов)" if file_count > 1 else f"PBF {size:.1f} МБ"
        self.pbf_info.setText(
            f"{title} · {counts}. Проверка ≈ {elapsed:.1f} с, "
            f"подготовка ≈ {estimate:.0f} с."
        )

    def _geofabrik_pending(self, plan=None):
        try:
            bbox = parse_bbox(self.bbox.text().strip())
            config = self._geofabrik_config(bbox)
        except Exception:
            return 0, 0
        pending = total = 0
        for status in self._geofabrik_selected_statuses():
            size = status.size or 0
            total += size
            if not geofabrik.local_ready(config, status.map):
                pending += size
        return pending, total

    @staticmethod
    def _geofabrik_estimate(pending, total):
        parts = []
        if pending:
            parts.append(f"Скачивание ≈ {_duration(pending / DOWNLOAD_RATE)}")
        if total:
            # Rough processing scale before exact object counts are known:
            # about 16 thousand OSM objects per megabyte at ~45 thousand
            # objects per second, plus layer assembly overhead.
            parts.append(f"подготовка ≈ {_duration(total / 1048576 * 0.45)}")
        if not parts:
            return "Оценка времени появится после проверки карт."
        text = ", ".join(parts)
        return (
            f"{text[0].upper()}{text[1:]}. Точная статистика появится "
            "после загрузки карт."
        )

    def _pbf_probe_failed(self, message):
        self.pbf_info.setText(f"Не удалось оценить PBF: {message}")

    def _server_changed(self, _index):
        if self.source_mode.currentData() != "overpass":
            return
        url = self.overpass_server.currentData()
        if url:
            self.source_value.setText(url)
            self.source_value.setEnabled(False)
            self.source_value.setPlaceholderText("URL выбранного сервера")
            self.source_hint.setText(
                "Проверка серверов выполняется в фоне. При подготовке данных "
                "плагин использует только доступные серверы."
            )
        else:
            self.source_value.setEnabled(True)
            self.source_value.clear()
            self.source_value.setPlaceholderText("https://example.org/api/interpreter")
            self.source_hint.setText("Укажите URL своего Overpass API.")

    def _check_servers(self, force=False):
        if not force and self._servers_checked:
            return
        if any(worker.isRunning() for worker in self.server_workers):
            return
        self._servers_checked = True
        self.recheck_servers.setEnabled(False)
        self.server_workers = []
        for server in DEFAULT_OVERPASS_SERVERS:
            self._set_server_status(server.url, "проверка…")
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
            self._set_server_status(url, f"доступен ({detail} с)")
            tooltip = f"Тестовый запрос выполнен за {detail} с"
        else:
            self._set_server_status(url, "недоступен")
            tooltip = detail
        self.overpass_server.setItemData(index, tooltip, _tooltip_role())
        if not ok and self.overpass_server.currentIndex() == index:
            self.source_hint.setText(
                "Выбранный сервер не отвечает. При подготовке данных плагин "
                "попробует остальные доступные серверы."
            )

    def showEvent(self, event):
        super().showEvent(event)
        if not self._narrowed:
            self._narrowed = True
            # После показа панели известна её естественная ширина.
            QTimer.singleShot(0, self._narrow_minimum_width)
        mode = self.source_mode.currentData()
        if mode == "overpass":
            self._check_servers()
        elif mode == "geofabrik":
            self._schedule_geofabrik()

    def _terrain_changed(self, enabled):
        has_dem = bool(self.dem_dir.text().strip())
        for widget in (
            self.dem_dir, self.hillshade_slope, self.isolines,
            self.smooth_isolines,
        ):
            widget.setEnabled(enabled)
        self.resample_method.setEnabled(
            enabled and self.hillshade_slope.isChecked()
        )
        self.download_terrain.setEnabled(enabled and not has_dem)
        self.download_terrain.setToolTip(
            "Отключено: используется DEM из указанного каталога."
            if has_dem else ""
        )
        steps_enabled = enabled and self.isolines.isChecked()
        for box in self.isolines_steps.values():
            box.setEnabled(steps_enabled)

    def _dem_dir_changed(self, text):
        if text.strip() and self.download_terrain.isChecked():
            self.download_terrain.setChecked(False)
        self._update_dem_hint(text)
        self._terrain_changed(self.generate_terrain.isChecked())

    def _update_dem_hint(self, text=None):
        value = self.dem_dir.text().strip() if text is None else text.strip()
        if not value:
            self.dem_hint.setText(
                "Если каталог указан, используется рельеф из него — "
                "скачивание SRTM отключается."
            )
            self.dem_hint.setStyleSheet("")
            return True
        path = Path(value).expanduser()
        if not path.exists():
            self.dem_hint.setText(f"Каталог DEM не найден: {path}")
            self.dem_hint.setStyleSheet("color: #b00020;")
            return False
        if not path.is_dir():
            self.dem_hint.setText(
                f"Указанный путь не является каталогом: {path}"
            )
            self.dem_hint.setStyleSheet("color: #b00020;")
            return False
        self.dem_hint.setText(
            f"Используется рельеф из каталога: {path}. SRTM не скачивается."
        )
        self.dem_hint.setStyleSheet("")
        return True

    def _current_isolines_step(self):
        checked = [
            value for value, box in self.isolines_steps.items()
            if box.isChecked()
        ]
        return min(checked) if checked else 10

    def _isolines_step_toggled(self, value, _checked):
        self._apply_isolines_step(value)

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
        self.source_mode.setCurrentIndex(
            max(0, self.source_mode.findData(self.settings.value("source_mode", "geofabrik")))
        )
        self._source_changed(0)
        mode = self.source_mode.currentData()
        if mode == "geofabrik":
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
        self.download_terrain.setChecked(self.settings.value("download_terrain", "false") == "true")
        if self.dem_dir.text().strip():
            self.download_terrain.setChecked(False)
        try:
            step = int(self.settings.value("isolines_step", 10))
        except (TypeError, ValueError):
            step = 10
        self._set_isolines_step(step)
        method = str(self.settings.value("terrain_resample_method", "cubicspline"))
        self.resample_method.setCurrentIndex(
            max(0, self.resample_method.findData(method))
        )
        self.generate_terrain.setChecked(self.settings.value("generate_terrain", "false") == "true")
        self._terrain_changed(self.generate_terrain.isChecked())

    def _save_settings(self):
        self.settings.setValue("projects_dir", self.projects_dir.text())
        self.settings.setValue("project_name", self.project_name.text())
        self.settings.setValue("bbox", self.bbox.text())
        mode = self.source_mode.currentData()
        self.settings.setValue("source_mode", mode)
        self.settings.setValue("server_url", self.overpass_server.currentData())
        value = "" if mode == "geofabrik" else self.source_value.text()
        self.settings.setValue("source_value", value)
        values = dict(self._mode_values)
        values[mode] = value
        for name, value in values.items():
            self.settings.setValue(f"source_value_{name}", value)
        self.settings.setValue("dem_dir", self.dem_dir.text())
        self.settings.setValue("download_terrain", str(self.download_terrain.isChecked()).lower())
        self.settings.setValue("generate_terrain", str(self.generate_terrain.isChecked()).lower())
        self.settings.setValue("isolines_step", str(self._current_isolines_step()))
        self.settings.setValue(
            "terrain_resample_method",
            self.resample_method.currentData() or "cubicspline",
        )

    def _connect_project_signals(self):
        project = QgsProject.instance()
        for signal_name, slot in (
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
        self._schedule_project_refresh()

    def _schedule_project_refresh(self, *_args):
        self._project_timer.start()

    def _refresh_project_state(self):
        was_own = self._own_project_open
        own = self._own_marker or ProjectPanel.is_xtopo_project()
        self._own_project_open = own
        for index in range(self.tabs.count()):
            is_project = index == self.project_tab_index
            self.tabs.setTabEnabled(index, is_project if own else not is_project)
        self.project_panel.refresh()
        if own:
            if not was_own:
                self.log_toggle.setChecked(False)
                set_canvas_magnification(self.iface)
            self.tabs.setCurrentWidget(self.project_panel)
            self.project_panel.focus_first()
        else:
            self.tabs.setCurrentIndex(0)
        self._update_action_buttons()

    def _update_action_buttons(self):
        can_create = not self._running and not self._own_project_open
        self.prepare_button.setEnabled(can_create)
        self.open_button.setEnabled(can_create)
        _set_primary(self.prepare_button, can_create)

    def _build_config(self):
        mode = self.source_mode.currentData()
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
        if mode == "geofabrik" and self._geofabrik_plan is not None:
            if not self._geofabrik_plan.maps:
                raise ValueError("Для этого bbox карты Geofabrik не найдены")
            if not self._geofabrik_selected_ids():
                raise ValueError("Выберите хотя бы одну карту Geofabrik")
        endpoints = [server.url for server in DEFAULT_OVERPASS_SERVERS]
        endpoint = endpoints[0]
        if mode == "overpass":
            if not self.overpass_server.currentData() and not value:
                raise ValueError("Укажите URL сервера Overpass")
            endpoint = (self.overpass_server.currentData() or value).strip()
            if endpoint in endpoints:
                endpoints.remove(endpoint)
            endpoints.insert(0, endpoint)
        dem_dir = self.dem_dir.text().strip()
        if dem_dir:
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
            source_mode=mode,
            pbf_path=value if mode == "pbf" else "",
            geofabrik_maps=(
                self._geofabrik_selected_ids() if mode == "geofabrik" else []
            ),
            overpass_endpoint=endpoint,
            overpass_endpoints=endpoints,
            overpass_endpoint_external=endpoint,
            overpass_endpoint_local=value if mode == "local" else "",
            overpass_endpoint_ssh=value if mode == "ssh" else "",
            generate_terrain=self.generate_terrain.isChecked(),
            download_terrain_tiles=self.download_terrain.isChecked(),
            get_terrain_tiles=bool(dem_dir) and not self.download_terrain.isChecked(),
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
            QMessageBox.warning(self, "QGIS-xtopo", str(exc))
            return
        self._pbf_probe_timer.stop()
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(5000)
        self._geofabrik_timer.stop()
        if self.geofabrik_worker is not None and self.geofabrik_worker.isRunning():
            self.geofabrik_worker.cancel()
            self.geofabrik_worker.wait(1000)
        self._save_settings()
        self.log.clear()
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
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.failed.connect(self.worker.deleteLater)
        self.worker.cancelled.connect(self.worker.deleteLater)
        self.worker.start()

    def _set_running(self, running):
        self._running = running
        self.cancel_button.setEnabled(running)
        self.tabs.setEnabled(not running)
        self.progress.setVisible(running)
        self._update_action_buttons()

    def _set_status(self, message):
        self.status.setText(message)
        self.status.setToolTip(message)

    def _append_log(self, message):
        timestamp = QTime.currentTime().toString("HH:mm:ss")
        self.log.appendPlainText(f"[{timestamp}] {message}")
        self._set_status(message)
        self.log.ensureCursorVisible()

    def _pipeline_percent(self, value):
        self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def _finished(self, path):
        self.worker = None
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._set_running(False)
        self._set_status("Готово")
        self._append_log(f"Готово: {path}")
        try:
            open_project(path, self.iface, repair=False)
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", f"Проект создан, но не открылся: {exc}")
        if self.source_mode.currentData() == "geofabrik":
            self._schedule_geofabrik()

    def _failed(self, message):
        self.worker = None
        self._set_running(False)
        self._set_status("Ошибка")
        self.log_toggle.setChecked(True)
        self._append_log(f"Ошибка: {message}")
        QMessageBox.critical(self, "QGIS-xtopo", message)

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
        if self.geofabrik_worker is not None and self.geofabrik_worker.isRunning():
            self.geofabrik_worker.cancel()
            cancelled = True
        if cancelled:
            self._set_status("Отмена...")

    def _open_project(self):
        try:
            config = self._build_config()
            path = project_file(config)
            if not path.exists():
                QMessageBox.warning(self, "QGIS-xtopo", f"Проект не найден: {path}")
                return
            open_project(path, self.iface)
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", str(exc))

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        for worker in list(self.server_workers):
            worker.cancel()
            worker.wait(12000)
        self._pbf_probe_timer.stop()
        self._geofabrik_timer.stop()
        if self.pbf_probe is not None and self.pbf_probe.isRunning():
            self.pbf_probe.cancel()
            self.pbf_probe.wait(3000)
        if self.geofabrik_worker is not None and self.geofabrik_worker.isRunning():
            self.geofabrik_worker.cancel()
            self.geofabrik_worker.wait(12000)
        self._save_settings()
        super().closeEvent(event)
