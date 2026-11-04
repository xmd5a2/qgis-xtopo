from qgis.core import *
from qgis.gui import *
import re

# Слова типа объекта: хребет и его отрог.
TYPE_WORDS = ("хребет", "отрог")


def _has_cyrillic(text):
    """Есть ли в названии кириллические буквы.

    Тип объекта по-русски приписывается только названию кириллицей:
    к грузинскому или латинскому названию «хр. » приписывать нечего.
    """
    return bool(re.search(r"[А-Яа-яЁё]", text))


def _type_word_only(text):
    """Название состоит только из слова типа: «Хребет», «отрог».

    Такое название префиксом не дополняется: иначе получается «хр. Хребет».
    """
    return text.strip().lower().rstrip(".") in TYPE_WORDS


@qgsfunction(args='auto', group='Custom')
def format_ridge_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
        if n.lower().startswith("хребет ") or n.lower().startswith("хребет. "):
            n = re.sub(r"^хребет\.? ", "хр. ", n, flags=re.IGNORECASE).strip()
        if n.lower().endswith(" хребет") or n.lower().endswith(" хребет."):
            n = re.sub(r" хребет\.?$", " хр.", n, flags=re.IGNORECASE).strip()
        if n.lower().startswith("отрог ") or n.lower().startswith("отрог. "):
            n = re.sub(r"^отрог\.? ", "отр. ", n, flags=re.IGNORECASE).strip()
        if n.lower().endswith(" отрог") or n.lower().endswith(" отрог."):
            n = re.sub(r" отрог\.?$", " отр.", n, flags=re.IGNORECASE).strip()
        if not "хр." in n.lower() \
        and not n.lower().startswith("отр. ") \
        and not n.lower().endswith(" отр.") \
        and not "скалы " in n.lower() \
        and not " скалы" in n.lower() \
        and not " лбы" in n.lower() \
        and not n.lower().endswith(" ребро") \
        and not n.lower().startswith("ребро ") \
        and not n.lower().endswith(" плечо") \
        and not n.lower().startswith("плечо ") \
        and not n.lower().endswith(" гребень") \
        and not n.lower().startswith("гребень ") \
        and not _type_word_only(n) \
        and _has_cyrillic(n): n = "хр. " + n
    elif n.lower().startswith('ridge '): n = re.sub("^ridge ","rid. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' ridge'): n = re.sub(" ridge$"," rid.", n, flags=re.IGNORECASE)
    return n
