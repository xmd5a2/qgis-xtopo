#/!bin/bash
if [[ -f "prepare_automap_for_github.sh" ]] ; then
	. prepare_automap_for_github.sh
fi
vector_data_dir=$PWD

if [[ -f "config_debug.ini" ]] ; then
	qgis_projects_dir=~/qgis_projects
	rm -f $vector_data_dir/icons/*.*
	cp -f $qgis_projects_dir/icons/*.* $vector_data_dir/icons/
	rm -f $vector_data_dir/QGIS3/profiles/default/python/expressions/*.py
fi
expressions_dir=$vector_data_dir/expressions
mkdir -p $vector_data_dir/QGIS3/profiles/default/python/expressions/
cp -f $expressions_dir/*.py $vector_data_dir/QGIS3/profiles/default/python/expressions/
docker build -t $(basename $vector_data_dir) . #--no-cache --pull
docker rmi $(docker images -q -f dangling=true)