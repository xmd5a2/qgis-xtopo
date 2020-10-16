#!/bin/bash
first_launch=
if [[ -f /mnt/external_scripts/config.ini ]] ; then
	. /mnt/external_scripts/config.ini
	if [[ -f /mnt/external_scripts/config_debug.ini ]] ; then
		. /mnt/external_scripts/config_debug.ini
		mkdir -p "/mnt/qgis_projects/$override_dir"
	fi
	mkdir -p "$project_dir"
	mkdir -p "$work_dir"
	mkdir -p "$dem_dir"
	mkdir -p "$osm_data_dir"
	mkdir -p "$temp_dir"

	if [[ -f /app/automap.qgs ]] ; then
		if [[ ! -f "$project_dir/$project_name.qgz" ]] ; then
			cd /app
			zip "$project_dir/$project_name.qgz" automap.qgs
			if [[ $? == 0 ]] ; then
				echo -e "\033[93mQGIS project '$project_name.qgz' is created. Check your config.ini and execute prepare_data script.\033[0m"
			else
				echo -e "\033[93mError creating '$project_name.qgz'. Check directory permissions.\033[0m"
				exit 1;
			fi
		fi
	fi
	rm -f "$project_dir/qgistopo_version.txt"
	sed -n 1p /app/README.md | grep -o '[^v]*$' > "$project_dir/qgistopo_version.txt"
elif [[ ! -f /mnt/external_scripts/config.ini ]] ; then
	echo -e "\033[93mLooks like this is the first launch of the script. To use qgis-topo you need to modify config.ini for your needs and execute docker_run script again.\nThe path to the config.ini file is set by the qgistopo_extdir variable in the docker_run file\033[0m"
	first_launch=true
elif cmp -s /app/config.ini /mnt/external_scripts/config.ini ; then
	echo -e "\033[93mconfig.ini was not modified. To use qgis-topo you need to modify config.ini for your needs. The path to the config.ini file is set by the qgistopo_extdir variable in the docker_run file\033[0m"
	first_launch=true
fi

files=(config.ini crop_template.geojson prepare_data.sh calc_srtm_tiles_list.py query_srtm_tiles_list.sh run_alg.py)
for f in ${files[@]}; do
	if [[ ! -f /mnt/external_scripts/$f ]] ; then
		cp /app/$f /mnt/external_scripts/$f
	fi
done

mkdir -p /mnt/external_scripts/queries
mkdir -p /mnt/external_scripts/QGIS3
mkdir -p /mnt/qgis_projects/icons

for q in /app/queries/*.txt; do
	if [[ ! -f /mnt/external_scripts/$(basename $q) ]] ; then
		cp /app/queries/$(basename $q) /mnt/external_scripts/queries/$(basename $q)
	fi
done
for i in /app/icons/*.svg; do
	if [[ ! -f /mnt/qgis_projects/icons/$(basename $i) ]] ; then
		cp /app/icons/$(basename $i) /mnt/qgis_projects/icons/$(basename $i)
	fi
done
if [[ -d /mnt/external_scripts/QGIS3 ]] ; then
	cp -r -n /app/QGIS3 /mnt/external_scripts
fi

qgis_config_path=/home/user/.local/share/QGIS
mkdir -p $qgis_config_path
if [[ ! -L $qgis_config_path/QGIS3 ]] || [[ ! -e $qgis_config_path/QGIS3/profiles/ ]] ; then
	ln -s /mnt/external_scripts/QGIS3 $qgis_config_path/QGIS3
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