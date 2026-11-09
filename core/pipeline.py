import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import (
    coastline, gdal_log, i18n, osm_convert, peak_filter, poi_icons, project,
    sources, terrain,
)
from .bbox import parse_bbox
from .query_recipes import recipe_for


class PipelineError(RuntimeError):
    pass


class Timings:
    """Хронометраж подготовки: время этапов и самых долгих шагов.

    Данные нужны, чтобы понимать, на что уходит время: выводятся в журнал
    в конце подготовки.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.stages = {}
        self.steps = []

    def add_stage(self, name, seconds):
        with self._lock:
            self.stages[name] = self.stages.get(name, 0.0) + seconds

    def add_step(self, name, operation, seconds):
        with self._lock:
            self.steps.append((seconds, name, operation))

    def top_steps(self, limit=15):
        with self._lock:
            steps = sorted(self.steps, reverse=True)[:limit]
        return steps

    def summary(self):
        with self._lock:
            stages = dict(self.stages)
            total_steps = sum(item[0] for item in self.steps)
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
            lines.append(i18n.trf("Хронометраж: {}", parts))
            lines.append(
                i18n.trf(
                    "Хронометраж: суммарно в слоях {} с",
                    f"{total_steps:.1f}",
                )
            )
        return lines


_TERRAIN_STEPS = (
    ("Preparing terrain", 0.05),
    ("Downloading", 0.15),
    ("Merging", 0.35),
    ("Reprojecting", 0.5),
    ("Generating slope", 0.65),
    ("Upscaling", 0.75),
    ("Generating hillshade", 0.85),
    ("Generating contour lines", 0.95),
)

# Формат промежуточных данных при обработке слоёв. GeoPackage с
# пространственным индексом заметно быстрее geojson: JSON не разбирается
# заново на каждом шаге, а наложения ищутся по индексу.
INTERMEDIATE_EXTENSION = os.environ.get(
    "QGIS_XTOPO_INTERMEDIATE", "gpkg"
).lower()


def progress_stages(config):
    """Stage weights for the preparation progress bar."""
    stages = [("Проверка параметров", 1), ("Создание проекта QGIS", 4)]
    if config.array_queries:
        stages.append(("Подготовка источника OSM", 30))
    stages.append(("Обработка слоёв", 45))
    if "coastline" in config.array_queries:
        stages.append(("Обработка береговой линии", 4))
    if config.generate_terrain:
        stages.append(("Обработка рельефа", 14))
    if "peak" in config.array_queries:
        stages.append(("Проверка пиков", 3))
    stages.append(("Удаление пустых слоёв", 2))
    return stages


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


class ProcessingRunner:
    def __init__(self, progress=None, cancel=None, fraction=None):
        import os
        import sys

        try:
            import processing
        except ImportError:
            from qgis.core import QgsApplication

            prefix = QgsApplication.prefixPath()
            for path in (
                os.path.join(prefix, "python", "plugins"),
                "/usr/share/qgis/python/plugins",
            ):
                if path not in sys.path:
                    sys.path.append(path)
            import processing
        from processing.core.Processing import Processing

        Processing.initialize()
        self.processing = processing
        self.progress = progress
        self.cancel = cancel
        self.fraction = fraction
        self.feedback = Feedback(
            progress=progress, cancel=cancel,
            fraction=lambda value: self.fraction and self.fraction(value),
        ).instance

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
        return other

    def has_algorithm(self, algorithm):
        QgsApplication, _ = _qgis()
        return QgsApplication.processingRegistry().algorithmById(algorithm) is not None

    def run(self, algorithm, parameters):
        if not self.has_algorithm(algorithm):
            raise PipelineError(f"QGIS algorithm is not available: {algorithm}")
        try:
            return self.processing.run(algorithm, _stringify(parameters), feedback=self.feedback)
        except Exception as exc:
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


def _recipe_outputs(name):
    outputs = {name}
    for operation, parameters in recipe_for(name).steps:
        if operation == "save":
            suffix = parameters.get("suffix") or ""
            outputs.add(parameters.get("layer_name") or f"{name}{suffix}")
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


class _GpkgWriter:
    """Потоковая запись в GeoPackage одной транзакцией."""

    def __init__(self, path, layer_name, definition, spatial_ref=None):
        from osgeo import ogr

        self.path = Path(path)
        self.count = 0
        self.driver = ogr.GetDriverByName("GPKG")
        self.dataset = self.driver.CreateDataSource(str(self.path))
        self.layer = self.dataset.CreateLayer(
            layer_name, spatial_ref, ogr.wkbUnknown
        )
        for index in range(definition.GetFieldCount()):
            field = definition.GetFieldDefn(index)
            self.layer.CreateField(ogr.FieldDefn(field.GetName(), field.GetType()))
        # Иначе GPKG фиксирует каждый объект отдельной транзакцией.
        self.layer.StartTransaction()

    def add(self, feature, geometry=None):
        from osgeo import ogr

        definition = self.layer.GetLayerDefn()
        output = ogr.Feature(definition)
        if geometry is None:
            geometry = feature.GetGeometryRef()
        if geometry is not None:
            output.SetGeometry(geometry)
        for index in range(definition.GetFieldCount()):
            value = feature.GetField(index)
            if value is not None:
                output.SetField(index, value)
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
                 fraction=None, timings=None):
        self.config = config
        self.bbox = bbox
        self.runner = runner
        self.progress = progress or (lambda message: None)
        self.cancel = cancel
        self.fraction = fraction or (lambda value: None)
        self.warnings = []
        self.counter = 0
        self.timings = timings or Timings()

    def run(self, source, query_names):
        if not query_names:
            return self.warnings
        workers = self._worker_count(len(query_names))
        if workers <= 1 or len(query_names) == 1:
            total = len(query_names)
            for index, name in enumerate(query_names, start=1):
                self._run_layer(name, source, index, total)
                self.fraction(index / total)
            return self.warnings
        return self._run_parallel(source, query_names, workers)

    def _worker_count(self, total):
        """Сколько слоёв обрабатывать одновременно."""
        configured = int(getattr(self.config, "parallel_workers", 0) or 0)
        if configured > 0:
            return max(1, min(configured, total))
        cpu = os.cpu_count() or 2
        # Оставляем пару ядер системе и самому QGIS.
        return max(1, min(6, cpu - 2, total))

    def _run_layer(self, name, source, index, total):
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
                self.progress(f"{name}: no objects, skipped")
                return
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
        finally:
            gdal_log.LOG.restore()

    def _run_parallel(self, source, query_names, workers):
        producers = {}
        for name in query_names:
            for output in _recipe_outputs(name):
                producers.setdefault(output, name)
        dependencies = {
            name: _recipe_dependencies(name, producers) for name in query_names
        }
        serial = {
            name for name in query_names
            if any(operation == "skeleton" for operation, _ in recipe_for(name).steps)
        }

        lock = threading.Lock()

        def report(message):
            with lock:
                self.progress(message)

        total = len(query_names)
        started = 0
        completed = set()
        pending = list(query_names)
        running = {}
        warnings = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while pending or running:
                if self.cancel is not None and self.cancel.is_set():
                    for future in running:
                        future.cancel()
                    raise InterruptedError("Preparation cancelled")

                serial_busy = any(
                    name in serial for name, _task in running.values()
                )
                for name in list(pending):
                    if len(running) >= workers:
                        break
                    if not dependencies[name].issubset(completed):
                        continue
                    if name in serial and serial_busy:
                        continue
                    pending.remove(name)
                    started += 1
                    task = Pipeline(
                        self.config, self.bbox, self.runner.clone(),
                        progress=report, cancel=self.cancel,
                        fraction=self.fraction, timings=self.timings,
                    )
                    future = pool.submit(task._run_layer, name, source, started, total)
                    running[future] = (name, task)
                    if name in serial:
                        serial_busy = True

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
                            report(f"{name}: skipped ({exc})")
                        else:
                            warnings.extend(task.warnings)
                        completed.add(name)
                        self.fraction(len(completed) / total)
                elif pending:
                    name = pending.pop(0)
                    started += 1
                    self._run_layer(name, source, started, total)
                    warnings.extend(self.warnings)
                    self.warnings = []
                    completed.add(name)
                    self.fraction(len(completed) / total)

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

    @staticmethod
    def _ndjson_to_geojson(source, destination):
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with source.open("r", encoding="utf-8") as reader, destination.open(
            "w", encoding="utf-8"
        ) as writer:
            writer.write('{"type":"FeatureCollection","features":[')
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                if count:
                    writer.write(",")
                writer.write(line)
                count += 1
            writer.write("]}")
        return count

    def _temp_path(self, name, suffix):
        self.counter += 1
        return self.config.temp_dir / (
            f"{name}_{suffix}_{self.counter}.{INTERMEDIATE_EXTENSION}"
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
            )
        if operation == "load":
            path = self.config.vector_dir / f"{parameters['name']}.sqlite"
            return path if path.exists() else current
        if operation == "fix":
            return self._algorithm("native:fixgeometries", {
                "INPUT": current,
                "OUTPUT": self._temp_path(name, "fixed"),
            })
        if operation == "dissolve":
            field = parameters.get("field")
            if isinstance(field, str):
                field = [field] if field else []
            elif field is None:
                field = []
            field = self._dissolve_fields(current, list(field))
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
                self.progress(f"{name}: overlay {parameters['overlay']} not found, skipped")
                return current
            algorithm = "native:difference" if operation == "difference" else "native:intersection"
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
            if not layers:
                return current
            layers.insert(0, current)
            return self._algorithm("native:mergevectorlayers", {
                "LAYERS": layers,
                "OUTPUT": self._temp_path(name, "merged"),
            })
        if operation == "skeleton":
            if not self.runner.has_algorithm("grass7:v.voronoi.skeleton"):
                self.warnings.append(
                    f"{name}: GRASS v.voronoi.skeleton is unavailable; names layer skipped"
                )
                return current
            original = _source_fields(current)
            output = self._algorithm("grass7:v.voronoi.skeleton", {
                "input": current,
                "smoothness": parameters.get("smoothness", 0.25),
                "thin": parameters.get("thin", -1),
                "-s": True,
                "output": self._temp_path(name, "skel"),
            })
            return self._restore_field_names(name, output, original)
        if operation == "split_layer":
            return self._split_layer(name, current)
        raise PipelineError(f"Unknown pipeline operation: {operation}")

    def _algorithm(self, algorithm, parameters):
        if not self.runner.has_algorithm(algorithm):
            self.warnings.append(f"{algorithm} is unavailable, step skipped")
            return parameters.get("INPUT") or parameters.get("input")
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
        if isinstance(output, str) and not output.startswith(
            ("memory:", "ogr:", "postgres:", "pointcloud:", "mesh:")
        ):
            path = Path(output)
            if not path.exists() or not _layer_has_features(path):
                raise PipelineError(f"{algorithm} produced an empty or unreadable layer")
        return output

    def _restore_field_names(self, name, path, original_names):
        """Возвращает слою исходные имена полей.

        GRASS при импорте атрибутов переименовывает поля: «name:ru» становится
        «name_ru», «@id» — «xid». Подписи в шаблоне проекта ищут поля по
        исходным именам тегов OSM, поэтому после скелета имена возвращаются.
        """
        from osgeo import ogr

        if not original_names:
            return path
        source = ogr.Open(str(path))
        if source is None:
            return path
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        fields = [
            definition.GetFieldDefn(index).GetName()
            for index in range(definition.GetFieldCount())
        ]
        reverse = {}
        for original in original_names:
            mangled = _grass_field_name(original)
            if mangled != original and mangled not in reverse:
                reverse[mangled] = original
        renames = {
            field: reverse[field]
            for field in fields
            if field in reverse and reverse[field] not in fields
        }
        if not renames:
            source = None
            return path
        destination = self._temp_path(name, "fields")
        destination.unlink(missing_ok=True)
        driver = ogr.GetDriverByName("GPKG")
        output = driver.CreateDataSource(str(destination))
        target = output.CreateLayer(
            layer.GetName(), layer.GetSpatialRef(), layer.GetGeomType()
        )
        for index in range(definition.GetFieldCount()):
            field = definition.GetFieldDefn(index)
            target.CreateField(
                ogr.FieldDefn(
                    renames.get(field.GetName(), field.GetName()),
                    field.GetType(),
                )
            )
        for feature in layer:
            item = ogr.Feature(target.GetLayerDefn())
            for index in range(definition.GetFieldCount()):
                item.SetField(index, feature.GetField(index))
            geometry = feature.GetGeometryRef()
            if geometry is not None:
                item.SetGeometry(geometry)
            target.CreateFeature(item)
        output = None
        source = None
        return destination

    def _save(self, name, current, suffix="", layer_name=None, target_crs=None):
        destination = self.config.vector_dir / f"{name}{suffix}.sqlite"
        save_vector(
            current,
            destination,
            layer_name or f"{name}{suffix}",
            target_crs=target_crs,
        )
        self.progress(f"{name}: saved {destination.name}")
        return current

    def _overlay_path(self, name):
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
        writer = _GpkgWriter(path, name, definition, spatial_ref)
        converted = 0
        dropped = 0
        try:
            for feature in layer:
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

    def _split_layer(self, name, current):
        """Делит слой на три группы по значению тега layer."""
        from osgeo import ogr

        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        definition = layer.GetLayerDefn()
        spatial_ref = layer.GetSpatialRef()
        writers = {}
        try:
            for suffix in ("", "layer_1", "layer_-1"):
                path = self.config.temp_dir / (
                    f"{name}{('_' + suffix) if suffix else ''}.gpkg"
                )
                path.unlink(missing_ok=True)
                writers[suffix] = _GpkgWriter(path, name, definition, spatial_ref)
            for feature in layer:
                index = feature.GetFieldIndex("layer")
                value = feature.GetField(index) if index >= 0 else None
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    suffix = "layer_1"
                elif value < 0:
                    suffix = "layer_-1"
                else:
                    suffix = ""
                writers[suffix].add(feature)
            for writer in writers.values():
                writer.close()
        except Exception:
            for writer in writers.values():
                writer.close(commit=False)
            raise
        finally:
            source = None

        base_path = current
        for suffix, writer in writers.items():
            if not writer.count:
                continue
            layer_name = name if not suffix else f"{name}_{suffix}"
            save_vector(
                writer.path,
                self.config.vector_dir / f"{layer_name}.sqlite",
                layer_name,
            )
            if not suffix:
                base_path = writer.path
            self.progress(f"{name}: saved {suffix or 'base'} layer")
        return base_path


def _grass_field_name(name):
    """Имя поля после импорта в GRASS.

    GRASS допускает в именах только латиницу, цифры и подчёркивание: первый
    недопустимый символ заменяется на «x», остальные — на «_». Так «name:ru»
    превращается в «name_ru», а «@id» — в «xid».
    """
    if not name:
        return name
    result = []
    for position, char in enumerate(name):
        if char.isascii() and (char.isalnum() or char == "_"):
            result.append(char)
        else:
            result.append("x" if position == 0 else "_")
    if result and result[0].isdigit():
        result[0] = "x"
    return "".join(result)


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
            # Заголовок противоречит реальному содержимому (смешанный
            # файл): единый -nlt искажал бы часть объектов, пишем слой
            # без фиксации типа — QGIS отфильтрует нужный по URI слоя.
            if _geometry_families(layer) - {header_family}:
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


def save_vector(source, destination, layer_name, target_crs=None):
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    ogr.UseExceptions()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    options = [
        "-nln", layer_name,
        "-nlt", _geometry_type(source),
        "-lco", "COMPRESS_GEOM=YES",
        "-unsetFid",
    ]
    names = _source_fields(source)
    keep = [
        name for name in names
        if name.lower() not in ("ogc_fid", "fid", "objectid")
    ]
    if keep and len(keep) != len(names):
        options.extend(["-select", ",".join(keep)])
    creation_options = gdal.GetDriverByName("SQLite").GetMetadataItem("DMD_CREATIONOPTIONLIST") or ""
    if "SPATIALITE" in creation_options:
        options.extend(["-dsco", "SPATIALITE=YES"])
    if target_crs:
        options.extend(["-t_srs", target_crs])
    try:
        result = gdal.VectorTranslate(str(destination), str(source), options=options)
    except Exception:
        result = None
    if result is None:
        # -skipfailures отключает пакетную запись в SQLite: каждая
        # геометрия сохраняется отдельной транзакцией, а перенос замедляется
        # в пять раз. Поэтому основной путь идёт без него, а запасной — с ним.
        destination.unlink(missing_ok=True)
        try:
            result = gdal.VectorTranslate(
                str(destination), str(source), options=options + ["-skipfailures"]
            )
        except Exception as exc:
            raise PipelineError(f"Failed to write {destination}: {exc}") from exc
    if result is None:
        raise PipelineError(f"Failed to write {destination}")
    result = None


def _clean_temp(config):
    for path in config.temp_dir.rglob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)


# Промежуточные файлы DEM — это кэш, а не слои проекта: их сохраняем,
# чтобы не пересобирать репроекцию DEM при каждой подготовке.
_RASTER_CACHE_PREFIX = "merged_dem"


def _clean_layers(config):
    """Удаляет ранее подготовленные векторные и растровые слои проекта.

    Возвращает число удалённых файлов.
    """
    removed = 0
    for directory, keep_cache in (
        (config.vector_dir, False),
        (config.raster_dir, True),
    ):
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if keep_cache and path.name.startswith(_RASTER_CACHE_PREFIX):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


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
    gdal_log.LOG.reset()
    bbox = parse_bbox(config.bbox)
    report("Очистка прежних слоёв")
    _clean_layers(config)
    report("Создание проекта QGIS")
    started = time.monotonic()
    project_path = project.prepare_project(config)
    _clean_temp(config)
    timings.add_stage("создание проекта", time.monotonic() - started)
    prepared = None
    try:
        if config.array_queries:
            report("Подготовка источника OSM")
            started = time.monotonic()
            # Чтение PBF идёт в главном потоке, поэтому перехватчик ставим
            # здесь: без него предупреждения драйвера OSM уходят в консоль.
            gdal_log.LOG.install()
            try:
                prepared = sources.prepare_sources(
                    config, bbox, config.array_queries, cancel=cancel,
                    progress=report, percent=tracker.download_percent,
                )
            finally:
                gdal_log.LOG.restore()
            elapsed = time.monotonic() - started
            # Скачивание карт и ответов Overpass считаем отдельно:
            # в «источник OSM» остаётся только подготовка данных.
            download, prepared_time = _source_timings(
                elapsed, getattr(prepared, "download_seconds", 0.0)
            )
            if download >= 0.05:
                timings.add_stage("скачивание данных", download)
            timings.add_stage("источник OSM", prepared_time)
        else:
            prepared = sources.SourceResult()
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
        pipeline.run(prepared, config.array_queries)
        timings.add_stage("обработка слоёв", time.monotonic() - started)
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
        if config.generate_terrain:
            report("Обработка рельефа")
            started = time.monotonic()
            gdal_log.LOG.install()
            try:
                terrain.prepare_terrain(
                    config, bbox, progress=report, cancel=cancel,
                    status=tracker.status,
                )
            finally:
                gdal_log.LOG.restore()
            timings.add_stage("рельеф", time.monotonic() - started)
        if "peak" in config.array_queries:
            report("Проверка пиков")
            started = time.monotonic()
            gdal_log.LOG.install()
            try:
                peak_filter.filter_peak_layer(
                    config, bbox, progress=report, cancel=cancel,
                    fraction=tracker.fraction,
                )
            except InterruptedError:
                raise
            except Exception as exc:
                report(f"WARNING: Пики: {exc}; проверка пропущена")
            finally:
                gdal_log.LOG.restore()
            timings.add_stage("проверка пиков", time.monotonic() - started)
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
        report("Удаление пустых слоёв")
        started = time.monotonic()
        project.clean_project_layers(project_path)
        timings.add_stage("очистка проекта", time.monotonic() - started)
        for line in timings.summary():
            report(line)
        for seconds, name, operation in timings.top_steps():
            report(
                i18n.trf(
                    "Хронометраж: {} [{}] — {} с",
                    name, operation, f"{seconds:.1f}",
                )
            )
        for line in gdal_log.LOG.summary():
            report(line)
        tracker.finish()
    finally:
        if prepared is not None and prepared.dataset is not None:
            prepared.dataset.close()
        _clean_temp(config)
    return project_path
