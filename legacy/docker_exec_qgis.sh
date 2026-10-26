#!/bin/bash
xhost +local:docker
docker exec -it --user user qgis-xtopo /app/exec_qgis.sh