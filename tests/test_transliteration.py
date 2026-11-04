import unittest

from qgis_xtopo.core.transliteration import (
    TRANS_TAG1,
    pick_name,
    pick_name_part,
    TRANS_TAG2,
    _capitalize_words as _capitalize,
    add_transliteration,
    functions_available,
    readable_name,
    script_of,
    strip_transliteration,
    transliterate_text,
)

EXPRESSION = (
    "CASE\n"
    "\tWHEN attribute(@name_tag_1) IS NOT NULL THEN "
    "format_place_name(attribute(@name_tag_1),999,@name_pref_suf_lang)\n"
    "\tWHEN attribute(@name_tag_2) IS NOT NULL THEN "
    "format_place_name(attribute(@name_tag_2),999,@name_pref_suf_lang)\n"
    "\tWHEN attribute(@name_tag_3) IS NOT NULL THEN attribute(@name_tag_3)\n"
    "END"
)

OLD_EXPRESSION = (
    "CASE\n"
    "\tWHEN attribute(@name_tag_1) IS NOT NULL THEN "
    "IF(@name_translit='1' AND @name_local='1',"
    "transliterate(attribute(@name_tag_1)),attribute(@name_tag_1))\n"
    "\tWHEN attribute(@name_tag_2) IS NOT NULL THEN "
    "IF(@name_translit='1',"
    "transliterate(attribute(@name_tag_2)),attribute(@name_tag_2))\n"
    "\tWHEN attribute(@name_tag_3) IS NOT NULL THEN attribute(@name_tag_3)\n"
    "END"
)


class TransliterationTest(unittest.TestCase):
    def test_russian_is_transliterated(self):
        self.assertEqual(transliterate_text("Москва"), "Moskva")
        self.assertEqual(
            transliterate_text("Санкт-Петербург"), "Sankt-Peterburg"
        )

    def test_other_scripts(self):
        self.assertEqual(transliterate_text("საქართველო"), "Sakartvelo")
        self.assertEqual(transliterate_text("Հայաստան"), "Hayastan")
        self.assertEqual(transliterate_text("תל אביב"), "Tl Abyb")
        self.assertEqual(transliterate_text("กรุงเทพ"), "Krungethph")
        self.assertEqual(transliterate_text("서울"), "Seoul")

    def test_words_are_capitalized(self):
        self.assertEqual(transliterate_text("москва"), "Moskva")
        self.assertEqual(transliterate_text("санкт-петербург"), "Sankt-Peterburg")
        self.assertEqual(transliterate_text("42-я улица"), "42-Ya Ulitsa")
        self.assertEqual(transliterate_text("«тбилиси»"), "«Tbilisi»")
        # Метка направления текста (RLM) в начале не мешает заглавной букве.
        self.assertEqual(transliterate_text("\u200fתל אביב"), "\u200fTl Abyb")

    def test_capitalization_keeps_the_rest(self):
        # Регистр остальных букв не меняется, цифра не даёт «42Nd».
        self.assertEqual(_capitalize("l'viv"), "L'viv")
        self.assertEqual(_capitalize("42nd street"), "42nd Street")
        self.assertEqual(_capitalize("sakArtvelo"), "SakArtvelo")

    def test_latin_text_is_unchanged(self):
        self.assertEqual(transliterate_text("Bern"), "Bern")
        self.assertEqual(transliterate_text(""), "")

    def test_unknown_script_is_unchanged(self):
        self.assertEqual(transliterate_text("東京"), "東京")

    def test_script_detection(self):
        self.assertEqual(script_of("Москва"), "cyrl")
        self.assertEqual(script_of("Berlin"), "latin")
        self.assertEqual(script_of("საქართველო"), "geor")
        self.assertEqual(script_of("123"), None)


class ReadableNameTest(unittest.TestCase):
    def test_known_script_is_kept(self):
        self.assertEqual(readable_name("Москва", "cyrl"), "Москва")
        self.assertEqual(readable_name("საქართველო", "geor"), "საქართველო")
        self.assertEqual(readable_name("Berlin", "latin"), "Berlin")

    def test_unknown_script_is_transliterated(self):
        self.assertEqual(readable_name("Москва", "latin"), "Moskva")
        self.assertEqual(readable_name("საქართველო", "cyrl"), "Sakartvelo")

    def test_local_names_keep_their_script(self):
        self.assertEqual(readable_name("Москва", "local"), "Москва")
        self.assertEqual(readable_name("საქართველო", "local"), "საქართველო")

    def test_script_without_table_is_kept(self):
        self.assertEqual(readable_name("東京", "latin"), "東京")


