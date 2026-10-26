import json
import os
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import coastline, osm_convert, project, sources, terrain
from .bbox import parse_bbox
from .query_recipes import recipe_for


class PipelineError(RuntimeError):
    pass


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
    stages.append(("Удаление пустых слоёв", 2))
    return stages


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


class Pipeline:
    def __init__(self, config, bbox, runner, progress=None, cancel=None,
                 fraction=None):
        self.config = config
        self.bbox = bbox
        self.runner = runner
        self.progress = progress or (lambda message: None)
        self.cancel = cancel
        self.fraction = fraction or (lambda value: None)
        self.warnings = []
        self.counter = 0

    def run(self, source, query_names):
        if not query_names:
            return self.warnings
        workers = min(2, os.cpu_count() or 1)
        if workers <= 1 or len(query_names) == 1:
            total = len(query_names)
            for index, name in enumerate(query_names, start=1):
                self._run_layer(name, source, index, total)
                self.fraction(index / total)
            return self.warnings
        return self._run_parallel(source, query_names, workers)

    def _run_layer(self, name, source, index, total):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        if total:
            self.runner.fraction = (
                lambda value, index=index, total=total:
                self.fraction((index - 1 + value) / total)
            )
        self.progress(f"[{index}/{total}] {name}")
        geojson = self._source_geojson(name, source)
        if geojson is None:
            self.progress(f"{name}: no objects, skipped")
            return
        current = geojson
        try:
            for operation, parameters in recipe_for(name).steps:
                current = self._step(name, current, operation, parameters)
        except PipelineError as exc:
            self.warnings.append(f"{name}: {exc}; layer skipped")
            self.progress(f"{name}: skipped ({exc})")

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
                        fraction=self.fraction,
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
    def _write_features(path, features):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(
                {"type": "FeatureCollection", "features": features},
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )

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
        return self.config.temp_dir / f"{name}_{suffix}_{self.counter}.geojson"

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
            return self._algorithm("grass7:v.voronoi.skeleton", {
                "input": current,
                "smoothness": parameters.get("smoothness", 0.25),
                "thin": parameters.get("thin", -1),
                "-s": True,
                "output": self._temp_path(name, "skel"),
            })
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
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _split_layer(self, name, current):
        from osgeo import ogr

        source = ogr.Open(str(current))
        if source is None:
            raise PipelineError(f"Cannot open {current}")
        layer = source.GetLayer(0)
        groups = {"": [], "layer_1": [], "layer_-1": []}
        for feature in layer:
            index = feature.GetFieldIndex("layer")
            value = feature.GetField(index) if index >= 0 else None
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                groups["layer_1"].append(feature.ExportToJson())
            elif value < 0:
                groups["layer_-1"].append(feature.ExportToJson())
            else:
                groups[""].append(feature.ExportToJson())
        source = None

        base_path = current
        for suffix, raw_features in groups.items():
            if not raw_features:
                continue
            features = [json.loads(item) for item in raw_features]
            path = self.config.temp_dir / f"{name}{('_' + suffix) if suffix else ''}.geojson"
            self._write_features(path, features)
            if suffix == "":
                base_path = path
                save_vector(path, self.config.vector_dir / f"{name}.sqlite", name)
            else:
                layer_name = f"{name}_{suffix}"
                save_vector(path, self.config.vector_dir / f"{layer_name}.sqlite", layer_name)
            self.progress(f"{name}: saved {suffix or 'base'} layer")
        return base_path


def _geometry_type(source):
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(source))
        if dataset is None:
            return "GEOMETRY"
        layer = dataset.GetLayer(0)
        name = ogr.GeometryTypeToName(layer.GetGeomType())
        dataset = None
    except Exception:
        return "GEOMETRY"
    return {
        "Point": "POINT",
        "Multi Point": "MULTIPOINT",
        "Line String": "LINESTRING",
        "Multi Line String": "MULTILINESTRING",
        "Polygon": "POLYGON",
        "Multi Polygon": "MULTIPOLYGON",
    }.get(name, "GEOMETRY")


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
        "-skipfailures",
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
    result = gdal.VectorTranslate(str(destination), str(source), options=options)
    if result is None:
        raise PipelineError(f"Failed to write {destination}")
    result = None


def _clean_temp(config):
    for path in config.temp_dir.rglob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def run_project(config, progress=None, cancel=None, percent=None, status=None):
    notify = progress or (lambda message: None)
    tracker = _ProgressTracker(
        progress_stages(config),
        percent,
        queries=len(config.array_queries),
        status=status,
    )
    report = tracker.wrap(notify)
    report("Проверка параметров")
    config.validate()
    bbox = parse_bbox(config.bbox)
    report("Создание проекта QGIS")
    project_path = project.prepare_project(config)
    _clean_temp(config)
    prepared = None
    try:
        if config.array_queries:
            report("Подготовка источника OSM")
            prepared = sources.prepare_sources(
                config, bbox, config.array_queries, cancel=cancel, progress=report,
                percent=tracker.download_percent,
            )
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
            fraction=tracker.fraction,
        )
        pipeline.run(prepared, config.array_queries)
        if "coastline" in config.array_queries:
            report("Обработка береговой линии")
            coastline.prepare_coastline(
                config, bbox, progress=report, cancel=cancel
            )
        if config.generate_terrain:
            report("Обработка рельефа")
            terrain.prepare_terrain(
                config, bbox, progress=report, cancel=cancel,
                status=tracker.status,
            )
        report("Удаление пустых слоёв")
        project.clean_project_layers(project_path)
        tracker.finish()
    finally:
        if prepared is not None and prepared.dataset is not None:
            prepared.dataset.close()
        _clean_temp(config)
    return project_path
