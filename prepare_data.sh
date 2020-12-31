#!/bin/bash
# Get and prepare OSM / terrain data for QGIS-topo project
# https://github.com/xmd5a2/qgis-xtopo
# Requirements: qgis >=3.16 with grass plugin, osmtogeojson, gdal, osmctools, osmium, jq, eio (pip elevation)
# Author: xmd5a (Leonid Barsukov)
#read -rsp $'Press any key to continue...\n' -n1 key

if [[ -f /.dockerenv ]] ; then
	scripts_dir=/app
	qgisxtopo_config_dir=/mnt/qgis_projects/qgisxtopo-config
	if [[ -f $qgisxtopo_config_dir/config.ini ]] ; then
		. $qgisxtopo_config_dir/config.ini
		if [[ -f $qgisxtopo_config_dir/set_dirs.ini ]] ; then
			. $qgisxtopo_config_dir/set_dirs.ini
		fi
		export XDG_RUNTIME_DIR=/mnt/qgis_projects/$project_name/tmp
	else
		echo -e "\033[91mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m" && exit 1;
	fi

	if [[ -f $qgisxtopo_config_dir/config_debug.ini ]] ; then
		. $qgisxtopo_config_dir/config_debug.ini
	fi
	rm -f /tmp/.X99-lock
	Xvfb :99 -ac -noreset &
	export DISPLAY=:99
else
	scripts_dir=$(pwd)
	qgisxtopo_config_dir=$(pwd)
	if [[ -f $qgisxtopo_config_dir/config.ini ]] ; then
		. $qgisxtopo_config_dir/config.ini
		if [[ -f $qgisxtopo_config_dir/set_dirs.ini ]] ; then
			. $qgisxtopo_config_dir/set_dirs.ini
		fi
	else
		echo -e "\031[93mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m" && exit 1;
	fi
	if [[ -f $qgisxtopo_config_dir/config_debug.ini ]] ; then
		. $qgisxtopo_config_dir/config_debug.ini
	fi
fi
err_flag_name=err_prepare_data.flag

function make_error_flag {
	rm -f $err_flag_name
	touch $qgisxtopo_config_dir/$err_flag_name
	if [[ $1 ]] ; then
		echo $1 > $qgisxtopo_config_dir/$err_flag_name
	fi
}
echo -e "\e[105mProject dir: $project_dir\e[49m"
if [[ $running_in_container == true ]] ; then
	echo -e "\e[100mRunning in docker\e[49m"
fi
echo -e "\e[100mconfig: $qgisxtopo_config_dir/config.ini\e[49m"
echo -e "\e[100mterrain dir: $terrain_src_dir\e[49m"

if [[ "$project_name" == "" ]] ; then
	echo -e "\033[91mproject_name not defined. Please define it in config.ini. Stopping.\033[0m"
	make_error_flag
	exit 1
fi
if [[ ! -d "$project_dir" ]] && [[ $running_in_container == true ]] ; then
	echo -e "\033[91mproject_dir $project_dir not found. Please check config.ini (project_name and project_dir variables) and directory itself. Also executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
	make_error_flag
	exit 1
fi
if [[ ! -d "$project_dir" ]] && [[ $running_in_container == false ]] ; then
	echo -e "\033[91mproject_dir $project_dir not found. Please check config.ini (project_name and project_dir variables) and directory itself. Stopping.\033[0m"
	make_error_flag
	exit 1;
fi
if [[ ! -f "$project_dir/$project_name.qgz" ]] ; then
	echo -e "\033[93m$project_dir/$project_name.qgz not found. Run docker_run to regenerate it.\033[0m"
fi
if [[ $get_terrain_tiles == "true" ]] && [[ $download_terrain_tiles == "true" ]] ; then
	echo -e "\033[91mget_terrain_tiles and download_terrain_tiles are incompatible with each other. Use only one of them. Check config.ini. Stopping.\033[0m"
	make_error_flag
	exit 1
fi
case $overpass_instance in
	"docker")
		if [[ $running_in_container == true ]] ; then
			req_path_string="/app/osm-3s/bin/osm3s_query --quiet --db-dir=/mnt/qgis_projects/overpass_db"
		else
			echo -e "\033[91moverpass_instance=docker can't be started outside of container. Please use overpass_instance=external/local/ssh. Stopping.\033[0m"
			make_error_flag
			exit 1
		fi
		;;
	"local")
		if [[ $running_in_container == false ]] ; then
			IFS=' ' read -r -a array_bbox <<< "$overpass_endpoint_local"
			if [[ -f "${array_bbox[0]}" ]] ; then
				req_path_string=$overpass_endpoint_local
			else
				echo -e "\033[91m${array_bbox[0]} not found. Check overpass_endpoint_local. Stopping.\033[0m"
				make_error_flag
				exit 1
			fi
		else
			echo -e "\033[91moverpass_instance=local can't be started inside a container. Please use overpass_instance=external/docker/ssh. Stopping.\033[0m"
			make_error_flag
			exit 1
		fi
		;;
	"ssh")
		if [[ $running_in_container == false ]] ; then
			req_path_string="$overpass_endpoint_ssh --quiet"
		else
			echo -e "\033[91moverpass_instance=ssh can't be started inside a container. Please use overpass_instance=docker/external/local. Stopping.\033[0m"
			make_error_flag
			exit 1
		fi
		;;
esac

override_dir=$qgis_projects_dir/$override_dir
merged_dem="$raster_data_dir/merged_dem.tif"

if [[ $running_in_container == false ]] ; then
	mkdir -p "$project_dir"
	mkdir -p "$vector_data_dir"
	mkdir -p "$override_dir"
	mkdir -p "$osm_data_dir"
	mkdir -p "$temp_dir"
else
	cd /app
