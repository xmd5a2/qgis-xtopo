from qgis.core import *
from qgis.gui import *
import math
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
    слова и высоты на карте сокращаются по-русски всегда. Отличить
    русскую карту от местной позволяет переменная ``@name_local``.
    """
    if str(name_pref_suf_lang or "").strip().lower() != "ru":
        return False
    if name_local is None:
        name_local = _project_variable(LOCAL_VARIABLE)
    local = str(name_local).strip().lower()
    return local not in ("1", "true")


@qgsfunction(args=-1, group='Custom')

def compose_mountain_pass_name(values, feature, parent):
    name, ele, rtsa_scale, map_scale, name_pref_suf_lang = values[:5]
    # Новый шаблон передаёт шестым аргументом @name_local. В проектах,
    # собранных прежним шаблоном, аргумента нет — читаем переменную.
    if len(values) > 5 and values[5] is not None:
        name_local = values[5]
    else:
        name_local = _project_variable(LOCAL_VARIABLE)
    result = ""
    if name_pref_suf_lang and name_pref_suf_lang == "ru":
        suffix = " м"
    else :
        suffix = " m"
    ele_rtsa_str = ""
    ele_plus_suffix_str=""
    if ele == -9999: ele = ""
    ele = num(ele)
    rtsa = "" if rtsa_scale is None else str(rtsa_scale)
    # Категория трудности (в том числе «н/к») — русская нотация, к имени
    # не кириллицей она не приписывается. На русской карте правило прежнее.
    if (not isBlank(rtsa) and not isBlank(name)
            and not _has_cyrillic(name)
            and not _map_language_is_ru(name_pref_suf_lang, name_local)):
        rtsa = ""
    if not isBlank(name): result = result + name;
    if not ele == "null" or not isBlank(rtsa):
        if not isBlank(name):
            result = result + "\n"
            ele_rtsa_str = ele_rtsa_str.strip()
        if not isBlank(rtsa): ele_rtsa_str = rtsa + " "
        if not ele == "null":
            if str(math.floor(ele)) not in name:
                ele_plus_suffix_str = str(round(ele)) + suffix
            ele_rtsa_str = ele_rtsa_str + ele_plus_suffix_str
        result = result + ele_rtsa_str
        if isBlank(name) and isBlank(rtsa) and map_scale > 100000: result = ""
    return result

def isBlank(myString):
    if myString and myString.strip():
        return False
    return True

def num(s):
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",","."))
        except ValueError:
            return "null"
