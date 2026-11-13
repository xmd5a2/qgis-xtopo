from qgis.core import *
from qgis.gui import *
import re

# Переменная проекта: 1 для «Местных названий», 0 для русского языка.
LOCAL_VARIABLE = "name_local"


def _has_cyrillic(text):
    """Есть ли в названии кириллические буквы."""
    return bool(re.search(r"[А-Яа-яЁё]", text or ""))


def _project_variable(name):
    try:
        scope = QgsExpressionContextUtils.projectScope(QgsProject.instance())
        return scope.variable(name)
    except Exception:
        return None


def _map_language_is_ru(name_pref_suf_lang, name_local=None):
    """Язык карты именно русский, а не «Местные названия».

    ``@name_pref_suf_lang`` равен ``ru`` и для местных названий: типовые
    слова сокращаются по-русски всегда. Отличить русскую карту от местной
    позволяет переменная ``@name_local``.
    """
    if str(name_pref_suf_lang or "").strip().lower() != "ru":
        return False
    if name_local is None:
        name_local = _project_variable(LOCAL_VARIABLE)
    local = str(name_local).strip().lower()
    return local not in ("1", "true")


# Типовые слова в названиях населённых пунктов и их сокращения.
# Правила применяются по очереди, каждое — только к границам слова,
# поэтому «Островского» не превращается в «о.ского».
PLACE_TYPE_WORDS = (
    ("санатория", "сан."),
    ("санаторий", "сан."),
    ("лесничества", "леснич."),
    ("лесничество", "леснич."),
    ("кордон", "корд."),
    ("участка", "уч."),
    ("участок", "уч."),
    ("имени", "им."),
    ("торфопредприятия", "торф."),
    ("торфопредприятие", "торф."),
    ("рыбопитомника", "рыб."),
    ("рыбопитомник", "рыб."),
    ("питомника", "пит."),
    ("питомник", "пит."),
    ("охотхозяйство", "охот."),
    ("охотхозяйства", "охот."),
    ("хозяйство", "хоз-во"),
    ("микрорайона", "мкр."),
    ("микрорайон", "мкр."),
    ("квартала", "кв."),
    ("квартал", "кв."),
    ("кутан", "кут."),
    ("район", "р-н"),
    ("станция", "ст."),
    ("полуостров", "п-ов."),
    ("пристань", "прист."),
    ("посёлок", "пос."),
    ("поселок", "пос."),
    ("урочища", "ур."),
    ("урочище", "ур."),
    ("долина", "дол."),
    ("поляна", "пол."),
    ("ущелье", "ущ."),
    ("застава", "заст."),
    ("озеро", "оз."),
    ("острова", "о-ва"),
    ("остров", "о."),
    ("болото", "бол."),
    ("зимник", "зим."),
    ("зимовка", "зим."),
    ("летник", "лет."),
    ("балка", "б."),
    ("овраг", "овр."),
    ("монастырь", "мон."),
    ("женский", "жен."),
    ("женская", "жен."),
    ("мужской", "муж."),
    ("мужская", "муж."),
    ("святой", "св."),
    ("святого", "св."),
    ("мыс", "м."),
    ("лагерь", "лаг."),
)

# Многословные названия: сокращаются целиком.
PLACE_PHRASES = (
    ("рабочий поселок", "раб. пос."),
    ("рабочий посёлок", "раб. пос."),
    ("жилой комплекс", "Ж/к"),
    ("дом отдыха", "д.о."),
    ("база отдыха", "б.о."),
    ("турбаза", "т/б"),
    ("турбазы", "т/б"),
    ("пионерлагерь", "пионерлаг."),
    ("альплагерь", "а/л"),
    ("садоводческое товарищество", "СТ"),
    ("коттеджный поселок", "КП"),
    ("коттеджный посёлок", "КП"),
)

# Признаки слова: буквы и цифры.
WORD_EDGE = r"0-9A-Za-zА-Яа-яЁё-"


def _replace_phrase(text, phrase, short, skip_whole=False):
    """Заменяет фразу целиком, не задевая похожие слова.

    При skip_whole название, состоящее только из этого слова, не
    меняется: иначе «Остров» превратился бы в «о.», а «Мыс» — в «м.».
    """
    pattern = (
        r"(?<![{edges}]){phrase}(?![{edges}])"
    ).format(edges=WORD_EDGE, phrase=re.escape(phrase))
    if skip_whole and re.fullmatch(pattern, text, flags=re.IGNORECASE):
        return text
    return re.sub(pattern, short, text, flags=re.IGNORECASE)


