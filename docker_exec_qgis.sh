#!/bin/bash
xhost +local:docker
docker exec -it --user user qgis-topo /app/exec_qgis.sh