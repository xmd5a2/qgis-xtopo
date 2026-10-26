from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_text_size_add(value, min, max, range, feature, parent):
    r = ((value - min) / (max - min)) * range
    if r > range: r = range
    if r < 0: r = 0
    return r
