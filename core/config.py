import re
from dataclasses import dataclass, field
from pathlib import Path

from . import dem_sources, i18n
from .overpass_servers import DEFAULT_OVERPASS_SERVERS
from .resources import resource_path

PROJECT_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "0123456789_+-"
)


def default_projects_dir():
    return str(Path.home() / "qgis_projects")


def normalize_project_name(value):
    return (value or "").strip()


def project_name_error(value):
    invalid = sorted(
        {char for char in (value or "") if char not in PROJECT_NAME_CHARS}
    )
    if not invalid:
        return ""
    readable = ", ".join(
        i18n.tr("пробел") if char == " " else f"«{char}»" for char in invalid
    )
    return (
        f"Уберите из имени проекта недопустимые символы: {readable}. "
        "Разрешены буквы (латиница и кириллица), цифры и _ - +."
    )


def _strip_comment(line):
    quote = None
    result = []
    for char in line:
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None:
            break
        result.append(char)
    return "".join(result)


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_bash_config(path):
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"<<'?#COMMENT#'?.*?#COMMENT#", "", text, flags=re.DOTALL)
    lines = text.splitlines()
    result = {}
    index = 0
    while index < len(lines):
        line = _strip_comment(lines[index]).strip()
        index += 1
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if value.startswith("("):
            buffer = value
            while ")" not in buffer and index < len(lines):
                buffer += " " + _strip_comment(lines[index]).strip()
                index += 1
            start = buffer.find("(") + 1
            end = buffer.rfind(")")
            content = buffer[start:end] if end >= start else buffer[start:]
            result[key] = content.split()
        else:
            result[key] = _unquote(value)
    return result


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def default_queries():
    path = Path(resource_path("config.ini"))
    if path.exists():
        return read_bash_config(path).get("array_queries", [])
    return []


