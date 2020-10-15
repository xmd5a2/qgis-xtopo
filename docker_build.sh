#/!bin/bash
if [ -f "prepare_automap_for_github.sh" ] ; then
	. prepare_automap_for_github.sh
fi
work_dir=$PWD

if [ -f "config_debug.ini" ] ; then
	qgis_projects_dir=~/qgis_projects
	expressions_dir=$work_dir/expressions
	rm $work_dir/icons/*.*
	cp -f $qgis_projects_dir/icons/*.* $work_dir/icons/
	rm $work_dir/QGIS3/profiles/default/python/expressions/*.py
	cp -f $expressions_dir/*.py $work_dir/QGIS3/profiles/default/python/expressions/
fi
docker build -t $(basename $work_dir) . #--no-cache --pull
docker rmi $(docker images -q -f dangling=true)