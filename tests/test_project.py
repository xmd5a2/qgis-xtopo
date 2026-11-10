import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from qgis_xtopo.core.project import project_file_path, rewrite_project_archive
from qgis_xtopo.core.resources import template_path


class ProjectTemplateTest(unittest.TestCase):
    def test_rewrite(self):
        source = template_path()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(source, destination, "demo")
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
                self.assertIn("demo.qgs", names)
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn('projectname="demo"', text)
        self.assertIn("<title>demo</title>", text)
        self.assertIn("<qgis_xtopo", text)

    def test_template_has_aerialway_station(self):
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("aerialway station icons", text)
        self.assertIn(
            "aerialway_station_buffered_dissolved_centroids.sqlite",
            text,
        )
        self.assertIn("../icons/aerialway_station.svg", text)
        self.assertIn('labelsEnabled="1"', text)
        # станции видны только до 1:100000 (minScale — предел отдаления)
        block = re.search(
            r"<maplayer[^>]*>(?:(?!</maplayer>).)*"
            r"aerialway station icons.*?</maplayer>",
            text, re.S,
        )
        self.assertIsNotNone(block)
        self.assertIn('minScale="100000"', block.group(0))
        self.assertIn('hasScaleBasedVisibilityFlag="1"', block.group(0))
        self.assertNotIn("100000 - 300000", block.group(0))
        # стрелки направления с учётом oneway, между точками пунктира
        self.assertGreaterEqual(text.count("filled_arrowhead"), 16)
        self.assertIn("offset_along_line", text)
        self.assertIn("&quot;oneway&quot;", text)

    def test_default_queries_include_aerialway_station(self):
        from qgis_xtopo.core.config import default_queries

        self.assertIn("aerialway_station", default_queries())
        self.assertLess(
            default_queries().index("aerialway_station"),
            default_queries().index("aerialway_cable_car_gondola"),
        )

    def test_template_overlay_filters_guarded_with_try(self):
        """overlay_* не должны падать, когда слой удалён из проекта.

        Пустые слои (spoil heap, prison, cliff, earth bank gully)
        вырезаются при сборке проекта, поэтому обращения к ним в
        фильтрах обёрнуты в try(..., fallback) — иначе весь фильтр
        даёт NULL и объекты пропадают с карты.
        """
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        unguarded = _unguarded_overlay_calls(text)
        if unguarded:
            self.fail(
                "незащищённый вызов overlay в шаблоне: " + unguarded[0]
            )


def _unguarded_overlay_calls(text):
    """Вызовы overlay_*, не обёрнутые в try(...).

    Разбор идёт одним проходом по тексту: раньше для каждого вызова
    скобки искались назад с начала файла, и на шаблоне в 6 МБ это
    занимало больше двух минут.
    """
    pairs = []
    stack = []
    for match in re.finditer(r"[()]", text):
        position = match.start()
        if match.group(0) == "(":
            # Имя функции ищем в небольшом окне: срез всего текста на
            # каждой скобке снова даёт квадратичное время.
            window = text[max(0, position - 64):position]
            name = re.search(r"([\w.]+)\s*$", window)
            stack.append((position, name.group(1) if name else ""))
        else:
            if stack:
                open_position, name = stack.pop()
                pairs.append((open_position, position, name))
    guarded = [
        (open_position, close_position)
        for open_position, close_position, name in pairs
        if name == "try"
    ]
    result = []
    for match in re.finditer(r"overlay_\w+\('[^']+'\)", text):
        position = match.start()
        if not any(
            open_position < position < close_position
            for open_position, close_position in guarded
        ):
            result.append(match.group(0))
    return result


