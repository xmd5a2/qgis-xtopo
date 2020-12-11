#!/bin/bash
qgisxtopo_config_dir=/mnt/qgis_projects/qgisxtopo-config
if [[ -f ${qgisxtopo_config_dir}/config.ini ]] ; then
	. ${qgisxtopo_config_dir}/config.ini
	if [[ -f ${qgisxtopo_config_dir}/set_dirs.ini ]] ; then
		. ${qgisxtopo_config_dir}/set_dirs.ini
	fi
else
	echo -e "\033[91mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m" && exit 1;
fi
qgis --project /mnt/qgis_projects/$project_name/$project_name.qgz