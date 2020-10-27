#!/bin/bash
first_launch=
mkdir -p /mnt/qgis_projects/qgistopo-config/
if [[ -f /mnt/qgis_projects/qgistopo-config/config.ini ]] ; then
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
	if [[ -f /app/automap.qgs ]] ; then
		if [[ ! -f "$project_dir/$project_name.qgz" ]] ; then
			cd /app
			zip "$project_dir/$project_name.qgz" automap.qgs
			if [[ $? == 0 ]] ; then
				echo -e "\033[93mQGIS project '$project_name.qgz' is created. Check your qgis_projects/qgistopo-config/config.ini and execute docker_prepare_data script. If you want to use docker overpass instance for OSM data source then place OSM files to osm_data dir in your project folder and run docker_populate_db script.\033[0m"
			else
				echo -e "\033[91mError creating '$project_name.qgz'. Check directory permissions.\033[0m" && exit 1;
			fi
		fi
	fi
	rm -f "$project_dir/qgistopo_version.txt"
	sed -n 1p /app/README.md | grep -o '[^v]*$' > "$project_dir/qgistopo_version.txt"
elif [[ ! -f /mnt/qgis_projects/qgistopo-config/config.ini ]] ; then
	echo -e "\033[93mLooks like this is the first launch of the script. To use qgis-topo you need to modify qgis_projects/qgistopo-config/config.ini. After that execute docker_run script again.\nPath to the qgis_projects folder is set in docker_run file.\033[0m"
	first_launch=true
elif cmp -s /app/config.ini /mnt/qgis_projects/qgistopo-config/config.ini ; then
	echo -e "\033[93mconfig.ini was not modified. To use qgis-topo you need to modify qgis_projects/qgistopo-config/config.ini file for your needs.\033[0m"
	first_launch=true
fi

if [[ ! -f /mnt/qgis_projects/qgistopo-config/config.ini ]] ; then
	cp /app/config.ini /mnt/qgis_projects/qgistopo-config/config.ini
fi

mkdir -p /mnt/qgis_projects/qgistopo-config/QGIS3
mkdir -p /mnt/qgis_projects/icons

cp /app/icons/*.svg /mnt/qgis_projects/icons/

if [[ -d /mnt/qgis_projects/qgistopo-config/QGIS3 ]] ; then
	cp -r -n /app/QGIS3 /mnt/qgis_projects/qgistopo-config
fi

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
	ln -s /mnt/terrain/ /home/user/terrain
	rm -f /home/user/terrain/terrain
fi

if [[ $first_launch != "true" ]] ; then
	echo -e "\e[42mInitialization finished\e[49m"
fi