#!/bin/bash
if [[ -f "docker_prepare_data.ini" ]] ; then
	. docker_prepare_data.ini
fi
if [[ ! $(docker container ls | grep qgis-xtopo) ]] ; then
	echo -e "\033[91mDocker container is not running. Execute docker_run with parameters. Stopping.\033[0m" && exit 1;
fi
docker exec -it --user user qgis-xtopo /app/prepare_data.sh