from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_tile_id(top, left, feature, parent):
    x = left
    y = top
    lat_str = "N"
    lon_str = "E"
    lat = 0
    lon = 0
    if int(y) > 0:
        lat_str = "N"
        lat = str(int(y) - 1)
    elif int(y) <= 0:
        lat_str = "S"
        lat = str(abs(int(y) - 1))
    if int(x) >= 0:
        lon_str = "E"
        lon = x
    elif int(x) < 0:
        lon_str = "W"
        lon = str(abs(int(x)))
    if int(lat) < 10:
        lat = "0" + lat
    if int(lon) < 100 and int(lon) >= 10:
        lon = "0" + lon
    elif int(lon) < 10:
        lon = "00" + lon
    tile_id = lat_str + lat + lon_str + lon
    return tile_id

def num(s):
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",","."))
        except ValueError:
            return "null"
