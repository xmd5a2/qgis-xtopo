"""Fast approximate statistics for PBF extracts.

The GDAL OSM driver gives exact numbers but has to decode every object.
For the UI estimate that is too slow on files of a few dozen megabytes,
so this module parses PBF blobs directly with the standard library and
samples a fraction of them.  The result is approximate and is only used
for the information label before preparation starts.
"""

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .pbf_source import (
    FILTER_MARGIN_DEGREES, FILTER_MARGIN_RATIO, QueryIndex, PbfError,
)

MAX_SAMPLES = 96
MIN_SAMPLES = 6


@dataclass
class FastProbe:
    total: int
    in_bbox: int
    matched: int
    elapsed: float
    sampled_blobs: int
    total_blobs: int
    # Площадь выгрузки в квадратных градусах и доля расширенной области
    # (bbox с запасом фильтра) в ней: по этой доле оценивается, сколько
    # объектов придётся прочитать драйверу.
    header_area: float = 0.0
    extended_ratio: float = 0.0

    @property
    def approximate(self):
        return self.sampled_blobs < self.total_blobs


class UnsupportedPbf(PbfError):
    pass


def _varint(buffer, position):
    result = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 63:
            raise UnsupportedPbf("Malformed varint in PBF data")


def _fields(buffer):
    position = 0
    size = len(buffer)
    while position < size:
        key, position = _varint(buffer, position)
        number = key >> 3
        wire = key & 7
        if wire == 0:
            value, position = _varint(buffer, position)
        elif wire == 2:
            length, position = _varint(buffer, position)
            value = buffer[position:position + length]
            position += length
        elif wire == 1:
            value = buffer[position:position + 8]
            position += 8
        elif wire == 5:
            value = buffer[position:position + 4]
            position += 4
        else:
            raise UnsupportedPbf(f"Unsupported protobuf wire type {wire}")
        yield number, value


def _packed(buffer):
    values = []
    position = 0
    size = len(buffer)
    while position < size:
        value, position = _varint(buffer, position)
        values.append(value)
    return values


def _zigzag(value):
    return (value >> 1) ^ -(value & 1)


def _signed64(value):
    return value - (1 << 64) if value >= (1 << 63) else value


def _blobs(handle):
    """Yield (offset, size) for every OSMData blob without decoding it."""
    while True:
        header_size_raw = handle.read(4)
        if not header_size_raw:
            return
        if len(header_size_raw) < 4:
            raise UnsupportedPbf("Truncated PBF file")
        header_size = struct.unpack(">I", header_size_raw)[0]
        header = handle.read(header_size)
        if len(header) < header_size:
            raise UnsupportedPbf("Truncated PBF block header")
        blob_type = None
        data_size = None
        for number, value in _fields(header):
            if number == 1:
                blob_type = value.decode("utf-8", "replace")
            elif number == 3:
                data_size = value
        if data_size is None:
            raise UnsupportedPbf("PBF block without a size")
        offset = handle.tell()
        handle.seek(offset + data_size)
        if blob_type == "OSMData":
            yield offset, data_size


def _blob_payload(handle, offset, size):
    handle.seek(offset)
    blob = handle.read(size)
    if len(blob) < size:
        raise UnsupportedPbf("Truncated PBF blob")
    for number, value in _fields(blob):
        if number == 1:
            return value
        if number == 3:
            return zlib.decompress(value)
        if number in (4, 5, 6, 7):
            raise UnsupportedPbf("Unsupported PBF compression; use zlib-compressed .pbf")
    raise UnsupportedPbf("PBF blob without data")


def _header_bbox(handle):
    """Read the optional HeaderBBox from the OSMHeader blob."""
    handle.seek(0)
    while True:
        header_size_raw = handle.read(4)
        if not header_size_raw:
            return None
        if len(header_size_raw) < 4:
            return None
        header_size = struct.unpack(">I", header_size_raw)[0]
        header = handle.read(header_size)
        blob_type = None
        data_size = None
        for number, value in _fields(header):
            if number == 1:
                blob_type = value.decode("utf-8", "replace")
            elif number == 3:
                data_size = value
        if data_size is None:
            return None
        offset = handle.tell()
        if blob_type != "OSMHeader":
            handle.seek(offset + data_size)
            continue
        try:
            payload = _blob_payload(handle, offset, data_size)
        except Exception:
            return None
        values = {}
        for number, value in _fields(payload):
            if number == 1:
                for field, item in _fields(value):
                    if field in (1, 2, 3, 4):
                        values[field] = _zigzag(item)
        if set(values) == {1, 2, 3, 4}:
            return (
                values[1] / 1e9,
                values[2] / 1e9,
                values[3] / 1e9,
                values[4] / 1e9,
            )
        return None


def _string_table(payload):
    strings = []
    for number, value in _fields(payload):
        if number == 1:
            strings.append(value.decode("utf-8", "replace"))
    return strings


def _tags(keys, values, strings):
    tags = {}
    for key, value in zip(keys, values):
        if key < len(strings) and value < len(strings):
            tags[strings[key]] = strings[value]
    return tags


def _matches(index, kind, tags):
    return bool(index.matching_names(kind, tags)) if index is not None and tags else False


