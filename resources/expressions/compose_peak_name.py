from qgis.core import *
from qgis.gui import *
import math

UNITS_VARIABLE = "map_units"
FEET_PER_METER = 3.28084


def _project_variable(name):
    """Переменная проекта: язык сокращений, единицы длины."""
    try:
        scope = QgsExpressionContextUtils.projectScope(QgsProject.instance())
        return scope.variable(name)
    except Exception:
        return None


def _feet_enabled():
    return str(_project_variable(UNITS_VARIABLE) or "").strip() == "ft"


def _elevation(value, feet):
    """Высота для подписи: целое число в выбранных единицах."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if feet:
        number *= FEET_PER_METER
    return int(round(number))


def _elevation_suffix(name_pref_suf_lang, feet):
    """« м»/« ф» на русской карте, « m»/« f» на остальных."""
    if name_pref_suf_lang and name_pref_suf_lang == "ru":
        return " ф" if feet else " м"
    return " f" if feet else " m"


@qgsfunction(args='auto', group='Custom')

def compose_peak_name(name, ele, map_scale, name_pref_suf_lang, feature, parent):
    result = ""
    ele_str = ""
    feet = _feet_enabled()
    suffix = _elevation_suffix(name_pref_suf_lang, feet)
    if ele < -500: ele = ""

    ele = num(ele)
    name_type=type(num(name))
    if not isBlank(name): result = name;
    if name_type == str:
        shown = _elevation(ele, feet) if ele != "null" else None
        if shown is not None and str(shown) not in name:
            if not isBlank(name): result = result + "\n"
            ele_str = ele_str + str(shown) + suffix
            if not isBlank(name): ele_str = ele_str.strip()
            result = result + ele_str
    elif name_type == float: result = str(_elevation(num(name), feet)) + suffix
    if isBlank(name) and map_scale > 100000: result = ""
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
