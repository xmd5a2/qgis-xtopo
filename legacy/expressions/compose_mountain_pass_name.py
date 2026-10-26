from qgis.core import *
from qgis.gui import *
import math

@qgsfunction(args='auto', group='Custom')

def compose_mountain_pass_name(name, ele, rtsa_scale, map_scale, name_pref_suf_lang, feature, parent):
    result = ""
    if name_pref_suf_lang and name_pref_suf_lang == "ru":
        suffix = " м"
    else :
        suffix = " m"
    ele_rtsa_str = ""
    ele_plus_suffix_str=""
    if ele == -9999: ele = ""
    ele = num(ele)
    if not isBlank(name): result = result + name;
    if not ele == "null" or not isBlank(str(rtsa_scale)):
        if not isBlank(name):
            result = result + "\n"
            ele_rtsa_str = ele_rtsa_str.strip()
        if not isBlank(str(rtsa_scale)): ele_rtsa_str = rtsa_scale + " "
        if not ele == "null":
            if str(math.floor(ele)) not in name:
                ele_plus_suffix_str = str(round(ele)) + suffix
            ele_rtsa_str = ele_rtsa_str + ele_plus_suffix_str
        result = result + ele_rtsa_str
        if isBlank(name) and isBlank(str(rtsa_scale)) and map_scale > 100000: result = ""
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
