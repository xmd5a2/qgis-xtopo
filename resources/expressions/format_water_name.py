from qgis.core import *
from qgis.gui import *
import re

# Слова, обозначающие тип водного объекта, и их сокращения.
# Порядок важен: срабатывает первое подходящее правило.
WATER_TYPE_WORDS = (
    ("озёра", "оз."),
    ("озеро", "оз."),
    ("торфопредприятия", "торф."),
    ("торфопредприятие", "торф."),
    ("торфоразработки", "торф."),
    ("торфоболото", "торф-бол."),
    ("рыбопитомника", "рыб."),
    ("рыбопитомник", "рыб."),
    ("хозяйство", "хоз-во"),
    ("лиман", "лим."),
    ("заводь", "зав."),
    ("затон", "зат."),
    ("плотина", "пл."),
    ("запруда", ""),
    ("пруды", "пр."),
    ("пруда", "пр."),
    ("пруд", "пр."),
    ("водохранилище", "вдхр."),
    ("водоём", "вод."),
    ("водоема", "вод."),
    ("водоем", "вод."),
    # Балка стоит раньше ручья и речки: сокращается слово типа в начале
    # названия, а «балка Третья речка» читается как «б. Третья речка».
    ("балка", "б."),
    ("ручей", "руч."),
    ("речка", "р."),
    ("река", "р."),
    ("бассейн", "басс."),
    ("лагуна", "лаг."),
    ("залив", "зал."),
    ("протока", "прот."),
    ("воложка", "вол."),
    ("старица", "стар."),
    ("овраг", "овр."),
    ("водопады", "вдп."),
    ("водопад", "вдп."),
    ("болота", "бол."),
    ("болото", "бол."),
    ("урочище", "ур."),
    ("ерик", "ер."),
    ("карьеры", "кар."),
    ("карьера", "кар."),
    ("карьер", "кар."),
    ("канал", "кан."),
)

# Слова, из которых название состоит целиком: подпись не нужна.
WATER_EMPTY_NAMES = (
    "водопад", "родник", "бассейн", "водоем", "водоём", "водохранилище",
    "воложка", "дренажная канава", "дренажный канал", "залив", "заводь",
    "запруда", "затон", "канава", "канал", "рыбопитомник", "карьер",
    "карьеры", "лагуна", "овраг", "озеро", "протока", "пруд", "река",
    "речка", "ручей", "старица", "spring", "water", "stream", "river",
    "drain", "canal", "исток", "балка",
)

# Признаки слова: буквы и цифры, чтобы «Прудовая» не попало под «пруд».
WORD_EDGE = r"0-9A-Za-zА-Яа-яЁё-"


def _replace_word(text, word, short):
    """Заменяет слово целиком, не задевая похожие слова."""
    pattern = (
        r"(?<![{edges}]){word}(?![{edges}])"
    ).format(edges=WORD_EDGE, word=re.escape(word))
    return re.sub(pattern, short, text, flags=re.IGNORECASE)


def _shorten_type(text):
    """Сокращает первое найденное слово типа водного объекта."""
    for word, short in WATER_TYPE_WORDS:
        result = _replace_word(text, word, short)
        if result != text:
            return result
    return text


