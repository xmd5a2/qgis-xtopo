#!/bin/bash
mkdir -p /mnt/qgis_projects/qgistopo-config/
sed -i "s/automap/$PROJECT_NAME_EXT/g" /app/config.ini
if [[ ! -f /mnt/qgis_projects/qgistopo-config/config.ini ]] ; then
	cp /app/config.ini /mnt/qgis_projects/qgistopo-config/config.ini
fi
. /mnt/qgis_projects/qgistopo-config/config.ini
if [[ -f /mnt/qgis_projects/qgistopo-config/config_debug.ini ]] ; then
	. /mnt/qgis_projects/qgistopo-config/config_debug.ini
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

mkdir -p /mnt/qgis_projects/qgistopo-config/QGIS3
if [[ -d /mnt/qgis_projects/qgistopo-config/QGIS3 ]] ; then
	cp -r -n /app/QGIS3 /mnt/qgis_projects/qgistopo-config
fi
mkdir -p /mnt/qgis_projects/icons
cp /app/icons/*.svg /mnt/qgis_projects/icons/

qgis_config_path=/home/user/.local/share/QGIS
mkdir -p $qgis_config_path
if [[ ! -L $qgis_config_path/QGIS3 ]] || [[ ! -e $qgis_config_path/QGIS3/profiles/ ]] ; then
	ln -s /mnt/qgis_projects/qgistopo-config/QGIS3 $qgis_config_path/QGIS3
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
	cd /app
	zip "$project_dir/$project_name.qgz" automap.qgs
	if [[ $? == 0 ]] ; then
		echo -e "\e[42mInitialization finished\e[49m"
	else
		echo -e "\033[91mError creating '$project_name.qgz'. Check directory permissions.\033[0m" && exit 1;
	fi
else
	echo -e "\033[93mQGIS project '"$project_dir/$project_name.qgz"' already exists.\033[0m" && exit 1;
fi