from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_order_add(value, min, max, range, feature, parent):
    value = num(value)
    min = num(min)
    max = num(max)
    range = num(range)
    return ((value - min) / (max - min)) * range

def num(s):
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",","."))
        except ValueError:
            return "null"
