from qgis.core import *
from qgis.gui import *
import re

# Признаки слова: буквы и цифры.
WORD_EDGE = r"0-9A-Za-zА-Яа-яЁё-"


def _replace_word(text, word, short):
    """Заменяет слово целиком, не задевая похожие слова.

    «Ледниковый» — имя собственное, а не тип объекта, поэтому
    «ледник» меняется только когда стоит отдельным словом.
    """
    pattern = (
        r"(?<![{edges}]){word}(?![{edges}])"
    ).format(edges=WORD_EDGE, word=re.escape(word))
    return re.sub(pattern, short, text, flags=re.IGNORECASE)


def _has_cyrillic(text):
    """Есть ли в названии кириллические буквы.

    Тип объекта по-русски приписывается только названию кириллицей:
    к грузинскому или латинскому названию «л. » приписывать нечего.
    """
    return bool(re.search(r"[А-Яа-яЁё]", text))


@qgsfunction(args='auto', group='Custom')
def format_glacier_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
        if len(n) <= 3 and not "л." in n.lower() and _has_cyrillic(n):
            n = "ледник " + n
        else:
            n = _replace_word(n, "ледники", "л.")
            n = _replace_word(n, "ледник", "л.")
        if not "ледн." in n.lower() and \
                not "ледник" in n.lower() and \
                not n.lower().startswith("л.") and \
                not n.lower().endswith("л.") and \
                not "пл. " in n.lower() and \
                not " пл." in n.lower() and \
                not "glacier" in n.lower() and \
                _has_cyrillic(n):
            n = "л. " + n
        elif n.lower().startswith('glacier '):
            n = re.sub("^glacier ", "gl. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' glacier'): n = re.sub(" glacier$", " gl.", n, flags=re.IGNORECASE)
    if n.strip() == "л.": n = ""
    return " ".join(n.strip().split())
