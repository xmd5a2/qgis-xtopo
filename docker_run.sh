#!/bin/bash
while getopts ":n:d:b:t:o:sxgv:" opt; do
  case $opt in
    n) PROJECT_NAME_EXT="$OPTARG"
    ;;
    d) qgis_projects_dir="$OPTARG"
    ;;
    b) BBOX_STR="$OPTARG"
    ;;
    t) terrain_src_dir="$OPTARG"
    ;;
    s) DOWNLOAD_TERRAIN_DATA=true
    ;;
    o) if [[ "$OPTARG" == "external" ]] || [[ "$OPTARG" == "ext" ]] ; then
	 OVERPASS_INSTANCE=external
       elif [[ "$OPTARG" == "docker" ]] ; then
	 OVERPASS_INSTANCE=docker
       else
	 echo -e "\033[91mInvalid -o value. Use [ external (ext) | docker ]\033[0m"
       fi
    ;;
    x) RUN_CHAIN=true
    ;;
    g) generate_terrain="$OPTARG"
    ;;
    v) OVERPASS_ENDPOINT_EXTERNAL="$OPTARG"
    ;;
    \?) echo -e "\033[91mInvalid option -$OPTARG\033[0m" >&2
    ;;
  esac
done
usage_str="Usage: ./docker_run.sh -n project_name -d /path/to/projects/dir [ -b lon_min,lat_min,lon_max,lat_max ] [ -o [external | docker] ] [ -s ] [ -t /path/to/terrain/dir ] { -o : use external / docker Overpass server, -s : automatically download SRTM30m terrain data, -x : execute sequential data preparation steps }"

lang_local_str=$(locale | grep LANG=)
lang=${lang_local_str#LANG=}
if [[ ! -z $lang ]] ; then
	lang_str="-e LANG=$lang"
fi

if [[ -f "docker_run.ini" ]] ; then
	. docker_run.ini
fi
if [[ -z $PROJECT_NAME_EXT ]] ; then
	echo -e "\033[93mProject name is not specified. Use 'automap' by default.\033[0m";
	PROJECT_NAME_EXT=automap
fi
if [[ -z $qgis_projects_dir ]] ; then
	echo -e "\033[91mQGIS projects dir is not specified. $usage_str\033[0m" && exit 1;
fi
if [[ ! -z $terrain_src_dir ]] && [[ ! -d $terrain_src_dir ]] ; then
	echo -e "\033[91mterrain_src_dir doesn't exist. $usage_str\033[0m" && exit 1;
fi

function echo_bbox_invalid {
	echo -e "\033[91mInvalid bbox format. Use OpenStreetMap link or left,bottom,right,top (lon_min,lat_min,lon_max,lat_max).\033[0m" && exit 1;
}

function overpass_endpoint_invalid {
	echo -e "\033[91mInvalid Overpass endpoint format\033[0m" && exit 1;
}
if [[ ! -z $BBOX_STR ]] ; then
	if [[ $BBOX_STR == *","* ]] && [[ $BBOX_STR != *"openstreetmap"* ]] ; then
		IFS=',' read -r -a array_bbox <<< "$BBOX_STR"
		lon_min=${array_bbox[0]}
		lat_min=${array_bbox[1]}
		lon_max=${array_bbox[2]}
		lat_max=${array_bbox[3]}
		if [[ ${#array_bbox[@]} -ne 4 ]] || [[ -z $lon_min ]] || [[ -z $lat_min ]] || [[ -z $lon_max ]] || [[ -z $lat_max ]] ; then echo_bbox_invalid; fi

		if (( $(echo "$lon_min > $lon_max" | bc -l) )) || (( $(echo "$lat_min > $lat_max" | bc -l) )) || \
			(( $(echo "$lat_max > 90" | bc -l) )) || (( $(echo "$lat_min < -90" | bc -l) )) || \
			(( $(echo "$lon_min < -179.99" | bc -l) )) || (( $(echo "$lon_max > 179.99" | bc -l) )) ; then
			echo_bbox_invalid
		fi
	elif [[ $BBOX_STR == *"openstreetmap"* ]] ; then
		BBOX_STR=\"$BBOX_STR\"
	else
		echo_bbox_invalid
	fi
fi
if [[ $OVERPASS_ENDPOINT_EXTERNAL == *"interpreter"* ]] ; then
	OVERPASS_ENDPOINT_EXTERNAL=\"$OVERPASS_ENDPOINT_EXTERNAL\"
elif [[ ! -z $OVERPASS_ENDPOINT_EXTERNAL ]] ; then
		overpass_endpoint_invalid
	fi
fi

if [[ -f "config_debug.ini" ]] ; then
	docker_image="qgis-xtopo"
else
	docker_image="xmd5a2/qgis-xtopo:latest"
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
	echo -e "\e[100mqgisxtopo_config_dir=$qgis_projects_dir/qgisxtopo-config\e[49m"
	if [[ ! -z $OVERPASS_INSTANCE ]] ; then
		echo -e "\e[100moverpass_instance=$OVERPASS_INSTANCE\e[49m"
	fi
	if [[ ! -z $DOWNLOAD_TERRAIN_DATA ]] ; then
		echo -e "\e[100mdownload_terrain_data=true\e[49m"
	fi
fi

if [[ $(docker container ls | grep qgis-xtopo) ]] ; then
	docker stop qgis-xtopo
fi
if [[ -d "$qgis_projects_dir" ]] ; then
	docker run -dti --rm -e PROJECT_NAME_EXT=$PROJECT_NAME_EXT -e BBOX_STR=$BBOX_STR -e OVERPASS_INSTANCE=$OVERPASS_INSTANCE \
		-e GENERATE_TERRAIN=$generate_terrain -e DOWNLOAD_TERRAIN_DATA=$DOWNLOAD_TERRAIN_DATA -e RUN_CHAIN=$RUN_CHAIN \
		-e OVERPASS_ENDPOINT_EXTERNAL=$OVERPASS_ENDPOINT_EXTERNAL \
		-v /tmp/.X11-unix:/tmp/.X11-unix $lang_str -e DISPLAY \
		--name qgis-xtopo \
		--mount type=bind,source=$qgis_projects_dir,target=/mnt/qgis_projects \
		$terrain_mount_str \
		$docker_image
fi

if [[ $(docker container ls | grep qgis-xtopo) ]] ; then
	docker exec -it --user user qgis-xtopo /app/init_docker.sh
fi
if [[ $RUN_CHAIN == true ]] ; then
	if [[ ! -f $config_dir/err_prepare_data.flag ]] && [[ ! -f $config_dir/err_populate_db.flag ]] ; then
		. docker_exec_qgis.sh
	fi
fi