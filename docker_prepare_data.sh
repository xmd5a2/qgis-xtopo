#!/bin/bash
if [[ -f "docker_prepare_data.ini" ]] ; then
	. docker_prepare_data.ini
fi
if [[ ! $(docker container ls | grep qgis-topo) ]] ; then
	. docker_run.sh
fi
docker exec -it --user user qgis-topo /app/prepare_data.sh