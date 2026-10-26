#!/bin/bash
docker stop qgis-xtopo
docker rmi xmd5a2/qgis-xtopo || docker rmi qgis-xtopo