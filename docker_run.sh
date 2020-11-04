#!/bin/bash
while getopts ":n:d:b:t:e:" opt; do
  case $opt in
    n) PROJECT_NAME_EXT="$OPTARG"
    ;;
    d) qgis_projects_dir="$OPTARG"
    ;;
    b) BBOX_STR="$OPTARG"
    ;;
    t) terrain_src_dir="$OPTARG"
    ;;
    e) OVERPASS_INSTANCE_EXTERNAL=true
    ;;
    \?) echo "Invalid option -$OPTARG" >&2
    ;;
  esac
done
usage_str="Usage: ./docker_run.sh -n project_name -d /path/to/projects/dir [ -b lon_min,lat_min,lon_max,lat_max -t /path/to/terrain/dir ]"
if [[ -f "docker_run.ini" ]] ; then
	. docker_run.ini
fi
if [[ -z $PROJECT_NAME_EXT ]] ; then
	echo -e "\033[91mProject name is not specified. $usage_str\033[0m" && exit 1;
fi
if [[ -z $qgis_projects_dir ]] ; then
	echo -e "\033[91mQGIS projects dir is not specified. $usage_str\033[0m" && exit 1;
fi
if [[ ! -z $terrain_src_dir ]] && [[ ! -d $terrain_src_dir ]] ; then
	echo -e "\033[91mterrain_src_dir doesn't exist. $usage_str\033[0m" && exit 1;
fi

function echo_bbox_invalid {
	echo -e "\033[91mInvalid bbox format. Use left,bottom,right,top (lon_min,lat_min,lon_max,lat_max).\033[0m" && exit 1;
}
if [[ ! -z $BBOX_STR ]] ; then
	if [[ $BBOX_STR == *","* ]] ; then
		IFS=',' read -r -a array_bbox <<< "$BBOX_STR"
		lon_min=${array_bbox[0]}
		lat_min=${array_bbox[1]}
		lon_max=${array_bbox[2]}
		lat_max=${array_bbox[3]}
		if  [[ -z $lon_min ]] || [[ -z $lat_min ]] || [[ -z $lon_max ]] || [[ -z $lat_max ]] ; then echo_bbox_invalid; fi

		if (( $(echo "$lon_min > $lon_max" | bc -l) )) || (( $(echo "$lat_min > $lat_max" | bc -l) )) || \
			(( $(echo "$lat_min > 90" | bc -l) )) || (( $(echo "$lat_min < -90" | bc -l) )) ; then
			echo_bbox_invalid
		fi
	else
		echo_bbox_invalid
	fi
fi
if [[ -f "config_debug.ini" ]] ; then
	docker_image="qgis-topo"
else
	docker_image="xmd5a2/qgis-topo:latest"
fi

if [[ ! -z $qgis_projects_dir ]] && [[ ! -d $qgis_projects_dir ]] ; then
	mkdir -p $qgis_projects_dir
fi
if [[ -d $terrain_src_dir ]] ; then
	terrain_mount_str="--mount type=bind,source=$terrain_src_dir,target=/mnt/terrain"
fi
if [[ ! -z $qgis_projects_dir ]] ; then
	echo -e "\e[100mqgis_projects_dir=$qgis_projects_dir\e[49m"
	echo -e "\e[100mproject_name=$PROJECT_NAME_EXT\e[49m"
	echo -e "\e[100mbbox=$BBOX_STR\e[49m"
	echo -e "\e[100mterrain_src_dir=$terrain_src_dir\e[49m"
	echo -e "\e[100moverpass_db_dir=$qgis_projects_dir/overpass_db\e[49m"
	echo -e "\e[100mqgistopo_config_dir=$qgis_projects_dir/qgistopo-config\e[49m"
	if [[ ! -z $OVERPASS_INSTANCE_EXTERNAL ]] ; then
		echo -e "\e[100moverpass_instance_external=true\e[49m"
	fi	
fi

if [[ $(docker container ls | grep qgis-topo) ]] ; then
	docker stop qgis-topo
fi
if [[ -d "$qgis_projects_dir" ]] ; then
	docker run -dti --rm -e PROJECT_NAME_EXT=$PROJECT_NAME_EXT -e BBOX_STR=$BBOX_STR -e OVERPASS_INSTANCE_EXTERNAL=$OVERPASS_INSTANCE_EXTERNAL -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
		--mount type=bind,source=$qgis_projects_dir,target=/mnt/qgis_projects \
		$terrain_mount_str \
		$docker_image
fi
if [[ $(docker container ls | grep qgis-topo) ]] ; then
	docker exec -it --user user qgis-topo /app/init_docker.sh
fi