def _process_group(group, strings, index, bbox, granularity=100,
                   lat_offset=0, lon_offset=0):
    """Return counters for one PrimitiveGroup.

    Nodes have coordinates, so their position is checked against the
    bbox.  Ways and relations do not carry coordinates in PBF; their
    share is estimated later from the node density.  ``nodes_all``
    includes untagged nodes (needed for the spatial share), while
    ``nodes`` counts only nodes that the GDAL OSM driver exposes as
    point features.
    """
    counters = {
        "nodes_all": 0,
        "nodes_all_bbox": 0,
        "nodes": 0,
        "nodes_bbox": 0,
        "nodes_matched": 0,
        "ways": 0,
        "ways_matched": 0,
        "relations": 0,
        "relations_matched": 0,
    }
    lon_min, lat_min, lon_max, lat_max = bbox.as_tuple()
    unit = granularity * 1e-9
    for number, value in _fields(group):
        if number == 2:
            ids = lats = lons = keys_values = None
            for field, item in _fields(value):
                if field == 1:
                    ids = _packed(item)
                elif field == 8:
                    lats = _packed(item)
                elif field == 9:
                    lons = _packed(item)
                elif field == 10:
                    keys_values = _packed(item)
            if not ids or lats is None or lons is None:
                continue
            count = min(len(ids), len(lats), len(lons))
            counters["nodes_all"] += count
            lat_raw = 0
            lon_raw = 0
            tag_position = 0
            for position in range(count):
                lat_raw += _zigzag(lats[position])
                lon_raw += _zigzag(lons[position])
                lat = (lat_offset + lat_raw) * unit
                lon = (lon_offset + lon_raw) * unit
                inside = lon_min <= lon <= lon_max and lat_min <= lat <= lat_max
                if inside:
                    counters["nodes_all_bbox"] += 1
                if not keys_values:
                    continue
                tags = {}
                while (
                    tag_position < len(keys_values)
                    and keys_values[tag_position] != 0
                ):
                    key = keys_values[tag_position]
                    tag_position += 1
                    if tag_position >= len(keys_values):
                        break
                    item = keys_values[tag_position]
                    tag_position += 1
                    if key < len(strings) and item < len(strings):
                        tags[strings[key]] = strings[item]
                tag_position += 1
                if not tags:
                    continue
                counters["nodes"] += 1
                if inside:
                    counters["nodes_bbox"] += 1
                    if _matches(index, "node", tags):
                        counters["nodes_matched"] += 1
        elif number == 1:
            lat_raw = lon_raw = None
            keys = values = None
            for field, item in _fields(value):
                if field == 2:
                    keys = _packed(item)
                elif field == 3:
                    values = _packed(item)
                elif field == 8:
                    lat_raw = _zigzag(item)
                elif field == 9:
                    lon_raw = _zigzag(item)
            if lat_raw is None or lon_raw is None:
                continue
            counters["nodes_all"] += 1
            lat = (lat_offset + lat_raw) * unit
            lon = (lon_offset + lon_raw) * unit
            inside = lon_min <= lon <= lon_max and lat_min <= lat <= lat_max
            if inside:
                counters["nodes_all_bbox"] += 1
            if keys is not None and values is not None:
                tags = _tags(keys, values, strings)
                if tags:
                    counters["nodes"] += 1
                    if inside:
                        counters["nodes_bbox"] += 1
                        if _matches(index, "node", tags):
                            counters["nodes_matched"] += 1
        elif number == 3:
            counters["ways"] += 1
            keys = values = None
            for field, item in _fields(value):
                if field == 2:
                    keys = _packed(item)
                elif field == 3:
                    values = _packed(item)
            if keys is not None and values is not None:
                if _matches(index, "way", _tags(keys, values, strings)):
                    counters["ways_matched"] += 1
        elif number == 4:
            counters["relations"] += 1
            keys = values = None
            for field, item in _fields(value):
                if field == 2:
                    keys = _packed(item)
                elif field == 3:
                    values = _packed(item)
            if keys is not None and values is not None:
                if _matches(index, "relation", _tags(keys, values, strings)):
                    counters["relations_matched"] += 1
    return counters


def _process_block(payload, index, bbox):
    strings = []
    groups = []
    granularity = 100
    lat_offset = 0
    lon_offset = 0
    for number, value in _fields(payload):
        if number == 1:
            strings = _string_table(value)
        elif number == 2:
            groups.append(value)
        elif number == 17:
            granularity = value
        elif number == 19:
            lat_offset = _signed64(value)
        elif number == 20:
            lon_offset = _signed64(value)

    counters = {
        "nodes_all": 0,
        "nodes_all_bbox": 0,
        "nodes": 0,
        "nodes_bbox": 0,
        "nodes_matched": 0,
        "ways": 0,
        "ways_matched": 0,
        "relations": 0,
        "relations_matched": 0,
    }
    for group in groups:
        partial = _process_group(
            group, strings, index, bbox, granularity, lat_offset, lon_offset
        )
        for key in counters:
            counters[key] += partial[key]
    return counters