@qgsfunction(args='auto', group='Custom')
def format_place_name(name, population, name_pref_suf_lang, feature, parent):
    n = name

    if "name" == n.lower(): n = ""
    if "name ?" == n.lower(): n = ""

    for phrase, short in PLACE_PHRASES:
        n = _replace_phrase(n, phrase, short)
    for word, short in PLACE_TYPE_WORDS:
        n = _replace_phrase(n, word, short, skip_whole=True)

    if "верхняя" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("верхняя","Верх.", n, flags=re.IGNORECASE)
    elif "верхний" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("верхний","Верх.", n, flags=re.IGNORECASE)
    elif "верхние" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("верхние","Верх.", n, flags=re.IGNORECASE)
    elif "верхнее" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("верхнее","Верх.", n, flags=re.IGNORECASE)
    elif "средняя" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("средняя","Средн.", n, flags=re.IGNORECASE)
    elif "средний" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("средний","Средн.", n, flags=re.IGNORECASE)
    elif "средние" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("средние","Средн.", n, flags=re.IGNORECASE)
    elif "среднее" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("среднее","Средн.", n, flags=re.IGNORECASE)
    elif "красная" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("красная","Кр.", n, flags=re.IGNORECASE)
    elif "красный" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("красный","Кр.", n, flags=re.IGNORECASE)
    elif "красные" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("красные","Кр.", n, flags=re.IGNORECASE)
    elif "красное" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("красное","Кр.", n, flags=re.IGNORECASE)
    elif "старая" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("старая","Стар.", n, flags=re.IGNORECASE)
    elif "старый" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("старый","Стар.", n, flags=re.IGNORECASE)
    elif "старые" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("старые","Стар.", n, flags=re.IGNORECASE)
    elif "старое" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("старое","Стар.", n, flags=re.IGNORECASE)
    elif "нижняя" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("нижняя","Ниж.", n, flags=re.IGNORECASE)
    elif "нижний" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("нижний","Ниж.", n, flags=re.IGNORECASE)
    elif "нижние" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("нижние","Ниж.", n, flags=re.IGNORECASE)
    elif "нижнее" in n.lower() and " " in n and "." not in n.lower(): n = re.sub("нижнее","Ниж.", n, flags=re.IGNORECASE)

    elif n.lower().startswith('левая '): n = re.sub("^левая ","Лев. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('левый '): n = re.sub("^левый ","Лев. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('левые '): n = re.sub("^левые ","Лев. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('левое '): n = re.sub("^левое ","Лев. ", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' левая'): n = re.sub(" левая$"," лев.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' левый'): n = re.sub(" левый$"," лев.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' левые'): n = re.sub(" левые$"," лев.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' левое'): n = re.sub(" левое$", "лев.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('правая '): n = re.sub("^правая ","Прав. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('правый '): n = re.sub("^правый ","Прав. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('правые '): n = re.sub("^правые ","Прав. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('правое '): n = re.sub("^правое ","Прав. ", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' правая'): n = re.sub(" правая$"," прав.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' правый'): n = re.sub(" правый$"," прав.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' правые'): n = re.sub(" правые$"," прав.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' правое'): n = re.sub(" правое$", "прав.", n, flags=re.IGNORECASE)

    elif "малая" in n.lower() and " " in n: n = re.sub("малая","Мал.", n, flags=re.IGNORECASE)
    elif "малый" in n.lower() and " " in n: n = re.sub("малый","Мал.", n, flags=re.IGNORECASE)
    elif "малые" in n.lower() and " " in n and 'о-ва' not in n: n = re.sub("малые","Мал.", n, flags=re.IGNORECASE)
    elif "малое" in n.lower() and " " in n: n = re.sub("малое","Мал.", n, flags=re.IGNORECASE)
    elif "большая" in n.lower() and " " in n: n = re.sub("большая","Бол.", n, flags=re.IGNORECASE)
    elif "большой" in n.lower() and " " in n: n = re.sub("большой","Бол.", n, flags=re.IGNORECASE)
    elif "большие" in n.lower() and " " in n and 'о-ва' not in n: n = re.sub("большие","Бол.", n, flags=re.IGNORECASE)
    elif "большое" in n.lower() and " " in n and 'торф' not in n and 'ебольшое' not in n and "." not in n.lower(): n = re.sub("большое","Бол.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новая '): n = re.sub("^новая ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новый '): n = re.sub("^новый ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новые '): n = re.sub("^новые ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новое '): n = re.sub("^новое ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новая'): n = re.sub(" новая$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новый'): n = re.sub(" новый$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новые'): n = re.sub(" новые$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новое'): n = re.sub(" новое$", "Нов.", n, flags=re.IGNORECASE)

    if n.lower().startswith("неизвестный"): n = re.sub("неизвестный","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестный"): n = re.sub("неизвестный","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестное"): n = re.sub("неизвестное","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестное"): n = re.sub("неизвестное","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестная"): n = re.sub("неизвестная","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестная"): n = re.sub("неизвестная","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестные"): n = re.sub("неизвестные","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестные"): n = re.sub("неизвестные","неизвестн.",n, flags=re.IGNORECASE)

    if n.lower().startswith("заброшенный "): n = re.sub("заброшенный ","забр. ",n, flags=re.IGNORECASE)
    if n.lower().startswith("заброшенная "): n = re.sub("заброшенная ","забр. ",n, flags=re.IGNORECASE)
    if n.lower().startswith("заброшенное "): n = re.sub("заброшенное ","забр. ",n, flags=re.IGNORECASE)
    if n.lower().startswith("заброшенные "): n = re.sub("заброшенные ","забр. ",n, flags=re.IGNORECASE)

    if "перевал " in n.lower(): n = re.sub("перевал ","пер. ", n, flags=re.IGNORECASE)

    # «(неж.)» — русская пометка, как и категория перевала: имени не
    # кириллицей на нерусской карте (включая «Местные названия») она не
    # приписывается. На русской карте и для кириллических имён остаётся.
    if population == "0" and (
            not (name or "").strip()
            or _has_cyrillic(name)
            or _map_language_is_ru(name_pref_suf_lang)):
        n = n + "\n(неж.)"

    return " ".join(n.split())
