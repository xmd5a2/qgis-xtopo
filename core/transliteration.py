"""Транслитерация названий и «читаемое» запасное название.

Когда для выбранного языка название отсутствует, показывается локальное
`name`. Чтобы оно читалось тем, кто смотрит карту на выбранном языке,
название приводится к понятному виду: если его письменность знакома
читателю — остаётся как есть, иначе переводится в латиницу.

Знакомые письменности заданы для каждого языка: носитель кириллического
языка читает и кириллицу, и латиницу, поэтому грузинское название на
русской карте показывается как «Sakartvelo», а русское — как «Москва».

Латинская запись приводится к виду топонима: заглавная первая буква
каждого слова. Это безопасно и для языков с письмом справа налево:
результат транслитерации — чистая латиница, а ведущие метки направления
текста и знаки препинания пропускаются.

Таблицы и правила определения письменности — в этом модуле, выражение
QGIS с ними работает в `resources/expressions/transliterate.py`.
"""

import re

LATIN = "latin"
CYRL = "cyrl"
GREK = "grek"
GEOR = "geor"
ARM = "arm"
HEBR = "hebr"
ARA = "ara"
THA = "tha"
HANG = "hang"
HANI = "hani"
KANA = "kana"
DEVA = "deva"
LOCAL = "local"

# Знакомые письменности для читателя выбранного языка.
READABLE_SCRIPTS = {
    LATIN: frozenset({LATIN}),
    CYRL: frozenset({LATIN, CYRL}),
    GREK: frozenset({LATIN, GREK}),
    GEOR: frozenset({LATIN, GEOR}),
    ARM: frozenset({LATIN, ARM}),
    HEBR: frozenset({LATIN, HEBR}),
    ARA: frozenset({LATIN, ARA}),
    THA: frozenset({LATIN, THA}),
    HANG: frozenset({LATIN, HANG}),
    HANI: frozenset({LATIN, HANI}),
    "jpan": frozenset({LATIN, HANI, KANA}),
}

# Порядок важен: более узкие диапазоны проверяются первыми.
_SCRIPT_RANGES = (
    (GREK, 0x0370, 0x03FF),
    (CYRL, 0x0400, 0x052F),
    (ARM, 0x0530, 0x058F),
    (HEBR, 0x0590, 0x05FF),
    (ARA, 0x0600, 0x06FF),
    (ARA, 0x0750, 0x077F),
    (DEVA, 0x0900, 0x097F),
    (THA, 0x0E00, 0x0E7F),
    (GEOR, 0x10A0, 0x10FF),
    (KANA, 0x3040, 0x30FF),
    (HANI, 0x3400, 0x4DBF),
    (HANI, 0x4E00, 0x9FFF),
    (HANI, 0xF900, 0xFAFF),
    (HANG, 0xAC00, 0xD7AF),
    (HANG, 0x1100, 0x11FF),
    (HANG, 0x3130, 0x318F),
    (LATIN, 0x0041, 0x005A),
    (LATIN, 0x0061, 0x007A),
    (LATIN, 0x00C0, 0x024F),
    (LATIN, 0x1E00, 0x1EFF),
)

_CYRILLIC_TABLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ґ": "g", "є": "ye", "і": "i", "ї": "yi", "ў": "u",
    "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz",
    "ѓ": "gj", "ќ": "kj", "ѕ": "dz", "ү": "u", "ҷ": "ch",
}

_GREEK_TABLE = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}

_GEORGIAN_TABLE = {
    "ა": "a", "ბ": "b", "გ": "g", "დ": "d", "ე": "e", "ვ": "v", "ზ": "z",
    "თ": "t", "ი": "i", "კ": "k", "ლ": "l", "მ": "m", "ნ": "n", "ჲ": "o",
    "ო": "o", "პ": "p", "ჟ": "zh", "რ": "r", "ს": "s", "ტ": "t", "უ": "u",
    "ფ": "p", "ქ": "k", "ღ": "gh", "ყ": "q", "შ": "sh", "ჩ": "ch",
    "ც": "ts", "ძ": "dz", "წ": "ts", "ჭ": "ch", "ხ": "kh", "ჴ": "ts",
    "ჯ": "j", "ჰ": "h",
}

