#!/bin/bash
# Populate Overpass DB from local sources in osm_data_dir
if [[ ! -f /.dockerenv ]] ; then
	echo -e "\033[91mThis script is not meant to run outside the docker container. Stopping.\033[0m" && exit 1;
fi
if [[ -f /mnt/qgis_projects/qgistopo-config/config.ini ]] ; then
	. /mnt/qgis_projects/qgistopo-config/config.ini
else
	echo -e "\033[91mconfig.ini not found. Check project installation integrity. Stopping.\033[0m" && exit 1;
fi
if [[ -f /mnt/qgis_projects/qgistopo-config/config_debug.ini ]] ; then
	. /mnt/qgis_projects/qgistopo-config/config_debug.ini
fi

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

osm_data_is_present=""
# Determine input file list
shopt -s nullglob
cd $osm_data_dir
for f in $osm_data_dir/*.osm; do
	[ -e "$f" ] && osm_data_is_present="true"; osm_str="$osm_data_dir/*.osm" # set_timestamp $f;
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
if [[ $osm_data_is_present == "true" ]]; then
	echo -e "\e[104mMerging and converting OSM files in osm_data_dir\e[49m"
	osmium cat $pbf_str $osm_str $osmbz2_str $o5m_str -o $osm_tmp_dir/input.osm.bz2 -f osm.bz2
	echo -e "\e[104mPopulating docker Overpass database\e[49m"
	bash /app/osm-3s/bin/init_osm3s.sh $osm_tmp_dir/input.osm.bz2 /mnt/qgis_projects/overpass_db /app/osm-3s
	if [[ -f /mnt/qgis_projects/overpass_db/nodes.map ]] ; then
		echo -e "\e[42mOverpass database is ready\e[49m"
		rm -f $osm_data_dir/tmp/input.osm.bz2
		rmdir $osm_data_dir/tmp --ignore-fail-on-non-empty
	else
		echo -e "\033[91mError populating Overpass database\033[0m"
	fi
else
	echo -e "\033[91mError. No osm.bz2 files found in $osm_tmp_dir\033[0m" && exit 1;
fi