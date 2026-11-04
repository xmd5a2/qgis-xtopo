from qgis.core import *
from qgis.gui import *
import re

# Слово типа объекта и его сокращение.
TYPE_WORD = "мыс"
TYPE_SHORT = "м."


def _has_cyrillic(text):
    """Есть ли в названии кириллические буквы.

    Тип объекта по-русски приписывается только названию кириллицей:
    к грузинскому или латинскому названию «м. » приписывать нечего.
    """
    return bool(re.search(r"[А-Яа-яЁё]", text))


def _type_word_only(text):
    """Название состоит только из слова типа: «Мыс», «мыс.».

    Такое название префиксом не дополняется: иначе получается «м. Мыс».
    В группе населённых пунктов так же оставлено «Остров», а не «о. Остров».
    """
    return text.strip().lower().rstrip(".") == TYPE_WORD


@qgsfunction(args='auto', group='Custom')
def format_cape_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
#         if len(n) <= 3 and not "м." in n.lower():
#             n = "м " + n
        if n.lower().startswith("мыс ") or n.lower().startswith("мыс. "):
            n = re.sub(r"^мыс\.? ", "м. ", n, flags=re.IGNORECASE).strip()
        elif n.lower().endswith(" мыс") or n.lower().endswith(" мыс."):
            n = re.sub(r" мыс\.?$", " м.", n, flags=re.IGNORECASE).strip()
        if not n.lower().startswith("мыс ") and \
                not n.lower().startswith("мыс. ") and \
                not n.lower().endswith(" мыс") and \
                not n.lower().endswith(" мыс.") and \
                not n.lower().startswith("м.") and \
                not n.lower().endswith(" м.") and \
                not _type_word_only(n) and \
                _has_cyrillic(n):
            n = "м. " + n
    if n.strip() == "м.": n = ""

    return n
