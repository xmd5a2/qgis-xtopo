#!/bin/bash
config_dir=/mnt/qgis_projects/qgistopo-config
mkdir -p $config_dir/

if [[ ! -f $config_dir/config.ini ]] ; then
	cp /app/config.ini $config_dir/config.ini
fi
sed -i "s/project_name=.*/project_name=\"$PROJECT_NAME_EXT\"/" $config_dir/config.ini
if [[ ! -z $BBOX_STR ]] ; then
	if [[ $(echo $BBOX_STR | grep \/ ) ]] ; then
		BBOX_STR=${BBOX_STR//\//\\/}
	fi
	if [[ $(echo $BBOX_STR | grep \& ) ]] ; then
		BBOX_STR=${BBOX_STR//\&/\\&}
	fi
	sed -i "s/^bbox=.*/bbox=${BBOX_STR}/" $config_dir/config.ini
fi
if [[ $OVERPASS_INSTANCE == "external" ]] || [[ $OVERPASS_INSTANCE == "ext" ]] ; then
	sed -i "s/overpass_instance=.*/overpass_instance=external/" $config_dir/config.ini
elif [[ $OVERPASS_INSTANCE == "docker" ]] ; then
	sed -i "s/overpass_instance=.*/overpass_instance=docker/" $config_dir/config.ini
fi
if [[ ! -z $DOWNLOAD_TERRAIN_DATA ]] ; then
	sed -i "s/generate_terrain=.*/generate_terrain=true/" $config_dir/config.ini
	sed -i "s/download_terrain_tiles=.*/download_terrain_tiles=true/" $config_dir/config.ini
else
	sed -i "s/download_terrain_tiles=.*/download_terrain_tiles=false/" $config_dir/config.ini
fi
if [[ -d /mnt/terrain ]] ; then
	sed -i "s/generate_terrain=.*/generate_terrain=true/" $config_dir/config.ini
	sed -i "s/get_terrain_tiles=.*/get_terrain_tiles=true/" $config_dir/config.ini
fi
. $config_dir/config.ini
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
rm -f "$project_dir/qgistopo_version.txt"
sed -n 1p /app/README.md | grep -o '[^v]*$' > "$project_dir/qgistopo_version.txt"

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
	echo -e "\033[93mQGIS project '"$project_dir/$project_name.qgz"' already exists. Usually it's ok.\033[0m"
fi
if [[ $RUN_CHAIN == true ]] ; then
	if [[ $OVERPASS_INSTANCE == docker ]] ; then
		. /app/populate_db.sh
	fi
	. /app/prepare_data.sh
fi