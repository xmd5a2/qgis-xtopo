from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def num(s,feature, parent):
    try:
        return float(s.replace(",",".").replace("m","").replace("м","").strip())
    except ValueError:
        return "null"
