"""Проверка правил сокращения типовых слов в названиях.

Правила проверяются на реальных названиях из OSM: сокращение должно
менять только слово целиком и не превращать «Островского» в «о.ского».
"""

import unittest


class LabelAbbreviationTests(unittest.TestCase):
    """Сокращения в выражениях подписей."""

    @classmethod
    def setUpClass(cls):
        from qgis.core import QgsApplication
        from qgis_xtopo.core.expressions import load_expressions

        cls.app = QgsApplication.instance()
        if cls.app is None:
            cls.app = QgsApplication([], False)
            cls.app.initQgis()
        load_expressions()

    def _evaluate(self, expression):
        from qgis.core import QgsExpression, QgsExpressionContext

        context = QgsExpressionContext()
        prepared = QgsExpression(expression)
        prepared.prepare(context)
        return prepared.evaluate(context)

    def water(self, name):
        return self._evaluate("format_water_name('%s')" % name)

    def place(self, name):
        return self._evaluate("format_place_name('%s', 999, 'ru')" % name)

    def uninhabited_place(self, name, pref="ru", local="0"):
        """Подпись населённого пункта с population=0."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        old = project.customVariables()
        project.setCustomVariables(dict(old, name_pref_suf_lang=pref,
                                        name_local=local))
        try:
            return self._evaluate(
                "format_place_name('%s', '0', '%s')"
                % (name.replace("'", "''"), pref)
            )
        finally:
            project.setCustomVariables(old)

    def peak(self, name):
        return self._evaluate("format_peak_pass_name('%s')" % name)

    def glacier(self, name):
        """Подпись ледника: как в стиле — сначала правило для вершин."""
        return self._evaluate(
            "format_glacier_name(format_peak_pass_name('%s'), 'ru')" % name
        )

    def test_water_type_words_are_shortened(self):
        """Типовые слова водных объектов сокращаются в любом месте."""
        self.assertEqual(self.water("река Адегой"), "р. Адегой")
        self.assertEqual(self.water("Тихая Речка"), "Тихая р.")
        self.assertEqual(self.water("1-й водопад Куаго"), "1-й вдп. Куаго")
        self.assertEqual(self.water("Старый канал"), "Старый кан.")
        self.assertEqual(self.water("Ангелинский Ерик"), "Ангелинский ер.")
        self.assertEqual(self.water("Большое озеро Казачье"), "Бол. оз. Казачье")

    def test_water_words_are_not_cut_inside_a_word(self):
        """«Прудовая» и «Ручейка» — имена, а не типы объектов."""
        self.assertEqual(self.water("Прудовая"), "Прудовая")
        self.assertEqual(self.water("Прудная"), "Прудная")
        self.assertEqual(self.water("Прудок-накопитель"), "Прудок-накопитель")
        self.assertEqual(self.water("канал Ручейка"), "кан. Ручейка")
        self.assertEqual(self.water("Бассейн-накопитель"), "Бассейн-накопитель")

    def test_water_abbreviation_keeps_space(self):
        """После сокращения должен оставаться пробел."""
        self.assertEqual(self.water("озёра Иовпыттапытур"), "оз. Иовпыттапытур")
        self.assertEqual(self.water("Озёра Душниковы"), "оз. Душниковы")

    def test_place_type_words_are_shortened(self):
        """Типовые слова населённых пунктов сокращаются."""
        self.assertEqual(self.place("урочище Волчьи Ворота"), "ур. Волчьи Ворота")
        self.assertEqual(self.place("Северный посёлок"), "Северный пос.")
        self.assertEqual(self.place("Мыс Казанский"), "м. Казанский")
        self.assertEqual(self.place("База отдыха"), "б.о.")

    def test_place_words_are_not_cut_inside_a_word(self):
        """Похожие слова не должны превращаться в сокращения."""
        self.assertEqual(self.place("Николая Островского"), "Николая Островского")
        self.assertEqual(self.place("Летниково"), "Летниково")
        self.assertEqual(self.place("Ярыженский"), "Ярыженский")
        self.assertEqual(self.place("Преображенская"), "Преображенская")
        self.assertEqual(self.place("Дагомыс"), "Дагомыс")
        self.assertEqual(self.place("Остров"), "Остров")

    def test_uninhabited_mark_is_hidden_for_non_cyrillic_local_names(self):
        """На «Местных названиях» латинское имя не получает «(неж.)»."""
        self.assertEqual(self.uninhabited_place("Nadăș", local="1"), "Nadăș")

    def test_uninhabited_mark_is_hidden_for_non_russian_map(self):
        """На нерусской карте латинское имя не получает «(неж.)»."""
        self.assertEqual(
            self.uninhabited_place("Nadăș", pref="en", local="0"), "Nadăș")

    def test_uninhabited_mark_stays_on_russian_map(self):
        """На русской карте пометка остаётся даже у латинского имени."""
        self.assertEqual(
            self.uninhabited_place("Nadăș"), "Nadăș (неж.)")

    def test_uninhabited_mark_stays_for_cyrillic_name(self):
        """Кириллическое имя сохраняет «(неж.)» при любом языке."""
        self.assertEqual(
            self.uninhabited_place("Ивановка", pref="en"),
            "Ивановка (неж.)",
        )

    def test_peak_and_pass_names_lose_extra_spaces(self):
        """«Перевал» убирается вместе с пробелом."""
        self.assertEqual(self.peak("Перевал Муркин"), "Муркин")
        self.assertEqual(self.peak("Оштеновский перевал"), "Оштеновский")
        self.assertEqual(self.peak("пер. Борзова-2"), "Борзова-2")

    def test_peak_word_inside_a_name_is_kept(self):
        """«Перевальная» — это название, а не тип объекта."""
        self.assertEqual(self.peak("Перевальная Южная"), "Перевальная Юж.")
        self.assertEqual(self.peak("Камень Перевалочный"), "Камень Перевалочный")

    def test_glacier_word_is_shortened_as_whole_word(self):
        """«Ледник» сокращается, «Ледниковый» — нет."""
        self.assertEqual(self.glacier("Ледник Малый"), "л. Мал.")
        self.assertEqual(self.glacier("Ледники Кавказа"), "л. Кавказа")
        self.assertEqual(self.glacier("Ледниковый"), "Ледниковый")

    def test_resort_keeps_name_when_no_rule_matched(self):
        """Название объекта не должно пропадать из подписи."""
        self.assertEqual(self._evaluate("format_resort_name('Сосна', '', '')"), "Сосна")
        self.assertEqual(
            self._evaluate("format_resort_name('База отдыха', '', '')"), "б.о."
        )

    def test_camp_site_hides_generic_names(self):
        """Общие описания места на карте не показываются."""
        self.assertEqual(self._evaluate("format_camp_site_name('Кемпинг')"), "")
        self.assertEqual(
            self._evaluate("format_camp_site_name('Палаточный лагерь Увильды')"), ""
        )

    def test_russian_type_is_not_added_to_foreign_name(self):
        """Русский тип объекта не приписывается названию не кириллицей.

        К грузинскому или латинскому названию «л. », «м. », «хр. » и «ур. »
        приписывать нечего: подпись остаётся местной.
        """
        self.assertEqual(self.glacier("შინო"), "შინო")
        self.assertEqual(self.glacier("Shinto"), "Shinto")
        self.assertEqual(
            self._evaluate("format_cape_name('მესტე', 'ru')"), "მესტე"
        )
        self.assertEqual(
            self._evaluate("format_cape_name('Cape Horn', 'ru')"), "Cape Horn"
        )
        self.assertEqual(
            self._evaluate("format_ridge_name('მთა', 'ru')"), "მთა"
        )
        self.assertEqual(
            self._evaluate("format_locality_name('სოხუმი', 'ru')"), "სოხუმი"
        )

    def test_russian_type_is_added_to_cyrillic_name(self):
        """Название кириллицей сокращается как обычно."""
        self.assertEqual(self.glacier("Ледник Малый"), "л. Мал.")
        self.assertEqual(self.glacier("Шинто"), "л. Шинто")
        self.assertEqual(
            self._evaluate("format_cape_name('мыс Казанский', 'ru')"), "м. Казанский"
        )
        self.assertEqual(
            self._evaluate("format_ridge_name('Главный хребет', 'ru')"),
            "Главный хр.",
        )
        self.assertEqual(
            self._evaluate("format_locality_name('Волчьи Ворота', 'ru')"),
            "ур. Волчьи Ворота",
        )

    def test_name_equal_to_type_word_is_not_doubled(self):
        """Имя из одного слова типа не дополняется префиксом.

        Иначе получается «м. Мыс» или «хр. Хребет».  В группе
        населённых пунктов так же оставлено «Остров», а не «о. Остров».
        """
        self.assertEqual(self._evaluate("format_cape_name('Мыс', 'ru')"), "Мыс")
        self.assertEqual(self._evaluate("format_cape_name('мыс.', 'ru')"), "мыс.")
        self.assertEqual(self._evaluate("format_ridge_name('Хребет', 'ru')"), "Хребет")
        self.assertEqual(self._evaluate("format_ridge_name('отрог', 'ru')"), "отрог")
        self.assertEqual(
            self._evaluate("format_locality_name('Урочище', 'ru')"), "Урочище"
        )

    def test_type_word_with_dot_is_recognized(self):
        """Тип объекта с точкой («мыс. Казанский») распознаётся."""
        self.assertEqual(
            self._evaluate("format_cape_name('мыс. Казанский', 'ru')"), "м. Казанский"
        )
        self.assertEqual(
            self._evaluate("format_ridge_name('хребет. Буцибаци', 'ru')"),
            "хр. Буцибаци",
        )

    def test_locality_type_word_is_replaced(self):
        """В группе урочищ слово «урочище» заменяется, а не дублируется."""
        self.assertEqual(
            self._evaluate("format_locality_name('урочище Волчьи Ворота', 'ru')"),
            "ур. Волчьи Ворота",
        )

    def test_gully_word_is_shortened_in_water_names(self):
        """«Балка» сокращается и в водной группе, как в группе мест.

        Иначе один и тот же объект на разных слоях подписывался бы
        по-разному: «б. Капельная» и «балка Капельная».
        """
        self.assertEqual(self.water("балка Капельная"), "б. Капельная")
        self.assertEqual(self.water("Горькая балка"), "Горькая б.")
        self.assertEqual(self.water("Балка Богословская"), "б. Богословская")
        # Ведущее слово типа сокращается, а слово внутри названия — нет.
        self.assertEqual(self.water("балка Третья речка"), "б. Третья речка")
        # Название из одного слова типа подписи не даёт.
        self.assertEqual(self.water("Балка"), "")
        # Остальные типы воды не изменились.
        self.assertEqual(self.water("река Адегой"), "р. Адегой")
        self.assertEqual(self.water("ручей Свинячий"), "руч. Свинячий")

    def pass_label(self, name, rtsa="н/к", ele=2800, scale=50000,
                   pref="ru", local="0", with_local=True):
        """Подпись перевала с заданными языком карты и названием."""
        from qgis.core import (
            QgsExpression, QgsExpressionContext, QgsExpressionContextUtils,
            QgsProject,
        )

        project = QgsProject.instance()
        scope = QgsExpressionContextUtils.projectScope(project)
        keys = ("name_pref_suf_lang", "name_local")
        previous = {key: scope.variable(key) for key in keys}

        def restore():
            for key, value in previous.items():
                if value is None:
                    QgsExpressionContextUtils.removeProjectVariable(
                        project, key
                    )
                else:
                    QgsExpressionContextUtils.setProjectVariable(
                        project, key, value
                    )

        self.addCleanup(restore)
        QgsExpressionContextUtils.setProjectVariable(
            project, "name_pref_suf_lang", pref
        )
        QgsExpressionContextUtils.setProjectVariable(
            project, "name_local", local
        )
        context = QgsExpressionContext()
        context.appendScope(
            QgsExpressionContextUtils.projectScope(project)
        )
        tail = ", @name_local" if with_local else ""
        expression = QgsExpression(
            "compose_mountain_pass_name('%s', %s, '%s', %s, "
            "@name_pref_suf_lang%s)"
            % (name.replace("'", "''"), ele, rtsa, scale, tail)
        )
        expression.prepare(context)
        return expression.evaluate(context)

    def test_pass_category_is_hidden_for_non_cyrillic_name(self):
        """«н/к» и категория не приписываются имени не кириллицей."""
        self.assertEqual(
            self.pass_label("Col de la Croix", pref="en"),
            "Col de la Croix\n2800 m",
        )
        self.assertNotIn(
            "1А", self.pass_label("Col de la Croix", rtsa="1А", pref="en")
        )

    def test_pass_category_stays_on_russian_map(self):
        """На русской карте старое поведение: категория у имени."""
        self.assertEqual(
            self.pass_label("Col de la Croix", pref="ru", local="0"),
            "Col de la Croix\nн/к 2800 м",
        )

    def test_pass_category_is_hidden_for_local_names(self):
        """«Местные названия» — не русская карта, категория снимается."""
        self.assertEqual(
            self.pass_label("Col de la Croix", pref="ru", local="1"),
            "Col de la Croix\n2800 м",
        )

    def test_pass_category_stays_for_cyrillic_name(self):
        """Кириллическое имя сохраняет категорию при любом языке."""
        text = self.pass_label("Перевал Кату-Ярык", pref="en")
        self.assertIn("н/к", text)
        self.assertIn("Перевал Кату-Ярык", text)

    def test_pass_category_stays_without_name(self):
        """Без названия категория остаётся: это не имя не кириллицей."""
        self.assertEqual(self.pass_label("", pref="en"), "н/к 2800 m")

    def test_pass_old_project_expression_still_works(self):
        """Проект со старым шаблоном передаёт пять аргументов."""
        self.assertEqual(
            self.pass_label(
                "Col de la Croix", pref="ru", local="1", with_local=False,
            ),
            "Col de la Croix\n2800 м",
        )


if __name__ == "__main__":
    unittest.main()