_ARMENIAN_TABLE = {
    "ա": "a", "բ": "b", "գ": "g", "դ": "d", "ե": "e", "զ": "z", "է": "e",
    "ը": "y", "թ": "t", "ժ": "zh", "ի": "i", "լ": "l", "խ": "kh",
    "ծ": "ts", "կ": "k", "հ": "h", "ձ": "dz", "ղ": "gh", "ճ": "ch",
    "մ": "m", "յ": "y", "ն": "n", "շ": "sh", "ո": "o", "չ": "ch",
    "պ": "p", "ջ": "j", "ռ": "r", "ս": "s", "վ": "v", "տ": "t",
    "ր": "r", "ց": "ts", "ւ": "v", "փ": "p", "ք": "k", "օ": "o", "ֆ": "f",
}

_HEBREW_TABLE = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "v", "ז": "z",
    "ח": "ch", "ט": "t", "י": "y", "כ": "k", "ך": "k", "ל": "l", "מ": "m",
    "ם": "m", "נ": "n", "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "p",
    "צ": "ts", "ץ": "ts", "ק": "k", "ר": "r", "ש": "sh", "ת": "t",
}

_ARABIC_TABLE = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "a", "ٱ": "a", "ب": "b", "ت": "t",
    "ث": "th", "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r",
    "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ک": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ه": "h", "ة": "h", "و": "w", "ؤ": "w",
    "ي": "y", "ى": "a", "ئ": "y", "ء": "", "پ": "p", "چ": "ch", "ژ": "zh",
}

_THAI_TABLE = {
    "ก": "k", "ข": "kh", "ฃ": "kh", "ค": "kh", "ฅ": "kh", "ฆ": "kh",
    "ง": "ng", "จ": "ch", "ฉ": "ch", "ช": "ch", "ซ": "s", "ฌ": "y",
    "ญ": "y", "ฎ": "d", "ฏ": "t", "ฐ": "th", "ฑ": "th", "ฒ": "th",
    "ณ": "n", "ด": "d", "ต": "t", "ถ": "th", "ท": "th", "ธ": "th", "น": "n",
    "บ": "b", "ป": "p", "ผ": "ph", "ฝ": "f", "พ": "ph", "ฟ": "f", "ภ": "ph",
    "ม": "m", "ย": "y", "ร": "r", "ล": "l", "ว": "w", "ศ": "s", "ษ": "s",
    "ส": "s", "ห": "h", "ฬ": "l", "อ": "o", "ฮ": "h",
    "ะ": "a", "า": "a", "ำ": "am", "ิ": "i", "ี": "i", "ึ": "ue",
    "ื": "uue", "ุ": "u", "ู": "uu", "เ": "e", "แ": "ae", "โ": "o",
    "ใ": "ai", "ไ": "ai", "ๅ": "la", "็": "e", "ๆ": "", "ํ": "", "ฺ": "b",
    "๎": "b",
}

# Диакритика, которую не переводим: убираем, чтобы не мешала чтению.
_DROPPED = frozenset(
    list(range(0x064B, 0x0660))
    + [0x0670]
    + list(range(0x06D6, 0x06ED))
    + [0x0E31]
    + list(range(0x0E47, 0x0E4F))
)

TABLES = {
    CYRL: _CYRILLIC_TABLE,
    GREK: _GREEK_TABLE,
    GEOR: _GEORGIAN_TABLE,
    ARM: _ARMENIAN_TABLE,
    HEBR: _HEBREW_TABLE,
    ARA: _ARABIC_TABLE,
    THA: _THAI_TABLE,
}

# Письменности, для которых есть таблица: их названия «знакомы» любому,
# кто выбрал местные названия.
KNOWN_SCRIPTS = frozenset(TABLES) | {LATIN, HANG}
# Письменности, которые пишутся не латиницей: по ним ищется переход
# на кириллицу внутри названия без разделителя.
LOCAL_SCRIPTS = frozenset(TABLES) - {LATIN}

_HANGUL_INITIAL = (
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
    "j", "jj", "ch", "k", "t", "p", "h",
)
_HANGUL_MEDIAL = (
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
    "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "yi", "i",
)
_HANGUL_FINAL = (
    "", "g", "gg", "gs", "n", "nj", "nh", "d", "l", "lg", "lm", "lb",
    "ls", "lt", "lp", "lh", "m", "b", "bs", "s", "ss", "ng", "j", "c",
    "k", "t", "p", "h",
)

