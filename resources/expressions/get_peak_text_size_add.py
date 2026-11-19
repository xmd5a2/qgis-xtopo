from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_peak_text_size_add(ele, feature, parent):
    try:
        value = float(str(ele).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    if value < 1000:
        return 0.0
    if value < 2000:
        return 0.5
    if value < 3000:
        return 1.0
    if value < 4000:
        return 1.5
    return 2.0
