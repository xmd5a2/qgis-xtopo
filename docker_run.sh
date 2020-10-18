#!/bin/bash
qgis_projects_dir=
terrain_dir=
overpass_db_dir=
qgistopo_config_dir=

if [ -f "docker_run.ini" ] ; then
	. docker_run.ini
fi

if [ ! -z $qgis_projects_dir ] ; then
	mkdir -p $qgis_projects_dir
else
	echo -e "\033[93mqgis_projects_dir is not defined. Stopping.\033[0m"
	exit 1
fi
if [ ! -z $overpass_db_dir ] ; then
	mkdir -p $overpass_db_dir
else
	echo -e "\033[93moverpass_db_dir is not defined. Stopping.\033[0m"
	exit 1
fi
if [ ! -z $qgistopo_config_dir ] ; then
	mkdir -p $qgistopo_config_dir
else
	echo -e "\033[93mqgistopo_config_dir is not defined. Stopping.\033[0m"
	exit 1
fi
if [[ -d $terrain_dir ]] ; then
	terrain_mount_str="--mount type=bind,source=$terrain_dir,target=/mnt/terrain"
fi

if [[ $(docker container ls | grep qgis-topo) ]] ; then
	docker stop qgis-topo
fi
#docker container rm qgis-topo
docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
	--mount type=bind,source=$qgis_projects_dir,target=/mnt/qgis_projects \
	$terrain_mount_str \
	--mount type=bind,source=$qgistopo_config_dir,target=/mnt/qgistopo-config \
	--mount type=bind,source=$overpass_db_dir,target=/mnt/overpass_db \
	qgis-topo:latest
docker exec -it --user user qgis-topo /app/init_docker.sh