@dataclass
class Config:
    qgis_projects_dir: str = field(default_factory=default_projects_dir)
    project_name: str = "automap"
    bbox: str = ""
    array_queries: list = field(default_factory=default_queries)

    source_mode: str = "geofabrik"
    pbf_path: str = ""
    geofabrik_maps: list = field(default_factory=list)
    overpass_endpoint: str = DEFAULT_OVERPASS_SERVERS[0].url
    overpass_endpoints: list = field(
        default_factory=lambda: [server.url for server in DEFAULT_OVERPASS_SERVERS]
    )
    overpass_instance: str = "external"
    overpass_endpoint_external: str = DEFAULT_OVERPASS_SERVERS[0].url
    overpass_endpoint_local: str = ""
    overpass_endpoint_ssh: str = ""

    generate_terrain: bool = False
    terrain_source: str = ""
    download_terrain_tiles: bool = False
    get_terrain_tiles: bool = False
    terrain_src_dir: str = ""
    generate_terrain_hillshade_slope: bool = True
    terrain_resample_method: str = "cubicspline"
    generate_terrain_isolines: bool = True
    isolines_step: int = 10
    smooth_isolines: bool = False
    manual_coastline_processing: bool = False

    # Сколько слоёв обрабатывается одновременно (0 — выбрать автоматически).
    parallel_workers: int = 0

    source_version: str = "0.7.0"

    def __post_init__(self):
        self.project_name = normalize_project_name(self.project_name)

    @property
    def projects_dir(self):
        return Path(self.qgis_projects_dir).expanduser()

    @property
    def config_dir(self):
        return self.projects_dir / "qgisxtopo-config"

    @property
    def project_dir(self):
        return self.projects_dir / self.project_name

    @property
    def vector_dir(self):
        return self.project_dir / "vector"

    @property
    def raster_dir(self):
        return self.project_dir / "raster"

    @property
    def terrain_input_dir(self):
        return self.project_dir / "input_terrain"

    @property
    def temp_dir(self):
        return self.project_dir / "tmp"

    @property
    def osm_data_dir(self):
        return self.project_dir / "osm_data"

    @property
    def cache_dir(self):
        return self.osm_data_dir / "cache"

    @property
    def geofabrik_dir(self):
        return self.projects_dir / "qgisxtopo-cache" / "geofabrik"

    @property
    def terrain_cache_dir(self):
        return self.projects_dir / "qgisxtopo-cache" / "srtm"

    @property
    def terrain_result_cache_dir(self):
        return self.projects_dir / "qgisxtopo-cache" / "terrain"

    def resolved_terrain_source(self):
        """Ключ источника рельефа.

        В старых config.ini поля terrain_source нет: если включён
        get_terrain_tiles, это ручной каталог, иначе SRTM.
        """
        if self.terrain_source:
            return self.terrain_source
        if self.get_terrain_tiles:
            return dem_sources.MANUAL_SOURCE
        return dem_sources.DEFAULT_SOURCE

    def validate(self):
        if not self.project_name:
            raise ValueError("Укажите имя проекта")
        error = project_name_error(self.project_name)
        if error:
            raise ValueError(error)
        if not self.bbox:
            raise ValueError("Bounding box is required")
        if self.source_mode not in ("geofabrik", "overpass", "pbf", "local", "ssh"):
            raise ValueError(f"Unknown source mode: {self.source_mode}")
        if self.source_mode == "pbf" and self.array_queries and not self.pbf_path:
            raise ValueError("PBF path is required for the local extract mode")
        if not self.array_queries and not self.generate_terrain:
            raise ValueError("Select at least one query or enable terrain processing")
        if self.generate_terrain and self.download_terrain_tiles and self.get_terrain_tiles:
            raise ValueError("download_terrain_tiles and get_terrain_tiles are mutually exclusive")
        if self.generate_terrain:
            source = self.resolved_terrain_source()
            if source not in dem_sources.SOURCE_KEYS:
                raise ValueError(f"Unknown terrain source: {source}")
            if source == dem_sources.MANUAL_SOURCE and not self.terrain_src_dir:
                raise ValueError("Укажите каталог DEM для источника «Свой каталог DEM»")

    @classmethod
    def from_file(cls, path):
        defaults = cls()
        values = read_bash_config(path)
        if not values:
            return defaults
        instance = values.get("overpass_instance", defaults.overpass_instance)
        source_mode = values.get("source_mode")
        if not source_mode:
            source_mode = {
                "external": "overpass",
                "docker": "overpass",
                "local": "local",
                "ssh": "ssh",
            }.get(instance, "overpass")
        return cls(
            qgis_projects_dir=values.get("qgis_projects_dir", defaults.qgis_projects_dir),
            project_name=values.get("project_name", defaults.project_name),
            bbox=values.get("bbox", defaults.bbox),
            array_queries=values.get("array_queries", defaults.array_queries),
            source_mode=source_mode,
            pbf_path=values.get("pbf_path", defaults.pbf_path),
            geofabrik_maps=values.get("geofabrik_maps", defaults.geofabrik_maps),
            overpass_endpoint=values.get(
                "overpass_endpoint",
                values.get("overpass_endpoint_external", defaults.overpass_endpoint),
            ),
            overpass_instance=instance,
            overpass_endpoint_external=values.get(
                "overpass_endpoint_external", defaults.overpass_endpoint_external
            ),
            overpass_endpoint_local=values.get("overpass_endpoint_local", ""),
            overpass_endpoint_ssh=values.get("overpass_endpoint_ssh", ""),
            generate_terrain=_as_bool(values.get("generate_terrain")),
            terrain_source=values.get("terrain_source", ""),
            download_terrain_tiles=_as_bool(values.get("download_terrain_tiles")),
            get_terrain_tiles=_as_bool(values.get("get_terrain_tiles")),
            terrain_src_dir=values.get("terrain_src_dir", ""),
            generate_terrain_hillshade_slope=_as_bool(
                values.get("generate_terrain_hillshade_slope"), True
            ),
            terrain_resample_method=values.get("terrain_resample_method", "cubicspline"),
            generate_terrain_isolines=_as_bool(values.get("generate_terrain_isolines"), True),
            isolines_step=_as_int(values.get("isolines_step"), 10),
            smooth_isolines=_as_bool(values.get("smooth_isolines")),
            manual_coastline_processing=_as_bool(values.get("manual_coastline_processing")),
            parallel_workers=_as_int(values.get("parallel_workers"), 0),
        )

    def to_file(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        queries = " ".join(self.array_queries)
        maps = " ".join(self.geofabrik_maps)
        content = f"""#!/bin/bash
# Generated by QGIS-xtopo {self.source_version}

qgis_projects_dir="{self.qgis_projects_dir}"
project_name="{self.project_name}"
bbox="{self.bbox}"

array_queries=(
{queries}
)

geofabrik_maps=(
{maps}
)

source_mode={self.source_mode}
pbf_path="{self.pbf_path}"
overpass_instance={self.overpass_instance}
overpass_endpoint="{self.overpass_endpoint}"
overpass_endpoint_external="{self.overpass_endpoint_external}"
overpass_endpoint_local="{self.overpass_endpoint_local}"
overpass_endpoint_ssh="{self.overpass_endpoint_ssh}"

generate_terrain={str(self.generate_terrain).lower()}
terrain_source={self.resolved_terrain_source()}
download_terrain_tiles={str(self.download_terrain_tiles).lower()}
get_terrain_tiles={str(self.get_terrain_tiles).lower()}
terrain_src_dir="{self.terrain_src_dir}"
generate_terrain_hillshade_slope={str(self.generate_terrain_hillshade_slope).lower()}
terrain_resample_method={self.terrain_resample_method}
generate_terrain_isolines={str(self.generate_terrain_isolines).lower()}
isolines_step={self.isolines_step}
smooth_isolines={str(self.smooth_isolines).lower()}
manual_coastline_processing={str(self.manual_coastline_processing).lower()}
parallel_workers={self.parallel_workers}
source_version="{self.source_version}"
"""
        path.write_text(content, encoding="utf-8")
        return path

    def ensure_directories(self):
        for path in (
            self.config_dir,
            self.project_dir,
            self.vector_dir,
            self.raster_dir,
            self.terrain_input_dir,
            self.temp_dir,
            self.osm_data_dir,
            self.cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
