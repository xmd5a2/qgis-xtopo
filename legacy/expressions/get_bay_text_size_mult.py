from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_bay_text_size_mult(area, feature, parent):
    mult=1
    if area > 250000 and area < 1000000 : mult = 1.1
    elif area >= 1000000 and area < 2500000 : mult = 1.2
    elif area >= 2500000 and area < 5000000 : mult = 1.3
    elif area >= 5000000 and area < 10000000 : mult = 1.45
    elif area >= 10000000 and area < 25000000 : mult = 1.6
    elif area >= 25000000 and area < 50000000 : mult = 1.7
    elif area >= 50000000 : mult = 1.8
    return mult