@qgsfunction(args='auto', group='Custom')
def format_water_name(name, feature, parent):
    n = name
    if n.lower() in WATER_EMPTY_NAMES: n = ''
    else:
        n = _shorten_type(n)

    if "карьер-" in n.lower(): n = ""
    elif "земснаряд " in n.lower(): n = ""

    if "(залив)" in n.lower(): n = re.sub("(залив)","",n, flags=re.IGNORECASE)
    if "(пролив)" in n.lower(): n = re.sub("(пролив)","",n, flags=re.IGNORECASE)

    if n.lower().startswith('большой') and " " in n and len(n) > 13: n = re.sub("большой","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большие') and " " in n and len(n) > 13: n = re.sub("большие","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большое') and " " in n and len(n) > 13: n = re.sub("большое","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большая') and " " in n and len(n) > 13: n = re.sub("большая","Бол.", n, flags=re.IGNORECASE)
    elif "малая" in n.lower() and " " in n and len(n) > 13: n = re.sub("малая","Мал.",n, flags=re.IGNORECASE)
    elif "малый" in n.lower() and " " in n and len(n) > 13: n = re.sub("малый","Мал.",n, flags=re.IGNORECASE)
    elif "малые" in n.lower() and " " in n and len(n) > 13: n = re.sub("малые","Мал.",n, flags=re.IGNORECASE)
    elif "малое" in n.lower() and " " in n and len(n) > 13: n = re.sub("малое","Мал.",n, flags=re.IGNORECASE)
    elif "старая" in n.lower() and " " in n and len(n) > 13: n = re.sub("старая","Стар.",n, flags=re.IGNORECASE)
    elif "старый" in n.lower() and " " in n and len(n) > 13: n = re.sub("старый","Стар.",n, flags=re.IGNORECASE)
    elif "старые" in n.lower() and " " in n and len(n) > 13: n = re.sub("старые","Стар.",n, flags=re.IGNORECASE)
    elif "старое" in n.lower() and " " in n and len(n) > 13: n = re.sub("старое","Стар.",n, flags=re.IGNORECASE)
    elif n.lower().startswith('новая ') and len(n) > 13: n = re.sub("^новая ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новый ') and len(n) > 13: n = re.sub("^новый ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новые ') and len(n) > 13: n = re.sub("^новые ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новое ') and len(n) > 13: n = re.sub("^новое ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новая') and len(n) > 13: n = re.sub(" новая$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новый') and len(n) > 13: n = re.sub(" новый$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новые') and len(n) > 13: n = re.sub(" новые$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новое') and len(n) > 13: n = re.sub(" новое$", "Нов.", n, flags=re.IGNORECASE)
    elif "нижняя" in n.lower() and " " in n and len(n) > 13: n = re.sub("нижняя","Ниж.",n, flags=re.IGNORECASE)
    elif "нижний" in n.lower() and " " in n and len(n) > 13: n = re.sub("нижний","Ниж.",n, flags=re.IGNORECASE)
    elif "нижние" in n.lower() and " " in n and len(n) > 13: n = re.sub("нижние","Ниж.",n, flags=re.IGNORECASE)
    elif "нижнее" in n.lower() and " " in n and len(n) > 13: n = re.sub("нижнее","Ниж.",n, flags=re.IGNORECASE)
    elif "верхняя" in n.lower() and " " in n and len(n) > 13: n = re.sub("верхняя","Верх.",n, flags=re.IGNORECASE)
    elif "верхний" in n.lower() and " " in n and len(n) > 13: n = re.sub("верхний","Верх.",n, flags=re.IGNORECASE)
    elif "верхние" in n.lower() and " " in n and len(n) > 13: n = re.sub("верхние","Верх.",n, flags=re.IGNORECASE)
    elif "верхнее" in n.lower() and " " in n and len(n) > 13: n = re.sub("верхнее","Верх.",n, flags=re.IGNORECASE)

    elif "красная" in n.lower() and " " in n and len(n) > 13: n = re.sub("красная","Кр.",n, flags=re.IGNORECASE)
    elif "красный" in n.lower() and " " in n and len(n) > 13: n = re.sub("красный","Кр.",n, flags=re.IGNORECASE)
    elif "красные" in n.lower() and " " in n and len(n) > 13: n = re.sub("красные","Кр.",n, flags=re.IGNORECASE)
    elif "красное" in n.lower() and " " in n and len(n) > 13: n = re.sub("красное","Кр.",n, flags=re.IGNORECASE)
    elif "черное" in n.lower() and " " in n and len(n) > 13: n = re.sub("черное","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрное" in n.lower() and " " in n and len(n) > 13: n = re.sub("чёрное","Чёрн.",n, flags=re.IGNORECASE)
    elif "черная" in n.lower() and " " in n and len(n) > 13: n = re.sub("черная","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрная" in n.lower() and " " in n and len(n) > 13: n = re.sub("чёрная","Чёрн.",n, flags=re.IGNORECASE)
    elif "черный" in n.lower() and " " in n and len(n) > 13: n = re.sub("черный","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрный" in n.lower() and " " in n and len(n) > 13: n = re.sub("чёрный","Чёрн.",n, flags=re.IGNORECASE)
    elif "черные" in n.lower() and " " in n and len(n) > 13: n = re.sub("черные","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрные" in n.lower() and " " in n and len(n) > 13: n = re.sub("чёрные","Чёрн.",n, flags=re.IGNORECASE)
    elif "белая" in n.lower() and " " in n and len(n) > 13: n = re.sub("белая","Бел.",n, flags=re.IGNORECASE)
    elif "белый" in n.lower() and " " in n and len(n) > 13: n = re.sub("белый","Бел.",n, flags=re.IGNORECASE)
    elif "белые" in n.lower() and " " in n and len(n) > 13: n = re.sub("белые","Бел.",n, flags=re.IGNORECASE)
    elif "белое" in n.lower() and " " in n and len(n) > 13: n = re.sub("белое","Бел.",n, flags=re.IGNORECASE)

    if n.lower().startswith("неизвестный"): n = re.sub("неизвестный","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестный"): n = re.sub("неизвестный","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестное"): n = re.sub("неизвестное","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестное"): n = re.sub("неизвестное","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестная"): n = re.sub("неизвестная","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестная"): n = re.sub("неизвестная","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестные"): n = re.sub("неизвестные","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестные"): n = re.sub("неизвестные","неизвестн.",n, flags=re.IGNORECASE)

    if "uvala" in n.lower() and " " in n: n = re.sub("uvala","Uv.",n, flags=re.IGNORECASE)
    elif "zaliv" in n.lower() and " " in n: n = re.sub("zaliv","zal.",n, flags=re.IGNORECASE)

    if " исток" in n.lower() and "." not in n: n = re.sub(" исток"," ист.",n, flags=re.IGNORECASE)
    if "исток " in n.lower() and "." not in n: n = re.sub("исток ","ист. ",n, flags=re.IGNORECASE)

    return " ".join(n.split())
