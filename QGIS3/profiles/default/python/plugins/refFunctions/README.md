#refFunctions v1.0#
the QGIS plugin provide a 'Reference' group under field calculator with function for analytical or spatial reference to featurse in other layers.  
Uninstalling plugin removes funtions from field calculator
##Table functions:
**dbvalue(targetLayer,targetField,keyField,conditionValue)**  
Retrieve first targetField value from targetLayer when keyField is equal to conditionValue  
**dbvaluebyid('targetLayer','targetField',featureID)**  
Retrieve the targetField value from targetLayer using internal feature ID  
**dbquery(targetLayer,targetField,whereClause)**  
Retrieve first targetField value from targetLayer when whereClause is true  
**dbsql(connectionName,sqlQuery)**  
Retrieve results from SQL query  
##WKT functions:
**WKTcentroid('WKTgeometry')**  
Return the center of mass of the given geometry as WKT point geometry  
**WKTpointonsurface('WKTgeometry')**  
Return the point within  the given geometry  
**WKTlenght('WKTgeometry')**  
Return the length of the given WKT geometry  
**WKTarea('WKTgeometry')**  
Return the area of the given WKT geometry  
##Geometry functions:  
**geomRedef('WKTgeometry')**  
redefine the current feature geometry with a new WKT geometry (experimental!)  
**geomnearest(targetLayer,targetField)**  
Retrieve target field value from the nearest target feature in target layer  
**geomdistance('targetLayer','targetField',distanceCheck)**  
Retrieve target field value from target feature in target layer if target feature is in distance  
**geomwithin(targetLayer,targetField)**  
Retrieve target field value when source feature is within target feature in target layer  
**geomtouches(targetLayer,targetField)**  
Retrieve target field value when source feature touches target feature in target layer  
**geomintersects(targetLayer,targetField)**  
Retrieve target field value when source feature intersects target feature in target layer  
**geomcontains(targetLayer,targetField)**  
Retrieve target field value when source feature contains target feature in target layer  
**geomcontains(targetLayer,targetField)**  
Retrieve target field value when source feature is disjoint from target feature in target layer  
**geomequals(targetLayer,targetField)**  
Retrieve target field value when source feature is equal to target feature in target layer  
**geomtouches(targetLayer,targetField)**  
Retrieve target field value when source feature touches target feature in target layer  
**geomoverlaps(targetLayer,targetField)**  
Retrieve target field value when source feature overlaps target feature in target layer  
**geomcrosses(targetLayer,targetField)**  
Retrieve target field value when source feature crosses target feature in target layer  