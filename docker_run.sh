#!/bin/bash
qgis_projects_dir=
terrain_dir=
overpass_db_dir=
qgistopo-config=

if [ -f "docker_run.ini" ] ; then
	. docker_run.ini
fi

mkdir -p $qgis_projects_dir
mkdir -p $overpass_db_dir
mkdir -p $qgistopo-config
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
	--mount type=bind,source=$qgistopo-config,target=/mnt/external_scripts \
	--mount type=bind,source=$overpass_db_dir,target=/mnt/overpass_db \
	qgis-topo:latest
docker exec -it --user user qgis-topo /app/init_docker.sh