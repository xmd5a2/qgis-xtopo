#!/bin/bash
qgis_projects_dir=
terrain_src_dir=
if [[ -f "config_debug.ini" ]] ; then
	docker_image="qgis-topo"
else
	docker_image="xmd5a2/qgis-topo:latest"
fi

if [[ -f "docker_run.ini" ]] ; then
	. docker_run.ini
fi

if [[ ! -z $qgis_projects_dir ]] ; then
	mkdir -p $qgis_projects_dir
else
	echo -e "\033[91mqgis_projects_dir is not defined. Stopping.\033[0m" && exit 1;
fi
if [[ -d $terrain_src_dir ]] ; then
	terrain_mount_str="--mount type=bind,source=$terrain_src_dir,target=/mnt/terrain"
fi

echo -e "\e[100mqgis_projects_dir=$qgis_projects_dir\e[49m"
echo -e "\e[100mterrain_src_dir=$terrain_src_dir\e[49m"
echo -e "\e[100moverpass_db_dir=$qgis_projects_dir/overpass_db\e[49m"
echo -e "\e[100mqgistopo_config_dir=$qgis_projects_dir/qgistopo-config\e[49m"

if [[ $(docker container ls | grep qgis-topo) ]] ; then
	docker stop qgis-topo
fi
docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
	--mount type=bind,source=$qgis_projects_dir,target=/mnt/qgis_projects \
	$terrain_mount_str \
	$docker_image
docker exec -it --user user qgis-topo /app/init_docker.sh