class PickNamePartTest(unittest.TestCase):
    """Выбор части составного названия для языка карты."""

    def test_russian_part_when_marked_as_translation(self):
        self.assertEqual(
            pick_name_part("ზედა (ru: Монастыри)", "cyrl"), "Монастыри"
        )
        self.assertEqual(
            pick_name_part("ჩხოროწყუ (/ ru: Чхороцку)", "cyrl"), "Чхороцку"
        )

    def test_cyrillic_part_is_taken(self):
        # Два равноправных названия: на русской карте берётся кириллическое.
        self.assertEqual(
            pick_name_part("ზედა / Монастырская", "cyrl"), "Монастырская"
        )
        # Дефис внутри названия частей не делит.
        self.assertEqual(
            pick_name_part("ქართული-Крепость", "cyrl"), "ქართული-Крепость"
        )

    def test_bracket_is_not_a_translation(self):
        # Уточнение в скобках не заменяет название.
        self.assertEqual(
            pick_name_part("Mersedes Benc (грузовой)", "cyrl"), "Mersedes Benc"
        )
        self.assertEqual(pick_name_part("Torex (двери)", "cyrl"), "Torex")
        self.assertEqual(
            pick_name_part("ჭიშველის ჯვარი (MG)", "cyrl"), "ჭიშველის ჯვარი"
        )

    def test_bracket_transliteration_is_not_taken_for_russian(self):
        # Латинская запись рядом с грузинской — для английской карты.
        self.assertEqual(pick_name_part("ზესხო (Zeskho)", "cyrl"), "ზესხო")

    def test_georgian_russian_pair_respects_transliteration(self):
        name = "წითელი ხიდი - Qırmızı Körpü"
        self.assertEqual(pick_name_part(name, "cyrl", False), "წითელი ხიდი")
        self.assertEqual(pick_name_part(name, "cyrl", True), "Tsiteli Khidi")

    def test_transliteration_only_when_enabled(self):
        self.assertEqual(pick_name_part("ანანური", "cyrl", False), "ანანური")
        self.assertEqual(pick_name_part("ანანური", "cyrl", True), "Ananuri")

    def test_cyrillic_is_not_transliterated(self):
        self.assertEqual(pick_name_part("Беслан", "cyrl", True), "Беслан")

    def test_local_names_follow_the_checkbox(self):
        self.assertEqual(pick_name_part("ანანური", "local", True), "ანანური")
        self.assertEqual(pick_name_part("ანანური", "local", False), "ანანური")

    def test_local_names_keep_the_whole_name(self):
        # Местные названия не сокращаются до первой части и не теряют скобки.
        for name in (
            "Тойота Центр (Toyota Center)",
            "ზესხო (Zeskho)",
            "Mersedes Benc (грузовой)",
            "ზედა / Монастырская",
            "მთა ენჩო - Энчо",
            "Каспий теңізі - Hazar deňzi",
        ):
            self.assertEqual(pick_name_part(name, "local", False), name)
            self.assertEqual(pick_name_part(name, "local", True), name)

    def test_part_in_parentheses_follows_language(self):
        self.assertEqual(pick_name_part("Тойота Центр (Toyota Center)", "cyrl"),
                         "Тойота Центр")
        self.assertEqual(pick_name_part("Тойота Центр (Toyota Center)", "latin"),
                         "Тойота Центр")

    def test_hyphen_inside_word_is_kept(self):
        self.assertEqual(pick_name_part("Ново-Афонская", "cyrl"), "Ново-Афонская")

    def test_type_word_is_dropped(self):
        self.assertEqual(pick_name_part("მთა ენჩო - Энчо", "cyrl"), "Энчо")

    def test_empty_and_missing(self):
        self.assertEqual(pick_name_part("", "cyrl"), "")
        self.assertIsNone(pick_name_part(None, "cyrl"))
        self.assertEqual(pick_name("ანანური", None, True), "ანანური")


class ExpressionTest(unittest.TestCase):
    def test_transliteration_is_added(self):
        updated = add_transliteration(EXPRESSION)
        self.assertIn(TRANS_TAG1, updated)
        self.assertIn(TRANS_TAG2, updated)
        self.assertIn("attribute(@name_tag_3)", updated)

    def test_patch_is_idempotent(self):
        once = add_transliteration(EXPRESSION)
        self.assertEqual(add_transliteration(once), once)

    def test_old_form_is_replaced(self):
        updated = add_transliteration(OLD_EXPRESSION)
        self.assertNotIn("transliterate(", updated)
        self.assertIn(TRANS_TAG1, updated)
        self.assertIn(TRANS_TAG2, updated)

    def test_other_expressions_are_untouched(self):
        self.assertEqual(add_transliteration("name"), "name")
        self.assertEqual(add_transliteration(""), "")

    def test_variables_are_used(self):
        updated = add_transliteration(EXPRESSION)
        self.assertIn("@name_translit", updated)
        self.assertIn("@name_script", updated)


class StripTest(unittest.TestCase):
    def test_branch_is_removed(self):
        patched = add_transliteration(EXPRESSION)
        stripped = strip_transliteration(patched)
        self.assertNotIn("readable_name", stripped)
        self.assertIn("attribute(@name_tag_1)", stripped)
        self.assertIn("attribute(@name_tag_2)", stripped)

    def test_old_branch_is_removed(self):
        stripped = strip_transliteration(OLD_EXPRESSION)
        self.assertNotIn("transliterate(", stripped)

    def test_plain_expression_is_untouched(self):
        self.assertEqual(strip_transliteration("name"), "name")

    def test_function_availability(self):
        # Функции выражений в тестах не регистрируются: значит, ветка
        # транслитерации из подписей должна убираться.
        self.assertFalse(functions_available())
