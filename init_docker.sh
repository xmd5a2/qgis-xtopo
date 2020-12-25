#!/bin/bash
config_dir=/mnt/qgis_projects/qgisxtopo-config
mkdir -p $config_dir/

if [[ ! -f $config_dir/config.ini ]] ; then
	cp /app/config.ini $config_dir/config.ini
fi
if [[ ! -f $config_dir/set_dirs.ini ]] ; then
	cp /app/set_dirs.ini $config_dir/set_dirs.ini
fi

if [[ ! -z $config_dir ]] ; then
	string_list="config_dir==${config_dir}|"
fi
if [[ ! -z $PROJECT_NAME_EXT ]] ; then
	string_list+="project_name==\"${PROJECT_NAME_EXT}\"|"
fi
if [[ ! -z $BBOX_STR ]] ; then
	string_list+="bbox==\"${BBOX_STR}\"|"
fi
if [[ ! -z $OVERPASS_INSTANCE ]] ; then
	string_list+="overpass_instance==${OVERPASS_INSTANCE}|"
fi
if [[ ! -z $OVERPASS_ENDPOINT_EXTERNAL ]] ; then
	string_list+="overpass_endpoint_external==\"${OVERPASS_ENDPOINT_EXTERNAL}\"|"
fi
if [[ ! -z $GENERATE_TERRAIN ]] ; then
	string_list+="generate_terrain==${GENERATE_TERRAIN}|"
fi
if [[ ! -z $DOWNLOAD_TERRAIN_DATA ]] ; then
	string_list+="download_terrain_tiles==${DOWNLOAD_TERRAIN_DATA}|"
fi
if [[ ! -z $GENERATE_TERRAIN_ISOLINES ]] ; then
	string_list+="generate_terrain_isolines==${GENERATE_TERRAIN_ISOLINES}|"
fi
if [[ ! -z $SMOOTH_ISOLINES ]] ; then
	string_list+="smooth_isolines==${SMOOTH_ISOLINES}|"
fi
if [[ ! -z $ISOLINES_STEP ]] ; then
	string_list+="isolines_step==${ISOLINES_STEP}|"
fi
if [[ -d /mnt/terrain ]] ; then
	string_list+="get_terrain_tiles==true"
else
	string_list+="get_terrain_tiles==false"
fi
python3 /app/update_config.py -str_in "$string_list"

. $config_dir/config.ini
. $config_dir/set_dirs.ini
if [[ -f $config_dir/config_debug.ini ]] ; then
	. $config_dir/config_debug.ini
	mkdir -p "/mnt/qgis_projects/$override_dir"
fi
mkdir -p "$project_dir"
mkdir -p "$vector_data_dir"
mkdir -p "$raster_data_dir"
mkdir -p "$terrain_input_dir"
mkdir -p "$osm_data_dir"
mkdir -p "$temp_dir"
rm -f "$project_dir/qgisxtopo_version.txt"
sed -n 1p /app/README.md | grep -o '[^v]*$' > "$project_dir/qgisxtopo_version.txt"

mkdir -p $config_dir/QGIS3
if [[ -d $config_dir/QGIS3 ]] ; then
	cp -r -n /app/QGIS3 $config_dir
fi
mkdir -p /mnt/qgis_projects/icons
cp /app/icons/*.svg /mnt/qgis_projects/icons/

qgis_config_path=/home/user/.local/share/QGIS
mkdir -p $qgis_config_path
if [[ ! -L $qgis_config_path/QGIS3 ]] || [[ ! -e $qgis_config_path/QGIS3/profiles/ ]] ; then
	ln -s $config_dir/QGIS3 $qgis_config_path/QGIS3
fi
if [[ ! -L /home/user/qgis_projects ]] || [[ ! -e /home/user/qgis_projects ]] ; then
	ln -s /mnt/qgis_projects/ /home/user/qgis_projects
	rm -f /home/user/qgis_projects/qgis_projects
fi
if [[ ! -L /home/user/terrain ]] || [[ ! -e /home/user/terrain ]] ; then
	ln -s /mnt/terrain/ /home/user/terrain > /dev/null 2>&1
	rm -f /home/user/terrain/terrain
fi

if [[ ! -f "$project_dir/$project_name.qgz" ]] ; then
	qgis_config_path=$config_dir/QGIS3/profiles/default/QGIS/QGIS3.ini
	# Modify recent project path in QGIS ini
	sed -i "s/recentProjects\\\1\\\path=.*/recentProjects\\\1\\\path=\\/home\\/user\\/qgis_projects\\/${project_name}\\/${project_name}.qgz/" $qgis_config_path
	sed -i "s/recentProjects\\\1\\\title=.*/recentProjects\\\1\\\title=${project_name}/" $qgis_config_path
	sed -i "s/lastProjectDir=.*/lastProjectDir=\\/home\\/user\\/qgis_projects\\/${project_name}/" $qgis_config_path
	sed -i "s/lastSaveAsImageDir=.*/lastSaveAsImageDir=\\/home\\/user\\/qgis_projects\\/${project_name}\\/output/" $qgis_config_path
	sed -i "s/lastFileNameWidgetDir=.*/lastFileNameWidgetDir=\\/home\\/user\\/qgis_projects\\/${project_name}/" $qgis_config_path
	sed -i "s/lastVectorFileFilterDir=.*/lastVectorFileFilterDir=\\/home\\/user\\/qgis_projects\\/${project_name}/" $qgis_config_path
	sed -i "s/lastLayoutExportDir=.*/lastLayoutExportDir=\\/home\\/user\\/qgis_projects\\/${project_name}\\/output\\/Detailed.png/" $qgis_config_path
	cd /app
	cp automap.qgs $project_name.qgs
	zip "$project_dir/$project_name.qgz" $project_name.qgs
	rm -f $project_name.qgs
	if [[ $? == 0 ]] ; then
		echo -e "\e[42mInitialization finished\e[49m"
	else
		echo -e "\033[91mError creating '$project_name.qgz'. Check directory permissions.\033[0m" && exit 1;
	fi
else
	echo -e "\033[93mQGIS project '"$project_dir/$project_name.qgz"' already exists. OK.\033[0m"
fi
rm -f $config_dir/err_populate_db.flag
rm -f $config_dir/err_prepare_data.flag
if [[ $RUN_CHAIN == true ]] ; then
	if [[ $OVERPASS_INSTANCE == docker ]] ; then
		. /app/populate_db.sh
	fi
	if [[ ! -f $config_dir/err_populate_db.flag ]] ; then
		. /app/prepare_data.sh
	fi
fi