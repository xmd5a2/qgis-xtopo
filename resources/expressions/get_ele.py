from qgis.core import *
from qgis.gui import *

UNITS_VARIABLE = "map_units"
FEET_PER_METER = 3.28084


def _project_variable(name):
    try:
        scope = QgsExpressionContextUtils.projectScope(QgsProject.instance())
        return scope.variable(name)
    except Exception:
        return None


def _feet_enabled():
    return str(_project_variable(UNITS_VARIABLE) or "").strip() == "ft"


def _elevation(number, feet):
    if feet:
        number *= FEET_PER_METER
    return int(round(number))


def _elevation_suffix(feet):
    """« м»/« ф» или « m»/« f» — по языку карты."""
    language = _project_variable("name_pref_suf_lang")
    if language and str(language) == "ru":
        return " ф" if feet else " м"
    return " f" if feet else " m"


@qgsfunction(args='auto', group='Custom')
def get_ele(ele, ele_calc, feature, parent):
    result=""
    if int(ele_calc) > -500: result=ele_calc
    if int(ele) > -500:
        feet = _feet_enabled()
        result="\n"+str(_elevation(float(ele), feet))+_elevation_suffix(feet)
    return result
