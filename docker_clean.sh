#!/bin/bash
docker stop qgis-topo
docker rmi xmd5a2/qgis-topo || docker rmi qgis-topo