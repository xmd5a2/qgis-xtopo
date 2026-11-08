import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config
from qgis_xtopo.core.pbf_source import (
    QueryIndex, _apply_spatial_filter, _filter_delivered, parse_tags,
    prepare_dataset,
)


class PbfSourceTest(unittest.TestCase):
    def test_json_tags(self):
        tags = parse_tags('{"natural":"water","name:ru":"Озеро"}')
        self.assertEqual(tags["natural"], "water")
        self.assertEqual(tags["name:ru"], "Озеро")

    def test_hstore_tags(self):
        tags = parse_tags('"natural"=>"water","name:ru"=>"Озеро","surface"=>"asphalt"')
        self.assertEqual(tags["natural"], "water")
        self.assertEqual(tags["name:ru"], "Озеро")
        self.assertEqual(tags["surface"], "asphalt")

    def test_hstore_escapes(self):
        text = r'''"name"=>"D\"Or","int_name"=>"Principat d\'Andorra"'''
        tags = parse_tags(text)
        self.assertEqual(tags["name"], 'D"Or')
        self.assertEqual(tags["int_name"], "Principat d'Andorra")

    def test_osm_config_keeps_ele(self):
        from qgis_xtopo.core.pbf_source import ensure_osm_config

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="osm",
                bbox="1,2,3,4",
            )
            config.ensure_directories()
            path = ensure_osm_config(config)
            self.assertTrue(path.exists())
            ignore = next(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("ignore=")
            )
            keys = [item.strip() for item in ignore[len("ignore="):].split(",")]
            self.assertNotIn("ele", keys)

    def test_query_index(self):
        index = QueryIndex({
            "water": 'way["natural"="water"]["intermittent"!="yes"];',
            "peak": 'node["natural"="peak"];',
        })
        self.assertEqual(index.matching_names("way", {"natural": "water"}), {"water"})
        self.assertEqual(index.matching_names("node", {"natural": "peak"}), {"peak"})
        self.assertEqual(
            index.matching_names("way", {"natural": "water", "intermittent": "yes"}),
            set(),
        )


    def test_filter_extent_expands_bbox(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.pbf_source import _filter_extent

        bbox = BBox(9.0, 46.0, 9.1, 46.1)
        min_x, min_y, max_x, max_y = _filter_extent(bbox)
        self.assertLess(min_x, bbox.lon_min)
        self.assertGreater(max_x, bbox.lon_max)
        self.assertLess(min_y, bbox.lat_min)
        self.assertGreater(max_y, bbox.lat_max)
        # Запас одинаков со всех сторон
        self.assertAlmostEqual(bbox.lon_min - min_x, bbox.lat_min - min_y, 6)
        self.assertAlmostEqual(bbox.lon_min - min_x, max_y - bbox.lat_max, 6)

    def test_filter_extent_margin_grows_with_bbox(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.pbf_source import _filter_extent

        small = _filter_extent(BBox(9.0, 46.0, 9.01, 46.01))
        large = _filter_extent(BBox(9.0, 46.0, 10.0, 47.0))
        self.assertGreater(
            large[2] - large[0], small[2] - small[0]
        )

    def test_spatial_filter_uses_gdal_argument_order(self):
        """Углы фильтра передаются в порядке GDAL (minx, miny, maxx, maxy)."""
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.pbf_source import _filter_extent

        bbox = BBox(9.0, 46.0, 9.1, 46.1)
        min_x, min_y, max_x, max_y = _filter_extent(bbox)
        self.assertLess(min_x, min_y)
        self.assertLess(max_x, max_y)


class CleanGeometryTest(unittest.TestCase):
    """Пустые части составной геометрии не должны попадать в слой."""

    def _geometry(self, wkt):
        from osgeo import ogr

        return ogr.CreateGeometryFromWkt(wkt)

    def test_empty_parts_are_dropped(self):
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry(
            "GEOMETRYCOLLECTION(POLYGON EMPTY,"
            "LINESTRING(7.4 46.9,7.41 46.91))"
        )
        cleaned = _clean_geometry(geometry)
        self.assertEqual(cleaned.GetGeometryName(), "LINESTRING")

    def test_collection_of_several_parts_is_kept(self):
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry(
            "GEOMETRYCOLLECTION(LINESTRING(7.4 46.9,7.41 46.91),"
            "LINESTRING(7.42 46.92,7.43 46.93))"
        )
        cleaned = _clean_geometry(geometry)
        self.assertEqual(cleaned.GetGeometryName(), "GEOMETRYCOLLECTION")
        self.assertEqual(cleaned.GetGeometryCount(), 2)

    def test_mixed_parts_stay_a_collection(self):
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry(
            "GEOMETRYCOLLECTION(LINESTRING(7.4 46.9,7.41 46.91),"
            "POLYGON((7.42 46.92,7.43 46.92,7.43 46.93,7.42 46.92,7.42 46.92)))"
        )
        cleaned = _clean_geometry(geometry)
        self.assertEqual(cleaned.GetGeometryName(), "GEOMETRYCOLLECTION")
        self.assertEqual(cleaned.GetGeometryCount(), 2)

    def test_fully_empty_collection_is_dropped(self):
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry("GEOMETRYCOLLECTION(POLYGON EMPTY)")
        self.assertIsNone(_clean_geometry(geometry))

    def test_part_without_points_is_dropped(self):
        """Полигон с нулём точек: IsEmpty() у него ложно."""
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry(
            "GEOMETRYCOLLECTION(POLYGON EMPTY,"
            "LINESTRING(7.4 46.9,7.41 46.91))"
        )
        cleaned = _clean_geometry(geometry)
        self.assertEqual(cleaned.GetGeometryName(), "LINESTRING")
        self.assertEqual(cleaned.GetPointCount(), 2)

    def test_plain_geometry_is_unchanged(self):
        from qgis_xtopo.core.pbf_source import _clean_geometry

        geometry = self._geometry(
            "POLYGON((7.4 46.9,7.41 46.9,7.41 46.91,7.4 46.91,7.4 46.9))"
        )
        self.assertIs(_clean_geometry(geometry), geometry)


if __name__ == "__main__":
    unittest.main()


class _FakeGeometry:
    def __init__(self, x, y):
        self._x = x
        self._y = y

    def IsEmpty(self):
        return False

    def GetEnvelope(self):
        return (self._x, self._x, self._y, self._y)

    def GetGeometryName(self):
        return "POINT"

    def GetPointCount(self):
        return 1

    def Intersects(self, other):
        try:
            min_x, max_x, min_y, max_y = other.GetEnvelope()
        except Exception:
            return True
        return min_x <= self._x <= max_x and min_y <= self._y <= max_y

    def ExportToJson(self):
        return json.dumps({"type": "Point", "coordinates": [self._x, self._y]})


class _FakeFieldDefn:
    def __init__(self, name):
        self._name = name

    def GetName(self):
        return self._name


class _FakeDefinition:
    def __init__(self, names):
        self._names = names

    def GetFieldCount(self):
        return len(self._names)

    def GetFieldDefn(self, index):
        return _FakeFieldDefn(self._names[index])


class _FakeFeature:
    def __init__(self, osm_id, tags, x, y):
        self._values = {"osm_id": osm_id, "other_tags": json.dumps(tags)}
        self._geometry = _FakeGeometry(x, y)

    def GetGeometryRef(self):
        return self._geometry

    def GetFieldIndex(self, name):
        return list(self._values).index(name) if name in self._values else -1

    def GetField(self, index):
        return list(self._values.values())[index]


class _FakeLayer:
    """Слой, который при фильтре отдаёт чужие объекты."""

    def __init__(self, features, name="lines"):
        self._features = list(features)
        self._name = name
        self.filter = None

    def GetName(self):
        return self._name

    def GetLayerDefn(self):
        return _FakeDefinition(["osm_id", "other_tags"])

    def SetSpatialFilterRect(self, *box):
        self.filter = box

    def SetSpatialFilter(self, value):
        self.filter = value

    def ResetReading(self):
        pass

    def GetNextFeature(self):
        if not self._features:
            return None
        return self._features.pop(0)


class _FakeDataset:
    def __init__(self, layer):
        self._layer = layer

    def GetLayerByName(self, name):
        return self._layer if name == self._layer.GetName() else None


class BrokenSpatialFilterTest(unittest.TestCase):
    """Драйвер OSM иногда отдаёт объекты из другого места файла.

    Тогда грубый фильтр не только бесполезен, но и вред��н: нужная
    область в вывод не попадает.  Плагин замечает это и читает файл целиком.
    """

    def _run(self, broken, whole, bbox):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "extract.osm.pbf"
            source.write_bytes(b"not a real pbf")
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()
            opened = []

            def open_dataset(*args, **kwargs):
                # Первый раз файл открывается с фильтром и отдаёт чужие
                # объекты, второй раз — целиком.
                features = broken if not opened else whole
                layer = _FakeLayer(features)
                opened.append(layer)
                return _FakeDataset(layer)

            with mock.patch(
                "qgis_xtopo.core.pbf_source._open_dataset",
                side_effect=open_dataset,
            ):
                dataset = prepare_dataset(
                    config, bbox, ["peak"], query_texts={
                        "peak": 'way["natural"="peak"];'
                    },
                    sources=[str(source)],
                )
            content = {}
            for name, path_ in dataset.feature_files.items():
                content[name] = path_.read_text(encoding="utf-8")
            return content, opened

    def test_broken_filter_is_dropped(self):
        inside = _FakeFeature(2, {"natural": "peak"}, 44.0, 41.8)
        broken = [_FakeFeature(1, {"natural": "peak"}, 10.0, 10.0)]
        whole = [broken[0], inside]
        bbox = BBox(43.8127, 41.7225, 44.2264, 41.9738)
        content, opened = self._run(broken, whole, bbox)
        # Файл открыт дважды: сначала с фильтром, потом целиком.
        self.assertEqual(len(opened), 2)
        self.assertIsNotNone(opened[0].filter)
        self.assertIsNone(opened[1].filter)
        self.assertIn("peak", content)
        self.assertIn("way/2", content["peak"])

    def test_working_filter_keeps_reading(self):
        inside = _FakeFeature(1, {"natural": "peak"}, 44.0, 41.8)
        bbox = BBox(43.8127, 41.7225, 44.2264, 41.9738)
        content, opened = self._run([inside], [inside], bbox)
        self.assertEqual(len(opened), 1)
        self.assertIsNotNone(opened[0].filter)
        self.assertIn("peak", content)


class FilterDeliveredTest(unittest.TestCase):
    """Фильтр драйвера считается рабочим, если прочитанное рядом с областью."""

    AREA = (43.8127, 44.2264, 41.7225, 41.9738)

    def test_read_box_covers_area(self):
        self.assertTrue(
            _filter_delivered((43.0, 41.5, 44.5, 42.5), self.AREA)
        )

    def test_read_box_elsewhere(self):
        self.assertFalse(
            _filter_delivered((41.6, 42.0, 43.7, 44.1), self.AREA)
        )

    def test_nothing_read(self):
        self.assertFalse(_filter_delivered(None, self.AREA))


class _NamedLayer:
    """Слой с именем: фильтр запоминается."""

    def __init__(self, name):
        self._name = name
        self.filter = None

    def GetName(self):
        return self._name

    def SetSpatialFilterRect(self, *box):
        self.filter = box


class SpatialFilterLayersTest(unittest.TestCase):
    """Фильтр ставится не на все слои: слой points ломает relations."""

    def _layers(self):
        return [_NamedLayer(name) for name in (
            "points", "lines", "multilinestrings", "multipolygons",
            "other_relations",
        )]

    def test_points_layer_is_not_filtered(self):
        layers = self._layers()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "extract.osm.pbf"
            source.write_bytes(b"not a real pbf")
            _apply_spatial_filter(layers, source, BBox(43.8, 41.7, 44.2, 42.0))
        points = layers[0]
        self.assertIsNone(
            points.filter,
            "фильтр на points обрезает кольца отношений и теряет их",
        )

    def test_other_layers_are_filtered(self):
        layers = self._layers()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "extract.osm.pbf"
            source.write_bytes(b"not a real pbf")
            _apply_spatial_filter(layers, source, BBox(43.8, 41.7, 44.2, 42.0))
        for layer in layers[1:]:
            self.assertIsNotNone(layer.filter, f"{layer.GetName()} не отфильтрован")

    def test_non_pbf_files_are_not_filtered(self):
        layers = self._layers()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "extract.osm"
            source.write_bytes(b"not a real pbf")
            self.assertFalse(
                _apply_spatial_filter(layers, source, BBox(43.8, 41.7, 44.2, 42.0))
            )
        self.assertIsNone(layers[0].filter)


class IndexingFallbackTest(unittest.TestCase):
    """Падение индекса GDAL приводит к повторному чтению без него."""

    def test_read_is_retried_without_custom_indexing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "extract.osm.pbf"
            source.write_bytes(b"not a real pbf")
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()
            calls = []

            def fake_open(gdal, path, temp_dir, custom_indexing=True):
                calls.append(custom_indexing)
                if custom_indexing:
                    raise RuntimeError("Non increasing node id")
                return None

            with mock.patch(
                "qgis_xtopo.core.pbf_source._open_dataset",
                side_effect=fake_open,
            ):
                with self.assertRaises(Exception):
                    prepare_dataset(
                        config, BBox(43.8, 41.7, 44.2, 42.0), ["peak"],
                        query_texts={"peak": 'node["natural"="peak"];'},
                        sources=[str(source)],
                    )
            self.assertEqual(calls, [True, False])



class MultiFileDatasetTest(unittest.TestCase):
    """Чтение нескольких файлов: данные одного файла не теряются.

    Файлы читаются по очереди, и каждый пишет в общие файлы слоёв.
    Откат «фильтр не сработал» затрагивает только текущий файл: иначе
    пустой файл горных массивов стирал бы слои предыдущего файла.
    """

    BBOX = BBox(43.8127, 41.7225, 44.2264, 41.9738)

    def _prepare(self, features, covering_names=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()
            files = []
            for name in features:
                path = root / name
                path.write_bytes(b"not a real pbf")
                files.append(str(path))
            covering = [str(root / name) for name in covering_names]
            opened = []

            def fake_open(gdal, source, temp_dir, custom_indexing=True):
                name = Path(source).name
                variants = features[name]
                index = len([item for item in opened if item[0] == name])
                layer = _FakeLayer(list(variants[min(index, len(variants) - 1)]))
                opened.append((name, index, layer))
                return _FakeDataset(layer)

            with mock.patch(
                "qgis_xtopo.core.pbf_source._open_dataset",
                side_effect=fake_open,
            ), mock.patch(
                "qgis_xtopo.core.osmium_prefilter.prefilter_sources",
                return_value=(files, [], covering),
            ):
                dataset = prepare_dataset(
                    config, self.BBOX, ["peak"],
                    query_texts={"peak": 'way["natural"="peak"];'},
                    sources=files,
                )
            content = {
                name: path.read_text(encoding="utf-8")
                for name, path in dataset.feature_files.items()
            }
            return content, opened

    def test_empty_covering_file_keeps_layers_of_previous_file(self):
        content, opened = self._prepare(
            {
                "extract.osm.pbf": [
                    [_FakeFeature(1, {"natural": "peak"}, 44.0, 41.8)],
                ],
                "covering.osm.pbf": [[]],
            },
            covering_names=["covering.osm.pbf"],
        )
        self.assertIn("peak", content)
        self.assertIn("way/1", content["peak"])
        # Файл горных массивов читается целиком: фильтр драйвера его
        # геометрию, лежащую вне области, отбросил бы.
        covering = [item for item in opened if item[0] == "covering.osm.pbf"]
        self.assertTrue(covering)
        self.assertIsNone(covering[0][2].filter)

    def test_rollback_of_one_file_keeps_other_files(self):
        content, _opened = self._prepare(
            {
                "extract.osm.pbf": [
                    [_FakeFeature(1, {"natural": "peak"}, 10.0, 10.0)],
                    [
                        _FakeFeature(1, {"natural": "peak"}, 10.0, 10.0),
                        _FakeFeature(2, {"natural": "peak"}, 44.0, 41.8),
                    ],
                ],
                "covering.osm.pbf": [[], []],
            },
        )
        self.assertIn("peak", content)
        self.assertIn("way/2", content["peak"])