# Смещение и размеры блоков Hangul в Unicode.
_HANGUL_BASE = 0xAC00
_HANGUL_INITIAL_COUNT = 19
_HANGUL_MEDIAL_COUNT = 21
_HANGUL_FINAL_COUNT = 28
_HANGUL_TOTAL = (
    _HANGUL_INITIAL_COUNT * _HANGUL_MEDIAL_COUNT * _HANGUL_FINAL_COUNT
)


def script_of(text):
    """Письменность текста по первому значимому символу."""
    if not text:
        return None
    for char in text:
        if char.isspace() or not char.isalpha():
            continue
        code = ord(char)
        for script, start, end in _SCRIPT_RANGES:
            if start <= code <= end:
                return script
        return None
    return None


def _hangul(text):
    """Романизация Hangul (упрощённая система ревизии)."""
    result = []
    for char in text:
        code = ord(char)
        if _HANGUL_BASE <= code < _HANGUL_BASE + _HANGUL_TOTAL:
            offset = code - _HANGUL_BASE
            initial = _HANGUL_INITIAL[offset // (_HANGUL_MEDIAL_COUNT * _HANGUL_FINAL_COUNT)]
            medial = _HANGUL_MEDIAL[
                (offset % (_HANGUL_MEDIAL_COUNT * _HANGUL_FINAL_COUNT))
                // _HANGUL_FINAL_COUNT
            ]
            final = _HANGUL_FINAL[offset % _HANGUL_FINAL_COUNT]
            result.append(initial + medial + final)
            continue
        result.append(char)
    return "".join(result)


def transliterate_text(text, script=None):
    """Латинская запись названия; неизвестные буквы не меняются.

    Результат оформляется как топоним: заглавная первая буква каждого слова.
    """
    if not text:
        return text
    script = script or script_of(text)
    result = []
    for char in text:
        code = ord(char)
        if code in _DROPPED:
            continue
        table = TABLES.get(script)
        if table is None:
            result.append(char)
            continue
        replacement = table.get(char.lower())
        if replacement is None:
            result.append(char)
            continue
        if char.isupper() and replacement:
            replacement = replacement[0].upper() + replacement[1:]
        result.append(replacement)
    if script == HANG:
        result = [_hangul("".join(result))]
    text = "".join(result)
    if script in TABLES or script == HANG:
        text = _capitalize_words(text)
    return text


# Разделители слов: после них первая буква с регистром становится заглавной.
# Апостроф разделителем не считается: «L'viv», а не «L'Viv».
_WORD_SEPARATORS = frozenset(
    " \t\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u202f\u205f\u3000"
    "-/\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
    "\"«»‘‚‛„‟‹›()[]{}"
)


def _capitalize_words(text):
    """Заглавная первая буква каждого слова; остальное не меняется.

    В отличие от str.title(), не портит остальные буквы («McArthur»)
    и не трогает слова, начинающиеся с апострофа. Символы без регистра
    (кавычки, метки направления текста, знаки препинания) пропускаются;
    цифра «закрывает» начало слова, чтобы «42nd Street» не стало «42Nd».
    """
    chars = list(text)
    word_start = True
    for index, char in enumerate(chars):
        if char in _WORD_SEPARATORS:
            word_start = True
            continue
        upper, lower = char.upper(), char.lower()
        if upper != lower:
            if word_start and char != upper:
                chars[index] = upper
            word_start = False
        elif char.isdigit():
            word_start = False
    return "".join(chars)


def readable_scripts(script):
    """Письменности, знакомые читателю выбранного языка."""
    if script == LOCAL:
        return KNOWN_SCRIPTS
    return READABLE_SCRIPTS.get(script, frozenset({LATIN}))


def readable_name(name, script=None):
    """Название в понятном для читателя виде.

    Если письменность названия знакома читателю выбранного языка, название
    не меняется; иначе оно переводится в латиницу (если таблица есть).
    """
    if not name:
        return name
    text = str(name)
    name_script = script_of(text)
    if name_script is None:
        return text
    if name_script in readable_scripts(script):
        return text
    translit = transliterate_text(text, name_script)
    return translit or text


# Разделители частей составного названия. Тире и косая черта отделяют
# пару «местное — русское» («ქართული-Крепость», «Мушак / მუშაკი»), а
# скобки — пояснение («მთა (Mount)», «ზედა (ru: Монастыри)»). Дефис
# внутри слова («Ново-Афонская») частей не делит: вокруг тире нужны
# пробелы.
_PART_SPLIT = re.compile(r"\s*[–—]\s*|\s+-\s+|\s*/\s*", re.IGNORECASE)
# Слова типа объекта перед названием: «мтa ენჩო» → «ენჩო».
_TYPE_WORDS = (
    "мта", "მთა", "mta", "гора", "gora", "mount", "mt.", "вулкан", "volcano",
    "г.", "хр.", "хребет", "ridge", "перевал", "pass", "село",
)
_TYPE_PREFIX = re.compile(
    r"^(?:" + "|".join(re.escape(word) for word in _TYPE_WORDS) + r")\s+",
    re.IGNORECASE,
)


def _strip_type(name):
    """Убирает слово типа перед названием: «мтa ენჩო» → «ენჩო»."""
    stripped = _TYPE_PREFIX.sub("", name, count=1).strip()
    return stripped or name


def _has_script(text, script):
    """Есть ли в тексте буква нужной письменности.

    `script` может быть одной письменностью (`CYRL`) или набором
    (`LOCAL_SCRIPTS`) — тогда годится любая из набора.
    """
    scripts = {script} if isinstance(script, str) else set(script)
    return any(
        script_of(char) in scripts for char in text if char.isalpha()
    )


_PARENTHESIS = re.compile(r"\(([^()]*)\)")


def _name_parts(name):
    """Части составного названия в порядке записи, без пустых.

    Сначала название делится по разделителям, затем у каждой части
    разбирается пояснение в скобках — оно встаёт перед ней; скобки
    сохраняются, чтобы отличить пояснение от простого слова:
    «Mersedes Benc (грузовой)» → «(грузовой)», «Mersedes Benc»;
    «ზედა (ru: Монастыри)» → «(ru: Монастыри)», «ზედა».
    """
    parts = []
    for chunk in _PART_SPLIT.split(name):
        chunk = chunk.strip()
        if not chunk:
            continue
        start = 0
        for match in _PARENTHESIS.finditer(chunk):
            tail = chunk[start:match.start()].strip(" .,")
            if tail:
                parts.append((tail, False))
            inside = match.group(1).strip(" .,")
            if inside:
                parts.append(("(" + inside + ")", True))
            start = match.end()
        tail = chunk[start:].strip(" .,")
        if tail:
            parts.append((tail, False))
    return parts


def _from_first_cyrillic(text):
    """Текст с первого слова на кириллице, когда разделителя нет.

    Ищется внутритекстовый переход: «ზედაМонастырская» → «Монастырская».
    «მთა ენჩო - Энчо» → «Энчо»: перед кириллицей пробел и тире.
    «ქართული-Крепость» не делится: дефис стоит внутри слова.
    «სუვენირები Souvenirs Сувениры» и «Mersedes Benc (грузовой)» тоже
    не меняются: кириллица не первая часть и отделена латинским словом
    — это перечисление, а не перевод.
    """
    match = re.search(r"[\u0400-\u052f]", text)
    if match is None or match.start() == 0:
        return text
    head = text[: match.start()]
    if not head[-1].isspace():
        return text
    stripped = head.rstrip()
    if not _has_script(stripped, LOCAL_SCRIPTS) or _has_script(stripped, LATIN):
        return text
    return text[match.start():]


_LANG_PREFIX = re.compile(
    r"^(?:[a-z]{2,3}(?:[-_][a-z]{2,4})?)\s*[:=]\s*",
    re.IGNORECASE,
)


def _unlabel(part):
    """Убирает метку языка: «ru: Монастыри» → «Монастыри»."""
    return _LANG_PREFIX.sub("", part).strip()


def _squashed(text):
    """Только буквы и цифры в нижнем регистре — для сравнения названий."""
    return "".join(
        char.lower() for char in text if char.isalnum()
    )


def _is_transliteration(source, translation):
    """Пишется ли `translation` тем же названием, что `source`, но своей азбукой.

    «ზესხო» ↔ «Zeskho» — одно и то же название: транслитерация одной
    строки в латиницу совпадает с другой строкой по буквам. «грузовой»
    ↔ «Mersedes Benc» — нет: это уточнение, а не перевод названия.
    """
    squashed = _squashed(translation)
    if not squashed or not source:
        return False
    source_script = script_of(source)
    target_script = script_of(translation)
    if source_script is None or target_script is None:
        return False
    if source_script == target_script:
        return _squashed(source) == squashed
    if target_script != LATIN:
        # Киррилическая или грузинская запись латинского названия
        # обратной таблицей не получается, такие пары не считаем переводом.
        return False
    return _squashed(transliterate_text(source, source_script)) == squashed


def _unlabel(part):
    """Убирает метку языка: «ru: Монастыри» → «Монастыри»."""
    return _LANG_PREFIX.sub("", part).strip()


def _squashed(text):
    """Только буквы и цифры в нижнем регистре — для сравнения названий."""
    return "".join(
        char.lower() for char in text if char.isalnum()
    )


def _is_transliteration(source, translation):
    """Пишется ли `translation` тем же названием, что `source`, но своей азбукой.

    «Тойота Центр» ↔ «Toyota Center», «ზესხო» ↔ «Zeskho» — одно и то же
    название: транслитерация одной строки в другую совпадает с ней по
    буквам (порядок перевода не важен). «грузовой» ↔ «Mersedes Benc» —
    нет: это уточнение, а не перевод названия.

    Перевод идёт в латиницу (кириллица и грузинский переводятся таблицей,
    латиница остаётся как есть), а обратно — «наоборот» через ту же
    таблицу: латиница в кириллицу не переводится, поэтому сравниваются
    только пары, где одна сторона не латинская.
    """
    squashed = _squashed(translation)
    if not squashed:
        return False
    source_script = script_of(source)
    target_script = script_of(translation)
    if not source_script or not target_script:
        return False
    if target_script == LATIN and source_script != LATIN:
        return _squashed(transliterate_text(source)) == squashed
    if source_script == LATIN and target_script != LATIN:
        # Кириллическая запись латинского названия: «Toyota» → «Тойота»
        # обратной таблицей не сделать, сравниваем только звучание.
        return False
    if source_script == target_script:
        return _squashed(source) == squashed
    return False


def _is_translation(chosen, local):
    """Считать ли часть переводом, а не уточнением или вторым названием.

    «ზედა (ru: Монастыри)», «ru: Монастыри» — перевод с меткой языка,
    выбирается всегда. Скобки без метки выбираются, только если это та же
    самая запись другой азбукой: «Тойота Центр (Toyota Center)»,
    «ზესხო (Zeskho)». Уточнение в скобках остаётся уточнением:
    «Mersedes Benc (грузовой)», «Torex (двери)», «Flora (живые цветы
    и подарки)». «სუვენირები Souvenirs Сувениры» — перечисление:
    кириллица стоит последней и отделена латиницей.
    """
    if _LANG_PREFIX.match(chosen):
        return True
    if _has_script(chosen, CYRL) and _has_script(chosen, LATIN):
        head = chosen.split()[0]
        if _has_script(head, LATIN):
            return False
    for match in _PARENTHESIS.finditer(chosen):
        inside = match.group(1)
        rest = (chosen[:match.start()] + " " + chosen[match.end():]).strip()
        if not rest:
            continue
        translated = script_of(rest) if local else CYRL
        if translated and _has_script(inside, translated):
            return _is_transliteration(rest, inside) or _is_transliteration(
                inside, rest
            )
    return False


def _labelled_part(parts):
    """Часть с меткой языка: «(ru: Монастыри)», «ru: Монастыри»."""
    for piece, _ in parts:
        body = piece.strip(" ()")
        if _LANG_PREFIX.match(body):
            return _unlabel(body)
    return None


def _bracket_translation(name, parts):
    """Перевод в скобках: та же запись другой азбукой.

    «Mersedes Benc (грузовой)» → `None` (уточнение), «ზედა
    (ru: Монастыри)» ловит `_labelled_part`. Есть пара до `_labelled_part`.
    """
    match = _PARENTHESIS.search(name)
    if not match:
        return None
    inside = match.group(1).strip()
    rest = (name[:match.start()] + " " + name[match.end():]).strip()
    if not inside or not rest:
        return None
    # На русской карте перевод берётся только кириллический: латинская
    # запись рядом с грузинской — это для английской карты.
    if _has_script(inside, CYRL) and _is_transliteration(rest, inside):
        return inside
    if _has_script(rest, CYRL) and _is_transliteration(inside, rest):
        return rest
    return None


def pick_name_part(name, script=None, translit=False):
    """Часть составного названия на языке карты.

    `name` в OSM часто записан сразу на нескольких языках: «Мушак / მუშაკი»,
    «Каспий теңізі - Hazar deňzi - Каспийское море». На русской карте
    выбирается кириллическая часть, если она помечена как перевод: с
    меткой языка («ზედა (ru: Монастыри)» → «Монастыри») или в скобках,
    где записана та же самая строка другой азбукой («ზესხო (Zeskho)»).
    Уточнение в скобках переводом не считается: «Mersedes Benc
    (грузовой)» остаётся «Mersedes Benc». Если кириллицы нет или она
    не перевод («ზედა / Монастырская»), название остаётся как записано
    слева направо, с первой части; при выбранной транслитерации она
    переводится в латиницу (`translit=True`). При местных названиях
    («Местные названия» в списке) название выводится целиком, без
    разбора: перевод не нужен, а обрезка до первой части теряла бы
    вторую («Тойота Центр (Toyota Center)»). Дефис внутри слова
    («Ново-Афонская») и пара через дефис («ქართული-Крепость») частями
    не делятся.
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return text
    if script == LOCAL:
        return text
    parts = _name_parts(text)
    chosen = None
    if script is not None:
        chosen = _labelled_part(parts)
        if chosen is None:
            chosen = _bracket_translation(text, parts)
        if chosen is None:
            chosen = _from_first_cyrillic(text) if _has_script(text, CYRL) else None
    if chosen is None:
        chosen = parts[0][0] if parts else text
    chosen = _strip_type(chosen) or chosen
    if _PARENTHESIS.search(chosen) and len(chosen) > 1:
        # Скобки внутри выбранной части — пояснение того же названия.
        base = _PARENTHESIS.sub(" ", chosen).strip()
        if base:
            chosen = base
    if not translit or _has_script(chosen, CYRL):
        return chosen
    return transliterate_text(chosen) or chosen


def pick_name(name, script=None, translit=False):
    """`pick_name_part` с учётом галочек языка карты в подписи.

    `translit` — включена ли транслитерация в интерфейсе; `script` NULL
    означает, что его не передали, и тогда название не разбирается:
    для местных названий перевод в латиницу зависит от языка карты.
    """
    if script is None:
        return name
    return pick_name_part(name, script, bool(translit))


# Форма выражения подписи: запасное название приводится к читаемому виду.
TRANS_TAG1 = (
    "IF(@name_translit='1' AND @name_local='1',"
    "readable_name(attribute(@name_tag_1), @name_script),"
    "attribute(@name_tag_1))"
)
TRANS_TAG2 = (
    "IF(@name_translit='1',"
    "readable_name(attribute(@name_tag_2), @name_script),"
    "attribute(@name_tag_2))"
)

# Прежняя форма (только транслитерация) — приводится к исходному виду.
_OLD_TAG1 = (
    "IF(@name_translit='1' AND @name_local='1',"
    "transliterate(attribute(@name_tag_1)),attribute(@name_tag_1))"
)
_OLD_TAG2 = (
    "IF(@name_translit='1',"
    "transliterate(attribute(@name_tag_2)),attribute(@name_tag_2))"
)

_MARKER = "readable_name"

_PATTERN = re.compile(
    r"(WHEN attribute\(@name_tag_1\) IS NOT NULL THEN )"
    r"(.*?)"
    r"(\n\tWHEN attribute\(@name_tag_2\) IS NOT NULL THEN )"
    r"(.*?)"
    r"(\n\tWHEN attribute\(@name_tag_3\) IS NOT NULL THEN )",
    re.DOTALL,
)


def add_transliteration(expression):
    """Добавляет в выражение подписи запасной вариант с транслитерацией."""
    if not expression:
        return expression
    text = expression.replace(_OLD_TAG1, "attribute(@name_tag_1)")
    text = text.replace(_OLD_TAG2, "attribute(@name_tag_2)")
    if _MARKER in text:
        return text

    def replace(match):
        first = match.group(2).replace(
            "attribute(@name_tag_1)", TRANS_TAG1
        )
        second = match.group(4).replace(
            "attribute(@name_tag_2)", TRANS_TAG2
        )
        return (
            match.group(1) + first + match.group(3) + second + match.group(5)
        )

    return _PATTERN.sub(replace, text)


def _rule_settings(labeling):
    """Пары «правило, настройки» для правило-based подписей."""
    try:
        from qgis.core import QgsRuleBasedLabeling
    except ImportError:
        return
    if not isinstance(labeling, QgsRuleBasedLabeling):
        return
    for rule in labeling.rootRule().children():
        yield from _rule_children(rule)


def _rule_children(rule):
    settings = rule.settings()
    if settings is not None:
        yield rule, settings
    for child in rule.children():
        yield from _rule_children(child)


def functions_available():
    """Зарегистрирована ли в QGIS функция `readable_name`.

    Пока функции нет, выражения подписей с ней работать не могут: QGIS
    не рисует такие подписи вовсе. Поэтому при её отсутствии ветка
    транслитерации из подписей убирается.
    """
    try:
        from .expressions import registered_functions
    except ImportError:
        return False
    return "readable_name" in registered_functions()


def strip_transliteration(expression):
    """Убирает ветку транслитерации из выражения подписи."""
    if not expression:
        return expression
    text = expression.replace(TRANS_TAG1, "attribute(@name_tag_1)")
    text = text.replace(TRANS_TAG2, "attribute(@name_tag_2)")
    text = text.replace(_OLD_TAG1, "attribute(@name_tag_1)")
    text = text.replace(_OLD_TAG2, "attribute(@name_tag_2)")
    return text


def patch_layer_labeling(layer, available=None):
    """Добавляет ветку транслитерации в подписи слоя.

    Если функция `readable_name` в QGIS не зарегистрирована, ветка, наоборот,
    убирается: иначе QGIS не нарисовал бы ни одной подписи.
    """
    if layer is None:
        return False
    labeling = layer.labeling()
    if labeling is None:
        return False
    if available is None:
        available = functions_available()
    changed = False
    for rule, settings in _rule_settings(labeling):
        expression = settings.fieldName
        if available:
            updated = add_transliteration(expression)
        else:
            updated = strip_transliteration(expression)
        if updated == expression:
            continue
        settings.fieldName = updated
        rule.setSettings(settings)
        changed = True
    if not changed:
        changed = _patch_simple_labeling(labeling, available)
    if not changed:
        return False
    try:
        layer.triggerRepaint()
        layer.emitStyleChanged()
    except Exception:
        pass
    return changed


def _patch_simple_labeling(labeling, available=None):
    """Подписи без правил: обычные настройки подписи слоя."""
    getter = getattr(labeling, "settings", None)
    setter = getattr(labeling, "setSettings", None)
    if not callable(getter) or not callable(setter):
        return False
    try:
        settings = getter()
    except Exception:
        return False
    if settings is None:
        return False
    if available is None:
        available = functions_available()
    expression = settings.fieldName
    if available:
        updated = add_transliteration(expression)
    else:
        updated = strip_transliteration(expression)
    if updated == expression:
        return False
    settings.fieldName = updated
    setter(settings)
    return True


def patch_project_labeling(project=None, available=None):
    """Добавляет ветку транслитерации в подписи всех слоёв проекта."""
    if project is None:
        try:
            from qgis.core import QgsProject
        except ImportError:
            return False
        project = QgsProject.instance()
    if available is None:
        available = functions_available()
    changed = False
    try:
        layers = list(project.mapLayers().values())
    except Exception:
        return False
    for layer in layers:
        if patch_layer_labeling(layer, available):
            changed = True
    return changed