class OverlayFilterPatchTest(unittest.TestCase):
    """Обращения к overlay_* обёрнуты в try(...)."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_filters_are_guarded(self):
        from qgis_xtopo.core.project import patch_overlay_filters

        text = (
            "filter=\"if(overlay_within('spoil heap'),false,true)\" "
            "filter=\"if(overlay_within('prison'),false,true)\" "
            "value=\"if(overlay_contains('earth bank gully') OR "
            "overlay_contains('cliff'),0,1)\""
        )
        patched = patch_overlay_filters(text)
        self.assertNotIn("if(overlay_within", patched)
        self.assertNotIn("if(overlay_contains", patched)
        self.assertEqual(patched.count("try(overlay"), 3)

    def test_patch_is_idempotent(self):
        from qgis_xtopo.core.project import patch_overlay_filters

        text = "filter=\"if(overlay_within('prison'),false,true)\""
        once = patch_overlay_filters(text)
        self.assertEqual(patch_overlay_filters(once), once)

    def test_repair_project_guards_filters(self):
        from qgis_xtopo.core.project import repair_project

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qgz"
            (Path(directory) / "vector").mkdir()
            (Path(directory) / "vector" / "roads.dat").write_text(
                "data", encoding="utf-8"
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "demo.qgs",
                    "<qgis><maplayer><id>x</id>"
                    "<datasource>./vector/roads.dat</datasource>"
                    "<renderer-v2><rule filter=\""
                    "if(overlay_within('prison'),false,true)\"/></renderer-v2>"
                    "</maplayer></qgis>",
                )
            self.assertTrue(repair_project(path))
            with zipfile.ZipFile(path) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn("try(overlay_within('prison'), false)", text)
        self.assertNotIn("if(overlay_within", text)


class ProjectDetailLevelTest(unittest.TestCase):
    """Уровень детализации из панели попадает в переменные проекта."""

    def _variables(self, text):
        start = text.rfind("<Variables>")
        block = text[start:text.find("</Variables>", start)]
        names, values = block.split("</variableNames>")
        return dict(zip(
            re.findall(r"<value>(.*?)</value>", names),
            re.findall(r"<value>(.*?)</value>", values),
        ))

    def _text(self, level):
        from qgis_xtopo.core.project import _set_detail_variable

        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        return text, self._variables(_set_detail_variable(text, level))

    def test_value_is_written_to_its_own_position(self):
        text, variables = self._text(5)
        self.assertEqual(variables["level_of_detail"], "5")

    def test_other_variables_are_untouched(self):
        with zipfile.ZipFile(template_path()) as archive:
            source = archive.read("automap.qgs").decode("utf-8")
        original = self._variables(source)
        _text, variables = self._text(3)
        for name, value in original.items():
            if name != "level_of_detail":
                self.assertEqual(variables[name], value, name)

    def test_missing_variable_keeps_text(self):
        from qgis_xtopo.core.project import _set_detail_variable

        text = "<Variables><variableNames type=\"QStringList\">" \
               "<value>other</value></variableNames>" \
               "<variableValues type=\"QStringList\">" \
               "<value>1</value></variableValues></Variables>"
        self.assertEqual(_set_detail_variable(text, 5), text)

    def test_rewrite_uses_selected_level(self):
        from qgis_xtopo.core import map_language

        previous = map_language.level_of_detail()
        try:
            map_language.set_level_of_detail(6, save=False)
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "demo.qgz"
                rewrite_project_archive(template_path(), destination, "demo")
                with zipfile.ZipFile(destination) as archive:
                    text = archive.read("demo.qgs").decode("utf-8")
        finally:
            map_language.set_level_of_detail(previous, save=False)
        self.assertEqual(self._variables(text)["level_of_detail"], "6")


class ProjectAttributionTest(unittest.TestCase):
    """Атрибуция источника рельефа попадает в подпись макета атласа."""

    def _variables(self, text):
        start = text.rfind("<Variables>")
        block = text[start:text.find("</Variables>", start)]
        names, values = block.split("</variableNames>")
        return dict(zip(
            re.findall(r"<value>(.*?)</value>", names),
            re.findall(r"<value>(.*?)</value>", values),
        ))

    def test_template_has_attribution_expression(self):
        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("@dem_attribution", text)
        self.assertIn("dem_attribution", self._variables(text))

    def test_rewrite_writes_attribution(self):
        attribution = (
            "Produced using Copernicus data and information funded by "
            "the European Union and ESA"
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(
                template_path(), destination, "demo",
                dem_attribution=attribution,
            )
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertEqual(self._variables(text)["dem_attribution"], attribution)

    def test_rewrite_without_attribution_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(template_path(), destination, "demo")
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertEqual(self._variables(text)["dem_attribution"], "")

    def test_prepare_project_uses_source_attribution(self):
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.project import prepare_project

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="7,46,7.1,46.1",
                array_queries=[],
                generate_terrain=True,
                terrain_source="cop30",
                download_terrain_tiles=True,
            )
            path = prepare_project(config)
            with zipfile.ZipFile(path) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn(
            "Produced using Copernicus data",
            self._variables(text)["dem_attribution"],
        )


class ProjectFilePathTest(unittest.TestCase):
    def test_path(self):
        path = project_file_path("/tmp/projects", "demo")
        self.assertEqual(
            path, Path("/tmp/projects/demo/demo.qgz"),
        )

    def test_invalid_name(self):
        self.assertIsNone(project_file_path("/tmp/projects", "bad name!"))


if __name__ == "__main__":
    unittest.main()
