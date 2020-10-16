#!/bin/bash
# Query SRTM tiles list based on bbox in config.ini
if [ -f /.dockerenv ] ; then
	qgistopo-config=/mnt/external_scripts
	if [[ -f ${qgistopo-config}/config.ini ]] ; then
		. ${qgistopo-config}/config.ini
	else
		echo -e "\033[93mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
		exit 1;
	fi
	if [[ -f ${qgistopo-config}/config_debug.ini ]] ; then
		. ${qgistopo-config}/config_debug.ini
	fi
	app_dir=/app
else
	qgistopo-config=$(pwd)
	if [[ -f ${qgistopo-config}/config.ini ]] ; then
		. ${qgistopo-config}/config.ini
	else
		echo -e "\033[93mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
		exit 1;
	fi
	if [[ -f ${qgistopo-config}/config_debug.ini ]] ; then
		. ${qgistopo-config}/config_debug.ini
	fi
	app_dir=$(pwd)
fi

IFS=',' read -r -a array_bbox <<< "$bbox"
lon_min=${array_bbox[0]}
lat_min=${array_bbox[1]}
lon_max=${array_bbox[2]}
lat_max=${array_bbox[3]}

if (( $(echo "$lon_min > $lon_max" | bc -l) )) || (( $(echo "$lat_min > $lat_max" | bc -l) )) || \
	(( $(echo "$lat_min > 90" | bc -l) )) || (( $(echo "$lat_min < -90" | bc -l) )) ; then
	echo -e "\033[93mInvalid bbox format. Use left,bottom,right,top (lon_min,lat_min,lon_max,lat_max)\033[0m"
	exit 1;
fi
bbox_query=$lat_min,$lon_min,$lat_max,$lon_max
echo -e "\e[100mbbox:" $bbox_query"\e[49m"

IFS=' ' read -r -a tiles_list <<< $(python3 $app_dir/calc_srtm_tiles_list.py -bbox "$bbox")
echo -e "\e[100mDEM tiles list: ${tiles_list[@]}\e[49m"