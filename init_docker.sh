#!/bin/bash
if [[ -f /mnt/external_scripts/config.ini ]] ; then
	. /mnt/external_scripts/config.ini
	if [[ -f /mnt/external_scripts/config_debug.ini ]] ; then
		. /mnt/external_scripts/config_debug.ini
		if [[ ! -d "/mnt/qgis_projects/$override_dir" ]] ; then
			mkdir "/mnt/qgis_projects/$override_dir"
		fi
	fi
	if [[ ! -d "$project_dir" ]] ; then
		mkdir "$project_dir"
	fi
	if [[ ! -d "$work_dir" ]] ; then
		mkdir "$work_dir"
	fi
	if [[ ! -d "$dem_dir" ]] ; then
		mkdir "$dem_dir"
	fi
	if [[ ! -d "$osm_data_dir" ]] ; then
		mkdir "$osm_data_dir"
	fi
	if [[ ! -d "$temp_dir" ]] ; then
		mkdir "$temp_dir"
	fi

	if [[ -f /app/automap.qgs ]] ; then
		if [[ ! -f "$project_dir/$project_name.qgz" ]] ; then
			cd /app
			zip "$project_dir/$project_name.qgz" automap.qgs
		fi
	fi
	rm -f "$project_dir/qgistopo_version.txt"
	sed -n 1p /app/README.md | grep -o '[^v]*$' > "$project_dir/qgistopo_version.txt"
fi

if [[ ! -f /mnt/external_scripts/config.ini ]] ; then
	echo -e "\033[93mThis is the first launch of the script. To use qgis-topo you need to modify config.ini for your needs and execute docker_run script again.\nThe path to the config.ini file is set by the qgistopo_extdir variable in the docker_run file\033[0m"
elif cmp -s /app/config.ini /mnt/external_scripts/config.ini ; then
	echo -e "\033[93mconfig.ini was not modified. To use qgis-topo you need to modify config.ini for your needs. The path to the config.ini file is set by the qgistopo_extdir variable in the docker_run file\033[0m"
fi
files=(config.ini crop_template.geojson prepare_data.sh calc_srtm_tiles_list.py query_srtm_tiles_list.sh run_alg.py)
for f in ${files[@]}; do
	if [[ ! -f /mnt/external_scripts/$f ]] ; then
		cp /app/$f /mnt/external_scripts/$f
	fi
done

if [[ ! -d /mnt/external_scripts/queries ]] ; then
	mkdir /mnt/external_scripts/queries
fi
if [[ ! -d /mnt/external_scripts/QGIS3 ]] ; then
	mkdir /mnt/external_scripts/QGIS3
fi
if [[ ! -d /mnt/qgis_projects/icons ]] ; then
	mkdir /mnt/qgis_projects/icons
fi

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

mkdir -p /home/user/.local/share/QGIS
ln -s /mnt/external_scripts/QGIS3 /home/user/.local/share/QGIS/QGIS3
ln -s /mnt/qgis_projects /home/user/qgis_projects
if [[ -d /mnt/terrain ]] ; then
	ln -s /mnt/terrain /home/user/terrain
fi

echo Initialization finished