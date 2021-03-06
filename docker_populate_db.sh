#!/bin/bash
if [[ -f "docker_run.ini" ]] ; then
	. docker_run.ini
fi
if [[ -f "update_local_config_dir.sh" ]] ; then
	. update_local_config_dir.sh
fi
if [[ -f "config.ini" ]] ; then
	. config.ini
	if [[ -f "set_dirs.ini" ]] ; then
		. set_dirs.ini
	fi
fi
docker exec -it --user user qgis-xtopo /app/populate_db.sh