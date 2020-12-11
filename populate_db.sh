#!/bin/bash
# Populate Overpass DB from local sources in osm_data_dir
config_dir=/mnt/qgis_projects/qgisxtopo-config
if [[ ! -f /.dockerenv ]] ; then
	echo -e "\033[91mThis script is not meant to run outside the docker container. Stopping.\033[0m" && exit 1;
fi
if [[ -f $config_dir/config.ini ]] ; then
	. $config_dir/config.ini
	if [[ -f $config_dir/set_dirs.ini ]] ; then
		. $config_dir/set_dirs.ini
	fi
else
	echo -e "\033[91mconfig.ini not found. Check project installation integrity. Stopping.\033[0m" && exit 1;
fi
if [[ -f $config_dir/config_debug.ini ]] ; then
	. $config_dir/config_debug.ini
fi

overpass_db_dir=$qgis_projects_dir/overpass_db
osm_tmp_dir=$osm_data_dir/tmp
if [[ ! -d $osm_data_dir ]] ; then
	echo -e "\033[91mosm_data_dir in project_dir does not exist. Stopping.\033[0m" && exit 1;
fi
if [[ ! -d $osm_tmp_dir ]] ; then
	mkdir $osm_tmp_dir
else
    rm -f $osm_tmp_dir/*.*
fi
if [[ ! -d /mnt/qgis_projects/overpass_db ]] ; then
	mkdir -p /mnt/qgis_projects/overpass_db
fi

function determine_input_file_list {
	osm_data_is_present=""
	shopt -s nullglob
	cd $osm_data_dir
	for f in $osm_data_dir/*.osm; do
		[ -e "$f" ] && osm_data_is_present="true"; osm_str="$osm_data_dir/*.osm"
		break
	done
	for f in $osm_data_dir/*.o5m; do
		[ -e "$f" ] && osm_data_is_present="true"; o5m_str="$osm_data_dir/*.o5m"
		break
	done
	for f in $osm_data_dir/*.osm.bz2; do
		[ -e "$f" ] && osm_data_is_present="true"; osmbz2_str="$osm_data_dir/*.osm.bz2"
		break
	done
	for f in $osm_data_dir/*.pbf; do
		[ -e "$f" ] && osm_data_is_present="true"; pbf_str="$osm_data_dir/*.pbf"
	done
	shopt -u nullglob
}

function merge_populate {
	determine_input_file_list
	if [[ $osm_data_is_present == "true" ]] ; then
		echo -e "\e[104mMerging and converting OSM files in osm_data_dir\e[49m"
		if [[ $overpass_endpoint_docker_use_bbox == true ]] ; then
			osmium cat $pbf_str $osm_str $osmbz2_str $o5m_str -o $osm_tmp_dir/input_tmp.pbf -f pbf
			echo -e "\e[104mSorting extract\033[0m"
			osmium sort $osm_tmp_dir/input_tmp.pbf -o $osm_tmp_dir/input.pbf -f pbf
			rm -f $osm_tmp_dir/input_tmp.pbf
			cd /app
			. /app/get_bbox.sh
			cd $osm_tmp_dir
			echo -e "\e[104mCropping extract by bbox\033[0m"
			osmconvert -b=$bbox --complex-ways --complete-ways $osm_tmp_dir/input.pbf --out-osm | lbzip2 > $osm_tmp_dir/input.osm.bz2
			if [[ $(wc -c <"$osm_tmp_dir/input.osm.bz2") -le 400 ]] ; then
				echo -e "\033[91mError. Cropped extract is empty. Check that bbox parameter matches the OSM data area or turn off 'overpass_endpoint_docker_use_bbox' option.\033[0m" && exit 1;
			fi
		else
			osmium cat $pbf_str $osm_str $osmbz2_str $o5m_str -o $osm_tmp_dir/input_tmp.pbf -f pbf
			echo -e "\e[104mSorting extract\033[0m"
			osmium sort $osm_tmp_dir/input_tmp.pbf -o $osm_tmp_dir/input.osm.bz2 -f osm.bz2
			rm -f $osm_tmp_dir/input_tmp.pbf
		fi
		echo -e "\e[104mPopulating docker Overpass database\033[0m"
		bash /app/osm-3s/bin/init_osm3s.sh $osm_tmp_dir/input.osm.bz2 /mnt/qgis_projects/overpass_db /app/osm-3s
		if [[ -f /mnt/qgis_projects/overpass_db/nodes.map ]] ; then
			echo -e "\e[42mOverpass database is ready\e[49m"
			rm -f $osm_data_dir/tmp/input.*
			rmdir $osm_data_dir/tmp --ignore-fail-on-non-empty
			rm -f $osm_data_dir/*.pbf
			rm -f $osm_data_dir/*.o5m
			rm -f $osm_data_dir/*.osm
			rm -f $osm_data_dir/*.osm.bz2
		else
			echo -e "\033[91mError populating Overpass database\033[0m" && exit 1;
		fi
	else
		echo -e "\033[91mError. No OSM data files found in $osm_data_dir.\033[0m" && exit 1;
	fi
}

function check_merge_populate {
	determine_input_file_list
	if [[ ! $osm_data_is_present ]] ; then
		echo -e "\033[93mPlease download OSM extract from https://protomaps.com/extracts or http://download.geofabrik.de and place it into $osm_data_dir\033[0m"
		read -rsp $'\033[93mPress any key to continue...\n\033[0m' -n1 key
		merge_populate
	else
		merge_populate
	fi
}

if [[ -f $overpass_db_dir/nodes.map ]] ; then
	if [[ $overpass_endpoint_docker_clean_db == "true" ]] ; then
		rm -f $overpass_db_dir/*.*
		check_merge_populate
	else
		echo -e "\033[93mNothing to do. Using the current database.\033[0m"
	fi
else
	echo -e "\033[93mYou choose to use overpass_instance=docker but Overpass database is empty\033[0m"
	check_merge_populate
fi