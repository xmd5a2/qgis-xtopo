from qgis.core import *
from qgis.gui import *

from qgis_xtopo.core.highway_integrity import calculate


@qgsfunction(args='auto', group='Custom')
def calculate_highway_integrity(highway, surface, smoothness, tracktype, feature, parent):
    # Значение материализуется при подготовке; выражение остаётся для
    # старых проектов и ручных вычислений в QGIS.
    return calculate(highway, surface, smoothness, tracktype)