def probe_fast(config, bbox, query_names, query_texts=None, cancel=None, progress=None):
    """Return approximate object counters for a PBF extract.

    Raises UnsupportedPbf for formats or compressions that cannot be
    handled by this lightweight reader.
    """
    import time

    started = time.monotonic()
    source = Path(config.pbf_path).expanduser()
    if source.suffix.lower() != ".pbf":
        raise UnsupportedPbf("Fast estimation supports .pbf files only")
    if not source.exists():
        raise FileNotFoundError(f"OSM extract not found: {source}")

    query_texts = query_texts or {}
    index = QueryIndex(
        {name: query_texts[name] for name in query_names if name in query_texts}
    )

    with source.open("rb") as handle:
        header_box = _header_bbox(handle)
        handle.seek(0)
        blobs = list(_blobs(handle))
        if not blobs:
            raise UnsupportedPbf("No OSMData blobs found in the PBF file")

        sample_count = min(len(blobs), MAX_SAMPLES)
        if sample_count < MIN_SAMPLES:
            sample_count = len(blobs)
        total_bytes = sum(size for _offset, size in blobs) or 1
        if sample_count >= len(blobs):
            indices = list(range(len(blobs)))
        else:
            # Выборка пропорциональна размеру блоков, а не их номеру.
            # В густо застроенных местах блоки мельче, и равномерная
            # выборка по номеру почти не попадает в них: оценка
            # плотных районов получалась занижена в десятки раз.
            step = total_bytes / sample_count
            indices = []
            accumulated = 0
            next_at = 0.0
            for position, (_offset, size) in enumerate(blobs):
                accumulated += size
                if accumulated >= next_at:
                    indices.append(position)
                    next_at += step
                    if len(indices) >= sample_count:
                        break

        counters = {
            "nodes_all": 0,
            "nodes_all_bbox": 0,
            "nodes": 0,
            "nodes_bbox": 0,
            "nodes_matched": 0,
            "ways": 0,
            "ways_matched": 0,
            "relations": 0,
            "relations_matched": 0,
        }
        decoded = 0
        for position, index_value in enumerate(indices):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("PBF estimation cancelled")
            offset, size = blobs[index_value]
            try:
                payload = _blob_payload(handle, offset, size)
                partial = _process_block(payload, index, bbox)
            except UnsupportedPbf:
                raise
            except Exception:
                continue
            decoded += 1
            for key in counters:
                counters[key] += partial[key]
            if progress and position and position % 6 == 0:
                progress(f"Быстрая оценка PBF: {position + 1}/{len(indices)} блоков")

        if not decoded:
            raise UnsupportedPbf("Could not decode PBF blocks")

        # Выборка сделана по размеру блоков, поэтому и масштабирование
        # ведётся по байтам: иначе крупные блоки в выборке приводят к
        # завышению в несколько раз.
        sampled_bytes = sum(blobs[position][1] for position in indices)
        ratio = (total_bytes / sampled_bytes) if sampled_bytes else 1.0
        nodes = counters["nodes"] * ratio
        nodes_bbox = counters["nodes_bbox"] * ratio

        # Ways and relations have no coordinates in PBF.  Use the share
        # of nodes inside the bbox as a rough spatial factor.  If the
        # user bbox covers the extract header bbox, everything is inside.
        if header_box is not None:
            left, right, top, bottom = header_box
            covers = (
                bbox.lon_min <= left and bbox.lon_max >= right
                and bbox.lat_min <= bottom and bbox.lat_max >= top
            )
            spatial = 1.0 if covers else (
                counters["nodes_all_bbox"] / counters["nodes_all"]
                if counters["nodes_all"] else 0.0
            )
        else:
            spatial = (
                counters["nodes_all_bbox"] / counters["nodes_all"]
                if counters["nodes_all"] else 0.0
            )

        ways = counters["ways"] * ratio
        relations = counters["relations"] * ratio
        ways_bbox = ways * spatial
        relations_bbox = relations * spatial

        matched = (
            counters["nodes_matched"] * ratio
            + counters["ways_matched"] * ratio * spatial
            + counters["relations_matched"] * ratio * spatial
        )

    total = int(round(nodes + ways + relations))
    in_bbox = int(round(nodes_bbox + ways_bbox + relations_bbox))
    header_area = 0.0
    extended_ratio = 0.0
    if header_box is not None:
        left, right, top, bottom = header_box
        header_area = max(1e-9, (right - left) * (top - bottom))
        margin = max(FILTER_MARGIN_DEGREES, FILTER_MARGIN_RATIO * max(
            bbox.lon_max - bbox.lon_min, bbox.lat_max - bbox.lat_min
        ))
        area = max(1e-9, (
            (bbox.lon_max - bbox.lon_min + 2 * margin)
            * (bbox.lat_max - bbox.lat_min + 2 * margin)
        ))
        extended_ratio = min(1.0, area / header_area)
    return FastProbe(
        total=total,
        in_bbox=in_bbox,
        matched=int(round(matched)),
        elapsed=time.monotonic() - started,
        sampled_blobs=len(indices),
        total_blobs=len(blobs),
        header_area=header_area,
        extended_ratio=extended_ratio,
    )