fi
if [[ -d "$temp_dir" ]] ; then
	rm -f "$temp_dir"/*.*
fi
rm -f $vector_data_dir/*.sqlite_tmp
rm -f $vector_data_dir/*.sqlite_tmp-journal

. get_bbox.sh

bbox_query=$lat_min,$lon_min,$lat_max,$lon_max
bbox_eio_query="$lon_min $lat_min $lon_max $lat_max"

command -v python3 >/dev/null 2>&1 || { echo >&2 -e "\033[91mpython3 is required but not installed.\033[0m" && exit 1;}
command -v osmtogeojson >/dev/null 2>&1 || { echo >&2 -e "\033[91mosmtogeojson is required but not installed. Follow installation instructions at https://github.com/tyrasd/osmtogeojson\033[0m" && exit 1;}
command -v gdalwarp >/dev/null 2>&1 || { echo >&2 -e "\033[91mGDAL is required but not installed. If you are using Ubuntu please install 'gdal-bin' package.\033[0m" && exit 1;}
command -v grass >/dev/null 2>&1 || { echo >&2 -e "\033[91mGRASS > 7.0 is required but not installed.\033[0m" && exit 1;}
command -v osmfilter >/dev/null 2>&1 || { echo >&2 -e "\033[91mosmfilter is required but not installed. If you are using Ubuntu please install 'osmctools' package.\033[0m" && exit 1;}
command -v osmconvert >/dev/null 2>&1 || { echo >&2 -e "\033[91mosmconvert is required but not installed. If you are using Ubuntu please install 'osmctools' package.\033[0m" && exit 1;}
command -v osmium >/dev/null 2>&1 || { echo >&2 -e "\033[91mosmium is required but not installed. If you are using Ubuntu please install 'osmium-tool' package.\033[0m" && exit 1;}
command -v jq >/dev/null 2>&1 || { echo >&2 -e "\033[91mjq is required but not installed. If you are using Ubuntu please install 'jq' package.\033[0m" && exit 1;}
command -v eio >/dev/null 2>&1 || { echo >&2 -e "\033[91meio is required but not installed. Please install python 'elevation' pip (https://github.com/bopen/elevation).\033[0m" && exit 1;}

function run_alg_linestopolygons {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "qgis:linestopolygons" \
		-param1 INPUT -value1 "$temp_dir/$1.$ext$3" \
		-param2 OUTPUT -value2 "$temp_dir/${1}_polygons.$ext"
}
function osmtogeojson_wrapper {
	mem=$(echo $(($(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) / (1024 * 1024))))
	node_mem=$(bc<<<$mem*2/3)
	node --max_old_space_size=$node_mem `which osmtogeojson` $1 > $2
	if [[ $? != 0 ]] ; then
		echo $?
		echo -e "\033[91mosmtogeojson error. Try reducing bbox.\033[0m"
		make_error_flag 1
		exit 1
	fi
}
function convert2spatialite {
	if [[ -f $2 ]] ; then
		rm $2
	fi
	if [[ $3 ]] ; then
		layername_string="-nln $3"
	else layername_string=""
	fi
	OGR_GEOJSON_MAX_OBJ_SIZE=1000MB ogr2ogr -dsco SPATIALITE=YES -lco COMPRESS_GEOM=YES -f SQLite $layername_string $2 $1
}
# Convert JSON lines to JSON
function jsonlines2json {
	jq -s '.' $1.geojson > ${1}_tmp.geojson
	mv -f ${1}_tmp.geojson $1.geojson
	sed -i '1s/\[/{\"type\": \"FeatureCollection\",\"features\":[/' $1.geojson
	sed -i '$ s/\]/\]}/' $1.geojson
}

echo -e "\e[100mbbox:" $bbox_query"\e[49m"

rm -f $project_dir/crop.geojson
echo '{ "type": "FeatureCollection","name": "crop","crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },
"features": [ { "type": "Feature", "properties": { "properties": null }, "geometry": { "type": "MultiPolygon", "coordinates": [ [ [ [ {lon_min}, {lat_max} ], [ {lon_max}, {lat_max} ], [ {lon_max}, {lat_min} ], [ {lon_min}, {lat_min} ], [ {lon_min}, {lat_max} ] ] ] ] } } ] }' >> $project_dir/crop.geojson

sed -i s/{lon_min}/$lon_min/g $project_dir/crop.geojson
sed -i s/{lon_max}/$lon_max/g $project_dir/crop.geojson
sed -i s/{lat_min}/$lat_min/g $project_dir/crop.geojson
sed -i s/{lat_max}/$lat_max/g $project_dir/crop.geojson

if [[ $generate_terrain == "true" ]] ; then
	rm -f "$merged_dem"
	IFS=' ' read -r -a tiles_list <<< $(python3 $(pwd)/calc_srtm_tiles_list.py -bbox "$bbox")
	echo -e "\e[100mDEM tiles list: ${tiles_list[@]}\e[49m"
	if [[ $download_terrain_tiles == "true" ]] ; then
		rm -f $terrain_input_dir/*.*
		echo -e "\e[104m=== Downloading terrain tiles...\e[49m"
		eio clip -o $terrain_input_dir/srtm.tif --bounds $bbox_eio_query
		if [[ $? != 0 ]] ; then
			echo -e "\033[91mError downloading terrain. Stopping.\033[0m"
			make_error_flag 5
			exit 1
		elif [[ $(gdalinfo $terrain_input_dir/srtm.tif | grep "Band 1") ]] ; then
			echo -e "\033[92mTerrain downloaded\033[0m"
		else
			echo -e "\033[91mError downloading terrain. Stopping.\033[0m"
			make_error_flag 5
			exit 1
		fi
		eio clean
	fi
	if [[ $get_terrain_tiles == "true" ]] ; then
		rm -f $terrain_input_dir/*.*
		echo -e "\e[104m=== Copying DEM tiles from $terrain_src_dir...\e[49m"
		if [[ $terrain_src_dir == "" ]] ; then
			echo -e "\033[91mterrain_src_dir "$terrain_src_dir" not defined in config but get_terrain_tiles=true. Stopping.\033[0m"
			make_error_flag
			exit 1
		fi
		if [[ ! -d $terrain_src_dir ]] ; then
			echo -e "\033[91mterrain_src_dir "$terrain_src_dir" don't exist but get_terrain_tiles=true. Turn it off or check path. Stopping.\033[0m"
			if [[ $running_in_container == true ]] ; then
				echo -e "\033[93mCheck /mnt/terrain docker mount\033[0m"
			fi
			make_error_flag
			exit 1
		fi
		for tile in "${tiles_list[@]}"
		do
			if [[ -f "$terrain_src_dir/${tile}.tif" ]] ; then
				echo -e "\033[92m$tile.tif found\033[0m"
				cp $terrain_src_dir/${tile}.tif $terrain_input_dir
				continue
			elif [[ -f "$terrain_src_dir/${tile}.zip" ]] ; then
				echo -e "\033[92m$tile.zip found\033[0m"
				cp $terrain_src_dir/${tile}.zip $terrain_input_dir
				continue
			elif [[ -f "$terrain_src_dir/${tile}.hgt" ]] ; then
				echo -e "\033[92m$tile.hgt found\033[0m"
				cp $terrain_src_dir/${tile}.hgt $terrain_input_dir
				continue
			else
				echo -e "\033[93m${tile}.tif not found. Possible cause: no data in this area\033[0m"
			fi
		done
	fi
	if [[ $get_terrain_tiles == "false" ]] && [[ $download_terrain_tiles == "false" ]] ; then
		if ! ls $terrain_input_dir/*.tif 1> /dev/null 2>&1 ; then
			echo -e "\033[93mNo DEM tiles found in '$terrain_input_dir'\nPlease download and manually add tiles from 'DEM tiles list' or restart script with download_terrain_tiles=true or get_terrain_tiles=true variables set in config.ini\033[0m"
			read -rsp $'\033[93mPress any key to continue...\n\033[0m' -n1 key
		fi
# 		if ! ls $terrain_input_dir/*.tif 1> /dev/null 2>&1 ; then
# 			echo -e "\033[93mStill no DEM tiles found"
# 			exit 1
# 		fi
	fi

	CUTLINE_STRING="-crop_to_cutline -cutline $project_dir/crop.geojson"

	# Extract and convert *.hgt.zip/*.hgt to GeoTIFF format
	shopt -s nullglob
	for f in "$terrain_input_dir"/*.zip; do
		[ -e "$f" ] && unzip -o $f -d "$terrain_input_dir" && rm $f
	done
	for f in "$terrain_input_dir"/*.hgt; do
		[ -e "$f" ] && gdalwarp -of GTiff $f ${f%.*}.tif && rm $f
	done
	for f in "$terrain_input_dir"/*.tif; do
		[ ! -e "$f" ] && echo -e "\033[91mNo DEM tiles (GeoTIFF/HGT) found in "$terrain_input_dir". Stopping.\033[0m" && make_error_flag 6 && exit 1;
		break;
	done
	shopt -u nullglob

	if ls $terrain_input_dir/*.tif 1> /dev/null 2>&1 ; then
		echo -e "\e[104m=== Merging DEM tiles...\e[49m"
		rm -f "$project_dir"/input_terrain.vrt
		gdalbuildvrt "$project_dir"/input_terrain.vrt $terrain_input_dir/*.tif
		gdalwarp -of GTiff -co "COMPRESS=LZW" $CUTLINE_STRING "$project_dir"/input_terrain.vrt "$merged_dem"
		rm -f "$project_dir"/input_terrain.vrt
		rm -f "$raster_data_dir/merged_dem_3857.tif"
		gdalwarp -t_srs EPSG:3857 -ot Float32 -co "COMPRESS=LZW" "$merged_dem" "$raster_data_dir"/merged_dem_3857.tif # for mountain_pass layer "ele_calc" attribute

		if [[ $generate_terrain_hillshade_slope == "true" ]] ; then
			echo -e "\e[104m=== Generating slopes...\e[49m"
			rm -f "$raster_data_dir/slope_upscaled.tif"
			gdaldem slope -compute_edges -s 111120 "$raster_data_dir"/merged_dem.tif "$raster_data_dir"/slope.tif
			gdal_calc.py -A "$raster_data_dir"/slope.tif --type=Float32 --co COMPRESS=LZW --outfile="$raster_data_dir"/slope_cut.tif --calc="A*(A>1.04)" --overwrite
			size_str=$(gdalinfo "$raster_data_dir"/slope_cut.tif | grep "Size is" | sed 's/Size is //g')
			width=$(echo $size_str | sed 's/,.*//')
			height=$(echo $size_str | sed 's/.*,//')
			width_mod=$(( $width * 3 ))
			height_mod=$(( $height * 3 ))
			echo -e "\033[95mResizing from $width x$height to $width_mod x $height_mod\033[0m"
			rm -f "$raster_data_dir/slope_upscaled.tif.ovr"
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$raster_data_dir"/slope_cut.tif "$raster_data_dir"/slope_upscaled.tif
			if [[ -f "$raster_data_dir/slope_upscaled.tif" ]] && [[ $(wc -c <"$raster_data_dir/slope_upscaled.tif") -ge 100000 ]] ; then
				echo -e "\033[95mGenerating slope overviews\033[0m"
				gdaladdo -ro --config COMPRESS_OVERVIEW LZW "$raster_data_dir/slope_upscaled.tif" 512 256 128 64 32 16 8 4 2
				echo -e "\033[92mSlopes generated\033[0m"
			else
				echo -e "\033[91mError. $raster_data_dir/slope_upscaled.tif is empty. Stopping.\033[0m"
				make_error_flag 7
				exit 1
			fi
			rm -f "$raster_data_dir"/slope.tif
			rm -f "$raster_data_dir"/slope_cut.tif

			echo -e "\e[104m=== Generating hillshade using terrain_resample_method=$terrain_resample_method\e[49m"
			size_str=$(gdalinfo "$raster_data_dir"/merged_dem.tif | grep "Size is" | sed 's/Size is //g')
			width=$(echo $size_str | sed 's/,.*//')
			height=$(echo $size_str | sed 's/.*,//')
			width_mod=$(( $width / 2 ))
			height_mod=$(( $height / 2 ))
			echo -e "\033[95mResizing from $width x$height to $width_mod x $height_mod\033[0m"
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$raster_data_dir"/merged_dem.tif "$raster_data_dir"/merged_dem_downscaled.tif
			width_mod=$(( $width * 3 ))
			height_mod=$(( $height * 3 ))
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$raster_data_dir"/merged_dem_downscaled.tif "$raster_data_dir"/merged_dem_upscaled.tif
			gdaldem hillshade -z 2 -s 111120 -compute_edges -multidirectional -alt 70 -co "COMPRESS=LZW" -co "BIGTIFF=YES" "$raster_data_dir"/merged_dem_upscaled.tif "$raster_data_dir"/hillshade.tif
			echo "0 255 255 255
	90 0 0 0" > "$raster_data_dir"/color_slope.txt
			gdaldem color-relief "$raster_data_dir"/slope_upscaled.tif "$raster_data_dir"/color_slope.txt "$raster_data_dir"/slope_shade.tif
			gdal_calc.py -A "$raster_data_dir"/hillshade.tif -B "$raster_data_dir"/slope_shade.tif --A_band=1 --type=Float32 --co COMPRESS=LZW --outfile="$raster_data_dir"/hillshade_composite.tif --calc="A+(B*0.5)" --overwrite
			rm -f "$raster_data_dir"/hillshade.tif
			rm -f "$raster_data_dir"/slope_shade.tif
			rm -f "$raster_data_dir"/merged_dem_downscaled.tif
			rm -f "$raster_data_dir"/color_slope.txt
			rm -f "$raster_data_dir/hillshade_composite.tif.ovr"
			if [[ -f "$raster_data_dir/hillshade_composite.tif" ]] && [[ $(wc -c <"$raster_data_dir/hillshade_composite.tif") -ge 100000 ]] ; then
				echo -e "\033[95mGenerating hillshade overviews\033[0m"
				gdaladdo -ro --config COMPRESS_OVERVIEW LZW "$raster_data_dir/hillshade_composite.tif" 512 256 128 64 32 16 8 4 2
				echo -e "\033[92mHillshade generated\033[0m"
			else
				echo -e "\033[93mWarning! $raster_data_dir/hillshade_composite.tif is empty.\033[0m";
			fi
		fi

		if [[ $generate_terrain_isolines == "true" ]] ; then
			rm -f "$vector_data_dir"/isolines_full.sqlite
			echo -e "\e[104m=== Generating isolines using isolines_step=$isolines_step\e[49m"
			if [[ $smooth_isolines == "true" ]] ; then
				isolines_source="merged_dem_upscaled.tif"
			else
				isolines_source="merged_dem.tif"
			fi
			gdal_contour -b 1 -a ELEV -i $isolines_step -f "ESRI Shapefile" -nln "isolines" "$raster_data_dir/$isolines_source" "$temp_dir/isolines_full.shp"
			# SHP step is needed because of ERROR 1: Cannot insert feature with geometry of type LINESTRINGZ in column GEOMETRY. Type LINESTRING expected
			#  when exporting directly to spatialite
			if [[ $(wc -c <"$temp_dir/isolines_full.shp") -le 150 ]] ; then
				isolines_are_empty=true
			fi
			convert2spatialite "$temp_dir/isolines_full.shp" "$vector_data_dir/isolines_full.sqlite"
			rm -f "$vector_data_dir/isolines_regular.sqlite"
			cp "$vector_data_dir/isolines_full.sqlite" "$vector_data_dir/isolines_regular.sqlite"
			rm -f "$temp_dir/isolines_full.shp"
			rm -f "$temp_dir/isolines_full.shx"
			rm -f "$temp_dir/isolines_full.dbf"
			rm -f "$temp_dir/isolines_full.prj"
			if [[ -f "$vector_data_dir/isolines_full.sqlite" ]] && [[ $(wc -c <"$vector_data_dir/isolines_full.sqlite") -ge 100000 ]] ; then
				echo -e "\033[92mIsolines generated\033[0m"
			else
				echo -e "\033[93mWarning! $vector_data_dir/isolines_full.sqlite is empty.\033[0m";
			fi
		fi
	else
		echo -e "\033[93mError. No DEM data found. Hillshade, slopes and isolines are not generated.\033[0m"
		echo -e "\033[93mCheck download_terrain_tiles=true or get_terrain_tiles=true options in config.ini\033[0m"
		make_error_flag 4
		exit 1
	fi
	if [[ $download_terrain_tiles == "true" ]] ; then
		rm -f $terrain_input_dir/*.*
	fi
fi

function run_grass_alg_voronoiskeleton {
	echo -e "\e[95malg: v.voronoi\e[39m"

	case $4 in
		"geojson")
			ext="geojson"
			format="GeoJSON"
			;;
		"sqlite")
			ext="sqlite"
			format="SQLite"
			;;
		*)
			ext="geojson"
			format="GeoJSON"
			;;
	esac

	rm -f $temp_dir/grassjob.sh
	rm -rf $temp_dir/grassdata/mytemploc
	rm -rf $temp_dir/grassdata
	echo "export GRASS_MESSAGE_FORMAT=plain
v.in.ogr input=$temp_dir/$1.$ext output=$1 --quiet
g.region vector=$1
v.voronoi -s input=$1 smoothness=$2 thin=$3 output=${1}_ovr --quiet
v.out.ogr input=${1}_ovr output=$temp_dir/${1}_skel.$ext format=$format --overwrite --quiet" > $temp_dir/grassjob.sh
	cat $temp_dir/grassjob.sh
	chmod u+x $temp_dir/grassjob.sh
	mkdir -p $temp_dir/grassdata
	grass -c epsg:4326 $temp_dir/grassdata/mytemploc -e
	export GRASS_BATCH_JOB="$temp_dir/grassjob.sh"
	cp $temp_dir/$1.$ext $temp_dir/grassdata/mytemploc/PERMANENT/$1.$ext
	grass $temp_dir/grassdata/mytemploc/PERMANENT
	unset GRASS_BATCH_JOB

	rm -rf $temp_dir/grassdata/mytemploc
	rm -rf $temp_dir/grassdata
}

function run_alg_dissolve {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:dissolve" \
		-param1 INPUT -value1 "$temp_dir/$1.$ext$4" \
		-param2 FIELD -value2 "$2" \
		-param3 OUTPUT -value3 "$temp_dir/${1}_dissolved.$ext"
}
function run_alg_polygonstolines {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:polygonstolines" \
		-param1 INPUT -value1 $temp_dir/$1.$ext \
		-param2 OUTPUT -value2 $temp_dir/${1}_lines.$ext
}
function run_alg_multiparttosingleparts {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:multiparttosingleparts" \
		-param1 INPUT -value1 $temp_dir/${1}.$ext \
		-param2 OUTPUT -value2 $temp_dir/${1}_parts.$ext
}
function run_alg_convertgeometrytype {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "qgis:convertgeometrytype" \
		-param1 INPUT -value1 $temp_dir/${1}.$ext$4 \
		-param2 TYPE -value2 $3 \
		-param3 OUTPUT -value3 $temp_dir/${1}_conv.$ext
}
function run_alg_difference {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:difference" \
		-param1 INPUT -value1 $temp_dir/${1}.$ext \
		-param2 OVERLAY -value2 $temp_dir/${2}.$ext$4$5 \
		-param3 OUTPUT -value3 $temp_dir/${1}_diff.$ext
}
function run_alg_intersection {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:intersection" \
		-param1 INPUT -value1 $temp_dir/${1}.$ext \
		-param2 OVERLAY -value2 $temp_dir/${2}.$ext$4$5 \
		-param3 OUTPUT -value3 $temp_dir/${1}_intersection.$ext
}
function run_alg_extractspecificvertices {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:extractspecificvertices" \
		-param1 INPUT -value1 $temp_dir/${1}.$ext \
		-param2 VERTICES -value2 $2 \
		-param3 OUTPUT -value3 $temp_dir/${1}_vertices.$ext
}
function run_alg_centroids {
	python3 $(pwd)/run_alg.py \
		-alg "native:centroids" \
		-param1 INPUT -value1 $temp_dir/$1.geojson \
		-param2 ALL_PARTS -value2 True \
		-param3 OUTPUT -value3 $temp_dir/${1}_centroids.geojson
}
function run_alg_smoothgeometry {
	case $5 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:smoothgeometry" \
		-param1 INPUT -value1 $temp_dir/$1.$ext$6 \
		-param2 ITERATIONS -value2 $2 \
		-param3 OFFSET -value3 $3 \
		-param4 MAX_ANGLE -value4 $4 \
		-param5 OUTPUT -value5 $temp_dir/${1}_smoothed.$ext
}
function merge_vector_layers {
	case $1 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
	esac
	case $2 in
		"LineString")
			geometrytype="|geometrytype=LineString"
			;;
		"Polygon")
			geometrytype="|geometrytype=Polygon"
			;;
		"Point")
			geometrytype="|geometrytype=Point"
			;;
	esac
	str=""
	if [[ $7 ]] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype,$temp_dir/$6.$ext$geometrytype,$temp_dir/$7.$ext$geometrytype"
	elif [[ $6 ]] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype,$temp_dir/$6.$ext$geometrytype"
	elif [[ $5 ]] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype"
	elif [[ $4 ]] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype"
	fi
	python3 $(pwd)/run_alg.py \
		-alg "native:mergevectorlayers" \
		-param1 LAYERS -value1 "[$str]" \
		-param2 CRS -value2 "QgsCoordinateReferenceSystem('EPSG:4326')" \
		-param3 OUTPUT -value3 $temp_dir/${3}_merged.$ext
}
function run_alg_buffer {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	case $4 in
		"LineString")
			geometrytype="|geometrytype=LineString"
			;;
		"Polygon")
			geometrytype="|geometrytype=Polygon"
			;;
		"MultiPolygon")
			geometrytype="|geometrytype=MultiPolygon"
			;;
		"Point")
			geometrytype="|geometrytype=Point"
			;;
		*)
			geometrytype=""
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:buffer" \
		-param1 INPUT -value1 $temp_dir/$1.$ext$geometrytype \
		-param2 DISTANCE -value2 $2 \
		-param3 OUTPUT -value3 $temp_dir/${1}_buffered.$ext
}
function run_alg_singlesidedbuffer {
	case $4 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "qgis:singlesidedbuffer" \
		-param1 INPUT -value1 "$temp_dir/$1.$ext|geometrytype=LineString" \
		-param2 DISTANCE -value2 $2 \
		-param3 SIDE -value3 $3 \
		-param4 JOIN_STYLE -value4 0 \
		-param5 OUTPUT -value5 $temp_dir/${1}_sbuffered.$ext
}
function run_alg_explodelines {
	python3 $(pwd)/run_alg.py \
		-alg "native:explodelines" \
		-param1 INPUT -value1 $temp_dir/$1.geojson \
		-param2 OUTPUT -value2 $temp_dir/${1}_exploded.geojson
}
function run_alg_splitwithlines {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:splitwithlines" \
		-param1 INPUT -value1 $temp_dir/$1.$ext \
		-param2 LINES -value2 $temp_dir/$2.$ext \
		-param3 OUTPUT -value3 $temp_dir/${1}_split.$ext
}
function run_alg_pyfieldcalc {
	python3 $(pwd)/run_alg.py \
		-alg "qgis:advancedpythonfieldcalculator" \
		-param1 INPUT -value1 $temp_dir/$1.geojson \
		-param2 FIELD_NAME -value2 $2 \
		-param3 FIELD_TYPE -value3 $3 \
		-param4 FORMULA -value4 $4 \
		-param5 OUTPUT -value5 $temp_dir/${1}_pyfieldcalc.geojson
}
function run_alg_joinattributesbylocation {
	python3 $(pwd)/run_alg.py \
		-alg "qgis:joinattributesbylocation" \
		-param1 INPUT -value1 $temp_dir/$1.geojson \
		-param2 JOIN -value2 $temp_dir/$2.geojson \
		-param3 PREDICATE -value3 $3 \
		-param4 JOIN_FIELDS -value4 $4 \
		-param5 METHOD -value5 $5 \
		-param6 OUTPUT -value6 $temp_dir/${1}_joinattrsloc.geojson
}
function run_alg_extractbylocation {
	case $4 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:extractbylocation" \
		-param1 INPUT -value1 $temp_dir/$1.$ext \
		-param2 PREDICATE -value2 $3 \
		-param3 INTERSECT -value3 $temp_dir/$2.$ext \
		-param4 OUTPUT -value4 $temp_dir/${1}_extracted.$ext
}
function run_alg_generalize {
	case $3 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "grass7:v.generalize" \
		-param1 input -value1 $temp_dir/$1.$ext$4 \
		-param2 method -value2 $2 \
		-param3 output -value3 $temp_dir/${1}_generalized.$ext \
		-param4 error -value4 $temp_dir/${1}_err.$ext
}
function run_alg_fixgeometries {
	case $3 in
		"workdir")
			dir=$vector_data_dir
			;;
		*)
			dir=$temp_dir
			;;
	esac
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:fixgeometries" \
		-param1 INPUT -value1 $dir/$1.$ext$4 \
		-param2 OUTPUT -value2 $dir/${1}_fixed.$ext
}
function run_alg_reprojectlayer {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	ogr2ogr -f $ext -t_srs EPSG:3857 $temp_dir/${1}_reproj.$ext $temp_dir/${1}.$ext
}
function set_projection {
	ogr2ogr -a_srs EPSG:4326 -f SQlite -dsco SPATIALITE=YES -skipfailures $1_tmp $1 && rm -f $1 && mv $1_tmp $1
}
function run_alg_simplifygeometries {
	case $2 in
		"geojson")
			ext="geojson"
			;;
		"sqlite")
			ext="sqlite"
			;;
		*)
			ext="geojson"
			;;
	esac
	python3 $(pwd)/run_alg.py \
		-alg "native:simplifygeometries" \
		-param1 INPUT -value1 "$temp_dir/$1.$ext$5" \
		-param2 METHOD -value2 $3 \
		-param3 TOLERANCE -value3 $4 \
		-param4 OUTPUT -value4 $temp_dir/${1}_simpl.$ext
}

echo -e "\e[104m=== Processing queries\e[49m"
echo ${array_queries[@]}
index=1
if [[ ${#array_queries[@]} -ge 1 ]] ; then
	case $overpass_instance in
		"docker")
			echo -e "\e[100mUsing docker overpass instance\e[49m"
			;;
		"local")
			echo -e "\e[100mUsing local overpass instance $overpass_endpoint_local\e[49m"
			;;
		"external")
			echo -e "\e[100mUsing external overpass instance $overpass_endpoint_external\e[49m"
			;;
		"ssh")
			echo -e "\e[100mUsing external overpass instance $overpass_endpoint_ssh\e[49m"
			;;
	esac
fi

for t in ${array_queries[@]}; do
	if [[ -f $vector_data_dir/$t.sqlite ]] ; then
		rm $vector_data_dir/$t.sqlite
	fi
	if [[ ! -f $(pwd)/queries/$t.txt ]] ; then
		echo -e "\033[93mQuery for $t not found.\033[0m"
	fi
	query=$(cat $(pwd)/queries/$t.txt)
	req_string_query='[out:xml][timeout:3600][maxsize:2000000000][bbox:'$bbox_query'];'$query
	req_string_out="out body;>;out skel qt;"
	echo -e "\033[92m\e[44m=== ($index / ${#array_queries[@]}) Downloading ${t}...\e[49m\033[0m"
	if [[ $overpass_instance == external ]] ; then
		req_string=$overpass_endpoint_external'?data='$req_string_query$req_string_out
		wget -O $vector_data_dir/$t.osm -t 1 --timeout=3600 --remote-encoding=utf-8 --local-encoding=utf-8 "$req_string"
	else
		req_string=$req_string_query$req_string_out
		echo "$req_string" | $req_path_string > $vector_data_dir/$t.osm
	fi
	if [[ $? != 0 ]] ; then
		echo -e "\033[91mOverpass server error. Stopping.\033[0m"
		make_error_flag 3
		exit 1
	fi
	if ! grep -q "tag k" "$vector_data_dir/$t.osm" || ( ( [[ $t == "admin_level_2" ]] || [[ $t == "admin_level_4" ]] ) && ! grep -q "way id" "$vector_data_dir/$t.osm" ); then
		echo -e "\033[93mResult is empty!\033[0m"
		rm -f "$vector_data_dir/$t.osm"
		if [[ $debug_copy_from_override_dir == true ]] ; then
			echo -e "\033[95mCopying from override dir...\033[0m"
			if [[ -f "$override_dir/$t.osm" ]] ; then
				cp $override_dir/$t.osm $vector_data_dir/$t.osm
			else
				echo -e "\033[91m$override_dir/$t.osm not found. Stopping.\033[0m"
				make_error_flag
				exit 1
			fi
		else
			((index++))
			continue
		fi

	fi
	if grep -q \</osm\> "$vector_data_dir/$t.osm" ; then
		echo -e "\033[92mOK\033[0m"
	else
		echo -e "\033[91m$vector_data_dir/$t.osm is incomplete. It looks like Overpass server has interrupted the transmission. Try again or use another server (overpass_instance and overpass_endpoint_* variables in config.ini). Stopping.\033[0m"
		make_error_flag 2
		exit 1
	fi

	case $t in
		"railway_all")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			run_alg_reprojectlayer ${t}_exploded
			mv -f $temp_dir/${t}_exploded_reproj.geojson $vector_data_dir/${t}_exploded.geojson
			convert2spatialite "$vector_data_dir/${t}_exploded.geojson" "$vector_data_dir/${t}_exploded.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"ridge" | "railway_all")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			run_alg_reprojectlayer ${t}_exploded
			mv -f $temp_dir/${t}_exploded_reproj.geojson $vector_data_dir/${t}_exploded.geojson
			convert2spatialite "$vector_data_dir/${t}_exploded.geojson" "$vector_data_dir/${t}_exploded.sqlite"

			run_alg_simplifygeometries $t "geojson" 0 0.0007
			run_alg_smoothgeometry ${t}_simpl 10 0.25 180 "geojson"
			run_alg_dissolve ${t}_simpl_smoothed "name" "geojson"
			convert2spatialite "$temp_dir/${t}_simpl_smoothed_dissolved.geojson" "$vector_data_dir/${t}_simpl_smoothed.sqlite"
			cp $temp_dir/${t}_simpl_smoothed_dissolved.geojson $vector_data_dir/${t}_simpl_smoothed.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"water" | "water_intermittent") # "coastline" should be requested after "water" to be correct
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $vector_data_dir/$t.geojson && mv $vector_data_dir/${t}_fixed.geojson $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_fixgeometries $t "geojson" "|geometrytype=Polygon"
			run_alg_dissolve ${t}_fixed "" "geojson" "|geometrytype=Polygon"
			run_alg_multiparttosingleparts ${t}_fixed_dissolved "geojson"
			cp "$temp_dir/${t}_fixed_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.geojson"
			convert2spatialite "$temp_dir/${t}_fixed_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"allotments")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			run_alg_multiparttosingleparts ${t}_dissolved
			mv -f $temp_dir/${t}_dissolved_parts.geojson $vector_data_dir/${t}_dissolved.geojson # for outline
			convert2spatialite "$vector_data_dir/${t}_dissolved.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			run_alg_buffer $t 0.001
			run_alg_dissolve ${t}_buffered
			run_alg_multiparttosingleparts ${t}_buffered_dissolved
			mv -f $temp_dir/${t}_buffered_dissolved_parts.geojson $vector_data_dir/${t}_buffered_dissolved.geojson
			convert2spatialite "$vector_data_dir/${t}_buffered_dissolved.geojson" "$vector_data_dir/${t}_buffered_dissolved.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"water_without_riverbanks")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			;;
		"river" | "river_intermittent") # should be requested after "water_without_riverbanks"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			if [[ -f $vector_data_dir/water_dissolved.geojson ]] ; then
				cp $vector_data_dir/water_without_riverbanks.geojson $temp_dir
				run_alg_difference $t "water_without_riverbanks"
				mv $temp_dir/${t}_diff.geojson $temp_dir/$t.geojson
			fi
			cp $temp_dir/$t.geojson $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			run_alg_simplifygeometries $t "geojson" 0 0.001 "|geometrytype=Linestring"
			run_alg_smoothgeometry ${t}_simpl 5 0.25 180 "geojson" "|geometrytype=LineString"
			convert2spatialite "$temp_dir/${t}_simpl_smoothed.geojson" "$vector_data_dir/${t}_smoothed.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"stream_intermittent") # should be requested after "water"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $temp_dir/$t.geojson
			if [[ -f $vector_data_dir/water_dissolved.geojson ]] ; then
				cp $vector_data_dir/water_dissolved.geojson $temp_dir
				run_alg_difference $t "water_dissolved"
				cp $temp_dir/${t}_diff.geojson $vector_data_dir/$t.geojson
				convert2spatialite "$temp_dir/${t}_diff.geojson" "$vector_data_dir/$t.sqlite"
			else
				convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			fi
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"stream") # should be requested after "water" and "stream_intermittent"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			cp $vector_data_dir/$t.sqlite $temp_dir
			if [[ -f $vector_data_dir/water_dissolved.geojson ]] ; then
				suffixdiff="_diff"
				cp $vector_data_dir/water_dissolved.geojson $temp_dir
				run_alg_difference $t "water_dissolved"
			fi
			if [[ -f $vector_data_dir/stream_intermittent.geojson ]] ; then
				suffixmerged="_merged"
				cp $vector_data_dir/stream_intermittent.geojson $temp_dir
				merge_vector_layers "geojson" "LineString" ${t}$suffixdiff "stream_intermittent" # for names
			fi
			convert2spatialite "$temp_dir/${t}${suffixdiff}${suffixmerged}.geojson" "$vector_data_dir/${t}${suffixmerged}.sqlite"
			run_alg_smoothgeometry ${t}${suffixdiff} 5 0.25 180 "geojson" "|geometrytype=LineString"
			convert2spatialite "$temp_dir/${t}${suffixdiff}_smoothed.geojson" "$vector_data_dir/${t}_smoothed.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"admin_level_2")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			run_alg_dissolve ${t}_lines
			run_alg_multiparttosingleparts ${t}_lines_dissolved "geojson"
			cp $temp_dir/${t}_lines_dissolved_parts.geojson $vector_data_dir/${t}_dissolved.geojson
			convert2spatialite "$temp_dir/${t}_lines_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
#			rm $temp_dir/*.*
			;;
		"admin_level_4") # should be requested after "admin_level_2"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			if [[ -f $vector_data_dir/admin_level_2_dissolved.geojson ]] ; then
				mv $vector_data_dir/admin_level_2_dissolved.geojson $temp_dir
				run_alg_difference ${t}_lines "admin_level_2_dissolved"
			else mv $temp_dir/${t}_lines.geojson $temp_dir/${t}_lines_diff.geojson
			fi
			run_alg_dissolve ${t}_lines_diff
			run_alg_multiparttosingleparts ${t}_lines_diff_dissolved "geojson"
			cp "$temp_dir/${t}_lines_diff_dissolved_parts.geojson" "$vector_data_dir/${t}_proc.geojson"
			convert2spatialite "$temp_dir/${t}_lines_diff_dissolved_parts.geojson" "$vector_data_dir/${t}_proc.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_admin_centre_6" | "place_admin_centre_4" | "place_admin_centre_2")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_pyfieldcalc $t admin_centre_${t: -1} 2 "value='true'"
			if [[ -f $vector_data_dir/places_main.geojson ]] ; then
				cp $vector_data_dir/places_main.geojson $temp_dir
			else
				echo -e "\033[91m$vector_data_dir/places_main.geojson not found. Please request places_main before $t. Stopping.\033[0m"
				make_error_flag
				exit 1
			fi
			run_alg_joinattributesbylocation places_main ${t}_pyfieldcalc 2 "admin_centre_${t: -1}" 0
			cp -f $temp_dir/places_main_joinattrsloc.geojson $vector_data_dir/places_main.geojson
			convert2spatialite "$vector_data_dir/places_main.geojson" "$vector_data_dir/places_main.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_of_worship_muslim" | "place_of_worship_hindu" | "place_of_worship_buddhist" | "place_of_worship_shinto" | "place_of_worship_jewish" | "place_of_worship_taoist" | "place_of_worship_sikh" | "place_of_worship_other" | "sinkhole_polygon" | "alpine_hut" | "wilderness_hut" | "memorial" | "monument" | "tower_communication" | "monastery_no_religion" | "barrier_border_control" | "cape" | "chimney" | "water_tower" | "volcano" | "volcano_dirt" | "mineshaft" | "mineshaft_abandoned")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $vector_data_dir/${t}_centroids.geojson
			convert2spatialite "$vector_data_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			;;
		"monastery_christian")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			run_alg_buffer ${t}_centroids 0.0025
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			cp $temp_dir/${t}_centroids_buffered.geojson $vector_data_dir/${t}_centroids_buffered.geojson
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			;;
		"place_of_worship_christian" | "place_of_worship_christian_ruins") # should be requested after "monastery_christian"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			if [[ -f "$vector_data_dir/monastery_christian_centroids_buffered.geojson" ]] ; then
				cp $vector_data_dir/monastery_christian_centroids_buffered.geojson $temp_dir/
				run_alg_difference ${t}_centroids "monastery_christian_centroids_buffered"
				run_alg_multiparttosingleparts ${t}_centroids
				run_alg_multiparttosingleparts ${t}_centroids_diff
				convert2spatialite "$temp_dir/${t}_centroids_parts.geojson" "$vector_data_dir/${t}_centroids.sqlite"
				convert2spatialite "$temp_dir/${t}_centroids_diff_parts.geojson" "$vector_data_dir/${t}_centroids_diff_parts.sqlite"
			else
				convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
				rm -f "$vector_data_dir/${t}_centroids_diff_parts.sqlite" && cp "$vector_data_dir/${t}_centroids.sqlite" "$vector_data_dir/${t}_centroids_diff_parts.sqlite"
			fi
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			;;
		"camp_site" | "attraction" | "ruins" | "track_bridge" | "aerodrome" | "castle" | "archaeological_site" | "observatory" | "picnic_site" | "tower_cooling")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $vector_data_dir/${t}_centroids.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			rm $vector_data_dir/$t.osm
			;;
		"waterfall" | "weir")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $vector_data_dir/${t}_centroids.geojson
			merge_vector_layers "geojson" "LineString" ${t}_lines $t
			cp $temp_dir/${t}_lines_merged.geojson $vector_data_dir/${t}_lines.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			convert2spatialite "$temp_dir/${t}_lines_merged.geojson" "$vector_data_dir/${t}_lines.sqlite"
			rm $vector_data_dir/$t.osm
			;;
		"highway_main_bridge")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			run_alg_dissolve $t 'highway'
			run_alg_multiparttosingleparts ${t}_dissolved "geojson"
			cp $temp_dir/${t}_centroids.geojson $vector_data_dir/${t}_centroids.geojson
			cp $temp_dir/${t}_dissolved_parts.geojson $vector_data_dir/${t}_dissolved.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			convert2spatialite "$temp_dir/${t}_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			rm $vector_data_dir/$t.osm
			;;
		"power_line")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			run_alg_multiparttosingleparts ${t}_dissolved
			convert2spatialite "$temp_dir/${t}_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"ford" | "railway_stop_names")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$vector_data_dir/${t}_centroids.sqlite"
			run_alg_buffer $t 0.006
			run_alg_dissolve ${t}_buffered 'name'
			run_alg_centroids ${t}_buffered_dissolved
			convert2spatialite "$temp_dir/${t}_buffered_dissolved_centroids.geojson" "$vector_data_dir/${t}_buffered_dissolved_centroids.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"railway_station_icons" | "railway_halt_icons")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_buffer $t 0.004
			run_alg_dissolve ${t}_buffered 'name'
			run_alg_centroids ${t}_buffered_dissolved
			run_alg_reprojectlayer ${t}_buffered_dissolved_centroids
			convert2spatialite "$temp_dir/${t}_buffered_dissolved_centroids_reproj.geojson" "$vector_data_dir/${t}_buffered_dissolved_centroids.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"mountain_pass")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_reprojectlayer $t
			convert2spatialite "$temp_dir/${t}_reproj.geojson" "$vector_data_dir/${t}_reproj.sqlite"
			cp "$temp_dir/${t}_reproj.geojson" $vector_data_dir
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"building_train_station")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_fixgeometries $t "geojson" "" "|geometrytype=Polygon" && rm -f $temp_dir/$t.geojson && mv $temp_dir/${t}_fixed.geojson $temp_dir/$t.geojson
			run_alg_centroids $t
			run_alg_reprojectlayer ${t}_centroids
			convert2spatialite "$temp_dir/${t}_centroids_reproj.geojson" "$vector_data_dir/${t}_reproj.sqlite"
			cp "$temp_dir/${t}_centroids_reproj.geojson" "$vector_data_dir/${t}_reproj.geojson"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_locality_node")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			if [[ -f "$vector_data_dir/place_locality_way.geojson" ]] ; then
				cp $vector_data_dir/place_locality_way.geojson $temp_dir
				run_alg_difference $t "place_locality_way"
				run_alg_multiparttosingleparts ${t}_diff "geojson"
				convert2spatialite "$temp_dir/${t}_diff_parts.geojson" "$vector_data_dir/${t}_diff.sqlite"
				cp "$temp_dir/${t}_diff.geojson" "$vector_data_dir/${t}_diff.geojson"
			else
				convert2spatialite "$temp_dir/$t.geojson" "$vector_data_dir/${t}_diff.sqlite"
				cp "$temp_dir/$t.geojson" "$vector_data_dir/${t}_diff.geojson"
			fi
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"route_hiking")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			convert2spatialite "$temp_dir/${t}_exploded.geojson" "$vector_data_dir/${t}_exploded.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"military")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			run_alg_multiparttosingleparts ${t}_dissolved "geojson"
			cp $temp_dir/${t}_dissolved_parts.geojson $vector_data_dir
			convert2spatialite "$temp_dir/${t}_dissolved_parts.geojson" "$vector_data_dir/${t}_dissolved.sqlite"
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"mountain_area")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			sed -i 's/name_/name:/g' $vector_data_dir/$t.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && mv $vector_data_dir/${t}_fixed.geojson $temp_dir/${t}_polygon_fixed.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=LineString" && mv $vector_data_dir/${t}_fixed.geojson $temp_dir/${t}_line_fixed.geojson
			run_alg_linestopolygons "${t}_line_fixed" "geojson" "|geometrytype=LineString"
			if [[ $(wc -c <"$temp_dir/${t}_line_fixed_polygons.geojson") -ge 220 ]] ; then
				merge_vector_layers "geojson" "MultiPolygon" ${t}_polygon_fixed ${t}_line_fixed_polygons
				mv $temp_dir/${t}_polygon_fixed_merged.geojson $temp_dir/$t.geojson
			else
				mv $temp_dir/${t}_polygon_fixed.geojson $temp_dir/$t.geojson
			fi

			convert2spatialite "$temp_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			run_alg_fixgeometries $t "sqlite" "workdir" && rm -f $vector_data_dir/$t.sqlite && mv $vector_data_dir/${t}_fixed.sqlite $vector_data_dir/$t.sqlite # If geojson with intersections processed with run_alg_fixgeometries, is converted to sqlite then it will contain intersections again
			set_projection $vector_data_dir/$t.sqlite
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"glacier" | "bay_polygon" | "wetland" | "wood")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			sed -i 's/name_/name:/g' $vector_data_dir/$t.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $vector_data_dir/$t.geojson && mv $vector_data_dir/${t}_fixed.geojson $vector_data_dir/$t.geojson
			cp $vector_data_dir/$t.geojson $temp_dir
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			run_alg_fixgeometries $t "sqlite" "workdir" "|geometrytype=Polygon" && rm -f $vector_data_dir/$t.sqlite && mv $vector_data_dir/${t}_fixed.sqlite $vector_data_dir/$t.sqlite # If geojson with intersections processed with run_alg_fixgeometries, is converted to sqlite then it will contain intersections again
			set_projection $vector_data_dir/$t.sqlite
			run_alg_polygonstolines $t
			convert2spatialite "$temp_dir/${t}_lines.geojson" "$vector_data_dir/${t}_lines.sqlite"
			cp "$vector_data_dir/$t.geojson" $temp_dir
			run_alg_buffer $t 0.002 "geojson"
			run_alg_buffer ${t}_buffered -0.002 "geojson"
			run_alg_simplifygeometries ${t}_buffered_buffered "geojson" 0 0.0002 "|geometrytype=Polygon"
			sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl.geojson
			run_grass_alg_voronoiskeleton ${t}_buffered_buffered_simpl 20 1 "geojson" "|geometrytype=Polygon"
			if [[ -f "$temp_dir/${t}_buffered_buffered_simpl_skel.geojson" ]] ; then
				run_alg_simplifygeometries ${t}_buffered_buffered_simpl_skel "geojson" 0 0.002
				sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl.geojson
				run_alg_smoothgeometry ${t}_buffered_buffered_simpl_skel_simpl 10 0.25 180 "geojson"
				sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed.geojson
				run_alg_dissolve ${t}_buffered_buffered_simpl_skel_simpl_smoothed "id" "geojson"
				convert2spatialite "$temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed_dissolved.geojson" "$vector_data_dir/${t}_names.sqlite"
				set_projection "$vector_data_dir/${t}_names.sqlite"
			else
				echo -e "\033[93mv.voronoi algorithm returned no data\033[0m"
				if [[ $t == "bay_polygon" ]] ; then
					cp -f $qgis_projects_dir/$override_dir/bay_polygon_names.sqlite $vector_data_dir/bay_polygon_names.sqlite
				fi
			fi
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"strait")
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			sed -i 's/name_/name:/g' $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			if grep -q "type\": \"Polygon" "$vector_data_dir/$t.geojson"; then
				run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $vector_data_dir/$t.geojson && mv $vector_data_dir/${t}_fixed.geojson $vector_data_dir/$t.geojson
				cp "$vector_data_dir/$t.geojson" $temp_dir
				run_alg_buffer $t 0.002 "geojson"
				run_alg_buffer ${t}_buffered -0.002 "geojson"
				run_alg_simplifygeometries ${t}_buffered_buffered "geojson" 0 0.0002 "|geometrytype=Polygon"
				sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl.geojson
				run_grass_alg_voronoiskeleton ${t}_buffered_buffered_simpl 0.25 -1 "geojson" "|geometrytype=Polygon"
				run_alg_simplifygeometries ${t}_buffered_buffered_simpl_skel "geojson" 0 0.0002
				sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl.geojson
				run_alg_smoothgeometry ${t}_buffered_buffered_simpl_skel_simpl 10 0.25 180 "geojson"
				sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed.geojson
				run_alg_extractbylocation ${t}_buffered_buffered_simpl ${t}_buffered_buffered_simpl_skel 2 "geojson" # Extract glaciers than were not skeletonized and run second pass because of v.voronoi.skeleton specifics
				run_alg_dissolve ${t}_buffered_buffered_simpl_skel_simpl_smoothed "id" "geojson"
				run_alg_multiparttosingleparts ${t}_buffered_buffered_simpl_skel_simpl_smoothed_dissolved "geojson"
				convert2spatialite "$temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed_dissolved_parts.geojson" "$vector_data_dir/${t}_skel.sqlite"
				set_projection "$vector_data_dir/${t}_skel.sqlite"
				rm $temp_dir/*.*
			fi
			rm $vector_data_dir/$t.osm
			;;
		"island_node") # should be requested after "island"
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			cp $vector_data_dir/$t.sqlite $temp_dir
			if [[ -f "$vector_data_dir/island.sqlite" ]] ; then
				suffixdiff="_diff"
				cp $vector_data_dir/island.sqlite $temp_dir
				run_alg_difference $t island sqlite
			fi
			convert2spatialite "$temp_dir/${t}$suffixdiff.sqlite" "$vector_data_dir/$t.sqlite"
			set_projection "$vector_data_dir/$t.sqlite"
			rm $vector_data_dir/$t.geojson
			rm $vector_data_dir/$t.osm
			rm $temp_dir/*.*
			;;

		"coastline") # Create ocean polygons and merge it with water polygons. Should be requested after "water","island"
			date
			if [[ $manual_coastline_processing == "true" ]] ; then
				sed 's/<relation/<relation version="1" timestamp="2007-02-14T19:11:58Z"/g' "$vector_data_dir/$t.osm" | sed 's/<way/<way version="1" timestamp="2007-02-14T19:11:58Z"/g' | sed 's/<node/<node version="1" timestamp="2007-02-14T19:11:58Z"/g' > "$vector_data_dir/$t.osm_new" && mv -f "$vector_data_dir/$t.osm_new" "$vector_data_dir/$t.osm"
				read -rsp $'\033[93mManually complete the coastline to a full ocean polygon, avoiding intersections and incorrect geometry. Coastline location: '$vector_data_dir/$t.osm.' Then save it and press any key.' -n1 key
				echo '\n'
			fi
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			if [[ $(wc -c <"$vector_data_dir/$t.sqlite") -ge 70 ]] ; then
				if [[ $manual_coastline_processing == "false" ]] || [[ $manual_coastline_processing == "" ]]; then
					cp $vector_data_dir/$t.sqlite $temp_dir/${t}_tmp.sqlite
					# Prepare coastline
					run_alg_polygonstolines ${t}_tmp "sqlite" "|geometrytype=Polygon"
					if [[ $(wc -c <"$temp_dir/${t}_tmp_lines.sqlite") -ge 25000 ]] ; then
						run_alg_convertgeometrytype ${t}_tmp_lines "sqlite" 2
						merge_vector_layers "sqlite" "LineString" ${t}_tmp_lines_conv ${t}_tmp
						rm -f $temp_dir/${t}_tmp.sqlite && mv $temp_dir/${t}_tmp_lines_conv_merged.sqlite $temp_dir/${t}_tmp.sqlite
					fi
					run_alg_dissolve ${t}_tmp 'natural' "sqlite"
					run_alg_simplifygeometries ${t}_tmp_dissolved "sqlite" 0 0.00005 "|geometrytype=LineString" && mv $temp_dir/${t}_tmp_dissolved_simpl.sqlite $temp_dir/${t}_dissolved_simpl.sqlite
					convert2spatialite "$project_dir/crop.geojson" "$temp_dir/crop.sqlite"
					# Split bbox polygon by coastline (time consuming process)
					time run_alg_splitwithlines "crop" ${t}_dissolved_simpl "sqlite"
					run_alg_fixgeometries crop_split "sqlite" && rm -f $temp_dir/crop_split.sqlite && mv $temp_dir/crop_split_fixed.sqlite $temp_dir/crop_split.sqlite
					# Add single-sided buffer to coastline to determine ocean side
					run_alg_singlesidedbuffer ${t}_dissolved_simpl 0.000001 1 "sqlite"
					run_alg_buffer ${t}_dissolved_simpl_sbuffered -0.00000048 "sqlite"
					run_alg_multiparttosingleparts ${t}_dissolved_simpl_sbuffered_buffered "sqlite"
					run_alg_intersection ${t}_dissolved_simpl_sbuffered_buffered_parts "crop" "sqlite"
					# Extract first vertice from each buffer object
					run_alg_extractspecificvertices ${t}_dissolved_simpl_sbuffered_buffered_parts_intersection 0 "sqlite"
					# Extract ocean from splitted bbox polygon by comparing it with nodes, obtained from single-sided buffer
					run_alg_extractbylocation crop_split ${t}_dissolved_simpl_sbuffered_buffered_parts_intersection_vertices [1,4] "sqlite"
					set_projection $temp_dir/crop_split_extracted.sqlite
					mv "$temp_dir/crop_split_extracted.sqlite" "$temp_dir/ocean.sqlite"
				else
					cp "$vector_data_dir/$t.sqlite" "$temp_dir/$t.sqlite"
					run_alg_dissolve "$t" "" "sqlite" "|geometrytype=LineString"
					run_alg_linestopolygons "${t}_dissolved" "sqlite" "|geometrytype=LineString"
					run_alg_fixgeometries "${t}_dissolved_polygons" "sqlite"
					run_alg_difference "${t}_dissolved_polygons_fixed" "$t" "sqlite" "|geometrytype=Polygon"
					mv "$temp_dir/${t}_dissolved_polygons_fixed_diff.sqlite" "$temp_dir/ocean.sqlite"
				fi
				if [[ -f $vector_data_dir/island.sqlite ]] ; then
					cp $vector_data_dir/island.sqlite $temp_dir
					run_alg_fixgeometries island "sqlite" && rm -f $temp_dir/island.sqlite && mv $temp_dir/island_fixed.sqlite $temp_dir/island.sqlite
					# Substract islands from ocean just in case
					run_alg_difference ocean island "sqlite"
					if [[ $manual_coastline_processing == "false" ]] ; then
						run_alg_difference ocean_diff ${t}_tmp "sqlite" "|geometrytype=Polygon" && rm -f "$temp_dir/ocean_diff.sqlite" && mv "$temp_dir/ocean_diff_diff.sqlite" "$temp_dir/ocean.sqlite"
					else
						mv "$temp_dir/ocean_diff.sqlite" "$temp_dir/ocean.sqlite"
					fi
				fi
				if [[ -f $vector_data_dir/water_dissolved.sqlite ]] ; then
					cp $vector_data_dir/water_dissolved.sqlite $temp_dir
					set_projection $temp_dir/ocean.sqlite
					# Merge ocean with inner water
					merge_vector_layers "sqlite" "Polygon" water_dissolved ocean
					run_alg_fixgeometries water_dissolved_merged "sqlite"
					run_alg_dissolve water_dissolved_merged_fixed 'natural' "sqlite"
					run_alg_multiparttosingleparts water_dissolved_merged_fixed_dissolved "sqlite"
					set_projection $temp_dir/water_dissolved_merged_fixed_dissolved_parts.sqlite
					convert2spatialite "$temp_dir/water_dissolved_merged_fixed_dissolved_parts.sqlite" "$vector_data_dir/water_dissolved.sqlite"
				else
					set_projection "$temp_dir/ocean.sqlite"
					convert2spatialite "$temp_dir/ocean.sqlite" "$vector_data_dir/water_dissolved.sqlite"
				fi
				rm $vector_data_dir/$t.osm
				rm $vector_data_dir/$t.geojson
			fi
			date
			;;
		"highway_main")
			osmium sort -o $vector_data_dir/${t}_sorted.osm $vector_data_dir/$t.osm && rm -f $vector_data_dir/$t.osm && mv $vector_data_dir/${t}_sorted.osm $vector_data_dir/$t.osm
			osmfilter $vector_data_dir/$t.osm --keep-ways-relations="layer>0" -o=$vector_data_dir/${t}_layer_1.osm
			osmfilter $vector_data_dir/$t.osm --keep-ways-relations="layer<0" -o=$vector_data_dir/${t}_layer_-1.osm
			osmfilter $vector_data_dir/$t.osm --drop-ways-relations="layer>0 or layer<0" -o=$vector_data_dir/${t}_new.osm && rm -f $vector_data_dir/$t.osm && mv $vector_data_dir/${t}_new.osm $vector_data_dir/$t.osm
			if [[ $? == 139 ]] ; then
				echo -e "\033[91mSegmentation fault\033[0m"
				make_error_flag
				exit 1
			fi
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			osmtogeojson_wrapper $vector_data_dir/${t}_layer_1.osm $vector_data_dir/${t}_layer_1.geojson
			# Remove orphaned nodes to reduce file size
			cat $vector_data_dir/${t}_layer_1.geojson | jq -c '.features[] | select(.geometry.type == "LineString")' > $vector_data_dir/${t}_layer_1_tmp.geojson && rm $vector_data_dir/${t}_layer_1.geojson && mv $vector_data_dir/${t}_layer_1_tmp.geojson $vector_data_dir/${t}_layer_1.geojson
			jsonlines2json $vector_data_dir/${t}_layer_1
			osmtogeojson_wrapper $vector_data_dir/${t}_layer_-1.osm $vector_data_dir/${t}_layer_-1.geojson
			cat $vector_data_dir/${t}_layer_-1.geojson | jq -c '.features[] | select(.geometry.type == "LineString")' > $vector_data_dir/${t}_layer_-1_tmp.geojson && rm $vector_data_dir/${t}_layer_-1.geojson && mv $vector_data_dir/${t}_layer_-1_tmp.geojson $vector_data_dir/${t}_layer_-1.geojson
			jsonlines2json $vector_data_dir/${t}_layer_-1
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			convert2spatialite "$vector_data_dir/${t}_layer_1.geojson" "$vector_data_dir/${t}_layer_1.sqlite"
			convert2spatialite "$vector_data_dir/${t}_layer_-1.geojson" "$vector_data_dir/${t}_layer_-1.sqlite"
			rm -f $vector_data_dir/$t.osm
			rm -f $vector_data_dir/${t}_layer_1.osm
			rm -f $vector_data_dir/${t}_layer_-1.osm
			rm -f $temp_dir/*.*
			;;
		*)
			osmtogeojson_wrapper $vector_data_dir/$t.osm $vector_data_dir/$t.geojson
			convert2spatialite "$vector_data_dir/$t.geojson" "$vector_data_dir/$t.sqlite"
			rm $vector_data_dir/$t.osm
			rm -f $temp_dir/*.*
			;;
	esac
	((index++))
done
# Following code is needed to split glacier isolines
if [[ $generate_terrain == "true" ]] && [[ $generate_terrain_isolines == "true" ]]; then
	if [[ ! -f $vector_data_dir/isolines_full.sqlite ]] ; then
		echo -e "\033[91m$vector_data_dir/isolines_full.sqlite not found\033[0m"
		make_error_flag
		exit 1
	fi
	rm -f "$vector_data_dir/isolines_glacier.sqlite"
# 	if [[ -f "$vector_data_dir/water.sqlite" ]] && [[ $(stat --printf="%s" "$vector_data_dir/water.sqlite") -ge 70 ]] ; then
# 		echo -e "\e[104m=== Substracting water from isolines...\e[49m"
# 		cp $vector_data_dir/isolines_full.sqlite $temp_dir/isolines_full.sqlite
# 		cp $vector_data_dir/water.sqlite $temp_dir
# 		time run_alg_difference isolines_full "water" "sqlite"
# 		set_projection $temp_dir/isolines_full_diff.sqlite
# 		convert2spatialite "$temp_dir/isolines_full_diff.sqlite" "$temp_dir/isolines_full_tmp.sqlite"
# 	else
	cp $vector_data_dir/isolines_full.sqlite $temp_dir/isolines_full_tmp.sqlite
# 	fi
	if [[ -f "$vector_data_dir/glacier.sqlite" ]] && [[ $(stat --printf="%s" "$vector_data_dir/glacier.sqlite") -ge 70 ]] && [[ $isolines_are_empty != "true" ]] ; then # should be requested after "glacier"
		echo -e "\e[104m=== Splitting isolines by glaciers...\e[49m"
		cp $vector_data_dir/glacier.sqlite $temp_dir
		cp $temp_dir/isolines_full_tmp.sqlite $temp_dir/isolines_gl_tmp.sqlite
		cp $temp_dir/isolines_full_tmp.sqlite $temp_dir/isolines_reg_tmp.sqlite
		run_alg_intersection isolines_gl_tmp "glacier" "sqlite"
		set_projection "$temp_dir/isolines_gl_tmp_intersection.sqlite"
		run_alg_difference isolines_reg_tmp "glacier" "sqlite"
		set_projection "$temp_dir/isolines_reg_tmp_diff.sqlite"
		mv "$temp_dir/isolines_gl_tmp_intersection.sqlite" "$temp_dir/isolines_gl_tmp.sqlite"
		mv "$temp_dir/isolines_reg_tmp_diff.sqlite" "$temp_dir/isolines_reg_tmp.sqlite"
		if [[ $(stat --printf="%s" "$temp_dir/isolines_gl_tmp.sqlite") != $(stat --printf="%s" "$temp_dir/isolines_reg_tmp.sqlite") ]] ; then # if file sizes are not equal
			convert2spatialite "$temp_dir/isolines_gl_tmp.sqlite" "$vector_data_dir/isolines_glacier.sqlite" "isolines_glacier"
		fi
		rm -f "$vector_data_dir/isolines_regular.sqlite"
		convert2spatialite "$temp_dir/isolines_reg_tmp.sqlite" "$vector_data_dir/isolines_regular.sqlite" "isolines_regular"
	fi
fi

rm -f "$temp_dir"/*.*

# Replace project extent by bbox
if [[ -f "$qgis_projects_dir/$project_name/$project_name.qgz" ]] ; then
	echo -e "\e[104mUpdating map extent in QGIS project\e[49m"
	cd $qgis_projects_dir/$project_name
	unzip -o $project_name.qgz
	python3 $scripts_dir/replace_bbox_xml.py -bbox $bbox -file $project_name.qgs
	sed -i "s/automap/${project_name}/" $project_name.qgs
	if [[ -f $project_name.qgd ]] ; then
		zip ${project_name}_tmp.qgz $project_name.qgs $project_name.qgd
	else
		zip ${project_name}_tmp.qgz $project_name.qgs
	fi
	rm -f $project_name.qgs
	if [[ $(wc -c <${project_name}_tmp.qgz) -ge 8000000 ]] ; then
		mv -f ${project_name}_tmp.qgz $project_name.qgz
	else
		echo -e "\033[91mError replacing project extent by bbox\033[0m"
		make_error_flag 8
		exit 1
	fi
fi

echo -e "\e[42m====== Data preparation finished\e[49m"

if [[ $running_in_container == "false" ]] && [[ $(command -v notify-send) == 0 ]]; then
	notify-send "QGIS-topo: data preparation finished"
fi