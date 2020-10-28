#!/bin/bash
qgis_projects_dir=
terrain_src_dir=
if [[ ! -z "$1" ]] ; then
	qgis_projects_dir=$1
fi
if [[ ! -z "$2" ]] ; then
	terrain_src_dir=$2
fi
if [[ -f "config_debug.ini" ]] ; then
	docker_image="qgis-topo"
else
	docker_image="xmd5a2/qgis-topo:latest"
fi
if [[ -f "docker_run.ini" ]] ; then
	. docker_run.ini
fi
if [[ ! -z $qgis_projects_dir ]] && [[ ! -d $qgis_projects_dir ]] ; then
	mkdir -p $qgis_projects_dir
fi
if [[ -d $terrain_src_dir ]] ; then
	terrain_mount_str="--mount type=bind,source=$terrain_src_dir,target=/mnt/terrain"
fi

if [[ ! -z $qgis_projects_dir ]] ; then
	echo -e "\e[100mqgis_projects_dir=$qgis_projects_dir\e[49m"
	echo -e "\e[100mterrain_src_dir=$terrain_src_dir\e[49m"
	echo -e "\e[100moverpass_db_dir=$qgis_projects_dir/overpass_db\e[49m"
	echo -e "\e[100mqgistopo_config_dir=$qgis_projects_dir/qgistopo-config\e[49m"
fi
if [[ -d "$qgis_projects_dir" ]] ; then
	if [[ $(docker container ls | grep qgis-topo) ]] ; then
		docker stop qgis-topo
	fi
	docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
		--mount type=bind,source=$qgis_projects_dir,target=/mnt/qgis_projects \
		$terrain_mount_str \
		$docker_image
fi
if [[ $(docker container ls | grep qgis-topo) ]] ; then
	docker exec -it --user user qgis-topo /app/init_docker.sh
fi