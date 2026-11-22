import importlib.util
import json
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None

CATALOG = Path(__file__).resolve().parent.parent / "resources" / "lang" / "en.json"
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.raw = CATALOG.read_text(encoding="utf-8")

    def test_catalog_is_valid_json(self):
        data = json.loads(self.raw)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        for source, target in data.items():
            self.assertTrue(source)
            self.assertTrue(target)

    def test_no_duplicate_source_keys(self):
        seen = []
        duplicates = []

        def hook(pairs):
            for key, _value in pairs:
                if key in seen:
                    duplicates.append(key)
                seen.append(key)
            return dict(pairs)

        json.loads(self.raw, object_pairs_hook=hook)
        self.assertEqual(duplicates, [])

    def test_placeholders_match(self):
        data = json.loads(self.raw)
        for source, target in data.items():
            self.assertEqual(
                len(PLACEHOLDER.findall(source)),
                len(PLACEHOLDER.findall(target)),
                source,
            )
            self.assertEqual(
                len(re.findall(r"%[sd]", source)),
                len(re.findall(r"%[sd]", target)),
                source,
            )


class TranslateTest(unittest.TestCase):
    def setUp(self):
        from qgis_xtopo.core import i18n

        self.i18n = i18n
        self.saved = i18n.language()
        i18n.reset_cache()
        i18n.set_language("ru", save=False)

    def tearDown(self):
        self.i18n.set_language(self.saved, save=False)

    def test_exact_translation_and_reverse(self):
        self.assertEqual(self.i18n.tr("Данные"), "Данные")
        self.i18n.set_language("en", save=False)
        self.assertEqual(self.i18n.tr("Данные"), "Data")
        self.assertEqual(self.i18n.tr("Подготовить данные"), "Prepare data")
        self.assertEqual(self.i18n.tr("Данные"), "Data")
        self.i18n.set_language("ru", save=False)
        self.assertEqual(self.i18n.tr("Data"), "Данные")

    def test_template_translation(self):
        i18n = self.i18n
        i18n.set_language("en", save=False)
        self.assertEqual(i18n.tr("Листов: 3"), "Sheets: 3")
        self.assertEqual(
            i18n.tr("Листов: 3 · выбран 2"), "Sheets: 3 · selected 2"
        )
        self.assertEqual(i18n.trf("{} МБ", "12.5"), "12.5 MB")
        timing = i18n.trf(
            "{} — {} с", i18n.tr("обработка слоёв"), "12.3"
        )
        self.assertEqual(
            i18n.trf("Хронометраж: по этапам — {}", timing),
            "Timing: by stage — layer processing — 12.3 s",
        )
        i18n.set_language("ru", save=False)
        self.assertEqual(i18n.tr("Sheets: 3"), "Листов: 3")

    def test_done_line_carries_total_time(self):
        i18n = self.i18n
        i18n.set_language("en", save=False)
        self.assertEqual(
            i18n.trf("Готово: {} (за {} с)", "/tmp/automap.qgz", "30.4"),
            "Done: /tmp/automap.qgz (in 30.4 s)",
        )
        i18n.set_language("ru", save=False)
        self.assertEqual(
            i18n.trf("Готово: {} (за {} с)", "/tmp/automap.qgz", "30.4"),
            "Готово: /tmp/automap.qgz (за 30.4 с)",
        )

    def test_unknown_text_is_unchanged(self):
        self.i18n.set_language("en", save=False)
        text = "Неизвестная строка 123"
        self.assertEqual(self.i18n.tr(text), text)

    def test_cycle_is_wrapped(self):
        i18n = self.i18n
        self.assertEqual(i18n.language(), "ru")
        self.assertEqual(i18n.cycle_language(), "en")
        self.assertEqual(i18n.cycle_language(), "ru")

    def test_listeners_and_removal(self):
        seen = []

        def listener(code):
            seen.append(code)

        self.i18n.add_listener(listener)
        try:
            self.i18n.set_language("en", save=False)
            self.i18n.set_language("en", save=False)
            self.i18n.set_language("ru", save=False)
        finally:
            self.i18n.remove_listener(listener)
        self.assertEqual(seen, ["en", "ru"])

    def test_settings_round_trip(self):
        settings = self.i18n.settings()
        saved = settings.value(self.i18n.SETTINGS_KEY)
        try:
            settings.setValue(self.i18n.SETTINGS_KEY, "en")
            self.assertEqual(self.i18n.load_language(), "en")
            settings.setValue(self.i18n.SETTINGS_KEY, "xx")
            self.assertEqual(self.i18n.load_language(), "ru")
        finally:
            if saved is None:
                settings.remove(self.i18n.SETTINGS_KEY)
            else:
                settings.setValue(self.i18n.SETTINGS_KEY, saved)

    def test_reverse_translation_picks_one_variant(self):
        """Одному английскому тексту отвечают разные русские строки.

        Обратный перевод не должен подставлять вариант «не того
        регистра» или другую по смыслу строку.
        """
        i18n = self.i18n
        i18n.set_language("ru", save=False)
        self.assertEqual(i18n.tr("OSM source"), "Источник OSM")
        self.assertEqual(i18n.tr("Geofabrik maps"), "Карты Geofabrik")
        self.assertEqual(i18n.tr("Hillshade"), "Затенение")

    def test_reverse_translation_is_deterministic(self):
        """Выбор варианта не зависит от порядка ключей каталога."""
        from qgis_xtopo.core import i18n as module

        pairs = {
            "Затенение рельефа": "Hillshade",
            "Затенение": "Hillshade",
        }
        variants = module._reverse_variants(pairs)
        self.assertEqual(
            module._reverse_choice(variants, "Hillshade"), "Затенение"
        )
        pairs = {
            "Затенение": "Hillshade",
            "Затенение рельефа": "Hillshade",
        }
        variants = module._reverse_variants(pairs)
        self.assertEqual(
            module._reverse_choice(variants, "Hillshade"), "Затенение"
        )


