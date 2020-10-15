# -*- coding: utf-8 -*-
"""
/***************************************************************************
ReferenceFunctions
                                 A QGIS plugin
 Provide field calculator function for Reference to other layers/features
 based on Nathan Woodrow work:
 http://nathanw.net/2012/11/10/user-defined-expression-functions-for-qgis/
                              -------------------
        begin                : 2014-09-20
        copyright            : (C) 2014 by enrico ferreguti
        email                : enricofer@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
# Import the PyQt and QGIS libraries
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtSql import *
try:
    from qgis.PyQt.QtWidgets import *
except:
    pass
#from qgis.core import *
import qgis
from qgis.utils import iface,qgsfunction
from qgis.core import QgsGeometry,QgsExpression,QgsMapLayer,QgsFeatureRequest
# Import the code for the dialog
from .reffunctionsdialog import refFunctionsDialog
import os.path
import sys


def _getLayerSet():
        try:    #qgis 2
            return {layer.name():layer for layer in iface.legendInterface().layers()}
        except: #qgis 3
            return {layer.name():layer for layer in qgis.core.QgsProject.instance().mapLayers().values()}



@qgsfunction(4, "Reference", register=False)
def dbvalue(values, feature, parent):
    """
        Retrieve first targetField value from targetLayer when keyField is equal to conditionValue

        <h4>Syntax</h4>
        <p>dbvalue(<i>targetLayer,targetField,keyField,conditionValue</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetLayer</i> &rarr; a field of targetLayer whom value is needed, for example 'myTargetField'. In case of multiple results only the first is retrieved. If targetLayer = '$geometry' geometry value is retrieved <br></p>
        <i>  keyField</i> &rarr; a field of targetLayer used to search between features, for example 'myKeyField'. <br></p>
        <i>  conditionValue</i> &rarr; value used for comparisons with keyField content. Note thet the value need to be of the same type of keyField<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             dbvalue('myLayer','myTargetField','myKeyField',value) <br>
        </p>
        <h4>Notes</h4>
        <p>
             The example function is similar to dbquery('myLayer','myTargetField','myKeyField =value') , but is significantly faster for large database
        </p>
    """
    dbg = debug()
    dbg.out("evaluating dbvalue")
    targetLayerName = values[0]
    targetFieldName = values[1]
    keyFieldName = values[2]
    contentCondition = values[3]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("Error: invalid targetLayerName")
        return

    #if not targetLayerName in iface.legendInterface().layers():
    #    parent.setEvalErrorString("error: targetLayer not present")
    #iface = QgsInterface.instance()
    res = None
    for feat in layerSet[targetLayerName].getFeatures():
        if feat.attribute(keyFieldName) == contentCondition:
            if targetFieldName == "$geometry":
                res = feat.geometry().asWkt()
            else:
                try:
                    res = feat.attribute(targetFieldName)
                except:
                    parent.setEvalErrorString("Error: invalid targetFieldName")
                    return
    return res

@qgsfunction(3, "Reference", register=False, usesgeometry=True)
def dbvaluebyid(values, feature, parent):
    """
        Retrieve the targetField value from targetLayer using internal feature ID

        <h4>Syntax</h4>
        <p>dbvaluebyid(<i>'targetLayer','targetField',featureID</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field of targetLayer whom value is needed, for example 'myTargetField'. In case of multiple results only the first is retrieved. If targetField = '$geometry' geometry value is retrieved <br></p>
        <i>  featureID</i> &rarr; A Integer number reference to internal feature ID. <br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             dbvaluebyid('myLayer','myTargetField',112) <br>
        </p>
    """
    dbg = debug()
    dbg.out("evaluating dbvalue")
    targetLayerName = values[0]
    targetFieldName = values[1]
    targetFeatureId = values[2]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("Error: invalid targetLayerName")
        return

    #if not targetLayerName in iface.legendInterface().layers():
    #    parent.setEvalErrorString("error: targetLayer not present")
    #iface = QgsInterface.instance()
    for name,layer in layerSet.items():
        if name == targetLayerName:
            try:
                targetFeatureIter = layer.getFeatures(QgsFeatureRequest(targetFeatureId))
                for targetFeature in targetFeatureIter:
                    pass
            except:
                parent.setEvalErrorString("Error: invalid targetFeatureIndex")
                return
            if targetFieldName == "$geometry":
                res = targetFeature.geometry().asWkt()
            else:
                try:
                    res = targetFeature.attribute(targetFieldName)
                except:
                    parent.setEvalErrorString("Error: invalid targetFieldName")
                    return
    return res



@qgsfunction(3, "Reference", register=False)
def dbquery(values, feature, parent):
    """
        Retrieve first targetField value from targetLayer when whereClause is true

        <h4>Syntax</h4>
        <p>dqquery(<i>targetLayer,targetField,whereClause</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetLayer</i> &rarr; a field of targetLayer whom value is needed, for example 'myField'. In case of multiple results only the first is retrieved. If targetLayer = '$geometry' geometry value is retrieved <br></p>
        <i>  whereClause</i> &rarr; a valid expression string without duoble quotes to identify fields, for example 'field1 > 1 and field2 = "foo"' <br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             dbquery('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    targetLayerName = values[0].replace('"','')
    targetFieldName = values[1].replace('"','')
    whereClause = values[2].replace('"','')
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("Error: invalid targetLayerName")
        return
    dbg=debug()
    dbg.out("evaluating dbquery")

    for layerName, iterLayer in layerSet.items():
        if layerName == targetLayerName:
            exp = QgsExpression(whereClause)
            exp.prepare(iterLayer.dataProvider().fields())
            for feat in iterLayer.getFeatures():
                if exp.evaluate(feature):
                    if targetFieldName == "$geometry":
                        return feat.geometry().asWkt()
                    else:
                        try:
                            return feat.attribute(targetFieldName)
                        except:
                            parent.setEvalErrorString("Error: invalid targetField")


@qgsfunction(2, "Reference", register=False)
def dbsql(values, feature, parent):
    """
        Retrieve results from SQL query

        <h4>Syntax</h4>
        <p>dbsql(<i>connectionName,sqlQuery</i>)</p>

        <h4>Arguments</h4>
        <p><i>  connectionName</i> &rarr; the name of a currently registered database connection, for example 'myConnection'.<br>
        <i>  sqlQuery</i> &rarr; a valid sql query string where do, for example 'field1 > 1 and field2 = "foo"' <br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             dbquery('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating dbsql")
    connectionName = values[0]
    sqlQuery = values[1].replace('""','@#@')
    sqlQuery = sqlQuery.replace('"',"'")
    sqlQuery = sqlQuery.replace('@#@','"')
    conn = SQLconnection(connectionName)
    if conn.lastError()=="":
        res = conn.submitQuery(sqlQuery)
        dbg.out(conn.lastError())
        if conn.lastError()=="":
            if res!=[]:
                if len(res)>1 or len(res[0])>1:
                    parent.setEvalErrorString("Error: multiple results")
                else:
                    return res[0][0]
            else:
                parent.setEvalErrorString("Error: null query result")
        else:
            parent.setEvalErrorString("Error: invalid query\n"+conn.lastError())
    else:
        parent.setEvalErrorString("Error: invalid connection")

@qgsfunction(1, "Reference", register=False, usesgeometry=True)
def geomRedef(values, feature, parent):
    """
        redefine the current feature geometry with a new WKT geometry

        <h4>Syntax</h4>
        <p>geomRedef(<i>WKTgeometry</i>)</p>

        <h4>Arguments</h4>
        <p><i>  WKTgeometry</i> &rarr; a valid WKT geometry provided by expression commands .<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myGeometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("self redefine geometry")
    targetGeometry = values[0]
    if iface.mapCanvas().currentLayer().isEditable():
        try:
            iface.mapCanvas().currentLayer().changeGeometry(feature.id(), targetGeometry)
            #iface.mapCanvas().currentLayer().updateExtents()
            #iface.mapCanvas().currentLayer().setCacheImage(None)
            iface.mapCanvas().currentLayer().triggerRepaint()
            #feature.setGeometry(targetGeometry)
            return 1
        except:
            parent.setEvalErrorString("Error: geometry is not valid")
            return 0

@qgsfunction(1, "Reference", register=False)
def xx(values, feature, parent):
    """
        Return the coordinate x of the given point geometry

        <h4>Syntax</h4>
        <p>dbsql(<i>geometry</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("xx")
    pass

@qgsfunction(1, "Transformation", register=False)
def canvaswidth(values, feature, parent):
    """
        Return the width of the current canvas width (in pixel or map units)

        <h4>Syntax</h4>
        <p>CanvasWidth(<i>'units'</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("CanvasWidth")
    mapCanvas = iface.mapCanvas()
    if values[0]=='pixels':
        res = iface.mapCanvas().mapRenderer().width()
    elif values[0]=='mapunits':
        res = iface.mapCanvas().mapRenderer().width()*iface.mapCanvas().mapRenderer().mapUnitsPerPixel ()
    elif values[0]=='mm':
        pass
    try:
        return res
    except:
        parent.setEvalErrorString("error: argument not valid")


@qgsfunction(1, "Transformation", register=False)
def canvasheight(values, feature, parent):
    """
        Return the width of the current canvas width (in pixel or map units)

        <h4>Syntax</h4>
        <p>CanvasWidth(<i>'units'</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("CanvasWidth")
    mapCanvas = iface.mapCanvas()
    if values[0]=='pixels':
        res = iface.mapCanvas().mapRenderer().height()
    elif values[0]=='mapunits':
        res = iface.mapCanvas().mapRenderer().height()*iface.mapCanvas().mapRenderer().mapUnitsPerPixel ()
    elif values[0]=='mm':
        pass
    try:
        return res
    except:
        parent.setEvalErrorString("error: argument not valid")

@qgsfunction(0, "Transformation", register=False, usesgeometry=True)
def canvasx(values, feature, parent):
    """
        Return the width of the current canvas width (in pixel or map units)

        <h4>Syntax</h4>
        <p>CanvasWidth(<i>'units'</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("canvasx")
    if iface.mapCanvas().mapRenderer().extent().intersects(feature.geometry().boundingBox()):
        leftX = iface.mapCanvas().mapRenderer().extent().xMinimum()
        featX = feature.geometry().pointOnSurface ().boundingBox().xMinimum()
        dbg.out(leftX)
        dbg.out(featX)
        canvasX = round ((featX-leftX)/iface.mapCanvas().mapRenderer().mapUnitsPerPixel())
        return canvasX
    else:
        return

@qgsfunction(0, "Transformation", register=False, usesgeometry=True)
def canvasy(values, feature, parent):
    """
        Return the width of the current canvas width (in pixel or map units)

        <h4>Syntax</h4>
        <p>CanvasWidth(<i>'units'</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("canvasy")
    if iface.mapCanvas().mapRenderer().extent().intersects(feature.geometry().boundingBox()):
        bottomY = iface.mapCanvas().mapRenderer().extent().yMinimum()
        featY = feature.geometry().pointOnSurface ().boundingBox().yMinimum()
        dbg.out(bottomY)
        dbg.out(featY)
        canvasY = round ((featY-bottomY)/iface.mapCanvas().mapRenderer().mapUnitsPerPixel())
        return canvasY
    else:
        return


@qgsfunction(1, "Reference", register=False)
def WKTcentroid(values, feature, parent):
    """
        Return the center of mass of the given geometry

        <h4>Syntax</h4>
        <p>WKTcentroid(<i>'WKTgeometry'</i>)</p>

        <h4>Arguments</h4>
        <p><i>WKTgeometry</i> &rarr; a valid WKTgeometry<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             WKTcentroid('POLYGON((602793.98 6414014.88,....))') <br>

        </p>
    """
    dbg=debug()
    dbg.out("centroid")
    ArgGeometry = QgsGeometry().fromWkt(values[0])
    try:
        return ArgGeometry.centroid().asWkt()
    except:
        parent.setEvalErrorString("error: WKT geometry not valid")
        return


@qgsfunction(1, "Reference", register=False)
def WKTpointonsurface(values, feature, parent):
    """
        Return the point within  the given geometry

        <h4>Syntax</h4>
        <p>WKTpointonsurface(<i>'WKTgeometry'</i>)</p>

        <h4>Arguments</h4>
        <p><i>WKTgeometry</i> &rarr; a valid WKTgeometry<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             WKTpointonsurface('POLYGON((602793.98 6414014.88,....))') <br>

        </p>
    """
    dbg=debug()
    dbg.out("centroid")
    ArgGeometry = QgsGeometry().fromWkt(values[0])
    try:
        return ArgGeometry.pointOnSurface().asWkt()
    except:
        parent.setEvalErrorString("error: WKT geometry not valid")
        return

@qgsfunction(1, "Reference", register=False)
def WKTlenght(values, feature, parent):
    """
        Return the length of the given geometry

        <h4>Syntax</h4>
        <p>WKTlenght<i>'WKTgeometry'</i>)</p>

        <h4>Arguments</h4>
        <p><i>WKTgeometry</i> &rarr; a valid WKTgeometry<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             WKTlenght('POLYGON((602793.98 6414014.88,....))') <br>

        </p>
    """
    dbg=debug()
    dbg.out("lenght")
    ArgGeometry = QgsGeometry().fromWkt(values[0])
    try:
        return ArgGeometry.length()
    except:
        parent.setEvalErrorString("error: WKT geometry not valid")
        return

@qgsfunction(1, "Reference", register=False)
def WKTarea(values, feature, parent):
    """
        Return the area of the given geometry

        <h4>Syntax</h4>
        <p>WKTarea<i>'WKTgeometry'</i>)</p>

        <h4>Arguments</h4>
        <p><i>WKTgeometry</i> &rarr; a valid "Well Known Text" geometry<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             WKTarea('POLYGON((602793.98 6414014.88,....))') <br>

        </p>
    """
    dbg=debug()
    dbg.out("area")
    ArgGeometry = QgsGeometry().fromWkt(values[0])
    try:
        print(ArgGeometry.asWkt() )
        return ArgGeometry.area()
    except:
        #parent.setEvalErrorString("error: WKT geometry not valid")
        return None

@qgsfunction(0,"Reference", register=False, usesgeometry=True)
def nearestVertex(values, feature, parent):
    """
        Return the nearest vertex as point geometry

        <h4>Syntax</h4>
        <p>dbsql(<i>geometry</i>)</p>

        <h4>Arguments</h4>
        <p><i>  geometry</i> &rarr; a valid geometry provided by expression commands 'myGeometry'.<br></p>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomRedef('myLayer','myField','field1 > 1 and field2 = "foo"') <br>
             dbquery('myLayer','$geometry','field1 > 1 and field2 = "foo"') <br></p>

        </p>
    """
    dbg=debug()
    dbg.out("nearestVertex")
    pass

@qgsfunction(2, "Reference", register=False, usesgeometry=True)
def geomnearest(values, feature, parent):
    """
        Retrieve target field value from the nearest target feature in target layer

        <h4>Syntax</h4>
        <p>geomnearest(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. <br/>
        If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved. If otherwise is equal to '$distance' the calculated distance between source and target features will be returned<br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomnearest('targetLayer','TargetField') <br>
             geomnearest('targetLayer','$geometry') <br>
             geomnearest('targetLayer','$distance') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomnearest")
    targetLayerName = values[0]
    targetFieldName = values[1]
    dmin = sys.float_info.max
    actualGeom = feature.geometry()
    layerSet = _getLayerSet()
    if not targetLayerName in layerSet.keys():
        parent.setEvalErrorString("error: targetLayer not present")
        return
    count = 0
    for name, layer in layerSet.items():
        if layer != iface.mapCanvas().currentLayer() and layer.type() == QgsMapLayer.VectorLayer and (targetLayerName == '' or layer.name() == targetLayerName ):
            dbg.out(layer.name())
            iter = layer.getFeatures()
            for feat in iter:
                dtest = actualGeom.distance(feat.geometry())
                count += 1
                if count < 100000:
                    if dtest<dmin:
                        dmin = dtest
                        if targetFieldName=="$geometry":
                            dminRes = feat.geometry().asWkt()
                        elif targetFieldName=="$distance":
                            dminRes = dmin
                        elif targetFieldName=="$id":
                            dminRes = feat.id()
                        else:
                            try:
                                dminRes = feat.attribute(targetFieldName)
                            except:
                                parent.setEvalErrorString("error: targetFieldName not present")
                                return
                else:
                    parent.setEvalErrorString("error: too many features to compare")
    dbg.out("DMIN")
    dbg.out(dmin)
    if count > 0:
        try:
            return dminRes
        except:
            return -1
    else:
        parent.setEvalErrorString("error: no features to compare")


@qgsfunction(3, "Reference", register=False, usesgeometry=True)
def geomdistance(values, feature, parent):
    """
        Retrieve target field value from target feature in target layer if target feature is in distance

        <h4>Syntax</h4>
        <p>geomdistance(<i>'targetLayer','targetField',distanceCheck</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. <br/>
        <i>  distance</i> &rarr; maximum distance from feature to be considered. <br/>
        If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved. If otherwise is equal to '$distance' the calculated distance between source and target features will be returned<br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomdistance('targetLayer','TargetField',100) <br>
             geomdistance('targetLayer','$geometry',100) <br>
             geomdistance('targetLayer','$distance',100) <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomdistance")
    targetLayerName = values[0]
    targetFieldName = values[1]
    distanceCheck = values[2]
    dmin = sys.float_info.max
    actualGeom = feature.geometry()
    layerSet = _getLayerSet()
    if not targetLayerName in layerSet.keys():
        parent.setEvalErrorString("error: targetLayer not present")
        return
    count = 0
    for name,layer in layerSet.items():
        if layer != iface.mapCanvas().currentLayer() and layer.type() == QgsMapLayer.VectorLayer and (targetLayerName == '' or layer.name() == targetLayerName ):
            dbg.out(layer.name())
            iter = layer.getFeatures()
            for feat in iter:
                dtest = actualGeom.distance(feat.geometry())
                count += 1
                if count < 100000:
                    if dtest<dmin and dtest<=distanceCheck:
                        dmin = dtest
                        if targetFieldName=="$geometry":
                            dminRes = feat.geometry().asWkt()
                        elif targetFieldName=="$distance":
                            dminRes = dmin
                        elif targetFieldName=="$id":
                            dminRes = feat.id()
                        else:
                            try:
                                dminRes = feat.attribute(targetFieldName)
                            except:
                                parent.setEvalErrorString("error: targetFieldName not present")
                                return
                else:
                    parent.setEvalErrorString("error: too many features to compare")
    dbg.out("DMIN")
    dbg.out(dmin)
    if count > 0:
        try:
            return dminRes
        except:
            return -1
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomwithin(values, feature, parent):
    """
        Retrieve target field value when source feature is within target feature in target layer

        <h4>Syntax</h4>
        <p>geomwithin(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomwithin('targetLayer','TargetField') <br>
             geomwithin('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomwithin")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    dbg.out(layerSet[targetLayerName].id())
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    #fieldSet = [field.name() for field in layerSet[targetLayerName].pendingFields()]
    #if not(targetFieldName in fieldSet):
    #    parent.setEvalErrorString("error: targetFieldName not present")
    #    return None
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().within(feat.geometry()):
                dbg.out('OK:'+str(feat.id()))
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName = '%s' not present" % targetFieldName)
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
            return None
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomtouches(values, feature, parent):
    """
        Retrieve target field value when source feature touches target feature in target layer

        <h4>Syntax</h4>
        <p>geomtouches(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomtouches('targetLayer','TargetField') <br>
             geomtouches('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomtouches")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().touches(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomintersects(values, feature, parent):
    """
        Retrieve target field value when source feature intersects target feature in target layer

        <h4>Syntax</h4>
        <p>geomintersects(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomintersects('targetLayer','TargetField') <br>
             geomintersects('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomtouches")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    dbg.out(layerSet)
    dbg.out(layerSet[targetLayerName].id())
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().intersects(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False, usesgeometry=True)
def geomcontains(values, feature, parent):
    """
        Retrieve target field value when source feature contains target feature in target layer

        <h4>Syntax</h4>
        <p>geomcontains(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomcontains('targetLayer','TargetField') <br>
             geomcontains('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomcontains")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    dbg.out(layerSet)
    dbg.out(layerSet[targetLayerName].id())
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().contains(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomdisjoint(values, feature, parent):
    """
        Retrieve target field value when source feature is disjoint from target feature in target layer

        <h4>Syntax</h4>
        <p>geomcontains(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomdisjoint('targetLayer','TargetField') <br>
             geomdisjoint('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomdisjoint")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().disjoint(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomequals(values, feature, parent):
    """
        Retrieve target field value when source feature is equal to target feature in target layer

        <h4>Syntax</h4>
        <p>geomequals(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomequals('targetLayer','TargetField') <br>
             geomequals('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomcontains")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().equals(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomtouches(values, feature, parent):
    """
        Retrieve target field value when source feature touches target feature in target layer

        <h4>Syntax</h4>
        <p>geomtouches(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomtouches('targetLayer','TargetField') <br>
             geomtouches('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomcontains")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().touches(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomoverlaps(values, feature, parent):
    """
        Retrieve target field value when source feature overlaps target feature in target layer

        <h4>Syntax</h4>
        <p>geomoverlaps(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomoverlaps('targetLayer','TargetField') <br>
             geomoverlaps('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomcontains")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().overlaps(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")

@qgsfunction(2, "Reference", register=False,usesgeometry=True)
def geomcrosses(values, feature, parent):
    """
        Retrieve target field value when source feature crosses target feature in target layer

        <h4>Syntax</h4>
        <p>geomcrosses(<i>targetLayer,targetField</i>)</p>

        <h4>Arguments</h4>
        <p><i>  targetLayer</i> &rarr; the name of a currently loaded layer, for example 'myLayer'.<br>
        <i>  targetField</i> &rarr; a field in target layer we want as result when source feature is within target feature, for example 'myField'. If targetField is equal to '$geometry' The WKT geometry of targetFeature willbe retrieved <br></p>

        <i>  Number of feature tested is limited to 100000 to avoid time wasting loops</i>

        <h4>Example</h4>
        <p><!-- Show examples of function.-->
             geomcrosses('targetLayer','TargetField') <br>
             geomcrosses('targetLayer','$geometry') <br>

        </p>
    """
    dbg=debug()
    dbg.out("evaluating geomcrosses")
    targetLayerName = values[0]
    targetFieldName = values[1]
    #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
    layerSet = _getLayerSet()
    if not (targetLayerName in layerSet.keys()):
        parent.setEvalErrorString("error: targetLayer not present")
        return
    if layerSet[targetLayerName].type() != QgsMapLayer.VectorLayer:
        parent.setEvalErrorString("error: targetLayer is not a vector layer")
        return
    count = 0
    for feat in layerSet[targetLayerName].getFeatures():
        count += 1
        if count < 100000:
            if feature.geometry().crosses(feat.geometry()):
                if targetFieldName=="$geometry":
                    dminRes = feat.geometry().asWkt()
                elif targetFieldName=="$id":
                    dminRes = feat.id()
                else:
                    try:
                        dminRes = feat.attribute(targetFieldName)
                    except:
                        parent.setEvalErrorString("error: targetFieldName not present")
                        return None
        else:
            parent.setEvalErrorString("error: too many features to compare")
    if count > 0:
        try:
            return dminRes
        except:
            return None
    else:
        parent.setEvalErrorString("error: no features to compare")
        
        
        
@qgsfunction(args=1, group='Reference',register = False, usesgeometry=True)
def intersecting_geom_count(values, feature, parent):
    """
        Get the count of the features in target layer intersected by the source feature
        
        <h4>Syntax</h4>
        <p>intersecting_geom_count(<i>'target_layer_name'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name </i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        
        <h4>Example</h4>
        <p><!-- Show example of function.-->
             intersecting_geom_count('COUNTRIES') &rarr; 665</p>
        
    """ 
    
    DEBUG = False
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    #targetFieldName = values[1]
    
    if feature.geometry() is not None:        
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
        
        
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().intersects(feature.geometry()):
                count += 1
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False
            

@qgsfunction(args=1, group='Reference',register = False, usesgeometry=True)
def within_geom_count(values, feature, parent):
    """
        Get the count of the features in target layer within  the source feature
        
        <h4>Syntax</h4>
        <p>within_geom_count(<i>'target_layer_name'</i>)</p>

        <h4>Arguments</h4>
        <p><i>  target_layer_name </i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        
        <h4>Example</h4>
        <p><!-- Show example of function.-->
             within_geom_count('COUNTRIES') &rarr; 665</p>
        
    """ 
    
    DEBUG = False
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    #targetFieldName = values[1]
    
    if feature.geometry() is not None:        
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
        
        
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().within(feature.geometry()):
                count += 1
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False
        
@qgsfunction(args=1, group='Reference',register = False, usesgeometry=True)
def overlapping_geom_count(values, feature, parent):
    """
        Get the count of the features in target layer overlaping  the source feature
        
        <h4>Syntax</h4>
        <p>overlapping_geom_count(<i>'target_layer_name'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name </i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        
        <h4>Example</h4>
        <p><!-- Show example of function.-->
             overlapping_geom_count('COUNTRIES') &rarr; 665</p>
        
    """ 
    
    DEBUG = False
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    #targetFieldName = values[1]
    
    if feature.geometry() is not None:        
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
        
        
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().overlaps(feature.geometry()):
                count += 1
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False

            

@qgsfunction(args=1, group='Reference',register = False, usesgeometry=True)
def equaling_geom_count(values, feature, parent):
    """
        Get the count of the features in target layer equaling (same geometry) by the source feature
        
        <h4>Syntax</h4>
        <p>equaling_geom_count(<i>'target_layer_name'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name </i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        
        <h4>Example</h4>
        <p><!-- Show example of function.-->
             equaling_geom_count('COUNTRIES') &rarr; 665</p>
        
    """ 
    
    DEBUG = True
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    #targetFieldName = values[1]
    
    if feature.geometry() is not None:        
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
        
        
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().isGeosEqual(feature.geometry()):
                count += 1
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False 
            
            
            
@qgsfunction(args=2, group='Reference',register = False, usesgeometry=True)
def intersecting_geom_sum(values, feature, parent):
    """
        Sums the geometries' attributes of the target layer intersected by the source feature
        
        <h4>Syntax</h4>
        <p>intersecting_geom_sum(<i>'target_layer_name','Field_name_to_sum'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name</i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        <i> Field_name_to_sum</i> : name of the field to sum, for exemple 'POPULATION' <br></p>

        <h4>Example</h4>
        <p><!-- Show example of function.-->
             intersecting_geom_sum('COUNTRIES','POPULATION') &rarr; 2165</p>
        
    """ 
    
    DEBUG = False
    
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    targetFieldName = values[1]
    
    if feature.geometry() is not None:
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
            
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0.0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().intersects(feature.geometry()):
                try:
                    count += float(feat[targetFieldName])
                except:
                    #case feat[targetFieldName] is null or string....
                    pass
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False
        
        

@qgsfunction(args=2, group='Reference',register = False, usesgeometry=True)
def within_geom_sum(values, feature, parent):
    """
        Sums the geometries' attributes of the target layer within  the source feature
        
        <h4>Syntax</h4>
        <p>within_geom_sum(<i>'target_layer_name','Field_name_to_sum'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name</i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        <i> Field_name_to_sum</i> : name of the field to sum, for exemple 'POPULATION' <br></p>

        <h4>Example</h4>
        <p><!-- Show example of function.-->
             within_geom_sum('COUNTRIES','POPULATION') &rarr; 2165</p>
        
    """ 
    
    DEBUG = False
    
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    targetFieldName = values[1]
    
    if feature.geometry() is not None:
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
            
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0.0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().within(feature.geometry()):
                try:
                    count += float(feat[targetFieldName])
                except:
                    #case feat[targetFieldName] is null or string....
                    pass
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False
        

@qgsfunction(args=2, group='Reference',register = False, usesgeometry=True)
def overlapping_geom_sum(values, feature, parent):
    """
        Sums the geometries' attributes of the target layer within  the source feature
        
        <h4>Syntax</h4>
        <p>overlapping_geom_sum(<i>'target_layer_name','Field_name_to_sum'</i>)</p>
        <h4>Arguments</h4>
        <p><i>  target_layer_name</i> : name of the target layer, for exemple 'COUNTRIES'.<br>
        <i> Field_name_to_sum</i> : name of the field to sum, for exemple 'POPULATION' <br></p>

        <h4>Example</h4>
        <p><!-- Show example of function.-->
             overlapping_geom_sum('COUNTRIES','POPULATION') &rarr; 2165</p>
        
    """ 
    
    DEBUG = False
    
    try:    #qgis 3
        if DEBUG : print('feat geom ',feature.geometry().asPolygon(), feature.geometry().area(), feature.hasGeometry())
    except: #qgis 2
        if DEBUG : print('feat geom ', feature.geometry())
    
    targetLayerName = values[0]
    targetFieldName = values[1]
    
    if feature.geometry() is not None:
        #layerSet = {layer.name():layer for layer in iface.legendInterface().layers()}
        layerSet = _getLayerSet()
            
        if not (targetLayerName in layerSet.keys()):
            parent.setEvalErrorString("error: targetLayer not present")
            return
        if layerSet[targetLayerName].type() != qgis.core.QgsMapLayer.VectorLayer:
            parent.setEvalErrorString("error: targetLayer is not a vector layer")
            return
            
        count = 0.0
        
        request = qgis.core.QgsFeatureRequest()
        request.setFilterRect(feature.geometry().boundingBox())
        for feat in layerSet[targetLayerName].getFeatures(request):
            if feat.geometry().overlaps(feature.geometry()):
                try:
                    count += float(feat[targetFieldName])
                except:
                    #case feat[targetFieldName] is null or string....
                    pass
        if DEBUG : print('feat ',feature.id(),'count',count)
        return count
        
    else:
        return False
        
        
        
        
class debug:

    def __init__(self):
        self.debug = None

    def out(self,string):
        if self.debug:
            print(string)

class SQLconnection:

    def __init__(self,conn):
        self.dbg = debug()
        s = QSettings()
        s.beginGroup("PostgreSQL/connections/"+conn)
        currentKeys = s.childKeys()
        self.PSQLDatabase=s.value("database", "" )
        self.PSQLHost=s.value("host", "" )
        self.PSQLUsername=s.value("username", "" )
        self.PSQLPassword=s.value("password", "" )
        self.PSQLPort=s.value("port", "" )
        self.PSQLService=s.value("service", "" )
        s.endGroup()
        self.db = QSqlDatabase.addDatabase("QPSQL")
        self.db.setHostName(self.PSQLHost)
        self.db.setPort(int(self.PSQLPort))
        self.db.setDatabaseName(self.PSQLDatabase)
        self.db.setUserName(self.PSQLUsername)
        self.db.setPassword(self.PSQLPassword)
        ok = self.db.open()
        if not ok:
            self.error = "Database Error: %s" % self.db.lastError().text()
            #QMessageBox.information(None, "DB ERROR:", error)
        else:
            self.error=""

    def submitQuery(self,sql):
        query = QSqlQuery(self.db)
        query.exec_(sql)
        self.dbg.out(sql)
        rows= []
        self.dbg.out("SQL RESULT:")
        self.dbg.out(query.lastError().type())
        self.dbg.out(query.lastError().text())
        if query.lastError().type() != QSqlError.NoError:
            self.error = "Database Error: %s" % query.lastError().text()
            #QMessageBox.information(None, "SQL ERROR:", resultQuery)
        else:
            self.error = ""
            while (query.next()):
                fields=[]
                count = 0
                #query.value(count)
                for k in range(0,query.record().count()):
                    try:
                        fields.append(unicode(query.value(k), errors='replace'))
                    except TypeError:
                        fields.append(query.value(k))
                    except AttributeError:
                        fields.append(str(query.value(k)))
                rows += [fields]
        self.dbg.out(rows)
        return rows

    def lastError(self):
        return self.error

class refFunctions:

    def __init__(self, iface):
        # Save reference to the QGIS interface
        self.iface = iface
        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)
        self.dbg = debug()
        self.dlg = refFunctionsDialog()

        # Create the dialog (after translation) and keep reference
        #self.dlg = refFunctionDialog()


    def initGui(self):
        self.dbg.out("initGui")
        QgsExpression.registerFunction(dbvalue)
        QgsExpression.registerFunction(dbvaluebyid)
        QgsExpression.registerFunction(dbquery)
        QgsExpression.registerFunction(dbsql)
        QgsExpression.registerFunction(WKTarea)
        QgsExpression.registerFunction(WKTcentroid)
        QgsExpression.registerFunction(WKTpointonsurface)
        QgsExpression.registerFunction(WKTlenght)
        QgsExpression.registerFunction(geomRedef)
        QgsExpression.registerFunction(geomnearest)
        QgsExpression.registerFunction(geomdistance)
        QgsExpression.registerFunction(geomwithin)
        QgsExpression.registerFunction(geomcontains)
        QgsExpression.registerFunction(geomcrosses)
        QgsExpression.registerFunction(geomdisjoint)
        QgsExpression.registerFunction(geomequals)
        QgsExpression.registerFunction(geomintersects)
        QgsExpression.registerFunction(geomoverlaps)
        QgsExpression.registerFunction(geomtouches)
        QgsExpression.registerFunction(canvaswidth)
        QgsExpression.registerFunction(canvasheight)
        QgsExpression.registerFunction(canvasx)
        QgsExpression.registerFunction(canvasy)
        
        QgsExpression.registerFunction(intersecting_geom_count)
        QgsExpression.registerFunction(within_geom_count)
        QgsExpression.registerFunction(overlapping_geom_count)
        QgsExpression.registerFunction(intersecting_geom_sum)
        QgsExpression.registerFunction(within_geom_sum)
        QgsExpression.registerFunction(overlapping_geom_sum)
        
        QgsExpression.registerFunction(equaling_geom_count)
        
        icon_path = os.path.join(self.plugin_dir,"icon.png")
        # map tool action
        self.action = QAction(QIcon(icon_path),"refFunctions", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&refFunctions", self.action)


    def unload(self):
        QgsExpression.unregisterFunction('dbvalue')
        QgsExpression.unregisterFunction('dbvaluebyid')
        QgsExpression.unregisterFunction('dbquery')
        QgsExpression.unregisterFunction('dbsql')
        QgsExpression.unregisterFunction('WKTarea')
        QgsExpression.unregisterFunction('WKTcentroid')
        QgsExpression.unregisterFunction('WKTpointonsurface')
        QgsExpression.unregisterFunction('WKTlenght')
        QgsExpression.unregisterFunction('geomRedef')
        QgsExpression.unregisterFunction('geomnearest')
        QgsExpression.unregisterFunction('geomdistance')
        QgsExpression.unregisterFunction('geomwithin')
        QgsExpression.unregisterFunction('geomcontains')
        QgsExpression.unregisterFunction('geomcrosses')
        QgsExpression.unregisterFunction('geomdisjoint')
        QgsExpression.unregisterFunction('geomequals')
        QgsExpression.unregisterFunction('geomintersects')
        QgsExpression.unregisterFunction('geomoverlaps')
        QgsExpression.unregisterFunction('geomtouches')
        QgsExpression.unregisterFunction('canvaswidth')
        QgsExpression.unregisterFunction('canvasheight')
        QgsExpression.unregisterFunction('canvasx')
        QgsExpression.unregisterFunction('canvasy')
        
        QgsExpression.unregisterFunction('intersecting_geom_count')
        QgsExpression.unregisterFunction('within_geom_count')
        QgsExpression.unregisterFunction('overlapping_geom_count')
        QgsExpression.unregisterFunction('intersecting_geom_sum')
        QgsExpression.unregisterFunction('within_geom_sum')
        QgsExpression.unregisterFunction('overlapping_geom_sum')
        
        QgsExpression.unregisterFunction('equaling_geom_count')
        
        self.iface.removePluginMenu(u"&refFunctions", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        self.dlg.show()
