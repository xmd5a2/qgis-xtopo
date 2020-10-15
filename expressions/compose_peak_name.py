from qgis.core import *
from qgis.gui import *
import math

@qgsfunction(args='auto', group='Custom')

def compose_peak_name(name, ele, map_scale, name_pref_suf_lang, feature, parent):
    result = ""
    ele_str = ""
    if name_pref_suf_lang and name_pref_suf_lang == "ru":
        suffix = " м"
    else :
        suffix = " m"
    if ele < -500: ele = ""

    ele = num(ele)
    print(type(num(name)))
    name_type=type(num(name))
    if not isBlank(name): result = name;
    if name_type == str:
        if not ele == "null" and str(math.floor(ele)) not in name:
            if not isBlank(name): result = result + "\n"
            if not ele == "null":
                ele_str = ele_str + str(round(ele)) + suffix
            if not isBlank(name): ele_str = ele_str.strip()
            result = result + ele_str
    elif name_type == float: result = str(round(num(name))) + suffix
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
