#!/bin/bash
qgistopo_config_dir=/mnt/qgis_projects/qgistopo-config
if [[ -f ${qgistopo_config_dir}/config.ini ]] ; then
	. ${qgistopo_config_dir}/config.ini
else
	echo -e "\033[91mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m" && exit 1;
fi
qgis --project /mnt/qgis_projects/$project_name/$project_name.qgz