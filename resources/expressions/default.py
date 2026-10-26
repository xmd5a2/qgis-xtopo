from qgis.core import *
from qgis.gui import *
from math import atan2

@qgsfunction(args='auto', group='Custom')
def get_azimuth(layername,tolerance, feature, parent):
    # initializations
    registry = QgsProject.instance()
    layer = registry.mapLayersByName( layername )[0]
    trails  = QgsProject.instance().addMapLayer(layer)
    x = feature.geometry().asPoint().x()
    y = feature.geometry().asPoint().y()

    # get the rectangular search area 
    searchRect = QgsRectangle(x - tolerance, y - tolerance,  x + tolerance, y + tolerance)

    # find trails 
    for trail in trails.getFeatures(QgsFeatureRequest().setFilterRect(searchRect)):
        # get the nearest vertex on trail and the one before and after
        pnt, v, b, a, d = trail.geometry().closestVertex(feature.geometry().asPoint())
        if v == -1:
            continue
        p1 = trail.geometry().vertexAt(v)
        # when vertex before exists look back, otherwise look forward
        if v>-1 and b>-1:
            p2 = trail.geometry().vertexAt(b)
        elif v>-1 and a>-1:
            p2 = trail.geometry().vertexAt(a)
        # calculate azimuth
        angle = atan2(p2.x() - p1.x(), p2.y() - p1.y()) / 0.017453
        return angle
