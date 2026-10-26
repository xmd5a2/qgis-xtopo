from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_highway_color(highway_integrity, tunnel, feature, parent):
    color = "#FFFFFFFF"
    if tunnel == "false" :
        if highway_integrity <= 2 : color="#FFFF7900" 
        if highway_integrity == 3 : color="#FFFFBB6E"
        if highway_integrity >= 4 and highway_integrity <= 10 : color="#FFFFDBB0"
    else :
        if highway_integrity <= 2 : color="#FFFFD0A6"
        if highway_integrity == 3 : color="#FFFFE1BF"
        if highway_integrity >= 4 and highway_integrity <= 10 : color="#FFFFEBD4"
    return color
