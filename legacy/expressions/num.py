from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def num(s, feature, parent):
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",","."))
        except ValueError:
            return ""