class CatalogCoverageTest(unittest.TestCase):
    """Все строки, которые видит пользователь, есть в каталоге."""

    ROOT = Path(__file__).resolve().parent.parent

    def _catalog(self):
        return json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_user_strings_are_translated(self):
        import ast

        catalog = self._catalog()
        missing = []
        for folder in ("gui", "core"):
            for path in sorted((self.ROOT / folder).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if not isinstance(func, ast.Attribute):
                        continue
                    if func.attr not in ("tr", "trf"):
                        continue
                    if not isinstance(func.value, ast.Name):
                        continue
                    if func.value.id != "i18n" or not node.args:
                        continue
                    first = node.args[0]
                    if not isinstance(first, ast.Constant):
                        continue
                    text = first.value
                    if isinstance(text, str) and text and text not in catalog:
                        missing.append(f"{path.name}:{first.lineno} {text}")
        self.assertEqual(missing, [])

    @staticmethod
    def _log_template(node):
        """Текст сообщения журнала; None — сообщение собирается динамически."""
        import ast

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                value.value
                if isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                else "{}"
                for value in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = CatalogCoverageTest._log_template(node.left)
            right = CatalogCoverageTest._log_template(node.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("tr", "trf")
                and node.args
            ):
                return CatalogCoverageTest._log_template(node.args[0])
        return None

    @staticmethod
    def _log_call_path(node):
        import ast

        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    def test_log_messages_are_translated(self):
        """Каждое сообщение журнала есть в каталоге или является именем."""
        import ast

        catalog = self._catalog()
        shapes = {PLACEHOLDER.sub("{}", text) for text in catalog}
        shapes |= {PLACEHOLDER.sub("{}", text) for text in catalog.values()}
        # Служебные строки: префиксы переводятся отдельно, имя слоя в
        # прогрессе — собственное имя и не переводится.
        allowed = {
            "[{}/{}] {}",
            "WARNING: {}",
            "ERROR: {}",
            "WARNING: {}: {}",
        }
        missing = []
        for folder in ("gui", "core"):
            for path in sorted((self.ROOT / folder).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    call = self._log_call_path(node.func)
                    if not (
                        call.endswith(
                            (
                                "progress",
                                "_append_log",
                                "statusMessage.emit",
                                "progress.emit",
                                "status.emit",
                            )
                        )
                        or call == "status"
                    ):
                        continue
                    text = self._log_template(node.args[0])
                    if text is None or text in allowed:
                        continue
                    candidates = {PLACEHOLDER.sub("{}", text)}
                    for prefix in ("WARNING: ", "ERROR: "):
                        if text.startswith(prefix):
                            candidates.add(
                                PLACEHOLDER.sub("{}", text[len(prefix):])
                            )
                    if not candidates & shapes:
                        missing.append(f"{path.name}:{node.lineno} {text}")
        self.assertEqual(missing, [])


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class TranslateTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis_xtopo.core import i18n

        self.i18n = i18n
        self.saved = i18n.language()
        i18n.set_language("ru", save=False)

    def tearDown(self):
        self.i18n.set_language(self.saved, save=False)

    def _widget(self):
        from qgis.PyQt.QtWidgets import (
            QComboBox, QGroupBox, QLabel, QPushButton, QTabWidget,
            QVBoxLayout, QWidget,
        )

        root = QWidget()
        layout = QVBoxLayout(root)
        label = QLabel("Данные")
        label.setToolTip("Прокрутка карты")
        layout.addWidget(label)
        button = QPushButton("Отмена")
        layout.addWidget(button)
        group = QGroupBox("Атлас")
        group_layout = QVBoxLayout(group)
        combo = QComboBox()
        combo.addItem("Все")
        combo.addItem("Ничего")
        group_layout.addWidget(combo)
        group.setLayout(group_layout)
        layout.addWidget(group)
        tabs = QTabWidget()
        tabs.addTab(QWidget(), "Проект")
        layout.addWidget(tabs)
        return root, label, button, group, combo, tabs

    def test_tree_translation_and_back(self):
        from qgis_xtopo.gui.translation import translate_tree

        root, label, button, group, combo, tabs = self._widget()
        self.i18n.set_language("en", save=False)
        translate_tree(root)
        self.assertEqual(label.text(), "Data")
        self.assertEqual(label.toolTip(), "Pan map")
        self.assertEqual(button.text(), "Cancel")
        self.assertEqual(group.title(), "Atlas")
        self.assertEqual(combo.itemText(0), "All")
        self.assertEqual(combo.itemText(1), "None")
        self.assertEqual(tabs.tabText(0), "Project")

        self.i18n.set_language("ru", save=False)
        translate_tree(root)
        self.assertEqual(label.text(), "Данные")
        self.assertEqual(label.toolTip(), "Прокрутка карты")
        self.assertEqual(button.text(), "Отмена")
        self.assertEqual(group.title(), "Атлас")
        self.assertEqual(combo.itemText(0), "Все")
        self.assertEqual(combo.itemText(1), "Ничего")
        self.assertEqual(tabs.tabText(0), "Проект")

    def test_actions_and_menus_are_translated(self):
        """Действия и меню — не виджеты, их обход не находит."""
        from qgis.PyQt.QtWidgets import QMenu, QVBoxLayout, QWidget

        from qgis_xtopo.gui.translation import translate_tree

        try:
            from qgis.PyQt.QtWidgets import QAction
        except ImportError:  # QGIS 4 / Qt 6
            from qgis.PyQt.QtGui import QAction

        root = QWidget()
        layout = QVBoxLayout(root)
        menu = QMenu("Атлас", root)
        action = QAction("Расстояние", menu)
        menu.addAction(action)
        toolbar_action = QAction("Прокрутка карты", root)
        layout.addWidget(menu)

        self.i18n.set_language("en", save=False)
        translate_tree(root)
        self.assertEqual(action.text(), "Distance")
        self.assertEqual(menu.title(), "Atlas")
        self.assertEqual(toolbar_action.text(), "Pan map")

        self.i18n.set_language("ru", save=False)
        translate_tree(root)
        self.assertEqual(action.text(), "Расстояние")
        self.assertEqual(menu.title(), "Атлас")
        self.assertEqual(toolbar_action.text(), "Прокрутка карты")


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class MapLanguageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_preferred_tags_and_dirty_state(self):
        from qgis.core import QgsProject
        from qgis_xtopo.core.map_language import apply_map_language

        project = QgsProject()
        project.setDirty(False)
        apply_map_language("ru", project=project)
        variables = project.customVariables()
        self.assertEqual(variables["name_tag_1"], "name:ru")
        self.assertEqual(variables["name_tag_2"], "name")
        self.assertEqual(variables["name_tag_3"], "name:en")
        self.assertEqual(variables["name_pref_suf_lang"], "ru")
        self.assertFalse(project.isDirty())

        apply_map_language("en", project=project)
        variables = project.customVariables()
        self.assertEqual(variables["name_tag_1"], "name:en")
        self.assertEqual(variables["name_tag_2"], "name")
        self.assertNotIn("name_tag_3", variables)
        self.assertEqual(variables["name_pref_suf_lang"], "en")
        self.assertFalse(project.isDirty())

        project.setDirty(True)
        apply_map_language("ru", project=project)
        self.assertTrue(project.isDirty())

    def test_local_names_use_name_only(self):
        from qgis.core import QgsProject
        from qgis_xtopo.core.map_language import apply_map_language

        project = QgsProject()
        project.setDirty(False)
        apply_map_language("local", project=project)
        variables = project.customVariables()
        self.assertEqual(variables["name_tag_1"], "name")
        self.assertNotIn("name_tag_2", variables)
        self.assertNotIn("name_tag_3", variables)
        self.assertEqual(variables["name_pref_suf_lang"], "ru")
        self.assertEqual(variables["name_local"], "1")
        self.assertEqual(variables["name_translit"], "0")
        self.assertFalse(project.isDirty())

    def test_local_language_keeps_label_functions_working(self):
        """Подписи не должны пропадать при местных названиях.

        Переменная языка сокращений должна оставаться непустой: пустое
        значение удаляется из переменных проекта, и тогда в функции
        подписи приходит NULL — выражение возвращает пустоту и название
        на карте исчезает.
        """
        from qgis.core import QgsExpression, QgsExpressionContext, QgsProject
        from qgis_xtopo.core.expressions import load_expressions
        from qgis_xtopo.core.map_language import apply_map_language

        load_expressions()
        project = QgsProject()
        apply_map_language("local", project=project)
        self.assertEqual(project.customVariables()["name_pref_suf_lang"], "ru")

        context = QgsExpressionContext()
        expression = QgsExpression(
            "format_place_name('Горки', 999, 'ru')"
        )
        expression.prepare(context)
        self.assertEqual(expression.evaluate(context), "Горки")

    def test_local_names_use_russian_abbreviations(self):
        """Местные названия сокращаются так же, как русские.

        Карта русская, поэтому тип объекта пишется по-русски при любом
        языке названий: «ледник» — «л.», «мыс» — «м.».
        """
        from qgis.core import QgsExpression, QgsExpressionContext, QgsProject
        from qgis_xtopo.core.expressions import load_expressions
        from qgis_xtopo.core.map_language import apply_map_language

        load_expressions()
        project = QgsProject()
        apply_map_language("local", project=project)
        language = project.customVariables()["name_pref_suf_lang"]

        context = QgsExpressionContext()
        glacier = QgsExpression(
            "format_glacier_name(format_peak_pass_name('Геналдонский ледник'),"
            " '%s')" % language
        )
        glacier.prepare(context)
        self.assertEqual(glacier.evaluate(context), "Геналдонский л.")

        cape = QgsExpression("format_cape_name('мыс Казанский', '%s')" % language)
        cape.prepare(context)
        self.assertEqual(cape.evaluate(context), "м. Казанский")

    def test_label_expression_keeps_name_for_local_language(self):
        """Выражение подписи (как в шаблоне) не должно давать пустоту.

        Если переменная языка сокращений исчезает из проекта, QGIS не
        вызывает функцию подписи вовсе и название на карте пропадает.
        """
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextScope,
            QgsExpressionContextUtils,
            QgsFeature,
            QgsProject,
            QgsVectorLayer,
        )
        from qgis_xtopo.core.expressions import load_expressions
        from qgis_xtopo.core.map_language import apply_map_language

        load_expressions()
        project = QgsProject()
        apply_map_language("local", project=project)

        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=name:string", "places", "memory"
        )
        feature = QgsFeature(layer.fields())
        feature.setAttribute("name", "Горки")
        layer.dataProvider().addFeature(feature)
        layer.updateExtents()
        feature = next(layer.getFeatures())
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.projectScope(project))
        context.appendScopes(
            QgsExpressionContextUtils.globalProjectLayerScopes(layer)
        )
        scope = QgsExpressionContextScope()
        scope.setFeature(feature)
        context.appendScope(scope)

        expression = QgsExpression(
            "CASE WHEN attribute(@name_tag_1) IS NOT NULL THEN "
            "format_place_name(attribute(@name_tag_1), 999, @name_pref_suf_lang) "
            "END"
        )
        expression.prepare(context)
        self.assertEqual(expression.evaluate(context), "Горки")

    def test_transliteration_variable(self):
        from qgis.core import QgsProject
        from qgis_xtopo.core import map_language

        saved = map_language.transliteration_enabled()
        self.addCleanup(map_language.set_transliteration, saved, False)
        map_language.set_transliteration(True)
        project = QgsProject()
        map_language.apply_map_language("en", project=project)
        self.assertEqual(project.customVariables()["name_translit"], "1")
        self.assertEqual(project.customVariables()["name_local"], "0")
        self.assertEqual(project.customVariables()["name_script"], "latin")

        # Кириллические языки тоже умеют транслитерацию: кириллица
        # знакома их читателю, а чужая письменность переводится в латиницу.
        map_language.apply_map_language("ru", project=project)
        self.assertEqual(project.customVariables()["name_translit"], "1")
        self.assertEqual(project.customVariables()["name_script"], "cyrl")

    def test_language_scripts(self):
        from qgis_xtopo.core import map_language

        self.assertEqual(map_language.script_for("ru"), "cyrl")
        self.assertEqual(map_language.script_for("uk"), "cyrl")
        self.assertEqual(map_language.script_for("el"), "grek")
        self.assertEqual(map_language.script_for("ka"), "geor")
        self.assertEqual(map_language.script_for("he"), "hebr")
        self.assertEqual(map_language.script_for("en"), "latin")
        self.assertEqual(map_language.script_for("local"), "local")

    def test_other_language_uses_name_tag(self):
        from qgis.core import QgsProject
        from qgis_xtopo.core.map_language import apply_map_language

        project = QgsProject()
        apply_map_language("de", project=project)
        variables = project.customVariables()
        self.assertEqual(variables["name_tag_1"], "name:de")
        self.assertEqual(variables["name_tag_2"], "name")
        self.assertNotIn("name_tag_3", variables)
        self.assertEqual(variables["name_pref_suf_lang"], "de")

    def test_selected_language_is_used_and_stored(self):
        from qgis.core import QgsProject
        from qgis_xtopo.core import map_language

        saved = map_language.language()
        saved_setting = map_language.settings().value(map_language.SETTINGS_KEY)

        def restore():
            map_language.set_map_language(saved, save=False)
            settings = map_language.settings()
            if saved_setting is None:
                settings.remove(map_language.SETTINGS_KEY)
            else:
                settings.setValue(
                    map_language.SETTINGS_KEY, saved_setting
                )

        self.addCleanup(restore)
        map_language.set_map_language("fr")
        self.assertEqual(
            map_language.settings().value(map_language.SETTINGS_KEY), "fr"
        )
        project = QgsProject()
        tags = map_language.apply_map_language(project=project)
        self.assertEqual(tags, ("name:fr", "name"))
        self.assertEqual(project.customVariables()["name_tag_1"], "name:fr")


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class DockLanguageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis_xtopo.core import basemap, i18n, map_language, text_size

        self.i18n = i18n
        self.map_language = map_language
        self.basemap = basemap
        self.text_size = text_size
        self.saved = i18n.language()
        i18n.set_language("ru", save=False)
        self.settings = i18n.settings()
        self.saved_setting = self.settings.value(i18n.SETTINGS_KEY)
        self.saved_map = map_language.language()
        map_language.set_map_language("ru", save=False)
        self.saved_map_setting = self.settings.value(map_language.SETTINGS_KEY)
        self.saved_translit = map_language.transliteration_enabled()
        map_language.set_transliteration(False, save=False)
        self.saved_translit_setting = self.settings.value(
            map_language.SETTINGS_KEY_TRANSLIT
        )
        self.saved_detail = map_language.level_of_detail()
        map_language.set_level_of_detail(
            map_language.DEFAULT_LEVEL_OF_DETAIL, save=False
        )
        self.saved_detail_setting = self.settings.value(
            map_language.SETTINGS_KEY_DETAIL
        )
        self.saved_text_size = text_size.percent()
        text_size.set_percent(text_size.DEFAULT_PERCENT, save=False)
        self.saved_text_size_setting = self.settings.value(
            text_size.SETTINGS_KEY
        )
        self.saved_basemap_setting = self.settings.value(
            basemap.SETTINGS_KEY
        )
        basemap.set_last_source(basemap.DEFAULT_SOURCE, save=False)

    def tearDown(self):
        self.i18n.set_language(self.saved, save=False)
        if self.saved_setting is None:
            self.settings.remove(self.i18n.SETTINGS_KEY)
        else:
            self.settings.setValue(
                self.i18n.SETTINGS_KEY, self.saved_setting
            )
        self.map_language.set_map_language(self.saved_map, save=False)
        if self.saved_map_setting is None:
            self.settings.remove(self.map_language.SETTINGS_KEY)
        else:
            self.settings.setValue(
                self.map_language.SETTINGS_KEY, self.saved_map_setting
            )
        self.map_language.set_transliteration(
            self.saved_translit, save=False
        )
        if self.saved_translit_setting is None:
            self.settings.remove(self.map_language.SETTINGS_KEY_TRANSLIT)
        else:
            self.settings.setValue(
                self.map_language.SETTINGS_KEY_TRANSLIT,
                self.saved_translit_setting,
            )
        self.map_language.set_level_of_detail(self.saved_detail, save=False)
        if self.saved_detail_setting is None:
            self.settings.remove(self.map_language.SETTINGS_KEY_DETAIL)
        else:
            self.settings.setValue(
                self.map_language.SETTINGS_KEY_DETAIL,
                self.saved_detail_setting,
            )
        self.text_size.set_percent(self.saved_text_size, save=False)
        if self.saved_text_size_setting is None:
            self.settings.remove(self.text_size.SETTINGS_KEY)
        else:
            self.settings.setValue(
                self.text_size.SETTINGS_KEY,
                self.saved_text_size_setting,
            )
        if self.saved_basemap_setting is None:
            self.settings.remove(self.basemap.SETTINGS_KEY)
        else:
            self.settings.setValue(
                self.basemap.SETTINGS_KEY,
                self.saved_basemap_setting,
            )

    def _dock(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        self.addCleanup(dock.detach_language_listener)
        self.addCleanup(dock.deleteLater)
        return dock

    def test_button_switches_interface(self):
        dock = self._dock()
        self.assertEqual(dock.tabs.tabText(0), "Данные")
        self.assertEqual(dock.language_button.text(), "RU")
        self.assertEqual(dock.prepare_button.text(), "Подготовить данные")

        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(dock.tabs.tabText(0), "Data")
        self.assertEqual(dock.language_button.text(), "EN")
        self.assertEqual(dock.prepare_button.text(), "Prepare data")

        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(dock.tabs.tabText(0), "Данные")
        self.assertEqual(dock.language_button.text(), "RU")

    def test_saved_language_is_restored(self):
        self.settings.setValue(self.i18n.SETTINGS_KEY, "en")
        self.i18n.load_language()
        dock = self._dock()
        self.assertEqual(dock.tabs.tabText(0), "Data")
        self.assertEqual(dock.language_button.text(), "EN")

    def test_button_fits_tab_header(self):
        dock = self._dock()
        bar = dock.tabs.tabBar()
        height = dock.language_button.height()
        self.assertGreaterEqual(height, 20)
        self.assertLessEqual(height, bar.sizeHint().height())
        self.assertIn("border", dock.language_button.styleSheet())

    def test_query_names_stay_file_names(self):
        from qgis_xtopo.core.resources import RESOURCES_DIR

        dock = self._dock()
        for _ in range(2):
            dock._switch_language()
            self.app.processEvents()
            names = dock._selected_queries()
            self.assertTrue(names)
            for name in names:
                path = RESOURCES_DIR / "queries" / f"{name}.txt"
                self.assertTrue(path.exists(), name)

    def test_default_queries_are_selected_by_name(self):
        from qgis_xtopo.core.config import default_queries

        dock = self._dock()
        dock._select_default_queries()
        selected = set(dock._selected_queries())
        self.assertEqual(selected, set(default_queries()))

    def test_button_does_not_change_map_language(self):
        dock = self._dock()
        combo = dock.project_panel.map_language_combo
        self.assertEqual(combo.currentData(), "ru")
        self.assertEqual(combo.short_text(), "RU")

        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(self.map_language.language(), "ru")
        self.assertEqual(combo.currentData(), "ru")
        self.assertEqual(combo.itemText(0), "Local names")
        self.assertEqual(combo.itemText(2), "English")
        self.assertEqual(combo.short_text(), "RU")

        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(combo.itemText(0), "Местные названия")

    def test_map_language_combo_list(self):
        dock = self._dock()
        combo = dock.project_panel.map_language_combo
        codes = [combo.itemData(index) for index in range(combo.count())]
        expected = [code for code, _label, _script in self.map_language.MAP_LANGUAGES]
        self.assertEqual(codes, expected)
        self.assertEqual(codes[:3], ["local", "ru", "en"])
        self.assertEqual(combo.itemText(0), "Местные названия")
        self.assertEqual(combo.itemText(1), "Русский")
        self.assertEqual(combo.itemText(2), "Английский")
        self.assertEqual(combo.currentData(), "ru")

    def test_language_list_size_and_order(self):
        codes = [code for code, _label, _script in self.map_language.MAP_LANGUAGES]
        labels = [label for _code, label, _script in self.map_language.MAP_LANGUAGES]
        self.assertEqual(len(codes), 50)
        self.assertEqual(len(set(codes)), 50)
        self.assertEqual(codes[:3], ["local", "ru", "en"])
        # После английского языки идут по алфавиту.
        self.assertEqual(labels[3:], sorted(labels[3:]))

    def test_transliteration_is_unavailable_for_local_names(self):
        dock = self._dock()
        panel = dock.project_panel
        panel.setEnabled(True)
        combo = panel.map_language_combo
        check = panel.transliteration_check

        combo.setCurrentIndex(combo.findData("ru"))
        self.app.processEvents()
        self.assertTrue(check.isEnabled())

        combo.setCurrentIndex(combo.findData("en"))
        self.app.processEvents()
        self.assertTrue(check.isEnabled())

        # Для местных названий запасного варианта нет.
        combo.setCurrentIndex(combo.findData("local"))
        self.app.processEvents()
        self.assertFalse(check.isEnabled())
        self.assertFalse(check.isChecked())
        self.assertFalse(self.map_language.transliteration_enabled())

    def test_transliteration_state_follows_checkbox(self):
        dock = self._dock()
        panel = dock.project_panel
        combo = panel.map_language_combo
        check = panel.transliteration_check
        combo.setCurrentIndex(combo.findData("en"))
        self.app.processEvents()

        check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.map_language.transliteration_enabled())

        check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.map_language.transliteration_enabled())

    def test_transliteration_setting_is_saved(self):
        dock = self._dock()
        panel = dock.project_panel
        combo = panel.map_language_combo
        combo.setCurrentIndex(combo.findData("en"))
        self.app.processEvents()
        panel.transliteration_check.setChecked(True)
        self.app.processEvents()
        self.assertEqual(
            self.settings.value(self.map_language.SETTINGS_KEY_TRANSLIT), "1"
        )
        panel.transliteration_check.setChecked(False)
        self.app.processEvents()
        self.assertEqual(
            self.settings.value(self.map_language.SETTINGS_KEY_TRANSLIT), "0"
        )

    def test_map_language_selection_is_saved(self):
        dock = self._dock()
        combo = dock.project_panel.map_language_combo
        combo.setCurrentIndex(combo.findData("de"))
        self.assertEqual(self.map_language.language(), "de")
        self.assertEqual(combo.short_text(), "DE")
        self.assertEqual(
            self.settings.value(self.map_language.SETTINGS_KEY), "de"
        )

    def test_detail_combo_follows_map_language(self):
        dock = self._dock()
        panel = dock.project_panel
        self.assertEqual(
            panel.detail_combo.currentData(),
            self.map_language.DEFAULT_LEVEL_OF_DETAIL,
        )
        layout = panel.layout()
        self.assertLess(
            layout.indexOf(panel.map_language_row),
            layout.indexOf(panel.detail_row),
        )
        row_layout = panel.detail_row.layout()
        widgets = [
            row_layout.itemAt(i).widget() for i in range(row_layout.count())
        ]
        self.assertLess(
            widgets.index(panel.detail_label),
            widgets.index(panel.detail_combo),
        )
        self.assertEqual(
            [panel.detail_combo.itemData(i)
             for i in range(panel.detail_combo.count())],
            list(self.map_language.DETAIL_LEVELS),
        )

    def test_detail_selection_is_saved(self):
        dock = self._dock()
        combo = dock.project_panel.detail_combo
        combo.setCurrentIndex(combo.findData(6))
        self.assertEqual(self.map_language.level_of_detail(), 6)
        self.assertEqual(
            self.settings.value(self.map_language.SETTINGS_KEY_DETAIL), "6"
        )

    def test_saved_detail_is_restored(self):
        self.settings.setValue(self.map_language.SETTINGS_KEY_DETAIL, "3")
        self.map_language.load_level_of_detail()
        dock = self._dock()
        self.assertEqual(dock.project_panel.detail_combo.currentData(), 3)

    def test_detail_is_written_to_open_project(self):
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.gui import projectpanel

        dock = self._dock()
        panel = dock.project_panel
        project = QgsProject.instance()
        QgsExpressionContextUtils.setProjectVariable(
            project, self.map_language.DETAIL_VARIABLE, "4"
        )
        try:
            combo = panel.detail_combo
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                combo.setCurrentIndex(combo.findData(5))
                self.app.processEvents()
            self.assertEqual(
                project.customVariables()[self.map_language.DETAIL_VARIABLE],
                "5",
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, self.map_language.DETAIL_VARIABLE
            )

    def test_refresh_writes_detail_into_project(self):
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.gui import projectpanel

        dock = self._dock()
        panel = dock.project_panel
        project = QgsProject.instance()
        QgsExpressionContextUtils.removeProjectVariable(
            project, self.map_language.DETAIL_VARIABLE
        )
        try:
            self.map_language.set_level_of_detail(6, save=False)
            combo = panel.detail_combo
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(combo.findData(6))
            finally:
                combo.blockSignals(False)
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.refresh()
            self.assertEqual(
                project.customVariables()[self.map_language.DETAIL_VARIABLE],
                "6",
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, self.map_language.DETAIL_VARIABLE
            )

    def test_detail_labels_follow_interface_language(self):
        dock = self._dock()
        combo = dock.project_panel.detail_combo
        self.assertTrue(combo.itemText(0).startswith("2"))
        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(dock.project_panel.detail_label.text(), "Detail")
        self.assertIn("minimum", combo.itemText(0))
        self.assertIn("main", combo.itemText(1))
        self.assertIn("everything", combo.itemText(4))

    def test_text_size_combo_offers_percents(self):
        dock = self._dock()
        panel = dock.project_panel
        layout = panel.layout()
        self.assertLess(
            layout.indexOf(panel.detail_row),
            layout.indexOf(panel.text_size_row),
        )
        row_layout = panel.text_size_row.layout()
        widgets = [
            row_layout.itemAt(i).widget() for i in range(row_layout.count())
        ]
        self.assertLess(
            widgets.index(panel.text_size_label),
            widgets.index(panel.text_size_combo),
        )
        self.assertEqual(
            [panel.text_size_combo.itemData(i)
             for i in range(panel.text_size_combo.count())],
            list(self.text_size.PERCENTS),
        )

    def test_text_size_selection_is_saved(self):
        dock = self._dock()
        combo = dock.project_panel.text_size_combo
        combo.setCurrentIndex(combo.findData(150))
        self.assertEqual(self.text_size.percent(), 150)
        self.assertEqual(
            self.settings.value(self.text_size.SETTINGS_KEY), "150"
        )

    def test_saved_text_size_is_restored(self):
        self.settings.setValue(self.text_size.SETTINGS_KEY, "75")
        self.text_size.load_percent()
        dock = self._dock()
        self.assertEqual(
            dock.project_panel.text_size_combo.currentData(), 75
        )

    def test_text_size_is_written_to_open_project(self):
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.gui import projectpanel

        dock = self._dock()
        panel = dock.project_panel
        project = QgsProject.instance()
        QgsExpressionContextUtils.setProjectVariable(
            project, self.text_size.VARIABLE, "1"
        )
        try:
            combo = panel.text_size_combo
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                combo.setCurrentIndex(combo.findData(200))
                self.app.processEvents()
            self.assertEqual(
                project.customVariables()[self.text_size.VARIABLE], "2"
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, self.text_size.VARIABLE
            )

    def test_refresh_writes_text_size_into_project(self):
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.gui import projectpanel

        dock = self._dock()
        panel = dock.project_panel
        project = QgsProject.instance()
        QgsExpressionContextUtils.removeProjectVariable(
            project, self.text_size.VARIABLE
        )
        try:
            self.text_size.set_percent(125, save=False)
            combo = panel.text_size_combo
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(combo.findData(125))
            finally:
                combo.blockSignals(False)
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.refresh()
            self.assertEqual(
                project.customVariables()[self.text_size.VARIABLE], "1.25"
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, self.text_size.VARIABLE
            )

    def test_old_project_multiplier_is_not_rewritten(self):
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.gui import projectpanel

        dock = self._dock()
        panel = dock.project_panel
        project = QgsProject.instance()
        QgsExpressionContextUtils.setProjectVariable(
            project, self.text_size.VARIABLE, "1.2"
        )
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.refresh()
            self.assertEqual(
                panel.text_size_combo.currentData(), 125
            )
            self.assertEqual(
                project.customVariables()[self.text_size.VARIABLE], "1.2"
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, self.text_size.VARIABLE
            )

    def test_text_size_labels_follow_interface_language(self):
        dock = self._dock()
        panel = dock.project_panel
        self.assertEqual(panel.text_size_label.text(), "Размер текста")
        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(panel.text_size_label.text(), "Text size")
        self.assertIn("multiplier", panel.text_size_combo.toolTip())

    def test_basemap_combo_offers_satellites(self):
        dock = self._dock()
        panel = dock.project_panel
        layout = panel.layout()
        self.assertLess(
            layout.indexOf(panel.text_size_row),
            layout.indexOf(panel.basemap_row),
        )
        self.assertEqual(
            [panel.basemap_combo.itemText(i)
             for i in range(panel.basemap_combo.count())],
            ["Google sat", "Bing sat", "ArcGIS sat"],
        )
        self.assertEqual(
            [panel.basemap_combo.itemData(i)
             for i in range(panel.basemap_combo.count())],
            ["google", "bing", "arcgis"],
        )
        self.assertFalse(panel.basemap_check.isChecked())
        self.assertFalse(panel.basemap_combo.isEnabled())

    def test_basemap_selection_is_saved(self):
        dock = self._dock()
        combo = dock.project_panel.basemap_combo
        combo.setCurrentIndex(combo.findData("arcgis"))
        self.assertEqual(
            self.settings.value(self.basemap.SETTINGS_KEY), "arcgis"
        )

    def test_saved_basemap_is_restored(self):
        self.settings.setValue(self.basemap.SETTINGS_KEY, "bing")
        dock = self._dock()
        self.assertEqual(
            dock.project_panel.basemap_combo.currentData(), "bing"
        )

    def test_basemap_toggle_adds_and_removes_layer(self):
        from unittest import mock

        from qgis.core import QgsProject
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                combo = panel.basemap_combo
                combo.setCurrentIndex(combo.findData("bing"))
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                self.assertTrue(combo.isEnabled())
                self.assertEqual(
                    self.basemap.active_source(project).key, "bing"
                )
                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                self.assertIsNone(self.basemap.active_source(project))
                self.assertFalse(combo.isEnabled())
        finally:
            self.basemap.remove(project)

    def test_basemap_is_restored_from_project(self):
        from unittest import mock

        from qgis.core import QgsProject
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        self.basemap.apply("arcgis", project)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.refresh()
                self.app.processEvents()
            self.assertTrue(panel.basemap_check.isChecked())
            self.assertEqual(
                panel.basemap_combo.currentData(), "arcgis"
            )
            self.assertTrue(panel.basemap_combo.isEnabled())
        finally:
            self.basemap.remove(project)

    def test_hillshade_returns_after_basemap(self):
        """Отмывка возвращается, когда подложку выключили."""
        from unittest import mock

        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        hillshade = QgsVectorLayer(
            "Polygon?crs=EPSG:4326", "[TERRAIN] hillshade var1", "memory"
        )
        project.addMapLayer(hillshade)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.hillshade_check.setChecked(True)
                self.app.processEvents()
                self.assertTrue(panel.hillshade_check.isChecked())
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                # На подложке отмывка выключена и заблокирована.
                self.assertFalse(panel.hillshade_check.isChecked())
                self.assertFalse(panel.hillshade_check.isEnabled())
                self.assertFalse(
                    panel._layer_visible("[TERRAIN] hillshade var1")
                )
                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                # После выключения подложки отмывка вернулась.
                self.assertTrue(panel.hillshade_check.isChecked())
                self.assertTrue(panel.hillshade_check.isEnabled())
                self.assertTrue(
                    panel._layer_visible("[TERRAIN] hillshade var1")
                )
        finally:
            self.basemap.remove(project)
            project.removeMapLayer(hillshade.id())

    def test_basemap_remembers_hillshade_state(self):
        """Состояние отмывки хранится в слое подложки."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        self.basemap.remove(project)
        try:
            self.basemap.apply("bing", project)
            self.assertFalse(self.basemap.hillshade_state(project))
            self.basemap.set_hillshade_state(True)
            self.assertTrue(self.basemap.hillshade_state(project))
            # Смена источника подложки состояние не теряет.
            self.basemap.apply("arcgis", project)
            self.assertTrue(self.basemap.hillshade_state(project))
        finally:
            self.basemap.remove(project)

    def test_slope_returns_after_basemap(self):
        """Раскраска уклонов возвращается, когда подложку выключили."""
        from unittest import mock

        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        steep = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "[TERRAIN] slope (steep) var1",
            "memory",
        )
        slight = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "[TERRAIN] slope (slight)",
            "memory",
        )
        project.addMapLayer(steep)
        project.addMapLayer(slight)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.steep_check.setChecked(True)
                panel.slight_check.setChecked(True)
                self.app.processEvents()
                self.assertTrue(
                    panel._layer_visible("[TERRAIN] slope (steep) var1")
                )
                self.assertTrue(
                    panel._layer_visible("[TERRAIN] slope (slight)")
                )

                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                # На подложке обе раскраски выключены и заблокированы.
                self.assertFalse(panel.steep_check.isChecked())
                self.assertFalse(panel.slight_check.isChecked())
                self.assertFalse(panel.steep_check.isEnabled())
                self.assertFalse(panel.slight_check.isEnabled())
                self.assertFalse(panel.steep_variant_button.isEnabled())
                self.assertFalse(
                    panel._layer_visible("[TERRAIN] slope (steep) var1")
                )
                self.assertFalse(
                    panel._layer_visible("[TERRAIN] slope (slight)")
                )
                self.assertEqual(
                    self.basemap.slope_state(project), (True, True)
                )

                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                # После выключения подложки обе раскраски вернулись.
                self.assertTrue(panel.steep_check.isChecked())
                self.assertTrue(panel.slight_check.isChecked())
                self.assertTrue(panel.steep_check.isEnabled())
                self.assertTrue(panel.slight_check.isEnabled())
                self.assertTrue(
                    panel._layer_visible("[TERRAIN] slope (steep) var1")
                )
                self.assertTrue(
                    panel._layer_visible("[TERRAIN] slope (slight)")
                )
        finally:
            self.basemap.remove(project)
            project.removeMapLayer(steep.id())
            project.removeMapLayer(slight.id())

    def test_saved_slope_state_returns_after_basemap(self):
        """Сохранённое состояние уклонов переживает открытие проекта."""
        from unittest import mock

        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        steep = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "[TERRAIN] slope (steep) var1",
            "memory",
        )
        slight = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "[TERRAIN] slope (slight)",
            "memory",
        )
        project.addMapLayer(steep)
        project.addMapLayer(slight)
        self.basemap.apply("arcgis", project)
        self.basemap.set_slope_state(True, True)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.refresh()
                self.app.processEvents()
                self.assertFalse(panel.steep_check.isChecked())
                self.assertFalse(panel.slight_check.isChecked())
                self.assertFalse(panel.steep_check.isEnabled())
                self.assertFalse(panel.slight_check.isEnabled())

                panel.basemap_check.setChecked(False)
                self.app.processEvents()
            self.assertTrue(panel.steep_check.isChecked())
            self.assertTrue(panel.slight_check.isChecked())
            self.assertTrue(panel.steep_check.isEnabled())
            self.assertTrue(panel.slight_check.isEnabled())
        finally:
            self.basemap.remove(project)
            project.removeMapLayer(steep.id())
            project.removeMapLayer(slight.id())

    def test_basemap_remembers_slope_state(self):
        """Состояние уклонов хранится в слое подложки."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        self.basemap.remove(project)
        try:
            self.basemap.apply("bing", project)
            self.assertEqual(self.basemap.slope_state(project), (False, False))
            self.basemap.set_slope_state(True, False)
            self.assertEqual(self.basemap.slope_state(project), (True, False))
            # Смена источника подложки состояние не теряет.
            self.basemap.apply("arcgis", project)
            self.assertEqual(self.basemap.slope_state(project), (True, False))
        finally:
            self.basemap.remove(project)

    def test_basemap_labels_follow_interface_language(self):
        dock = self._dock()
        panel = dock.project_panel
        self.assertEqual(
            panel.basemap_check.text(), "Растровая подложка"
        )
        dock._switch_language()
        self.app.processEvents()
        self.assertEqual(panel.basemap_check.text(), "Raster basemap")
        self.assertEqual(
            panel.basemap_invert_check.text(), "Inversion"
        )
        self.assertIn("satellite", panel.basemap_combo.toolTip())
        self.assertIn("light", panel.basemap_invert_check.toolTip())

    def test_inversion_checkbox_fits_panel_width(self):
        """Галка инверсии помещается в панель по ширине."""
        dock = self._dock()
        panel = dock.project_panel
        # Панель сжимают до узкой ширины — строка подложки с галкой
        # инверсии должна помещаться в неё целиком.
        narrow = dock._narrow_minimum_width()
        self.assertLessEqual(
            panel.basemap_row.sizeHint().width(), narrow
        )
        self.assertEqual(
            panel.basemap_invert_check.text(), "Инверсия"
        )

    def test_label_inversion_checkbox_is_disabled_without_basemap(self):
        """Без подложки галка инверсии есть, но недоступна."""
        dock = self._dock()
        panel = dock.project_panel
        self.assertEqual(
            panel.basemap_invert_check.text(), "Инверсия"
        )
        self.assertFalse(panel.basemap_invert_check.isEnabled())
        self.assertFalse(panel.basemap_invert_check.isChecked())

    def test_basemap_enables_inversion_and_blocks_hillshade(self):
        """Подложка включает инверсию и выключает отмывку."""
        from unittest import mock

        from qgis.core import QgsProject
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.hillshade_check.setChecked(True)
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                # Инверсия включается автоматически.
                self.assertTrue(panel.basemap_invert_check.isChecked())
                self.assertTrue(panel.basemap_invert_check.isEnabled())
                self.assertTrue(
                    self.basemap.label_inversion_enabled(project)
                )
                # Отмывку включить нельзя, пока стоит подложка.
                self.assertFalse(panel.hillshade_check.isEnabled())
                self.assertFalse(panel.hillshade_check.isChecked())
                self.assertIn(
                    "растровой подложке",
                    panel.hillshade_check.toolTip(),
                )
                # Подложка выключается — отмывка снова доступна.
                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                self.assertTrue(panel.hillshade_check.isEnabled())
                self.assertFalse(panel.basemap_invert_check.isEnabled())
        finally:
            self.basemap.remove(project)

    def test_basemap_inversion_repaints_track(self):
        """Галка инверсии перекрашивает дороги и возвращает цвета."""
        from unittest import mock

        from qgis.core import (
            QgsProject,
            QgsSimpleLineSymbolLayer,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        dock = self._dock()
        panel = dock.project_panel
        renderer, _labeling = projectpanel._template_layer_objects("track")
        track = QgsVectorLayer(
            "LineString?crs=EPSG:4326", "track", "memory"
        )
        track.setRenderer(renderer)
        project.addMapLayer(track)

        def colors():
            return [
                symbol_layer.color()
                for symbol_layer in self.basemap._symbol_layers_by_key(
                    track
                ).values()
                if isinstance(symbol_layer, QgsSimpleLineSymbolLayer)
            ]

        before = colors()
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                self.assertTrue(colors())
                self.assertTrue(all(
                    color == QColor(self.basemap.ROAD_COLOR)
                    for color in colors()
                ))
                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                self.assertEqual(colors(), before)
        finally:
            project.removeMapLayer(track.id())
            self.basemap.remove(project)

    def test_basemap_does_not_block_isoline_controls(self):
        """Подложка переключает изогипсы сама, но не запрещает их менять."""
        from unittest import mock

        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        renderer, labeling = projectpanel._template_layer_objects(
            projectpanel.ISOLINES_LAYER
        )
        isolines = QgsVectorLayer(
            "LineString?crs=EPSG:4326",
            projectpanel.ISOLINES_LAYER,
            "memory",
        )
        isolines.setRenderer(renderer)
        isolines.setLabeling(labeling)
        project.addMapLayer(isolines)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel.isolines_check.setChecked(True)
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                self.assertTrue(panel.isolines_width_button.isEnabled())
                self.assertTrue(panel.isolines_color_button.isEnabled())
        finally:
            self.basemap.remove(project)
            project.removeMapLayer(isolines.id())

    def test_manual_isoline_choice_survives_basemap(self):
        """Выбранный пользователем цвет изолипс подложка не затирает."""
        from unittest import mock

        from qgis.core import QgsProject, QgsVectorLayer
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        self.basemap.remove(project)
        renderer, labeling = projectpanel._template_layer_objects(
            projectpanel.ISOLINES_LAYER
        )
        isolines = QgsVectorLayer(
            "LineString?crs=EPSG:4326",
            projectpanel.ISOLINES_LAYER,
            "memory",
        )
        isolines.setRenderer(renderer)
        isolines.setLabeling(labeling)
        project.addMapLayer(isolines)
        dock = self._dock()
        panel = dock.project_panel
        try:
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                panel.setEnabled(True)
                panel._set_isoline_color(QColor(255, 0, 0))
                panel.basemap_check.setChecked(True)
                self.app.processEvents()
                # Подложка не переключает цвет, выбранный вручную.
                self.assertEqual(
                    panel._isoline_swatch_color(), QColor(255, 0, 0)
                )
                # Смена источника подложки тоже его не трогает.
                panel.basemap_combo.setCurrentIndex(
                    panel.basemap_combo.findData("bing")
                )
                self.app.processEvents()
                self.assertEqual(
                    panel._isoline_swatch_color(), QColor(255, 0, 0)
                )
                # После выключения подложки выбор остаётся.
                panel.basemap_check.setChecked(False)
                self.app.processEvents()
                self.assertEqual(
                    panel._isoline_swatch_color(), QColor(255, 0, 0)
                )
        finally:
            self.basemap.remove(project)
            project.removeMapLayer(isolines.id())

    def test_saved_map_language_is_restored(self):
        self.settings.setValue(self.map_language.SETTINGS_KEY, "fr")
        self.map_language.load_map_language()
        dock = self._dock()
        combo = dock.project_panel.map_language_combo
        self.assertEqual(combo.currentData(), "fr")
        self.assertEqual(combo.short_text(), "FR")

    def test_dynamic_texts_follow_language(self):
        dock = self._dock()
        url = dock.overpass_server.itemData(1)
        dock._set_server_status(
            url, self.i18n.trf("доступен ({} с)", "0.4")
        )
        self.assertIn("доступен", dock.overpass_server.itemText(1))
        dock._pbf_info_state = ("estimate", 100 * 1048576, 200 * 1048576)
        dock._render_pbf_info()
        self.assertIn("Скачивание", dock.pbf_info.text())

        dock._switch_language()
        self.app.processEvents()
        self.assertIn("reachable", dock.overpass_server.itemText(1))
        self.assertIn("Download", dock.pbf_info.text())

        dock._switch_language()
        self.app.processEvents()
        self.assertIn("доступен", dock.overpass_server.itemText(1))
        self.assertIn("Скачивание", dock.pbf_info.text())

    def test_english_interface_has_no_russian(self):
        dock = self._dock()
        self.i18n.set_language("en", save=False)
        dock.retranslate()
        self.app.processEvents()
        cyrillic = re.compile(r"[А-Яа-яЁё]")
        from qgis.PyQt.QtWidgets import (
            QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit,
            QListWidget, QTabWidget, QWidget,
        )

        found = set()

        def check(text):
            if text and cyrillic.search(text):
                found.add(text)

        for widget in [dock] + dock.findChildren(QWidget):
            if isinstance(widget, QLabel):
                check(widget.text())
            if isinstance(widget, QAbstractButton):
                check(widget.text())
            if isinstance(widget, QGroupBox):
                check(widget.title())
            if isinstance(widget, QComboBox):
                for index in range(widget.count()):
                    check(widget.itemText(index))
            if isinstance(widget, QTabWidget):
                for index in range(widget.count()):
                    check(widget.tabText(index))
            if isinstance(widget, QLineEdit):
                check(widget.placeholderText())
            if isinstance(widget, QListWidget):
                for index in range(widget.count()):
                    item = widget.item(index)
                    if item is not None:
                        check(item.text())
            check(widget.toolTip())
        self.assertEqual(sorted(found), [])


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class CoreMessagesTest(unittest.TestCase):
    """Сообщения ядра переводятся при выводе в журнал."""

    SAMPLES = (
        "Проверка параметров",
        "Обработка слоёв",
        "Береговая линия: подготовка линий",
        "WARNING: Береговая линия не обработана: boom",
        "Загрузка рельефа: 12.3 МБ · 1.0 МБ/с",
        "Карта switzerland: доступна",
        "Карта switzerland: недоступна",
        "Скачана карта Swiss (12.3 МБ)",
        "Загрузка карт: 12% (1.2/3.4 МБ)",
        "Загрузка карт: 12.3 МБ · 2/3 · 1.0 МБ/с",
        "Скачивание карт: 2 шт., 12.5 МБ",
        "Склейка карт osmium (2 шт.)…",
        "Недостаточно места: нужно 1.2 ГБ, свободно 0.5 ГБ",
        "Файл swiss не похож на PBF",
        "Не удалось загрузить индекс Geofabrik: timeout",
        "Не удалось обновить индекс Geofabrik: timeout",
        "Индекс Geofabrik обновлён",
        "Быстрая оценка PBF: 1/4 блоков",
        "PBF: проверено 123 объектов",
        "source.pbf: фильтр по области не работает, файл читается целиком",
        "GeoFabrik: 2 карты обработаны как один источник.",
        "Разделение ответа Overpass: water",
        "name: пакетный ответ не разобран, обычный запрос",
        "Проверка Overpass: https://a",
        "Overpass доступен: https://a",
        "Overpass недоступен: https://a: timeout",
        "WARNING: не удалось разделить ответ Overpass: x",
        "Все ответы Overpass уже в кэше, проверка сервера не требуется",
        "Карта Geofabrik не найдена в индексе: x",
        "Склейка osmium не удалась: x",
        "Ни один Overpass-сервер не прошёл тестовый запрос. timeout",
        "Уберите из имени проекта недопустимые символы: {}. Разрешены буквы (латиница и кириллица), цифры и _ - +.",
        "Отключено: используется DEM из указанного каталога.",
        "Используется рельеф из каталога: /tmp/dem. SRTM не скачивается.",
        "Лист 1 добавлен",
        "Лист 3 удалён",
        "Листы атласа очищены",
        "Экспорт PNG: лист 1 из 3…",
        "Экспорт при 300 dpi может потребовать около 1.2 ГБ памяти.",
        "Снизить разрешение до 150 dpi?",
        "Прозрачность слоя «slope (steep)»",
        "Прозрачность теней рельефа: 50% — исходный вид, меньше — темнее (0% — тени непрозрачные), больше — светлее.",
        "SliceOSM: скачано 12.3 МБ",
        "Склейка вырезанных областей…",
        "osmium: карт 2, память под вырезку 1.5 ГБ",
        "osmium: вырезка области из switzerland-latest.osm.pbf (1/2)…",
        "osmium: поиск горных массивов в switzerland-latest.osm.pbf (1/2)…",
        "source.pbf: файл читается целиком (горные массивы, геометрия вне области)",
        "PBF: индекс GDAL не справился с повторяющимися id, файл читается заново без индекса",
        "wood: отброшено 3 объектов без полигональной геометрии",
        "SliceOSM: загрузка карты плотности узлов",
        "SliceOSM: в очереди 3 заданий",
        "SliceOSM: вырез готов, switzerland",
        "SliceOSM: сборка выреза… 50% (осталось ~1 min)",
        "WARNING: сборка рельефа N42E044 не удалась: boom; повтор",
        "Использован кэш рельефа: повторная обработка не требуется",
        "Использован кэш изолиний: повторная обработка не требуется",
        "WARNING: не удалось подписать ссылку source: boom",
        "WARNING: мало места для предзагрузки рельефа — тайлы читаются по сети",
        "WARNING: не удалось собрать source: boom",
        "WARNING: Функция readable_name не зарегистрирована — подписи показаны без транслитерации. Перезапустите QGIS.",
        "WARNING: поток worker не остановился — принудительное завершение",
        "WARNING: Склеенный файл не читается драйвером GDAL, карты обработаны по отдельности",
        "WARNING: Пики: boom; проверка пропущена",
        "WARNING: POI: boom; подавление наложений пропущено",
        "WARNING: cliff: слой water не найден, шаг пропущен — данные могут быть неполными",
        "PBF: чтение идёт с грубым фильтром по области, остальные объекты файла пропускаются.",
        "PBF: выгрузки объединены в один источник.",
        "PBF: запросы по членам отношений не поддерживаются и пропущены: a, b. Для этих слоёв нужен источник Overpass.",
    )

    ENGLISH_SAMPLES = (
        "Parsed 3 objects",
        "cliff: cached",
        "cliff: retrying another endpoint",
        "cliff: downloaded",
        "PBF: 3 objects, 2 feature matches",
        "Opening source.pbf with GDAL OSM driver (1/2)",
        "PBF: file 1/2 processed",
        "PBF: processed 3 objects (12%)",
        "cliff: saved out.sqlite",
        "cliff: converted 3 closed lines to polygons",
        "cliff: label lines for 3 objects",
        "cliff: label lines restored from cache",
        "cliff: saved base layer",
        "cliff: skipped (source layer failed)",
        "cliff: no objects, skipped",
        "cliff: skipped (unresolved dependency)",
        "cliff: skipped (boom)",
        "cliff: skipped, source layer failed",
        "cliff: skipped, unresolved dependency",
        "cliff: repairing geometry before retrying \"centroid\"",
        "cliff: boom; layer skipped",
        "Generating slope",
        "Writing slope",
        "Generating hillshade",
        "Generating contour lines",
        "Contour lines cut by water: 3",
        "Glacier contour lines ready: 3",
        "Contour lines outside glaciers: 3",
        "Preparing terrain",
        "Merging 3 DEM tile(s)",
        "Contour lines: 3 segments",
        "WARNING: DEM tile not found: N42E044.tif",
        "Glacier contour lines skipped: 3",
        "Using cached DEM tile N42E044.tif",
        "WARNING: cannot download N42E044.tif: boom",
        "Using N42E044.tif",
        "Contour lines: tile 1 of 2",
        "WARNING: expression slope.py failed: boom",
        "ERROR: Parsed 3 objects",
        "WARNING: cliff: skipped, source layer failed",
    )

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis_xtopo.core import i18n

        self.i18n = i18n
        self.saved = i18n.language()
        i18n.set_language("en", save=False)

    def tearDown(self):
        self.i18n.set_language(self.saved, save=False)

    def test_no_russian_left(self):
        cyrillic = re.compile(r"[А-Яа-яЁё]")
        left = [
            text for text in self.SAMPLES if cyrillic.search(self.i18n.tr(text))
        ]
        self.assertEqual(left, [])

    def test_english_messages_are_translated(self):
        """Английские сообщения журнала переводятся на русский."""
        self.i18n.set_language("ru", save=False)
        unchanged = [
            text
            for text in self.ENGLISH_SAMPLES
            if self.i18n.tr(text) == text
        ]
        self.assertEqual(unchanged, [])

    def test_log_prefixes_are_translated(self):
        """Уровень сообщения переводится, тело остаётся своим."""
        self.i18n.set_language("ru", save=False)
        self.assertEqual(
            self.i18n.tr("WARNING: Generating slope"),
            "ПРЕДУПРЕЖДЕНИЕ: Расчёт уклона",
        )
        self.assertEqual(
            self.i18n.tr("ERROR: Parsed 3 objects"),
            "ОШИБКА: Разобрано 3 объектов",
        )
        self.i18n.set_language("en", save=False)
        self.assertEqual(
            self.i18n.tr("ПРЕДУПРЕЖДЕНИЕ: Расчёт уклона"),
            "WARNING: Generating slope",
        )
        self.assertEqual(
            self.i18n.tr("ОШИБКА: Разобрано 3 объектов"),
            "ERROR: Parsed 3 objects",
        )

    def test_proper_names_are_kept(self):
        """Имена собственные остаются в тексте сообщения."""
        self.i18n.set_language("ru", save=False)
        self.assertIn(
            "switzerland", self.i18n.tr("Карта switzerland: доступна")
        )
        self.assertIn(
            "N42E044.tif",
            self.i18n.tr("WARNING: cannot download N42E044.tif: timeout"),
        )
        self.i18n.set_language("en", save=False)
        self.assertIn(
            "switzerland", self.i18n.tr("Карта switzerland: доступна")
        )
        self.assertIn(
            "N42E044.tif",
            self.i18n.tr("WARNING: cannot download N42E044.tif: timeout"),
        )

    def test_duration_units_are_translated(self):
        from qgis_xtopo.core.extracts import _duration

        self.i18n.set_language("ru", save=False)
        self.assertEqual(_duration(40), "40 с")
        self.assertEqual(_duration(90), "1 мин")
        self.i18n.set_language("en", save=False)
        self.assertEqual(_duration(40), "40 s")
        self.assertEqual(_duration(90), "1 min")

    def test_eta_is_translated(self):
        from qgis_xtopo.core.geofabrik import _format_eta

        self.assertEqual(_format_eta(5), "~5 s")
        self.assertEqual(_format_eta(120), "~2 min")
        self.assertEqual(_format_eta(3900), "~1 h 5 min")

    def test_gdal_summary(self):
        description = self.i18n.tr(
            "незамкнутые кольца в данных OSM (GDAL закрывает их сам)"
        )
        line = self.i18n.trf(
            "GDAL: ошибок «{}» — {}", description, 5
        )
        self.assertEqual(
            line,
            "GDAL: errors “unclosed rings in OSM data (GDAL closes them itself)” — 5",
        )


if __name__ == "__main__":
    unittest.main()
