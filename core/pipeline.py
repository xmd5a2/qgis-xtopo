import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import (
    area_field, coastline, dem_sources, disk_space, field_subset, gdal_log,
    highway_integrity, i18n, icon_angles, label_cache, oneway, osm_convert,
    peak_filter, poi_icons, polygon_overlaps, project, road_packing, sources,
    stream_drying, terrain,
)
from .bbox import parse_bbox
from .cpu import cpu_workers
from .memory import layer_worker_limit
from .query_recipes import recipe_for


class PipelineError(RuntimeError):
    pass


# Этап, в котором считается сумма времени операций по слоям.
_LAYER_STAGE = "обработка слоёв"

# Этапы, которые идут одновременно с обработкой слоёв: их времена не
# складываются в общее время подготовки.
_PARALLEL_STAGES = ("источник OSM", "рельеф")


class Timings:
    """Хронометраж подготовки: время этапов и самых долгих шагов.

    Данные нужны, чтобы понимать, на что уходит время: выводятся в журнал
    в конце подготовки.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.stages = {}
        self.steps = []
        self.workers = 0

    def add_stage(self, name, seconds):
        with self._lock:
            self.stages[name] = self.stages.get(name, 0.0) + seconds

    def add_step(self, name, operation, seconds):
        with self._lock:
            self.steps.append((seconds, name, operation))

    def set_workers(self, count):
        """Сколько слоёв обрабатывалось одновременно."""
        with self._lock:
            self.workers = max(int(count or 0), 0)

    def top_steps(self, limit=15):
        with self._lock:
            steps = sorted(self.steps, reverse=True)[:limit]
        return steps

    def summary(self):
        with self._lock:
            stages = dict(self.stages)
            steps = list(self.steps)
            workers = self.workers
        lines = []
        if stages:
            parts = ", ".join(
                i18n.trf(
                    "{} — {} с",
                    i18n.tr(name),
                    f"{seconds:.1f}",
                )
                for name, seconds in sorted(stages.items(), key=lambda i: -i[1])
            )
            lines.append(i18n.trf("Хронометраж: по этапам — {}", parts))
            parallel = [name for name in _PARALLEL_STAGES if name in stages]
            if parallel:
                lines.append(
                    i18n.trf(
                        "Хронометраж: {} идут параллельно со слоями, "
                        "поэтому сумма этапов больше общего времени",
                        ", ".join(i18n.tr(name) for name in parallel),
                    )
                )
        if steps:
            lines.append(self._layers_line(steps, workers, stages))
        return lines

    def _layers_line(self, steps, workers, stages):
        """Сумма времени операций по слоям и её отношение к этапу.

        Слои готовятся параллельно, поэтому сумма времён отдельных
        операций больше времени этапа: это объём работы, а не длительность
        сборки.  Без пояснения число выглядит противоречивым.
        """
        total = sum(item[0] for item in steps)
        stage = stages.get(_LAYER_STAGE)
        if stage is None:
            return i18n.trf(
                "Хронометраж: слои — сумма операций {} с",
                f"{total:.1f}",
            )
        if workers > 1:
            return i18n.trf(
                "Хронометраж: слои — сумма операций {} с при {} потоках, "
                "этап «{}» занял {} с",
                f"{total:.1f}",
                f"{workers}",
                i18n.tr(_LAYER_STAGE),
                f"{stage:.1f}",
            )
        return i18n.trf(
            "Хронометраж: слои — сумма операций {} с, этап «{}» занял {} с",
            f"{total:.1f}",
            i18n.tr(_LAYER_STAGE),
            f"{stage:.1f}",
        )


_TERRAIN_STEPS = (
    ("Preparing terrain", 0.05),
    ("Загрузка рельефа", 0.15),
    ("Merging ", 0.35),
    ("Reprojecting DEM", 0.5),
    ("Generating slope", 0.65),
    ("Writing slope", 0.72),
    ("Generating hillshade", 0.85),
    ("Generating contour lines", 0.95),
)

# Формат промежуточных данных при обработке слоёв. GeoPackage с
# пространственным индексом заметно быстрее geojson: JSON не разбирается
# заново на каждом шаге, а наложения ищутся по индексу.
INTERMEDIATE_EXTENSION = os.environ.get(
    "QGIS_XTOPO_INTERMEDIATE", "gpkg"
).lower()

# Разность со слоем-наложением считается только для пересекающихся
# объектов: native:difference тратит время на каждый объект слоя, даже
# если он далеко от overlay.  Деление окупается на больших слоях; для
# мелких дешевле вызвать алгоритм целиком.
DIFFERENCE_SPLIT_MIN_FEATURES = 1000
# Overlay целиком держится в памяти для пространственного индекса.
# Больше этого предела индексация дороже самой разности.
DIFFERENCE_SPLIT_MAX_OVERLAY = 20000


def _prepared_geometry(geometry):
    """Подготовленная GEOS-геометрия для повторных проверок пересечений.

    ``QgsGeometry.intersects`` строит индекс рёбер сложного полигона
    заново на каждый вызов: десяток тысяч проверок ручья у воды занимает
    секунды.  OGR с ``CreatePreparedGeometry`` делает ту же работу за
    доли секунды.  Если GDAL собран без GEOS, вернётся ``None``, и
    вызывающий проверит пересечение обычным путём.
    """
    create = getattr(geometry, "CreatePreparedGeometry", None)
    if create is None:
        return None
    try:
        return create()
    except Exception:  # noqa: BLE001 - проверка пойдёт обычным путём
        return None


def progress_stages(config):
    """Stage weights for the preparation progress bar."""
    stages = [("Проверка параметров", 1), ("Создание проекта QGIS", 4)]
    if config.array_queries:
        stages.append(("Подготовка источника OSM", 30))
    stages.append(("Обработка слоёв", 45))
    # Порядок стадий совпадает с ходом run_project: сначала догоняется
    # рельеф, потом береговая линия, пики и очистка.
    if config.generate_terrain:
        stages.append(("Обработка рельефа", 14))
    if "coastline" in config.array_queries:
        stages.append(("Обработка береговой линии", 4))
    if "peak" in config.array_queries:
        stages.append(("Проверка пиков", 3))
    if config.resolve_polygon_overlaps:
        stages.append(("Пересечения полигонов", 3))
    stages.append(("Размещение значков POI", 2))
    stages.append(("Углы поворота значков", 1))
    stages.append(("Упаковка площадей, зданий и линий", 15))
    stages.append(("Удаление пустых слоёв", 2))
    return stages


def _report_terrain_stage(thread, report):
    """Объявляет стадию рельефа до ожидания потока.

    Рельеф качается параллельно слоям, и к моменту их окончания загрузка
    может быть ещё далека от завершения. Без этой строки в журнале остаётся
    последнее сообщение про слои, и непонятно, что работа ещё идёт.
    """
    report("Обработка рельефа")
    if thread.is_alive():
        report("Рельеф: продолжается загрузка тайлов и сборка DEM")


def _source_timings(elapsed, download):
    """Делит время подготовки источника на скачивание и остальную работу.

    Скачивание карт и ответов Overpass учитывается отдельно, чтобы в
    хронометраже было видно, сколько времени ушло на сеть.
    """
    elapsed = max(float(elapsed or 0.0), 0.0)
    download = min(max(float(download or 0.0), 0.0), elapsed)
    return download, elapsed - download


class _ProgressTracker:
    """Maps pipeline log messages to a monotone 0..100 progress value.

    The scale is split into weighted stages, so it moves steadily through the
    whole preparation instead of sitting in indefinite mode.  Sub-stage
    feedback comes from layer and PBF messages that the pipeline already emits.
    """

    _layer = re.compile(r"^\[(\d+)/(\d+)\]")
    _file = re.compile(r"(\d+)/(\d+)")

    def __init__(self, stages, percent=None, queries=0, status=None):
        self.percent = percent
        self.status = status
        self.queries = queries
        self.queries_done = 0
        self.source_download = False
        self.name = ""
        self.value = 0
        self.last = -1
        self._lock = threading.Lock()
        total = float(sum(weight for _, weight in stages)) or 1.0
        self.bounds = {}
        cursor = 0.0
        for name, weight in stages:
            span = weight / total * 100.0
            self.bounds[name] = (cursor, cursor + span)
            cursor += span

    def wrap(self, notify):
        def report(message):
            self.observe(message)
            notify(message)
        return report

    def observe(self, message):
        if message in self.bounds:
            with self._lock:
                self.name = message
                self._emit(self.bounds[message][0])
            return
        if self.name == "Обработка слоёв":
            match = self._layer.match(message)
            if match:
                self.fraction((int(match.group(1)) - 1) / max(1, int(match.group(2))))
        elif self.name == "Подготовка источника OSM":
            match = re.search(r"\((\d+)%\)", message)
            if match:
                value = min(1.0, int(match.group(1)) / 100.0)
                if self.source_download:
                    value = 0.7 + 0.3 * value
                self.fraction(value)
            elif message.endswith(": downloaded") or message.endswith(": cached"):
                self.queries_done += 1
                if self.queries:
                    self.fraction(self.queries_done / self.queries)
            elif message.startswith("Opening "):
                match = self._file.search(message)
                if match:
                    self.fraction(
                        (int(match.group(1)) - 1) / max(1, int(match.group(2)))
                    )
            elif message.startswith("PBF: file "):
                match = self._file.search(message)
                if match:
                    self.fraction(
                        int(match.group(1)) / max(1, int(match.group(2)))
                    )
        elif self.name == "Обработка рельефа":
            for prefix, value in _TERRAIN_STEPS:
                if message.startswith(prefix):
                    self.fraction(value)
                    break

    def fraction(self, fraction):
        if not self.name:
            return
        with self._lock:
            low, high = self.bounds[self.name]
            value = low + (high - low) * max(0.0, min(1.0, fraction))
            self._emit(value)

    def download_percent(self, value, message=None):
        """Map Geofabrik download progress to the source stage."""
        if message and self.status:
            # Строка состояния показывает ход загрузки вместе со скоростью.
            self.status(message)
        if value is None or value < 0:
            return
        self.source_download = True
        self.fraction(0.7 * min(100, value) / 100.0)

    def finish(self):
        with self._lock:
            self.name = ""
            self.value = 100
            self.last = 100
            if self.percent:
                self.percent(100)

    def _emit(self, value):
        value = max(self.value, min(99, int(value)))
        if value == self.last:
            return
        self.last = value
        self.value = value
        if self.percent:
            self.percent(value)


def _qgis():
    from qgis.core import QgsApplication, QgsProcessingFeedback

    return QgsApplication, QgsProcessingFeedback


class Feedback:
    def __init__(self, progress=None, cancel=None, fraction=None):
        _, base = _qgis()

        class _Feedback(base):
            def __init__(self):
                super().__init__()
                self.errors = []

            def isCanceled(self):
                return cancel is not None and cancel.is_set()

            def setProgressText(self, text):
                if progress:
                    progress(text)

            def reportError(self, error, fatalError=False):
                self.errors.append(error)
                if progress:
                    progress(f"ERROR: {error}")

            def pushInfo(self, info):
                if progress:
                    progress(info)

            def pushWarning(self, warning):
                if progress:
                    progress(f"WARNING: {warning}")

        self.instance = _Feedback()
        if fraction:
            self.instance.progressChanged.connect(
                lambda value: fraction(max(0.0, min(1.0, value / 100.0)))
            )

    def __getattr__(self, item):
        return getattr(self.instance, item)


def _load_processing():
    """Импортирует QGIS Processing, не полагаясь на текущий каталог.

    QGIS при запуске из корня репозитория создаёт пустой каталог
    ``processing/`` (models, outputs, scripts). Python принимает его за
    namespace-пакет: ``import processing`` проходит, а
    ``processing.core`` — нет. Поэтому путь QGIS ставится в начало
    sys.path, а уже импортированный пустой пакет убирается из
    sys.modules.
    """
    import sys

    from qgis.core import QgsApplication

    prefix = QgsApplication.prefixPath()
    paths = ["/usr/share/qgis/python/plugins"]
    if prefix:
        paths.insert(0, os.path.join(prefix, "python", "plugins"))
    for path in reversed(paths):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    try:
        import processing
        from processing.core.Processing import Processing
        return processing, Processing
    except ImportError:
        for name in list(sys.modules):
            if name == "processing" or name.startswith("processing."):
                del sys.modules[name]
        import processing
        from processing.core.Processing import Processing
        return processing, Processing


def _warm_up_style():
    """Открывает базу стилей в главном потоке.

    ``QgsStyle.defaultStyle()`` создаётся лениво и без блокировки: если
    это случится сразу в нескольких рабочих потоках, база стилей
    откроется дважды.  Прогрев в главном потоке снимает гонку.
    """
    try:
        from qgis.core import QgsStyle

        QgsStyle.defaultStyle(True)
    except Exception:  # noqa: BLE001 - стиль не обязателен
        pass


def _source_size(source, name):
    """Размер исходных данных слоя — грубая оценка трудоёмкости.

    По размеру выгрузки видно, какой слой дольше всего считать:
    тяжёлые слои стоит запускать первыми, иначе они затягивают конец
    подготовки.
    """
    dataset = getattr(source, "dataset", None)
    files = getattr(dataset, "feature_files", None)
    if not files:
        return 0
    path = files.get(name)
    if path is None:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


class ProcessingRunner:
    def __init__(self, progress=None, cancel=None, fraction=None):
        processing, Processing = _load_processing()
        Processing.initialize()
        self.processing = processing
        self.progress = progress
        self.cancel = cancel
        self.fraction = fraction
        self.feedback = Feedback(
            progress=progress, cancel=cancel,
            fraction=lambda value: self.fraction and self.fraction(value),
        ).instance
        self.context = None
        self.context_project = None
        _warm_up_style()

    def clone(self):
        other = object.__new__(ProcessingRunner)
        other.processing = self.processing
        other.progress = self.progress
        other.cancel = self.cancel
        other.fraction = self.fraction
        other.feedback = Feedback(
            progress=self.progress, cancel=self.cancel,
            fraction=lambda value: other.fraction and other.fraction(value),
        ).instance
        other.context = None
        other.context_project = None
        return other

    def release(self):
        """Освобождает проект потока до его завершения.

        ``QgsProject`` — QObject.  Если Python отпускает его в другом
        потоке, PyQt откладывает удаление до завершения потока-владельца,
        а там ``thread_local`` регулярное выражение в
        ``QgsProject::writeEntry`` уже разрушено — QGIS падает.  Поэтому
        проект, созданный в рабочем потоке, надо отпускать в нём же.
        """
        self.context = None
        self.context_project = None

    def _processing_context(self):
        """Свой контекст и проект на поток.

        ``processing.run`` без контекста берёт общий ``QgsProject.instance()``
        и строит на нём контекст выражений.  Несколько слоёв в разных
        потоках одновременно обращаются к одному проекту — QGIS падает с
        segfault.  Свой проект и контекст на каждый поток убирают гонку.
        """
        if self.context is not None:
            return self.context
        from qgis.core import (
            QgsExpressionContext, QgsExpressionContextUtils,
            QgsFeatureRequest, QgsProcessingContext, QgsProcessingUtils,
            QgsProject, QgsSettings,
        )

        project = QgsProject()
        context = QgsProcessingContext()
        context.setProject(project)
        context.setFeedback(self.feedback)
        expression = QgsExpressionContext()
        expression.appendScope(QgsExpressionContextUtils.globalScope())
        expression.appendScope(QgsExpressionContextUtils.projectScope(project))
        context.setExpressionContext(expression)
        try:
            from processing.core.ProcessingConfig import ProcessingConfig

            invalid = ProcessingConfig.getSetting(
                ProcessingConfig.FILTER_INVALID_GEOMETRIES
            )
            if invalid is not None:
                context.setInvalidGeometryCheck(
                    QgsFeatureRequest.InvalidGeometryCheck(int(invalid))
                )
            settings = QgsSettings()
            context.setDefaultEncoding(
                QgsProcessingUtils.resolveDefaultEncoding(
                    settings.value("/Processing/encoding")
                )
            )
        except Exception:  # noqa: BLE001 - настройки не обязательны
            pass
        self.context_project = project
        self.context = context
        return context

    def has_algorithm(self, algorithm):
        QgsApplication, _ = _qgis()
        return QgsApplication.processingRegistry().algorithmById(algorithm) is not None

    def run(self, algorithm, parameters):
        if not self.has_algorithm(algorithm):
            raise PipelineError(f"QGIS algorithm is not available: {algorithm}")
        try:
            return self.processing.run(
                algorithm, _stringify(parameters), feedback=self.feedback,
                context=self._processing_context(),
            )
        except Exception as exc:
            if self.feedback.isCanceled():
                # Отмена — это не сбой алгоритма: её нельзя выдавать за
                # ошибку слоя, иначе сборка выглядит успешной.
                raise InterruptedError("Preparation cancelled") from exc
            raise PipelineError(f"{algorithm} failed: {exc}") from exc


def _stringify(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _stringify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(item) for item in value]
    return value


def _layer_has_features(path):
    try:
        from osgeo import ogr
    except ImportError:
        return True
    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        if str(path).lower().endswith((".geojson", ".json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return bool(json.load(handle).get("features"))
            except Exception:
                return True
        return True
    try:
        layer = dataset.GetLayer(0)
        return layer is not None and layer.GetNextFeature() is not None
    except Exception:
        return True


def _layer_geometry_families(path):
    """Какие семейства геометрий есть в слое: (полигоны, линии).

    ``native:polygonstolines`` рассчитан на полигоны; линии он
    превращает в пустые геометрии.  Слой из одних линий шаг нужно
    пропустить, а смешанный — обработать отдельно, иначе следующие
    операции теряют линейные объекты.
    """
    try:
        from osgeo import ogr
    except ImportError:
        return True, False
    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return True, False
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return True, False
        flatten = getattr(ogr, "wkbFlatten", None) or ogr.GT_Flatten
        polygon_types = (ogr.wkbPolygon, ogr.wkbMultiPolygon)
        line_types = (ogr.wkbLineString, ogr.wkbMultiLineString)
        polygons = lines = False
        # Заголовок слоя бывает неточным: после fixgeometries слой с
        # одной площадью и линиями объявляется линейным.  Поэтому
        # смотрим сами объекты, а не только GetGeomType().
        for feature in layer:
            has_polygons, has_lines = _geometry_family_flags(
                feature.GetGeometryRef(), flatten,
                polygon_types, line_types,
            )
            polygons = polygons or has_polygons
            lines = lines or has_lines
            if polygons and lines:
                break
        return polygons, lines
    except Exception:
        return True, False
    finally:
        dataset = None


def _geometry_family_flags(geometry, flatten, polygon_types, line_types):
    """Полигоны и линии внутри геометрии, включая коллекции."""
    if geometry is None or geometry.IsEmpty():
        return False, False
    kind = flatten(geometry.GetGeometryType())
    if kind in polygon_types:
        return True, False
    if kind in line_types:
        return False, True
    polygons = lines = False
    for index in range(geometry.GetGeometryCount()):
        has_polygons, has_lines = _geometry_family_flags(
            geometry.GetGeometryRef(index), flatten,
            polygon_types, line_types,
        )
        polygons = polygons or has_polygons
        lines = lines or has_lines
        if polygons and lines:
            break
    return polygons, lines


def _recipe_outputs(name):
    """Файлы, которые производит рецепт слоя.

    Это имена файлов, а не имена слоёв внутри них: ``save`` с
    ``layer_name`` меняет имя слоя в файле, но файл называется по
    имени слоя и суффиксу.  Зависимости ищутся по именам файлов.
    """
    outputs = {name}
    for operation, parameters in recipe_for(name).steps:
        if operation == "save":
            suffix = parameters.get("suffix") or ""
            outputs.add(f"{name}{suffix}")
        elif operation == "split_layer":
            outputs.update({name, f"{name}_layer_1", f"{name}_layer_-1"})
    return outputs


def _recipe_dependencies(name, producers):
    dependencies = set()
    for operation, parameters in recipe_for(name).steps:
        if operation in ("load", "difference", "intersection"):
            references = [parameters.get("name") or parameters.get("overlay")]
        elif operation == "merge":
            references = parameters.get("layers") or []
        else:
            references = []
        for reference in references:
            producer = producers.get(reference) if reference else None
            if producer and producer != name:
                dependencies.add(producer)
    return dependencies


def _label_name(feature):
    """Название объекта для поиска дубликатов подписи."""
    for field in ("name", "name:ru", "name:en"):
        index = feature.GetFieldIndex(field)
        if index >= 0:
            value = feature.GetField(index)
            if value:
                return value
    return None


def _same_outline(first, second, tolerance=1e-5, area_share=0.95):
    """Похожи ли контуры: дубликаты OSM расходятся на доли метра."""
    first_envelope, first_area = first
    second_envelope, second_area = second
    if any(abs(a - b) > tolerance
           for a, b in zip(first_envelope, second_envelope)):
        return False
    return (min(first_area, second_area)
            / max(first_area, second_area, 1e-12) >= area_share)


class _GpkgWriter:
    """Потоковая запись в GeoPackage одной транзакцией."""

    def __init__(self, path, layer_name, definition, spatial_ref=None,
                 geometry_type=None, cancel=None):
        from osgeo import ogr

        self.path = Path(path)
        self.count = 0
        self.cancel = cancel
        self.driver = ogr.GetDriverByName("GPKG")
        try:
            self.dataset = self.driver.CreateDataSource(str(self.path))
        except Exception as exc:  # noqa: BLE001 - сообщаем как сбой записи
            raise PipelineError(f"Cannot create {self.path}: {exc}") from exc
        if self.dataset is None:
            raise PipelineError(f"Cannot create {self.path}")
        try:
            self.layer = self.dataset.CreateLayer(
                layer_name, spatial_ref,
                ogr.wkbUnknown if geometry_type is None else geometry_type,
            )
        except Exception as exc:  # noqa: BLE001 - сообщаем как сбой записи
            raise PipelineError(
                f"Cannot create layer {layer_name} in {self.path}: {exc}"
            ) from exc
        if self.layer is None:
            raise PipelineError(f"Cannot create layer {layer_name} in {self.path}")
        for index in range(definition.GetFieldCount()):
            field = definition.GetFieldDefn(index)
            self.layer.CreateField(ogr.FieldDefn(field.GetName(), field.GetType()))
        # Иначе GPKG фиксирует каждый объект отдельной транзакцией.
        self.layer.StartTransaction()

    def add(self, feature, geometry=None):
        from osgeo import ogr

        if self.cancel is not None and self.count % 64 == 0 and self.cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        definition = self.layer.GetLayerDefn()
        if geometry is None:
            geometry = feature.GetGeometryRef()
        output = ogr.Feature(definition)
        # Копирование полей в C++ в разы дешевле цикла по сотням
        # колонок: на смешанных слоях этот цикл держал GIL.
        output.SetFrom(feature)
        if geometry is not None:
            output.SetGeometry(geometry)
        self.layer.CreateFeature(output)
        self.count += 1

    def close(self, commit=True):
        if self.layer is None:
            return
        if commit:
            self.layer.CommitTransaction()
        else:
            self.layer.RollbackTransaction()
        self.layer = None
        self.dataset = None


class Pipeline:
    def __init__(self, config, bbox, runner, progress=None, cancel=None,
                 fraction=None, timings=None, empty_outputs=None):
        self.config = config
        self.bbox = bbox
        self.runner = runner
        self.progress = progress or (lambda message: None)
        self.cancel = cancel
        self.fraction = fraction or (lambda value: None)
        self.warnings = []
        self.counter = 0
        self.timings = timings or Timings()
        # Выходы слоёв, у которых не оказалось исходных объектов.  Общий
        # набор для последовательной и параллельной обработки: если
        # зависимость пуста, предупреждать о её отсутствии не нужно.
        self.empty_outputs = (
            empty_outputs if empty_outputs is not None else set()
        )

    def run(self, source, query_names):
        if not query_names:
            return self.warnings
        workers = self._worker_count(len(query_names))
        # Число потоков нужно хронометражу: сумма времени операций по
        # слоям объясняется параллельной обработкой.
        self.timings.set_workers(workers)
        if workers <= 1 or len(query_names) == 1:
            self._run_serial(source, query_names)
            return self.warnings
        return self._run_parallel(source, query_names, workers)

    def _dependency_graph(self, query_names):
        """Кто что производит и от кого зависит каждый слой.

        Слой может читать другой (вычитание воды, объединение потоков),
        и порядок запросов пользователя этому не соответствует.  Граф
        зависимостей строится одинаково для последовательного и
        параллельного пути.
        """
        producers = {}
        for name in query_names:
            for output in _recipe_outputs(name):
                producers.setdefault(output, name)
        return {
            name: _recipe_dependencies(name, producers) for name in query_names
        }

    def _run_serial(self, source, query_names):
        """Последовательная обработка слоёв с соблюдением зависимостей."""
        dependencies = self._dependency_graph(query_names)
        total = len(query_names)
        done = set()
        failed = set()
        pending = list(query_names)
        processed = 0
        while pending:
            for name in [n for n in pending if dependencies[n] & failed]:
                pending.remove(name)
                failed.add(name)
                self.warnings.append(f"{name}: skipped, source layer failed")
                self.progress(f"{name}: skipped (source layer failed)")
            ready = [name for name in pending if dependencies[name].issubset(done)]
            if not ready:
                # Зависимости не разрешаются (цикл или слой вне набора):
                # дальше идти нельзя, остальные слои пропускаем.
                for name in pending:
                    self.warnings.append(f"{name}: skipped, unresolved dependency")
                    self.progress(f"{name}: skipped (unresolved dependency)")
                failed.update(pending)
                pending.clear()
                break
            name = ready[0]
            pending.remove(name)
            processed += 1
            if self._run_layer(name, source, processed, total):
                done.add(name)
            else:
                failed.add(name)
            self.fraction(processed / total)

    def _worker_count(self, total):
        """Сколько слоёв обрабатывать одновременно.

        Потолок по ядрам не должен приводить к OOM: на машине с 8 ГБ
        одновременно помещается заметно меньше слоёв, чем на 32 ГБ.
        Явно заданное ``parallel_workers`` тоже ограничивается памятью.
        Параллельно слоям идёт поток рельефа, поэтому его память
        вычитается из бюджета.
        """
        configured = int(getattr(self.config, "parallel_workers", 0) or 0)
        # 3/4 ядер, но не больше 8: на 16 ядрах 12 слоёв оказались
        # медленнее восьми из-за конкуренции за диск и память.
        requested = configured if configured > 0 else cpu_workers(limit=8)
        requested = max(1, min(requested, total))
        terrain = bool(getattr(self.config, "generate_terrain", False))
        return max(
            1,
            min(requested, layer_worker_limit(requested, terrain=terrain)),
        )

    def _run_layer(self, name, source, index, total):
        """Готовит один слой. True — слой собран, False — пропущен."""
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        if total:
            self.runner.fraction = (
                lambda value, index=index, total=total:
                self.fraction((index - 1 + value) / total)
            )
        self.progress(f"[{index}/{total}] {name}")
        # GDAL различает обработчики ошибок по потокам, а слои готовятся в
        # отдельных потоках: перехватчик ставим здесь, иначе предупреждения
        # уйдут в консоль QGIS мимо журнала.
        gdal_log.LOG.install()
        try:
            geojson = self._source_geojson(name, source)
            if geojson is None:
                self.empty_outputs.update(_recipe_outputs(name))
                self.progress(f"{name}: no objects, skipped")
                return True
            current = geojson
            try:
                for operation, parameters in recipe_for(name).steps:
                    started = time.monotonic()
                    current = self._step(name, current, operation, parameters)
                    self.timings.add_step(
                        name, operation, time.monotonic() - started
                    )
            except PipelineError as exc:
                self.warnings.append(f"{name}: {exc}; layer skipped")
                self.progress(f"{name}: skipped ({exc})")
                return False
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 - слой не валит сборку
                self.warnings.append(f"{name}: {exc}; layer skipped")
                self.progress(f"{name}: skipped ({exc})")
                return False
            return True
        finally:
            gdal_log.LOG.restore()

    @staticmethod
    def _run_parallel_layer(task, name, source, started, total):
        """Слой в отдельном потоке; проект потока отпускается здесь же."""
        try:
            return task._run_layer(name, source, started, total)
        finally:
            task.runner.release()

    def _run_parallel(self, source, query_names, workers):
        dependencies = self._dependency_graph(query_names)
        lock = threading.Lock()

        def report(message):
            with lock:
                self.progress(message)

        total = len(query_names)
        started = 0
        completed = set()
        failed = set()
        # Тяжёлые слои идут первыми: по размеру исходных данных видно,
        # сколько работы ждёт слой.  Иначе лес и ледники (самые долгие)
        # стартуют последними и затягивают конец подготовки.
        pending = sorted(query_names, key=lambda name: -_source_size(source, name))
        running = {}
        warnings = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while pending or running:
                if self.cancel is not None and self.cancel.is_set():
                    for future in running:
                        future.cancel()
                    raise InterruptedError("Preparation cancelled")

                for name in list(pending):
                    if len(running) >= workers:
                        break
                    if dependencies[name] & failed:
                        # Слой, на который опирается этот, не удался:
                        # ждать его бессмысленно.
                        pending.remove(name)
                        failed.add(name)
                        report(f"{name}: skipped (source layer failed)")
                        warnings.append(f"{name}: skipped, source layer failed")
                        continue
                    if not dependencies[name].issubset(completed):
                        continue
                    pending.remove(name)
                    started += 1
                    task = Pipeline(
                        self.config, self.bbox, self.runner.clone(),
                        progress=report, cancel=self.cancel,
                        fraction=self.fraction, timings=self.timings,
                        empty_outputs=self.empty_outputs,
                    )
                    future = pool.submit(
                        self._run_parallel_layer, task, name, source,
                        started, total,
                    )
                    running[future] = (name, task)

                if running:
                    done, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        name, task = running.pop(future)
                        try:
                            future.result()
                        except InterruptedError:
                            for pending_future in running:
                                pending_future.cancel()
                            raise
                        except Exception as exc:
                            # Упавший слой не считается выполненным:
                            # зависящие от него слои пропускаются.
                            report(f"{name}: skipped ({exc})")
                            warnings.append(f"{name}: skipped ({exc})")
                            failed.add(name)
                        else:
                            warnings.extend(task.warnings)
                            completed.add(name)
                        self.fraction(
                            (len(completed) + len(failed)) / total
                        )
                elif pending:
                    name = pending.pop(0)
                    started += 1
                    if self._run_layer(name, source, started, total):
                        completed.add(name)
                    else:
                        failed.add(name)
                    warnings.extend(self.warnings)
                    self.warnings = []
                    self.fraction(
                        (len(completed) + len(failed)) / total
                    )

        self.warnings.extend(warnings)
        return self.warnings

    def _source_geojson(self, name, source):
        destination = self.config.temp_dir / f"{name}_source.geojson"
        if source.dataset is not None:
            path = source.dataset.feature_files.get(name)
            if path is None:
                return None
            count = self._ndjson_to_geojson(path, destination)
            return destination if count else None
        osm_path = source.files.get(name)
        if osm_path is None:
            return None
        count = osm_convert.convert_osm_xml(
            osm_path, destination, progress=self.progress, cancel=self.cancel
        )
        if count == 0:
            return None
        return destination

    def _ndjson_to_geojson(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with source.open("r", encoding="utf-8") as reader, destination.open(
            "w", encoding="utf-8"
        ) as writer:
            writer.write('{"type":"FeatureCollection","features":[')
            for line in reader:
                if self.cancel is not None and count % 2000 == 0 and self.cancel.is_set():
                    raise InterruptedError("Preparation cancelled")
                line = line.strip()
                if not line:
                    continue
                if count:
                    writer.write(",")
                writer.write(line)
                count += 1
            writer.write("]}")
        return count

    def _temp_path(self, name, suffix, extension=None):
        self.counter += 1
        return self.config.temp_dir / (
            f"{name}_{suffix}_{self.counter}."
            f"{extension or INTERMEDIATE_EXTENSION}"
        )

    def _dissolve_fields(self, source, fields):
        """Поля растворения, пригодные для данного слоя.

        В слое может не оказаться поля, по которому рецепт хочет
        растворить объекты (например, ``name`` у слоя без названий).
        ``native:dissolve`` в таком случае считает все объекты одним
        значением и сливает слой в единственный объект — вместо десятков
        точек остаётся одна. Если поле отсутствует или пустое у всех
        объектов, растворяем по ``@id`` (идентификатору объекта):
        объекты остаются раздельными.
        """
        if not fields:
            return fields
        available = {name.lower() for name in _source_fields(source)}
        usable = {
            name.lower()
            for name in fields
            if name.lower() in available and _has_values(source, name)
        }
        if not usable:
            if "@id" in available:
                return ["@id"]
            return []
        return [name for name in fields if name.lower() in usable]

    def _step(self, name, current, operation, parameters):
        if operation == "save":
            return self._save(
                name,
                current,
                parameters.get("suffix", ""),
                parameters.get("layer_name"),
                parameters.get("target_crs"),
                parameters.get("fields"),
            )
        if operation == "load":
            path = self.config.vector_dir / f"{parameters['name']}.sqlite"
            if path.exists():
                return path
            self._missing_layer(name, parameters["name"])
            return current
        if operation == "fix":
            return self._algorithm("native:fixgeometries", {
                "INPUT": current,
                "OUTPUT": self._temp_path(
                    name, "fixed", parameters.get("extension")
                ),
            })
        if operation == "dissolve":
            field = parameters.get("field")
            if isinstance(field, str):
                field = [field] if field else []
            elif field is None:
                field = []
            requested = bool(field)
            field = self._dissolve_fields(current, list(field))
            if requested and not field:
                # Растворять не по чему: пустой FIELD слил бы весь слой
                # в один объект.  Оставляем объекты раздельными.
                return current
            return self._algorithm("native:dissolve", {
                "INPUT": current,
                "FIELD": list(field),
                "SEPARATE_DISJOINT": False,
                "OUTPUT": self._temp_path(name, "dissolved"),
            })
        if operation == "singleparts":
            return self._algorithm("native:multiparttosingleparts", {
                "INPUT": current,
                "OUTPUT": self._temp_path(name, "parts"),
            })
        if operation == "simplify":
            return self._algorithm("native:simplifygeometries", {
                "INPUT": current,
                "METHOD": 0,
                "TOLERANCE": parameters["tolerance"],
                "OUTPUT": self._temp_path(name, "simpl"),
            })
        if operation == "smooth":
            return self._algorithm("native:smoothgeometry", {
                "INPUT": current,
                "ITERATIONS": parameters.get("iterations", 5),
                "OFFSET": parameters.get("offset", 0.25),
                "MAX_ANGLE": parameters.get("max_angle", 180),
                "OUTPUT": self._temp_path(name, "smoothed"),
            })
        if operation == "buffer":
            return self._algorithm("native:buffer", {
                "INPUT": current,
                "DISTANCE": parameters["distance"],
                "SEGMENTS": 5,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2,
                "DISSOLVE": parameters.get("dissolve", False),
                "OUTPUT": self._temp_path(name, "buffered"),
            })
        if operation == "centroid":
            return self._algorithm("native:centroids", {
                "INPUT": current,
                "ALL_PARTS": False,
                "OUTPUT": self._temp_path(name, "centroids"),
            })
        if operation == "polygonstolines":
            polygons, lines = _layer_geometry_families(current)
            if not polygons:
                # В вырезе PBF у границ остаются только пограничные
                # линии, без полигонов.  Превращать в линии нечего:
                # алгоритм вернул бы пустые геометрии, и слой пропал бы
                # на следующем шаге.
                return current
            if lines:
                # В смешанном слое линии уже есть (пограничная линия
                # way и полигоны relation).  native:polygonstolines
                # превращает их в пустые геометрии, поэтому контуры и
                # линии собираются вместе вручную.
                return self._polygonstolines_mixed(name, current)
            return self._algorithm("native:polygonstolines", {
                "INPUT": current,
                "OUTPUT": self._temp_path(name, "lines"),
            })
        if operation == "linestopolygons":
            return self._lines_to_polygons(
                name, current, drop_others=parameters.get("drop_others", False)
            )
        if operation == "explode":
            return self._algorithm("native:explodelines", {
                "INPUT": current,
                "OUTPUT": self._temp_path(name, "exploded"),
            })
        if operation == "reproject":
            from qgis.core import QgsCoordinateReferenceSystem

            return self._algorithm("native:reprojectlayer", {
                "INPUT": current,
                "TARGET_CRS": QgsCoordinateReferenceSystem(parameters.get("target_crs", "EPSG:3857")),
                "OUTPUT": self._temp_path(name, "reproj"),
            })
        if operation in ("difference", "intersection"):
            overlay = self._overlay_path(parameters["overlay"])
            if overlay is None:
                # Слой-источник не выбран или не собрался: шаг
                # пропускается, но это видно в журнале и в списке
                # предупреждений — иначе вода молча остаётся в реках.
                self._missing_layer(name, parameters["overlay"])
                return current
            algorithm = "native:difference" if operation == "difference" else "native:intersection"
            if operation == "difference":
                filtered = self._difference_filtered(
                    name, current, overlay
                )
                if filtered is not None:
                    return filtered
            return self._algorithm(algorithm, {
                "INPUT": current,
                "OVERLAY": overlay,
                "OUTPUT": self._temp_path(name, operation),
            })
        if operation == "merge":
            layers = []
            for layer_name in parameters["layers"]:
                path = self._overlay_path(layer_name)
                if path is not None:
                    layers.append(path)
                else:
                    self._missing_layer(name, layer_name)
            if not layers:
                return current
            layers.insert(0, current)
            return self._algorithm("native:mergevectorlayers", {
                "LAYERS": layers,
                "OUTPUT": self._temp_path(name, "merged"),
            })
        if operation == "label_axis":
            return self._label_axis(
                name, current, bool(parameters.get("main_flow", False))
            )
        if operation == "split_layer":
            return self._split_layer(
                name, current, parameters.get("fields")
            )
        if operation == "highway_integrity":
            if not highway_integrity.store(self.config.vector_dir, name):
                # Без столбца фильтры дорог по интегритету не сработают.
                self.warnings.append(
                    f"{name}: highway_integrity не записан в данные"
                )
            return current
        if operation == "oneway":
            if not oneway.store(self.config.vector_dir, name):
                self.warnings.append(
                    f"{name}: oneway не нормализован в данных"
                )
            return current
        if operation == "pack_roads":
            packed = road_packing.pack(
                self.config.vector_dir, name, cancel=self.cancel
            )
            if packed:
                self.progress(f"{name}: упаковано {packed} объектов")
            else:
                self.warnings.append(f"{name}: дороги не упакованы")
            return current
        raise PipelineError(f"Unknown pipeline operation: {operation}")

    def _algorithm(self, algorithm, parameters, allow_empty=False):
        if not self.runner.has_algorithm(algorithm):
            # Шаг без алгоритма пропускать нельзя: слой сохранился бы
            # необработанным под именем обработанного, и пользователь
            # не увидел бы разницы.
            raise PipelineError(f"{algorithm} is unavailable")
        result = self.runner.run(algorithm, parameters)
        output = None
        for key, value in result.items():
            if str(key).lower() == "output":
                output = value
                break
        if output is None:
            output = (
                parameters.get("OUTPUT")
                or parameters.get("output")
                or parameters.get("INPUT")
                or parameters.get("input")
            )
        if (
            not allow_empty
            and isinstance(output, str)
            and not output.startswith(
                ("memory:", "ogr:", "postgres:", "pointcloud:", "mesh:")
            )
        ):
            path = Path(output)
            if not path.exists() or not _layer_has_features(path):
                raise PipelineError(f"{algorithm} produced an empty or unreadable layer")
        return output

    def _save(self, name, current, suffix="", layer_name=None, target_crs=None,
              fields=None):
        destination = self.config.vector_dir / f"{name}{suffix}.sqlite"
        save_vector(
            current,
            destination,
            layer_name or f"{name}{suffix}",
            target_crs=target_crs,
            fields=fields,
        )
        if name in ELEVATION_LAYERS:
            cleared = clear_zero_elevation(destination)
            if cleared:
                self.progress(
                    f"{name}: высота 0 убрана у {cleared} объектов"
                )
        self.progress(f"{name}: saved {destination.name}")
        return current

    def _missing_layer(self, name, reference):
        """Предупреждение о ненайденном слое-источнике шага.

        Шаг без источника не отменяет слой, но и молчать о нём нельзя:
        невычтенная вода или необъединённые потоки выглядят как обычные
        данные.  Если источник пуст законно — в нём не было объектов, —
        вычитать нечего, и предупреждения нет.
        """
        if reference in self.empty_outputs:
            return
        message = (
            f"{name}: слой {reference} не найден, шаг пропущен — "
            "данные могут быть неполными"
        )
        self.warnings.append(message)
        self.progress(f"WARNING: {message}")

    def _overlay_path(self, name):
        if name in self.empty_outputs:
            # Не подхватывать файл от прошлой сборки: источник пуст, и
            # вычитать старые данные нельзя.
            return None
        candidates = [
            self.config.vector_dir / f"{name}.sqlite",
            self.config.vector_dir / f"{name}.geojson",
            self.config.temp_dir / f"{name}.geojson",
            self.config.temp_dir / f"{name}.sqlite",
            self.config.temp_dir / f"{name}.gpkg",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _difference_filtered(self, name, current, overlay):
        """``native:difference`` только для объектов у overlay.

        Алгоритм обрабатывает каждый объект входного слоя, даже если он
        не пересекает overlay: на 15 тыс. ручьёв это около 70 с, хотя
        вода касается только 1,6 тыс.  Здесь объекты делятся по
        пространственному индексу, разность считается для пересекающихся,
        остальные переносятся как есть с тем же мульти-типом, что выдал
        бы алгоритм.  Сама разность кандидатов повторяет
        ``native:difference`` напрямую в OGR (unary union и Difference),
        а processing остаётся запасным путём на случай ошибки GEOS.

        Возвращает путь к готовому слою или ``None``, если деление
        невыгодно — тогда вызывающий считает разность целиком.
        """
        from osgeo import ogr
        from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex

        source = ogr.Open(str(current))
        if source is None:
            return None
        layer = source.GetLayer(0)
        if layer is None:
            return None
        definition = layer.GetLayerDefn()
        id_index = definition.GetFieldIndex("@id")
        if id_index < 0:
            return None
        # Деление проверено на линейных слоях.  У точек native:difference
        # ведёт себя иначе: полный слой оставляет точки внутри полигонов,
        # а разность по подмножеству их убирает, поэтому для точечных
        # слоёв считается как раньше.
        polygons, lines = _layer_geometry_families(current)
        if polygons or not lines:
            return None
        known_count = layer.GetFeatureCount()
        if 0 <= known_count < DIFFERENCE_SPLIT_MIN_FEATURES:
            return None
        overlay_source = ogr.Open(str(overlay))
        if overlay_source is None:
            return None
        overlay_layer = overlay_source.GetLayer(0)
        if overlay_layer is None:
            return None
        overlay_count = overlay_layer.GetFeatureCount()
        if (
            overlay_count <= 0
            or overlay_count > DIFFERENCE_SPLIT_MAX_OVERLAY
        ):
            return None

        index = QgsSpatialIndex()
        geometries = []
        sources = []
        prepared = []
        insert = getattr(index, "addFeature", None) or index.insertFeature
        for position, feature in enumerate(overlay_layer):
            if self.cancel is not None and position % 500 == 0 and self.cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            qgis_geometry = QgsGeometry()
            qgis_geometry.fromWkb(geometry.ExportToIsoWkb())
            if qgis_geometry.isNull():
                continue
            item = QgsFeature(len(geometries))
            item.setGeometry(qgis_geometry)
            insert(item)
            geometries.append(qgis_geometry)
            # Подготовленная геометрия ссылается на исходную, поэтому
            # нужна копия: объекты слоя живут только до следующего чтения.
            clone = geometry.Clone()
            sources.append(clone)
            prepared.append(_prepared_geometry(clone))
        overlay_layer = None
        overlay_source = None
        if not geometries:
            return None

        candidates_path = self._temp_path(
            name, "difference_candidates", "gpkg"
        )
        candidates_path.unlink(missing_ok=True)
        writer = _GpkgWriter(
            candidates_path, name, definition, layer.GetSpatialRef(),
            cancel=self.cancel,
        )
        candidate_ids = set()
        direct_geometries = {}
        direct_ok = True
        seen_ids = set()
        total = 0
        abort = False
        try:
            for feature in layer:
                if (
                    self.cancel is not None
                    and total % 500 == 0
                    and self.cancel.is_set()
                ):
                    raise InterruptedError("Preparation cancelled")
                total += 1
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    abort = True
                    break
                identifier = feature.GetField(id_index)
                if identifier in seen_ids:
                    # Повторяющийся @id склеил бы два объекта в один.
                    abort = True
                    break
                seen_ids.add(identifier)
                qgis_geometry = QgsGeometry()
                qgis_geometry.fromWkb(geometry.ExportToIsoWkb())
                if qgis_geometry.isNull():
                    abort = True
                    break
                found = False
                matches = []
                for position in sorted(index.intersects(
                    qgis_geometry.boundingBox()
                )):
                    check = prepared[position]
                    if check is not None:
                        hit = check.Intersects(geometry)
                    else:
                        hit = geometries[position].intersects(qgis_geometry)
                    if hit:
                        found = True
                        matches.append(sources[position])
                if found:
                    writer.add(feature)
                    candidate_ids.add(identifier)
                    if direct_ok:
                        try:
                            cut = _difference_lines(geometry, matches)
                        except Exception:  # noqa: BLE001 - ниже обычный путь
                            # Неподходящая геометрия overlay или сбой GEOS:
                            # слой считается алгоритмом целиком, как раньше.
                            direct_ok = False
                            direct_geometries.clear()
                        else:
                            if cut is not None:
                                direct_geometries[identifier] = cut
            if abort:
                writer.close(commit=False)
                candidates_path.unlink(missing_ok=True)
                return None
            writer.close()
        except Exception:
            writer.close(commit=False)
            candidates_path.unlink(missing_ok=True)
            raise
        finally:
            layer = None
            source = None
            prepared = None
            sources = None

        if (
            total < DIFFERENCE_SPLIT_MIN_FEATURES
            or not candidate_ids
            or len(candidate_ids) == total
        ):
            candidates_path.unlink(missing_ok=True)
            return None
        self.progress(
            f"{name}: разность только для {len(candidate_ids)} "
            f"из {total} объектов"
        )

        difference_geometries = {}
        target_type = None
        if direct_ok:
            # Разность уже посчитана по ходу отбора кандидатов: промежуточный
            # слой и вызов processing не нужны.
            candidates_path.unlink(missing_ok=True)
            difference_geometries = direct_geometries
            target_type = ogr.wkbMultiLineString
        else:
            difference_path = self._algorithm(
                "native:difference",
                {
                    "INPUT": str(candidates_path),
                    "OVERLAY": str(overlay),
                    "OUTPUT": str(self._temp_path(name, "difference_candidates")),
                },
                allow_empty=True,
            )
            if difference_path and Path(difference_path).exists():
                difference_source = ogr.Open(str(difference_path))
                if difference_source is not None:
                    difference_layer = difference_source.GetLayer(0)
                    if difference_layer is not None:
                        target_type = difference_layer.GetGeomType()
                        difference_index = (
                            difference_layer.GetLayerDefn().GetFieldIndex("@id")
                        )
                        if difference_index < 0:
                            difference_layer = None
                            difference_source = None
                            candidates_path.unlink(missing_ok=True)
                            return None
                        for feature in difference_layer:
                            geometry = feature.GetGeometryRef()
                            if geometry is not None and not geometry.IsEmpty():
                                difference_geometries[
                                    feature.GetField(difference_index)
                                ] = geometry.Clone()
                    difference_layer = None
                    difference_source = None
        if target_type in (None, ogr.wkbUnknown):
            target_type = _multi_type_for_layer(current)

        source = ogr.Open(str(current))
        if source is None:
            return None
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        merged_path = self._temp_path(name, "difference")
        merged_path.unlink(missing_ok=True)
        writer = _GpkgWriter(
            merged_path, name, definition, layer.GetSpatialRef(),
            geometry_type=target_type, cancel=self.cancel,
        )
        written = 0
        processed = 0
        try:
            for feature in layer:
                processed += 1
                if (
                    self.cancel is not None
                    and processed % 500 == 0
                    and self.cancel.is_set()
                ):
                    raise InterruptedError("Preparation cancelled")
                identifier = feature.GetField(id_index)
                if identifier in candidate_ids:
                    geometry = difference_geometries.get(identifier)
                    if geometry is None:
                        # Объект полностью ушёл в воду.
                        continue
                    writer.add(feature, geometry)
                else:
                    writer.add(
                        feature,
                        _promote_geometry(
                            feature.GetGeometryRef(), target_type
                        ),
                    )
                written += 1
            writer.close()
        except Exception:
            writer.close(commit=False)
            merged_path.unlink(missing_ok=True)
            raise
        finally:
            layer = None
            source = None
        if not written:
            merged_path.unlink(missing_ok=True)
            raise PipelineError(
                f"{name}: difference removed every object"
            )
        return merged_path

    def _polygonstolines_mixed(self, name, current):
        """Контуры полигонов и уже существующие линии в одном слое.

        ``native:polygonstolines`` возвращает для линий пустые
        геометрии, поэтому в смешанном слое пограничная линия way
        пропадала рядом с полигонами relation.  Здесь контур полигона и
        исходная линия пишутся в общий линейный слой.
        """
        from osgeo import ogr

        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        spatial_ref = layer.GetSpatialRef()
        path = self._temp_path(name, "lines")
        path.unlink(missing_ok=True)
        writer = _GpkgWriter(
            path, name, definition, spatial_ref,
            geometry_type=ogr.wkbMultiLineString, cancel=self.cancel,
        )
        converted = 0
        kept = 0
        try:
            for feature in layer:
                geometry = feature.GetGeometryRef()
                parts = _line_parts(geometry)
                if not parts:
                    continue
                multi = ogr.Geometry(ogr.wkbMultiLineString)
                for part in parts:
                    multi.AddGeometry(part)
                writer.add(feature, multi)
                if _is_polygonal(geometry):
                    converted += 1
                else:
                    kept += 1
            writer.close()
        except Exception:
            writer.close(commit=False)
            raise
        finally:
            source = None
        if not writer.count:
            raise PipelineError(
                "no objects left after converting polygons to lines"
            )
        self.progress(
            f"{name}: полигонов преобразовано в линии: {converted}, "
            f"линий сохранено: {kept}"
        )
        return writer.path

    def _lines_to_polygons(self, name, current, drop_others=False):
        """Замкнутые линии превращает в полигоны.

        Площадные дороги и площади OSM заданы замкнутым контуром, но
        драйвер отдаёт их линиями: слой с заливкой такой контур не рисует.
        Остальные геометрии переносятся без изменений, а с drop_others=True
        отбрасываются: площадный слой (ледники) должен состоять только из
        полигонов — разомкнутый контур ледника это ошибка данных, на карте
        он всё равно не рисуется, а в слое портит тип геометрии.
        """
        from osgeo import ogr

        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        spatial_ref = layer.GetSpatialRef()
        path = self._temp_path(name, "polygons")
        path.unlink(missing_ok=True)
        writer = _GpkgWriter(path, name, definition, spatial_ref,
                              cancel=self.cancel)
        converted = 0
        dropped = 0
        try:
            for feature in layer:
                if self.cancel is not None and converted % 500 == 0 and self.cancel.is_set():
                    raise InterruptedError("Preparation cancelled")
                geometry = feature.GetGeometryRef()
                polygon = _polygon_from_geometry(geometry)
                if polygon is not None:
                    converted += 1
                    writer.add(feature, polygon)
                    continue
                if drop_others and not _is_polygonal(geometry):
                    dropped += 1
                    continue
                writer.add(feature, None)
            writer.close()
        except Exception:
            writer.close(commit=False)
            raise
        finally:
            source = None
        if not writer.count:
            raise PipelineError("no objects left after converting lines to polygons")
        self.progress(f"{name}: converted {converted} closed lines to polygons")
        if dropped:
            self.progress(
                f"{name}: отброшено {dropped} объектов без полигональной геометрии"
            )
        return writer.path

    def _label_axis(self, name, current, main_flow=False):
        """Линия подписи вдоль полигона: свой алгоритм вместо скелета GRASS."""
        from osgeo import ogr

        from .label_line import label_line_geometry

        path = self.config.temp_dir / f"{name}_axis.gpkg"
        path.unlink(missing_ok=True)
        # Линия подписи — чистая функция геометрии слоя, поэтому при
        # повторной подготовке она берётся из кэша.
        key = label_cache.cache_key(self.config, name, current, main_flow)
        if label_cache.restore(self.config, key, path):
            self.progress(f"{name}: label lines restored from cache")
            return path
        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        spatial_ref = layer.GetSpatialRef()
        writer = _GpkgWriter(
            path, name, definition, spatial_ref,
            geometry_type=ogr.wkbMultiLineString,
            cancel=self.cancel,
        )
        built = 0
        # Одинаковые названия у разных relation — дубликаты OSM. Второй
        # контур с тем же именем и почти той же геометрией пропускаем,
        # иначе на карте появляются две подписи друг над другом.
        seen = {}
        try:
            for feature in layer:
                if self.cancel is not None and built % 200 == 0 and self.cancel.is_set():
                    raise InterruptedError("Preparation cancelled")
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                label_name = _label_name(feature)
                outline = None
                if label_name:
                    try:
                        outline = (geometry.GetEnvelope(), geometry.GetArea())
                    except Exception:  # noqa: BLE001 - битая геометрия не дубликат
                        outline = None
                    if outline is not None and any(
                            _same_outline(outline, other)
                            for other in seen.get(label_name, ())):
                        continue
                line = label_line_geometry(geometry, main_flow=main_flow)
                if line is None:
                    continue
                if outline is not None:
                    seen.setdefault(label_name, []).append(outline)
                # Шаблон проекта ждёт мультилинию, как у прежнего скелета.
                multi = ogr.Geometry(ogr.wkbMultiLineString)
                multi.AddGeometry(line)
                writer.add(feature, multi)
                built += 1
        except Exception:
            writer.close(commit=False)
            raise
        finally:
            source = None
        writer.close()
        if not writer.count:
            raise PipelineError(f"{name}: no label lines built")
        label_cache.store(self.config, key, path)
        self.progress(f"{name}: label lines for {built} objects")
        return writer.path

    def _split_layer(self, name, current, fields=None):
        """Делит слой на три группы по значению тега layer.

        Фильтр по слою уходит в GDAL, и каждая группа сразу пишется в
        SQLite.  Промежуточный GeoPackage со всей копией слоя стоил
        дороже самого деления: его приходилось читать трижды.
        """
        from osgeo import ogr

        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        has_layer = layer.GetLayerDefn().GetFieldIndex("layer") >= 0
        source = None
        groups = (
            ("", '(CAST("layer" AS INTEGER) = 0 OR "layer" IS NULL)'),
            ("layer_1", 'CAST("layer" AS INTEGER) > 0'),
            ("layer_-1", 'CAST("layer" AS INTEGER) < 0'),
        )
        if not has_layer:
            groups = (("", None),)
        base_path = None
        for suffix, where in groups:
            layer_name = name if not suffix else f"{name}_{suffix}"
            destination = (
                self.config.vector_dir / f"{layer_name}.sqlite"
            )
            save_vector(
                current, destination, layer_name,
                fields=fields, where=where,
            )
            if not _layer_has_features(destination):
                destination.unlink(missing_ok=True)
                continue
            if not suffix:
                base_path = destination
            self.progress(f"{name}: saved {suffix or 'base'} layer")
        if base_path is None:
            raise PipelineError("no features left after splitting by layer")
        return base_path


def _ring_points(line):
    """Кольцо замкнутой линии; None, если линия не замкнута."""
    from osgeo import ogr

    count = line.GetPointCount()
    if count < 4:
        return None
    first = line.GetPoint(0)
    last = line.GetPoint(count - 1)
    if abs(first[0] - last[0]) > 1e-9 or abs(first[1] - last[1]) > 1e-9:
        return None
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for index in range(count):
        x, y = line.GetPoint(index)[:2]
        ring.AddPoint_2D(x, y)
    return ring


def _is_polygonal(geometry):
    """Полигон или мультиполигон (непустой)."""
    return (
        geometry is not None
        and not geometry.IsEmpty()
        and geometry.GetGeometryName() in ("POLYGON", "MULTIPOLYGON")
    )


def _difference_lines(geometry, overlay_geometries):
    """Разность линии с объединением пересекающихся полигонов.

    Повторяет шаги ``native:difference``: полигоны overlay объединяются
    GEOS-функцией unary union, затем из линии вычитается результат.
    Прямой вызов OGR дешевле алгоритма QGIS: overlay не перечитывается
    на каждый объект и не создаётся промежуточный слой.
    """
    from osgeo import ogr

    polygons = ogr.Geometry(ogr.wkbMultiPolygon)
    for other in overlay_geometries:
        name = other.GetGeometryName()
        if name == "POLYGON":
            polygons.AddGeometry(other)
        elif name == "MULTIPOLYGON":
            for index in range(other.GetGeometryCount()):
                polygons.AddGeometry(other.GetGeometryRef(index))
        else:
            raise TypeError(f"unexpected overlay geometry: {name}")
    merged = polygons.UnionCascaded()
    return _difference_result(geometry.Difference(merged))


def _difference_result(geometry):
    """Линейный результат разности в MultiLineString, как у QGIS."""
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return None
    name = geometry.GetGeometryName()
    if name == "LINESTRING":
        result = ogr.Geometry(ogr.wkbMultiLineString)
        result.AddGeometry(geometry.Clone())
        return result
    if name == "MULTILINESTRING":
        return geometry.Clone()
    if name != "GEOMETRYCOLLECTION":
        # QGIS оставляет из коллекции только линейные части.
        return None
    result = ogr.Geometry(ogr.wkbMultiLineString)
    _add_line_parts(geometry, result)
    if result.GetGeometryCount() == 0:
        return None
    return result


def _add_line_parts(geometry, result):
    name = geometry.GetGeometryName()
    if name == "LINESTRING":
        result.AddGeometry(geometry.Clone())
    elif name in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
        for index in range(geometry.GetGeometryCount()):
            _add_line_parts(geometry.GetGeometryRef(index), result)


def _promote_geometry(geometry, target_type):
    """Геометрия в мульти-тип, который выдаёт ``native:difference``.

    QGIS возвращает результат разности линий как MultiLineString,
    площадей — как MultiPolygon, даже если объект не пересекался с
    overlay.  При склейке результата с нетронутыми объектами тип надо
    привести к тому же виду, иначе итоговый слой станет смешанным.
    """
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return None
    if target_type in (ogr.wkbUnknown, ogr.wkbGeometryCollection):
        return geometry.Clone()
    name = geometry.GetGeometryName()
    expected = {
        ogr.wkbMultiLineString: "MULTILINESTRING",
        ogr.wkbMultiPolygon: "MULTIPOLYGON",
        ogr.wkbMultiPoint: "MULTIPOINT",
        ogr.wkbLineString: "LINESTRING",
        ogr.wkbPolygon: "POLYGON",
        ogr.wkbPoint: "POINT",
    }.get(target_type)
    if expected is None or name == expected:
        return geometry.Clone()
    multi = {
        "LINESTRING": (ogr.wkbMultiLineString, ogr.wkbLineString),
        "POLYGON": (ogr.wkbMultiPolygon, ogr.wkbPolygon),
        "POINT": (ogr.wkbMultiPoint, ogr.wkbPoint),
    }.get(name)
    if multi is None:
        return geometry.Clone()
    multi_type, single_type = multi
    if geometry.GetGeometryType() != single_type:
        return geometry.Clone()
    result = ogr.Geometry(multi_type)
    result.AddGeometry(geometry.Clone())
    return result


def _multi_type_for_layer(path):
    """Мульти-тип, соответствующий семейству геометрий слоя."""
    from osgeo import ogr

    polygons, lines = _layer_geometry_families(path)
    if polygons and not lines:
        return ogr.wkbMultiPolygon
    if lines and not polygons:
        return ogr.wkbMultiLineString
    return ogr.wkbUnknown


def _line_parts(geometry):
    """Линейные части геометрии; у полигонов — их границы."""
    if geometry is None or geometry.IsEmpty():
        return []
    name = geometry.GetGeometryName()
    if name in ("POLYGON", "MULTIPOLYGON"):
        return _line_parts(geometry.Boundary())
    if name == "LINESTRING":
        return [geometry.Clone()]
    if name == "MULTILINESTRING":
        return [
            geometry.GetGeometryRef(index).Clone()
            for index in range(geometry.GetGeometryCount())
        ]
    if name == "GEOMETRYCOLLECTION":
        parts = []
        for index in range(geometry.GetGeometryCount()):
            parts.extend(_line_parts(geometry.GetGeometryRef(index)))
        return parts
    return []


def _polygon_from_geometry(geometry):
    """Полигон из замкнутой линии.

    None, если геометрия не является замкнутой линией: полигоны и точки
    остаются без изменений.
    """
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return None
    name = geometry.GetGeometryName()
    if name == "LINESTRING":
        ring = _ring_points(geometry)
        if ring is None:
            return None
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        return polygon
    if name == "MULTILINESTRING":
        rings = []
        for index in range(geometry.GetGeometryCount()):
            ring = _ring_points(geometry.GetGeometryRef(index))
            if ring is None:
                return None
            rings.append(ring)
        if not rings:
            return None
        multi = ogr.Geometry(ogr.wkbMultiPolygon)
        for ring in rings:
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            multi.AddGeometry(polygon)
        return multi
    return None


# Маркеры типа геометрии в GeoJSON.  Разбор всех объектов через OGR
# стоит около секунды на сотню тысяч объектов, а нужно знать только,
# какие типы встречаются в файле.
_GEOJSON_TYPE_MARKERS = {
    "POINT": ("Point", "MultiPoint"),
    "LINESTRING": ("LineString", "MultiLineString"),
    "POLYGON": ("Polygon", "MultiPolygon"),
}

# Результат определения типа геометрии по файлу: один и тот же файл
# сохраняется несколько раз (save() и save("_smoothed")), а определение
# типа обходом всех объектов стоит секунды.
_GEOMETRY_TYPE_CACHE = {}
_GEOMETRY_TYPE_CACHE_LIMIT = 256


def _geojson_families(path):
    """Семейства геометрий в GeoJSON-файле (поиском по содержимому).

    None — файл не GeoJSON или прочитать его не удалось: тогда семейства
    определяются обычным обходом объектов.
    """
    needles = {}
    for family, names in _GEOJSON_TYPE_MARKERS.items():
        marks = []
        for name in names:
            marks.append(f'"type": "{name}"'.encode("utf-8"))
            marks.append(f'"type":"{name}"'.encode("utf-8"))
        needles[family] = marks
    families = set()
    try:
        with open(path, "rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(4 * 1024 * 1024)
                if not chunk:
                    break
                data = tail + chunk
                for family, marks in needles.items():
                    if family in families:
                        continue
                    if any(mark in data for mark in marks):
                        families.add(family)
                tail = data[-64:]
    except OSError:
        return None
    return families or None


def _geometry_families(layer):
    """Семейства геометрий, реально присутствующие в объектах слоя.

    Заголовок слоя GeoJSON со смешанными геометриями отражает тип лишь
    части объектов (например, только way-линий рядом с relation-полигонами):
    если по нему выбрать -nlt для VectorTranslate, полигоны сплющатся
    в линии и перестанут рисоваться. Поэтому тип проверяем по объектам.
    """
    from osgeo import ogr

    families = set()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        code = geometry.GetGeometryType() & 0x7FFFFFFF
        if code in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
            families.add("POLYGON")
        elif code in (ogr.wkbLineString, ogr.wkbMultiLineString):
            families.add("LINESTRING")
        elif code in (ogr.wkbPoint, ogr.wkbMultiPoint):
            families.add("POINT")
        else:
            families.add("GEOMETRY")
        if len(families) > 1:
            break
    return families


def _geometry_type(source):
    """Тип геометрии слоя для -nlt.

    Определение обходом всех объектов повторялось при каждом сохранении
    одного и того же файла, поэтому результат запоминается по файлу.
    """
    path = Path(source)
    key = None
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        key = None
    if key is not None:
        cached = _GEOMETRY_TYPE_CACHE.get(key)
        if cached is not None:
            return cached
    result = _compute_geometry_type(source)
    if key is not None:
        if len(_GEOMETRY_TYPE_CACHE) >= _GEOMETRY_TYPE_CACHE_LIMIT:
            _GEOMETRY_TYPE_CACHE.clear()
        _GEOMETRY_TYPE_CACHE[key] = result
    return result


def _compute_geometry_type(source):
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(source))
        if dataset is None:
            return "GEOMETRY"
        layer = dataset.GetLayer(0)
        name = ogr.GeometryTypeToName(layer.GetGeomType())
        result = {
            "Point": "POINT",
            "Multi Point": "MULTIPOINT",
            "Line String": "LINESTRING",
            "Multi Line String": "MULTILINESTRING",
            "Polygon": "POLYGON",
            "Multi Polygon": "MULTIPOLYGON",
        }.get(name, "GEOMETRY")
        if result != "GEOMETRY":
            header_family = {
                "POINT": "POINT", "MULTIPOINT": "POINT",
                "LINESTRING": "LINESTRING", "MULTILINESTRING": "LINESTRING",
                "POLYGON": "POLYGON", "MULTIPOLYGON": "POLYGON",
            }[result]
            families = None
            if str(source).lower().endswith((".geojson", ".json")):
                families = _geojson_families(source)
            if families is None:
                families = _geometry_families(layer)
            # Заголовок противоречит реальному содержимому (смешанный
            # файл): единый -nlt искажал бы часть объектов, пишем слой
            # без фиксации типа — QGIS отфильтрует нужный по URI слоя.
            if families - {header_family}:
                result = "GEOMETRY"
        dataset = None
    except Exception:
        return "GEOMETRY"
    return result


def _has_values(source, field):
    """Есть ли у объектов слоя непустое значение поля."""
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(source))
        if dataset is None:
            return False
        layer = dataset.GetLayer(0)
        if layer is None:
            return False
        definition = layer.GetLayerDefn()
        index = -1
        for position in range(definition.GetFieldCount()):
            if definition.GetFieldDefn(position).GetName() == field:
                index = position
                break
        if index < 0:
            return False
        for feature in layer:
            value = feature.GetField(index)
            if value not in (None, ""):
                return True
        return False
    except Exception:
        return False


def _source_fields(source):
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(source))
        if dataset is None:
            return []
        layer = dataset.GetLayer(0)
        if layer is None:
            return []
        definition = layer.GetLayerDefn()
        return [
            definition.GetFieldDefn(index).GetName()
            for index in range(definition.GetFieldCount())
        ]
    except Exception:
        return []
    finally:
        dataset = None


# Слои, где высота OSM участвует в подписи.  Нулевая высота там —
# ошибка ввода, а не уровень моря: подпись должна взять высоту рельефа
# (виртуальное поле ele_calc).
ELEVATION_LAYERS = frozenset({
    "peak", "hill", "volcano", "volcano_dirt", "mountain_pass",
})


def clear_zero_elevation(path):
    """Заменяет ele=0 на NULL, возвращает число исправленных объектов.

    OSM иногда содержит ``ele=0.0`` у вершины (ошибка ввода).  Подписи
    берут ``ele``, только если оно не NULL, иначе — ``ele_calc``; NULL
    заставляет их показать высоту рельефа вместо «0 м».
    """
    from osgeo import ogr

    ogr.UseExceptions()
    dataset = ogr.Open(str(path), 1)
    if dataset is None:
        return 0
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return 0
        index = layer.GetLayerDefn().GetFieldIndex("ele")
        if index < 0:
            return 0
        fids = []
        for feature in layer:
            value = feature.GetField(index)
            if value in (None, ""):
                continue
            try:
                number = float(str(value).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if number == 0:
                fids.append(feature.GetFID())
        for fid in fids:
            feature = layer.GetFeature(fid)
            if feature is None:
                continue
            feature.SetFieldNull(index)
            layer.SetFeature(feature)
        layer.SyncToDisk()
        return len(fids)
    finally:
        dataset = None


def save_vector(source, destination, layer_name, target_crs=None,
                fields=None, where=None):
    """Переводит источник в SQLite, при необходимости с фильтром и полями."""
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    ogr.UseExceptions()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Пишем во временный файл и подменяем готовый: упавшая запись не
    # должна стирать прежний слой.
    temporary = destination.with_name(
        f"{destination.stem}.part{destination.suffix}"
    )
    temporary.unlink(missing_ok=True)
    options = [
        "-nln", layer_name,
        "-nlt", _geometry_type(source),
        "-unsetFid",
    ]
    names = _source_fields(source)
    if fields:
        keep = field_subset.select(fields, names)
    else:
        keep = [
            name for name in names
            if name.lower() not in ("ogc_fid", "fid", "objectid")
        ]
    if keep and len(keep) != len(names):
        options.extend(["-select", ",".join(keep)])
    creation_options = gdal.GetDriverByName("SQLite").GetMetadataItem("DMD_CREATIONOPTIONLIST") or ""
    if "SPATIALITE" in creation_options:
        # COMPRESS_GEOM=YES вместе с SPATIALITE=YES меняет топологию:
        # в воде появляются самопересечения, и difference пропускает
        # объект. Экономия от сжатия около 3%, поэтому не сжимаем.
        options.extend(["-dsco", "SPATIALITE=YES"])
    if target_crs:
        options.extend(["-t_srs", target_crs])
    if where:
        options.extend(["-where", where])
    try:
        result = gdal.VectorTranslate(str(temporary), str(source), options=options)
    except Exception:
        result = None
    if result is None:
        # -skipfailures отключает пакетную запись в SQLite: каждая
        # геометрия сохраняется отдельной транзакцией, а перенос замедляется
        # в пять раз. Поэтому основной путь идёт без него, а запасной — с ним.
        temporary.unlink(missing_ok=True)
        try:
            result = gdal.VectorTranslate(
                str(temporary), str(source), options=options + ["-skipfailures"]
            )
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise PipelineError(f"Failed to write {destination}: {exc}") from exc
    if result is None:
        temporary.unlink(missing_ok=True)
        raise PipelineError(f"Failed to write {destination}")
    result = None
    try:
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PipelineError(f"Failed to write {destination}: {exc}") from exc


def _clean_temp(config):
    for path in config.temp_dir.rglob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _join_thread(thread, timeout=None):
    """Ждёт поток, но не бесконечно.

    Поток подготовки сам проверяет отмену и выходит быстро, поэтому
    ждать его можно ограниченное время: зависший поток не должен
    держать закрытие панели.
    """
    if thread is None:
        return
    thread.join(timeout)


def _use_project_temp(config):
    """Держать временные файлы подготовки в каталоге проекта.

    GDAL иначе пишет крупные промежуточные файлы в системный каталог
    временных файлов: он может быть мал или очищаться при перезагрузке.
    """
    directory = config.temp_dir
    directory.mkdir(parents=True, exist_ok=True)
    try:
        from osgeo import gdal
    except ImportError:
        return
    gdal.SetConfigOption("CPL_TMPDIR", str(directory))


def run_project(config, progress=None, cancel=None, percent=None, status=None):
    notify = progress or (lambda message: None)
    tracker = _ProgressTracker(
        progress_stages(config),
        percent,
        queries=len(config.array_queries),
        status=status,
    )
    report = tracker.wrap(notify)
    timings = Timings()
    report("Проверка параметров")
    config.validate()
    # Каталоги нужны и подготовке источника, которая теперь стартует
    # раньше создания проекта.
    config.ensure_directories()
    # Места на диске может не хватить под карты, растры и слои: об этом
    # лучше узнать в начале, а не после часа работы.
    free = disk_space.free_bytes(config.project_dir)
    if free is not None:
        report(f"Свободно на диске: {disk_space.human_size(free)}")
        if free < disk_space.GIB:
            report(
                "WARNING: На диске почти нет места: свободно "
                f"{disk_space.human_size(free)}. Подготовка может прерваться."
            )
    _use_project_temp(config)
    gdal_log.LOG.reset()
    bbox = parse_bbox(config.bbox)
    # Слои новой сборки готовятся в отдельном каталоге: прежний проект
    # остаётся целым, пока сборка не завершится успешно.  Ошибка или
    # отмена больше не оставляют проект без данных.
    project.begin_staging(config)
    committed = False
    # Временный каталог чистим до параллельного запуска: подготовка
    # источника OSM пишет туда вырезки PBF, и поздняя уборка удалила бы
    # их прямо во время работы.
    _clean_temp(config)
    prepared = None
    source_thread = None
    source_error = []
    source_result = {}
    source_seconds = [0.0]
    terrain_thread = None
    terrain_error = []
    terrain_keys = {}
    terrain_used = []
    terrain_seconds = [0.0]
    try:
        report("Создание проекта QGIS")
        # Создание проекта и подготовка источника OSM друг от друга не
        # зависят: первая пишет qgz и служебные файлы, вторая читает
        # карты.  Параллельный запуск убирает из холодной подготовки
        # лишнюю очередь из двух долгих стадий.  Сообщение о стадии
        # источника идёт последним: пока потоки работают, трекер должен
        # показывать именно её.
        if config.array_queries:
            report("Подготовка источника OSM")

            def _source_worker():
                started_source = time.monotonic()
                try:
                    gdal_log.LOG.install()
                    try:
                        source_result["prepared"] = sources.prepare_sources(
                            config, bbox, config.array_queries, cancel=cancel,
                            progress=report, percent=tracker.download_percent,
                        )
                    finally:
                        gdal_log.LOG.restore()
                except InterruptedError:
                    pass  # отмену проверяет основной поток после join
                except Exception as exc:  # noqa: BLE001 - всплывёт после join
                    source_error.append(exc)
                finally:
                    source_seconds[0] = time.monotonic() - started_source

            source_thread = threading.Thread(
                target=_source_worker, name="xtopo-sources", daemon=True,
            )
            source_thread.start()
        started = time.monotonic()
        project_path = project.prepare_project(config)
        timings.add_stage("создание проекта", time.monotonic() - started)
        # Скачивание тайлов и сборка растра рельефа не зависят ни от
        # данных OSM, ни от слоёв проекта.  Поток рельефа запускается
        # сразу после создания проекта: сеть за тайлами DEM работает,
        # пока готовится источник OSM.  Изолинии зависят от слоёв
        # (режутся по воде и ледникам) — их pipeline догоняет после
        # pipeline.run.
        if config.generate_terrain:
            def _terrain_worker():
                started_terrain = time.monotonic()
                try:
                    gdal_log.LOG.install()
                    try:
                        terrain_keys["dem"] = terrain.prepare_terrain_raster(
                            config, bbox, progress=report, cancel=cancel,
                            status=tracker.status, used_out=terrain_used,
                        )
                    finally:
                        gdal_log.LOG.restore()
                except InterruptedError:
                    pass  # отмену проверяет основной поток после join
                except Exception as exc:  # noqa: BLE001 - всплывёт после join
                    terrain_error.append(exc)
                finally:
                    # Время самого рельефа: ожидание слоёв в счёт не
                    # идёт, рельеф считается параллельно им.
                    terrain_seconds[0] = time.monotonic() - started_terrain
            terrain_thread = threading.Thread(
                target=_terrain_worker, name="xtopo-terrain", daemon=True,
            )
            terrain_thread.start()
        if source_thread is not None:
            source_thread.join()
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            if source_error:
                raise source_error[0]
            prepared = source_result["prepared"]
            # Скачивание карт и ответов Overpass считаем отдельно:
            # в «источник OSM» остаётся только подготовка данных.
            download, prepared_time = _source_timings(
                source_seconds[0], getattr(prepared, "download_seconds", 0.0)
            )
            if download >= 0.05:
                timings.add_stage("скачивание данных", download)
            timings.add_stage("источник OSM", prepared_time)
        else:
            prepared = sources.SourceResult()
        for note in prepared.notes:
            report(note)
        for warning in prepared.warnings:
            report(f"WARNING: {warning}")
        report("Обработка слоёв")
        runner = ProcessingRunner(
            progress=report, cancel=cancel, fraction=tracker.fraction
        )
        pipeline = Pipeline(
            config, bbox, runner, progress=report, cancel=cancel,
            fraction=tracker.fraction, timings=timings,
        )
        started = time.monotonic()
        warnings = pipeline.run(prepared, config.array_queries)
        timings.add_stage(_LAYER_STAGE, time.monotonic() - started)
        # Предупреждения конвейера не должны теряться: по ним видно,
        # какие слои собраны не полностью.
        for warning in warnings or ():
            report(f"WARNING: {warning}")
        if terrain_thread is not None:
            _report_terrain_stage(terrain_thread, report)
            terrain_thread.join()
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            if terrain_error:
                raise terrain_error[0]
            timings.add_stage("рельеф", terrain_seconds[0])
            gdal_log.LOG.install()
            try:
                terrain.prepare_terrain_isolines(
                    config, bbox, terrain_keys.get("dem"),
                    progress=report, cancel=cancel,
                )
            finally:
                gdal_log.LOG.restore()
        if "coastline" in config.array_queries:
            report("Обработка береговой линии")
            started = time.monotonic()
            gdal_log.LOG.install()
            try:
                coastline.prepare_coastline(
                    config, bbox, progress=report, cancel=cancel
                )
            finally:
                gdal_log.LOG.restore()
            timings.add_stage("береговая линия", time.monotonic() - started)
        if "peak" in config.array_queries:
            report("Проверка пиков")
            started = time.monotonic()
            gdal_log.LOG.install()
            try:
                peak_filter.filter_peak_layer(
                    config, bbox, progress=report, cancel=cancel,
                    fraction=tracker.fraction,
                    dem_key=terrain_keys.get("dem"),
                )
            except InterruptedError:
                raise
            except Exception as exc:
                report(f"WARNING: Пики: {exc}; проверка пропущена")
            finally:
                gdal_log.LOG.restore()
            timings.add_stage("проверка пиков", time.monotonic() - started)
        if config.resolve_polygon_overlaps:
            report("Пересечения полигонов")
            started = time.monotonic()
            gdal_log.LOG.install()
            try:
                polygon_overlaps.resolve_polygon_overlaps(
                    config, progress=report, cancel=cancel,
                )
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 - карта не должна страдать
                report(
                    i18n.trf(
                        "WARNING: пересечения полигонов: {}; шаг пропущен",
                        exc,
                    )
                )
            finally:
                gdal_log.LOG.restore()
            timings.add_stage("пересечения полигонов", time.monotonic() - started)
        report("Размещение значков POI")
        started = time.monotonic()
        try:
            poi_icons.build_poi_icons(
                config, progress=report, cancel=cancel,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            # Без служебного слоя try(...) в правилах оставляет все
            # значки видимыми, поэтому ошибка не отменяет сборку карты.
            report(f"WARNING: POI: {exc}; подавление наложений пропущено")
        timings.add_stage("значки POI", time.monotonic() - started)
        report("Углы поворота значков")
        started = time.monotonic()
        try:
            icon_angles.compute_icon_angles(
                config, progress=report, cancel=cancel,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            # Без готового угла значок останется неповёрнутым: карта
            # собирается, поворот пересчитается при следующей сборке.
            report(f"WARNING: углы значков: {exc}; поворот пропущен")
        timings.add_stage("углы значков", time.monotonic() - started)
        # Фильтры рендерера по площади должны уходить в SQL, а не
        # считаться для каждого объекта в кадре.  Поле добавляется после
        # всех шагов, которые переписывают слои, и до очистки проекта.
        report("Поле площади")
        started = time.monotonic()
        area_field.ensure_area_fields(config.vector_dir)
        timings.add_stage("поле площади", time.monotonic() - started)
        report("Подсыхание ручьёв")
        started = time.monotonic()
        try:
            stats = stream_drying.apply(
                config.vector_dir, cancel=cancel, progress=report,
            )
            if stats.get("warning"):
                report(i18n.trf(
                    "WARNING: источник ручья: {}; защита пропущена",
                    stats["warning"],
                ))
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 - карта не должна страдать
            report(i18n.trf(
                "WARNING: подсыхание ручьёв: {}; шаг пропущен", exc,
            ))
            try:
                stream_drying.ensure_visible(config.vector_dir)
            except Exception as fallback_exc:  # noqa: BLE001
                report(i18n.trf(
                    "WARNING: ручьи не удалось оставить видимыми: {}",
                    fallback_exc,
                ))
        timings.add_stage("подсыхание ручьёв", time.monotonic() - started)
        report("Упаковка площадей, зданий и линий")
        started = time.monotonic()
        road_packing.pack_all(
            config.vector_dir, cancel=cancel, progress=report,
        )
        timings.add_stage(
            "упаковка площадей, зданий и линий", time.monotonic() - started
        )
        report("Удаление пустых слоёв")
        started = time.monotonic()
        # Атрибуция наборов DEM пишется здесь же: отдельная перезапись
        # архива проекта стоила бы ещё полминуты.
        attribution = ""
        if terrain_used:
            attribution = dem_sources.attribution_for_keys(terrain_used)
        project.clean_project_layers(project_path, dem_attribution=attribution)
        timings.add_stage("очистка проекта", time.monotonic() - started)
        for line in timings.summary():
            report(line)
        steps = timings.top_steps()
        if steps:
            report(i18n.tr("Хронометраж: самые долгие операции:"))
            for seconds, name, operation in steps:
                report(
                    i18n.trf(
                        "{} [{}] — {} с",
                        name, operation, f"{seconds:.1f}",
                    )
                )
        for line in gdal_log.LOG.summary():
            report(line)
        # Готово: прежние слои заменяются собранными.
        project_path = project.commit_staging(config)
        committed = True
        tracker.finish()
    finally:
        if source_thread is not None and source_thread.is_alive():
            # Ошибка или отмена до join: поток источника дорабатывает.
            # Без ожидания уборка временного каталога оборвала бы его
            # файлы на полуслове.
            _join_thread(source_thread, 300)
        if terrain_thread is not None and terrain_thread.is_alive():
            # Отмена или ошибка до join: поток рельефа дорабатывает
            # (функции рельефа сами реагируют на cancel).
            _join_thread(terrain_thread, 300)
        if prepared is not None and prepared.dataset is not None:
            prepared.dataset.close()
        _clean_temp(config)
        if not committed:
            # Сборка не удалась: прежние слои остаются на месте, а
            # недоделанная сборка убирается.
            project.discard_staging(config)
    return project_path
