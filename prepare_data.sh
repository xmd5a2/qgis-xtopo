#!/bin/bash
# Get and prepare OSM / terrain data for QGIS-topo project
# Requirements: qgis >=3.14 with grass plugin, osmtogeojson, gdal
# Place DEM tiles (GeoTIFF/HGT) to project_dir/input_dem or use get_dem_tiles and source_dem_dir variables
#read -rsp $'Press any key to continue...\n' -n1 key
if [ -f /.dockerenv ] ; then
	qgistopo_config_dir=/mnt/qgistopo-config
	if [[ -f ${qgistopo_config_dir}/config.ini ]] ; then
		. ${qgistopo_config_dir}/config.ini
		export XDG_RUNTIME_DIR=/mnt/qgis_projects/$project_name/tmp
	else
		echo -e "\033[93mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
		exit 1;
	fi

	if [[ -f ${qgistopo_config_dir}/config_debug.ini ]] ; then
		. ${qgistopo_config_dir}/config_debug.ini
	fi
	rm -f /tmp/.X99-lock
	Xvfb :99 -ac -noreset &
	export DISPLAY=:99
else
	qgistopo_config_dir=$(pwd)
	if [[ -f ${qgistopo_config_dir}/config.ini ]] ; then
		. ${qgistopo_config_dir}/config.ini
	else
		echo -e "\033[93mconfig.ini not found. Executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
		exit 1;
	fi
	if [[ -f ${qgistopo_config_dir}/config_debug.ini ]] ; then
		. ${qgistopo_config_dir}/config_debug.ini
	fi
fi

echo -e "\e[105mProject: $project_dir\e[49m"
echo -e "\e[100mconfig: ${qgistopo_config_dir}/config.ini\e[49m"

if [[ "$project_name" == "" ]] ; then
	echo -e "\033[93mproject_name not defined. Please define it in config.ini. Stopping.\033[0m"
	exit 1;
fi
if [[ ! -d "$project_dir" ]] && [[ $running_in_container == true ]] ; then
	echo -e "\033[93mproject_dir $project_dir not found. Please check config.ini (project_name and project_dir variables) and directory itself. Also executing of initialization script (docker_run) can solve this. Stopping.\033[0m"
	exit 1;
fi
if [[ ! -d "$project_dir" ]] && [[ $running_in_container == false ]] ; then
	echo -e "\033[93mproject_dir $project_dir not found. Please check config.ini (project_name and project_dir variables) and directory itself. Stopping.\033[0m"
	exit 1;
fi
case $overpass_instance in
	"docker")
		if [[ $running_in_container == true ]] ; then
			req_path_string="/app/osm-3s/bin/osm3s_query --quiet --db-dir=/mnt/overpass_db"
		else
			echo -e "\033[93moverpass_instance=docker can't be started outside of container. Please use overpass_instance=external/local/ssh. Stopping.\033[0m" && exit 1;
		fi
		;;
	"local")
		if [[ $running_in_container == false ]] ; then
			IFS=' ' read -r -a array_bbox <<< "$overpass_endpoint_local"
			if [[ -f "${array_bbox[0]}" ]] ; then
				req_path_string=$overpass_endpoint_local
			else
				echo -e "\033[93m${array_bbox[0]} not found. Check overpass_endpoint_local. Stopping.\033[0m"
				exit 1;
			fi
		else
			echo -e "\033[93moverpass_instance=local can't be started inside a container. Please use overpass_instance=external/docker/ssh. Stopping.\033[0m" && exit 1;
		fi
		;;
	"ssh")
		if [[ $running_in_container == false ]] ; then
			req_path_string="$overpass_endpoint_ssh --quiet"
		else
			echo -e "\033[93moverpass_instance=ssh can't be started inside a container. Please use overpass_instance=docker/external/local. Stopping.\033[0m" && exit 1;
		fi
		;;
esac

override_dir=$qgis_projects_dir/$override_dir
merged_dem="$project_dir/merged_dem.tif"

if [[ $running_in_container == false ]] ; then
	mkdir -p "$project_dir"
	mkdir -p "$work_dir"
	mkdir -p "$override_dir"
	mkdir -p "$osm_data_dir"
	mkdir -p "$temp_dir"
else
	cd /app
fi
if [[ -d "$temp_dir" ]] ; then
	rm -f "$temp_dir"/*.*
fi
# rm -f $project_dir/log.txt
# exec > >(tee -a $project_dir/log.txt)

IFS=',' read -r -a array_bbox <<< "$bbox"
lon_min=${array_bbox[0]}
lat_min=${array_bbox[1]}
lon_max=${array_bbox[2]}
lat_max=${array_bbox[3]}

if (( $(echo "$lon_min > $lon_max" | bc -l) )) || (( $(echo "$lat_min > $lat_max" | bc -l) )) || \
	(( $(echo "$lat_min > 90" | bc -l) )) || (( $(echo "$lat_min < -90" | bc -l) )) ; then
	echo -e "\033[93mInvalid bbox format. Use left,bottom,right,top (lon_min,lat_min,lon_max,lat_max)\033[0m"
	exit 1;
fi
bbox_query=$lat_min,$lon_min,$lat_max,$lon_max

command -v osmtogeojson >/dev/null 2>&1 || { echo >&2 -e "\033[93mosmtogeojson is required but not installed. Follow installation instructions at https://github.com/tyrasd/osmtogeojson\033[0m"; sleep 60 && exit 1;}
command -v gdalwarp >/dev/null 2>&1 || { echo >&2 -e "\033[93mGDAL is required but not installed. If you are using Ubuntu please install 'gdal-bin' package.\033[0m"; sleep 60 && exit 1;}
command -v grass >/dev/null 2>&1 || { echo >&2 -e "\033[93mGRASS > 7.0 is required but not installed.\033[0m"; sleep 60 && exit 1;}
command -v osmfilter >/dev/null 2>&1 || { echo >&2 -e "\033[93mosmfilter is required but not installed. If you are using Ubuntu please install 'osmctools' package.\033[0m"; sleep 60 && exit 1;}
command -v osmconvert >/dev/null 2>&1 || { echo >&2 -e "\033[93mosmconvert is required but not installed. If you are using Ubuntu please install 'osmctools' package.\033[0m"; sleep 60 && exit 1;}
command -v osmium >/dev/null 2>&1 || { echo >&2 -e "\033[93mosmium is required but not installed. If you are using Ubuntu please install 'osmium-tool' package.\033[0m"; sleep 60 && exit 1;}
command -v jq >/dev/null 2>&1 || { echo >&2 -e "\033[93mjq is required but not installed. If you are using Ubuntu please install 'jq' package.\033[0m"; sleep 60 && exit 1;}

function run_alg_linestopolygons {
	python3 $(pwd)/run_alg.py \
		-alg "qgis:linestopolygons" \
		-param1 INPUT -value1 "$temp_dir/$1.geojson" \
		-param2 OUTPUT -value2 $temp_dir/${1}_polygonized.geojson
}
function osmtogeojson_wrapper {
	mem=$(echo $(($(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) / (1024 * 1024))))
	node_mem=$(bc<<<$mem*2/3)
	node --max_old_space_size=$node_mem `which osmtogeojson` $1 > $2
}
function convert2spatialite {
	if [ -f $2 ] ; then
		rm $2
	fi
	if [ $3 ] ; then
		layername_string="-nln $3"
	else layername_string=""
	fi
	ogr2ogr -dsco SPATIALITE=YES -lco COMPRESS_GEOM=YES -f SQLite $layername_string $2 $1 # $osm_string
}
function jsonlines2json { # Convert JSON lines to JSON
	jq -s '.' $1.geojson > ${1}_tmp.geojson
	mv -f ${1}_tmp.geojson $1.geojson
	sed -i '1s/\[/{\"type\": \"FeatureCollection\",\"features\":[/' $1.geojson
	sed -i '$ s/\]/\]}/' $1.geojson
}

echo -e "\e[100mbbox:" $bbox_query"\e[49m"

rm -f "$project_dir"/crop.geojson
echo '{ "type": "FeatureCollection","name": "crop","crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },
"features": [ { "type": "Feature", "properties": { "properties": null }, "geometry": { "type": "MultiPolygon", "coordinates": [ [ [ [ {lon_min}, {lat_max} ], [ {lon_max}, {lat_max} ], [ {lon_max}, {lat_min} ], [ {lon_min}, {lat_min} ], [ {lon_min}, {lat_max} ] ] ] ] } } ] }' >> "$project_dir"/crop.geojson

sed -i s/{lon_min}/$lon_min/g "$project_dir"/crop.geojson
sed -i s/{lon_max}/$lon_max/g "$project_dir"/crop.geojson
sed -i s/{lat_min}/$lat_min/g "$project_dir"/crop.geojson
sed -i s/{lat_max}/$lat_max/g "$project_dir"/crop.geojson

if [[ $generate_terrain == "true" ]] ; then
	rm -f "$merged_dem"
	IFS=' ' read -r -a tiles_list <<< $(python3 $(pwd)/calc_srtm_tiles_list.py -bbox "$bbox")
	echo -e "\e[100mDEM tiles list: ${tiles_list[@]}\e[49m"
	if [[ $get_dem_tiles == "true" ]] ; then
		rm -f $dem_dir/*.*
		echo -e "\e[104m=== Copying DEM tiles from $source_dem_dir...\e[49m"
		if [[ $source_dem_dir == "" ]] ; then
			echo -e "\033[93msource_dem_dir "$source_dem_dir" not defined in config but get_dem_tiles=true. Stopping.\033[0m"
			exit 1;
		fi
		if [[ ! -d $source_dem_dir ]] ; then
			echo -e "\033[93msource_dem_dir "$source_dem_dir" don't exist but get_dem_tiles=true. Turn it off or check path. Stopping.\033[0m"
			if [[ $running_in_container == true ]] ; then
				echo -e "\033[93mCheck terrain_dir in docker_run script\033[0m"
			fi
			exit 1;
		fi
		for tile in "${tiles_list[@]}"
		do
			if [[ -f "$source_dem_dir/${tile}.tif" ]] ; then
				echo "${tile}.tif found"
				cp $source_dem_dir/${tile}.tif $dem_dir
				continue
			elif [[ -f "$source_dem_dir/${tile}.zip" ]] ; then
				echo "${tile}.zip found"
				cp $source_dem_dir/${tile}.zip $dem_dir
				continue
			elif [[ -f "$source_dem_dir/${tile}.hgt" ]] ; then
				echo "${tile}.hgt found"
				cp $source_dem_dir/${tile}.hgt $dem_dir
				continue
			else
				echo -e "\033[93m${tile}.tif not found. Possible cause: no data\033[0m"
			fi
		done
	fi
	CUTLINE_STRING="-crop_to_cutline -cutline "$project_dir"/crop.geojson"

	# Extract and convert *.hgt.zip/*.hgt to GeoTIFF format
	shopt -s nullglob
	for f in "$dem_dir"/*.zip; do
		[ -e "$f" ] && unzip -o $f -d "$dem_dir" && rm $f
	done
	for f in "$dem_dir"/*.hgt; do
		[ -e "$f" ] && gdalwarp -of GTiff $f ${f%.*}.tif && rm $f
	done
	for f in "$dem_dir"/*.tif; do
		[ ! -e "$f" ] && echo -e "\033[93mNo DEM tiles (GeoTIFF/HGT) found in "$dem_dir". Add them manually or use get_dem_tiles=true option. Stopping.\033[0m" && exit 1;
		break;
	done
	shopt -u nullglob
	echo -e "\e[104m=== Merging DEM tiles...\e[49m"
	if ls $dem_dir/*.tif 1> /dev/null 2>&1 ; then
		rm -f "$project_dir"/input_dem.vrt
		gdalbuildvrt "$project_dir"/input_dem.vrt $dem_dir/*.tif
		gdalwarp -of GTiff -co "COMPRESS=LZW" $CUTLINE_STRING "$project_dir"/input_dem.vrt "$merged_dem"
		rm -f "$project_dir"/input_dem.vrt
		rm -f "$project_dir/merged_dem_3857.tif"
		gdalwarp -t_srs EPSG:3857 -ot Float32 -co "COMPRESS=LZW" "$merged_dem" "$project_dir"/merged_dem_3857.tif # for mountain_pass layer "ele_calc" attribute

		if [[ $generate_terrain_hillshade_slope == "true" ]] ; then
			echo -e "\e[104m=== Generating slopes...\e[49m"
			rm -f "$project_dir/slope_upscaled.tif"
			gdaldem slope -compute_edges -s 111120 "$project_dir"/merged_dem.tif "$project_dir"/slope.tif
			gdal_calc.py -A "$project_dir"/slope.tif --type=Float32 --co COMPRESS=LZW --outfile="$project_dir"/slope_cut.tif --calc="A*(A>1.04)" --overwrite
			size_str=$(gdalinfo "$project_dir"/slope_cut.tif | grep "Size is" | sed 's/Size is //g')
			width=$(echo $size_str | sed 's/,.*//')
			height=$(echo $size_str | sed 's/.*,//')
			width_mod=$(( $width * 3 ))
			height_mod=$(( $height * 3 ))
			echo "Resizing from $width x$height"
			rm -f "$project_dir/slope_upscaled.tif.ovr"
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$project_dir"/slope_cut.tif "$project_dir"/slope_upscaled.tif
			if [[ -f "$project_dir/slope_upscaled.tif" ]] && [[ $(wc -c <"$project_dir/slope_upscaled.tif") -ge 100000 ]] ; then
				echo "Generating slope overviews"
				gdaladdo -ro --config COMPRESS_OVERVIEW LZW "$project_dir/slope_upscaled.tif" 512 256 128 64 32 16 8 4 2
				echo "Slopes generated"
			else
				echo -e "\033[93mError. $project_dir/slope_upscaled.tif is empty.\033[0m"
				exit 1;
			fi
			rm -f "$project_dir"/slope.tif
			rm -f "$project_dir"/slope_cut.tif

			echo -e "\e[104m=== Generating hillshade...\e[49m"
			size_str=$(gdalinfo "$project_dir"/merged_dem.tif | grep "Size is" | sed 's/Size is //g')
			width=$(echo $size_str | sed 's/,.*//')
			height=$(echo $size_str | sed 's/.*,//')
			width_mod=$(( $width / 2 ))
			height_mod=$(( $height / 2 ))
			echo "Resizing from $width x$height"
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$project_dir"/merged_dem.tif "$project_dir"/merged_dem_downscaled.tif
			width_mod=$(( $width * 3 ))
			height_mod=$(( $height * 3 ))
			gdalwarp -overwrite -ts $width_mod $height_mod -r $terrain_resample_method -co "COMPRESS=LZW" -co "BIGTIFF=YES" -ot Float32 "$project_dir"/merged_dem_downscaled.tif "$project_dir"/merged_dem_upscaled.tif
			gdaldem hillshade -z 2 -s 111120 -compute_edges -multidirectional -alt 70 -co "COMPRESS=LZW" -co "BIGTIFF=YES" "$project_dir"/merged_dem_upscaled.tif "$project_dir"/hillshade.tif
			echo "0 255 255 255
	90 0 0 0" > "$project_dir"/color_slope.txt
			gdaldem color-relief "$project_dir"/slope_upscaled.tif "$project_dir"/color_slope.txt "$project_dir"/slope_shade.tif
			gdal_calc.py -A "$project_dir"/hillshade.tif -B "$project_dir"/slope_shade.tif --A_band=1 --type=Float32 --co COMPRESS=LZW --outfile="$project_dir"/hillshade_composite.tif --calc="A+(B*0.5)" --overwrite
	# 		# Normalize hillshade raster
	# 		stat_max_hillshade=$(gdalinfo -stats "$project_dir"/hillshade_composite.tif | sed -ne 's/.*STATISTICS_MAXIMUM=//p')
	# 		stat_mean_hillshade=$(gdalinfo -stats "$project_dir"/hillshade_composite.tif | sed -ne 's/.*STATISTICS_MEAN=//p')
	# 		stat_min_hillshade_calc=$(bc<<<2.5*$stat_mean_hillshade-1.5*$stat_max_hillshade)
	# 		if (( $(echo "$stat_min_hillshade_calc < 0" |bc -l) )); then
	# 			stat_min_hillshade_calc=0
	# 		fi
	# 		echo "$stat_min_hillshade_calc 0 0 0
	# 376 255 255 255" > "$project_dir"/color_hillshade.txt
	# 		gdaldem color-relief "$project_dir"/hillshade_composite.tif "$project_dir"/color_hillshade.txt "$project_dir"/hillshade_composite_tmp.tif && rm -f "$project_dir"/hillshade_composite.tif && mv "$project_dir"/hillshade_composite_tmp.tif "$project_dir"/hillshade_composite.tif

			rm -f "$project_dir"/hillshade.tif
			rm -f "$project_dir"/slope_shade.tif
			rm -f "$project_dir"/merged_dem_downscaled.tif
			rm -f "$project_dir"/color_slope.txt
			rm -f "$project_dir/hillshade_composite.tif.ovr"
			if [[ -f "$project_dir/hillshade_composite.tif" ]] && [[ $(wc -c <"$project_dir/hillshade_composite.tif") -ge 100000 ]] ; then
				echo "Generating hillshade overviews"
				gdaladdo -ro --config COMPRESS_OVERVIEW LZW "$project_dir/hillshade_composite.tif" 512 256 128 64 32 16 8 4 2
				echo "Hillshade generated"
			else
				echo -e "\033[93mError. $project_dir/hillshade_composite.tif is empty.\033[0m"
				exit 1;
			fi
		fi

		if [[ $generate_terrain_isolines == "true" ]] ; then
			rm -f "$project_dir"/isolines_full.sqlite
			echo -e "\e[104m=== Generating isolines...\e[49m"
			if [ $smooth_isolines == "true" ] ; then
				isolines_source="merged_dem_upscaled.tif"
			else
				isolines_source="merged_dem.tif"
			fi
			gdal_contour -b 1 -a ELEV -i $isolines_step -f "ESRI Shapefile" -nln "isolines" "$project_dir/$isolines_source" "$project_dir/isolines_full.shp"
			# SHP step is needed because of ERROR 1: Cannot insert feature with geometry of type LINESTRINGZ in column GEOMETRY. Type LINESTRING expected
			#  when exporting directly to spatialite
			convert2spatialite "$project_dir/isolines_full.shp" "$project_dir/isolines_full.sqlite"
			rm -f "$project_dir/isolines_regular.sqlite"
			cp "$project_dir/isolines_full.sqlite" "$project_dir/isolines_regular.sqlite"
			rm -f "$project_dir/isolines_full.shp"
			rm -f "$project_dir/isolines_full.shx"
			rm -f "$project_dir/isolines_full.dbf"
			rm -f "$project_dir/isolines_full.prj"
			if [[ -f "$project_dir/isolines_full.sqlite" ]] && [[ $(wc -c <"$project_dir/isolines_full.sqlite") -ge 100000 ]] ; then
				echo "Isolines generated"
			else
				echo -e "\033[93mError. $project_dir/isolines_full.sqlite is empty.\033[0m"
				exit 1;
			fi
		fi
	else
		echo -e "\033[93mWarning! No DEM data found. Hillshade, slopes and isolines are not generated.\033[0m"
		sleep 5;
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
# function run_alg_union {
# 	case $3 in
# 		"geojson")
# 			ext="geojson"
# 			;;
# 		"sqlite")
# 			ext="sqlite"
# 			;;
# 		*)
# 			ext="geojson"
# 			;;
# 	esac
# 	python3 $(pwd)/run_alg.py \
# 		-alg "native:union" \
# 		-param1 INPUT -value1 $temp_dir/${1}.$ext \
# 		-param2 OVERLAY -value2 $temp_dir/${2}.$ext$4$5 \
# 		-param3 OUTPUT -value3 $temp_dir/${1}_union.$ext
# }
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
	if [ $7 ] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype,$temp_dir/$6.$ext$geometrytype,$temp_dir/$7.$ext$geometrytype"
	elif [ $6 ] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype,$temp_dir/$6.$ext$geometrytype"
	elif [ $5 ] ; then
		str="$temp_dir/$3.$ext$geometrytype,$temp_dir/$4.$ext$geometrytype,$temp_dir/$5.$ext$geometrytype"
	elif [ $4 ] ; then
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
	python3 $(pwd)/run_alg.py \
		-alg "native:buffer" \
		-param1 INPUT -value1 $temp_dir/$1.$ext \
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
# function run_alg_rasterize {
# 	python3 $(pwd)/run_alg.py \
# 		-alg "gdal:rasterize" \
# 		-param1 INPUT -value1 $temp_dir/$1.geojson \
# 		-param2 WIDTH -value2 $2 \
# 		-param3 HEIGHT -value3 $3 \
# 		-param4 DATA_TYPE -value4 1 \
# 		-param5 UNITS -value5 0 \
# 		-param6 INVERT -value6 True \
# 		-param7 INIT -value7 1 \
# 		-param8 EXTENT -value8 $bbox_rasterize \
# 		-param9 NODATA -value9 0 \
# 		-param10 OUTPUT -value10 $temp_dir/${1}_rast.tiff
# }
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
# function run_alg_extractbyattribute {
# 	python3 $(pwd)/run_alg.py \
# 		-alg "native:extractbyattribute" \
# 		-param1 INPUT -value1 $temp_dir/$1.geojson \
# 		-param2 FIELD -value2 $2 \
# 		-param3 OPERATOR -value3 $3 \
# 		-param4 VALUE -value4 $4 \
# 		-param5 OUTPUT -value5 $temp_dir/${1}_exploded.geojson
# }
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
			dir=$work_dir
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
			echo -e "\e[100mUsing internal (docker) overpass instance\e[49m"
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
	if [ -f $work_dir/$t.sqlite ] ; then
		rm $work_dir/$t.sqlite
	fi
	if [[ ! -f $(pwd)/queries/$t.txt ]] ; then
		echo -e "\033[93mqueries/$t.txt not found.\033[0m"
	fi
	query=$(cat $(pwd)/queries/$t.txt)
	req_string_query='[out:xml][timeout:3600][maxsize:2000000000][bbox:'$bbox_query'];'$query
	req_string_out="out body;>;out skel qt;"
	echo -e "\e[92m=== ($index / ${#array_queries[@]}) Downloading ${t}...\e[39m"
	if [[ $overpass_instance == external ]] ; then
		req_string=$overpass_endpoint_external'?data='$req_string_query$req_string_out
		wget -O $work_dir/$t.osm -t 1 --timeout=3600 --remote-encoding=utf-8 --local-encoding=utf-8 "$req_string"
	else
		req_string=$req_string_query$req_string_out
		echo "$req_string" | $req_path_string > $work_dir/$t.osm
	fi
	if [[ $? != 0 ]] ; then
		echo -e "\033[93mOverpass server error. Stopping.\033[0m"
		exit 1;
	fi
	if ! grep -q "tag k" "$work_dir/$t.osm" || ( ( [[ $t == "admin_level_2" ]] || [[ $t == "admin_level_4" ]] ) && ! grep -q "way id" "$work_dir/$t.osm" ); then
		echo -e "\033[93mResult is empty!\033[0m"
		rm -f "$work_dir/$t.osm"
		if [[ $debug_copy_from_override_dir == true ]] ; then
			echo "Copying from override dir..."
			if [[ -f "$override_dir/$t.osm" ]] ; then
				cp $override_dir/$t.osm $work_dir/$t.osm
			else
				echo -e "\033[93m$override_dir/$t.osm not found. Stopping.\033[0m"
				exit 1;
			fi
		else
			((index++))
			continue
		fi

	fi
	if grep -q \</osm\> "$work_dir/$t.osm" ; then
		echo -e "\033[92mOK\033[0m"
	else
		echo -e "\033[93m$work_dir/$t.osm is incomplete. It looks like overpass server has interrupted the transmission. Try again or use another server (overpass_instance and overpass_endpoint_* variables in config.ini). Stopping.\033[0m"
		exit 1;
	fi

	case $t in
		"railway_all")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			run_alg_reprojectlayer ${t}_exploded
			mv -f $temp_dir/${t}_exploded_reproj.geojson $work_dir/${t}_exploded.geojson
			convert2spatialite "$work_dir/${t}_exploded.geojson" "$work_dir/${t}_exploded.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"ridge" | "railway_all")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			run_alg_reprojectlayer ${t}_exploded
			mv -f $temp_dir/${t}_exploded_reproj.geojson $work_dir/${t}_exploded.geojson
			convert2spatialite "$work_dir/${t}_exploded.geojson" "$work_dir/${t}_exploded.sqlite"

			run_alg_simplifygeometries $t "geojson" 0 0.0007
			run_alg_smoothgeometry ${t}_simpl 10 0.25 180 "geojson"
			run_alg_dissolve ${t}_simpl_smoothed "name" "geojson"
			convert2spatialite "$temp_dir/${t}_simpl_smoothed_dissolved.geojson" "$work_dir/${t}_simpl_smoothed.sqlite" "ridge_names_smoothed"
			cp $temp_dir/${t}_simpl_smoothed_dissolved.geojson $work_dir/${t}_simpl_smoothed.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"water" | "water_intermittent") # "coastline" should be requested after "water" to be correct
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $work_dir/$t.geojson && mv $work_dir/${t}_fixed.geojson $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_fixgeometries $t "geojson" "|geometrytype=Polygon"
			run_alg_dissolve ${t}_fixed "" "geojson" "|geometrytype=Polygon"
			run_alg_multiparttosingleparts ${t}_fixed_dissolved "geojson"
			cp "$temp_dir/${t}_fixed_dissolved_parts.geojson" "$work_dir/${t}_dissolved.geojson"
			convert2spatialite "$temp_dir/${t}_fixed_dissolved_parts.geojson" "$work_dir/${t}_dissolved.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"allotments")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			run_alg_multiparttosingleparts ${t}_dissolved
			mv -f $temp_dir/${t}_dissolved_parts.geojson $work_dir/${t}_dissolved.geojson # for outline
			convert2spatialite "$work_dir/${t}_dissolved.geojson" "$work_dir/${t}_dissolved.sqlite"
			run_alg_buffer $t 0.001
			run_alg_dissolve ${t}_buffered
			run_alg_multiparttosingleparts ${t}_buffered_dissolved
			mv -f $temp_dir/${t}_buffered_dissolved_parts.geojson $work_dir/${t}_buffered_dissolved.geojson
			convert2spatialite "$work_dir/${t}_buffered_dissolved.geojson" "$work_dir/${t}_buffered_dissolved.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"water_without_riverbanks")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			rm $work_dir/$t.osm
			;;
		"river" | "river_intermittent") # should be requested after "water_without_riverbanks"
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			if [ -f $work_dir/water_dissolved.geojson ] ; then
				cp $work_dir/water_without_riverbanks.geojson $temp_dir
				run_alg_difference $t "water_without_riverbanks"
				mv $temp_dir/${t}_diff.geojson $temp_dir/$t.geojson
			fi
			cp $temp_dir/$t.geojson $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			run_alg_simplifygeometries $t "geojson" 0 0.001 "|geometrytype=Linestring"
			run_alg_smoothgeometry ${t}_simpl 5 0.25 180 "geojson" "|geometrytype=LineString"
			convert2spatialite "$temp_dir/${t}_simpl_smoothed.geojson" "$work_dir/${t}_smoothed.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"stream_intermittent") # should be requested after "water"
			osmtogeojson $work_dir/$t.osm > $temp_dir/$t.geojson
			if [ -f $work_dir/water_dissolved.geojson ] ; then
				cp $work_dir/water_dissolved.geojson $temp_dir
				run_alg_difference $t "water_dissolved"
				cp $temp_dir/${t}_diff.geojson $work_dir/$t.geojson
				convert2spatialite "$temp_dir/${t}_diff.geojson" "$work_dir/$t.sqlite"
			else
				convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			fi
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"stream") # should be requested after "water" and "stream_intermittent"
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			cp $work_dir/$t.sqlite $temp_dir
			if [ -f $work_dir/water_dissolved.geojson ] ; then
				suffixdiff="_diff"
				cp $work_dir/water_dissolved.geojson $temp_dir
				run_alg_difference $t "water_dissolved"
			fi
			if [ -f $work_dir/stream_intermittent.geojson ] ; then
				suffixmerged="_merged"
				cp $work_dir/stream_intermittent.geojson $temp_dir
				merge_vector_layers "geojson" "LineString" ${t}$suffixdiff "stream_intermittent" # for names
			fi
			convert2spatialite "$temp_dir/${t}${suffixdiff}${suffixmerged}.geojson" "$work_dir/${t}${suffixmerged}.sqlite"
			run_alg_smoothgeometry ${t}${suffixdiff} 5 0.25 180 "geojson" "|geometrytype=LineString"
			convert2spatialite "$temp_dir/${t}${suffixdiff}_smoothed.geojson" "$work_dir/${t}_smoothed.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"admin_level_2")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			run_alg_dissolve ${t}_lines
			cp $temp_dir/${t}_lines_dissolved.geojson $work_dir/${t}_dissolved.geojson
			convert2spatialite "$temp_dir/${t}_lines_dissolved.geojson" "$work_dir/${t}_dissolved.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
#			rm $temp_dir/*.*
			;;
		"admin_level_4") # should be requested after "admin_level_2"
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			if [ -f $work_dir/admin_level_2_dissolved.geojson ] ; then
				mv $work_dir/admin_level_2_dissolved.geojson $temp_dir
				run_alg_difference ${t}_lines "admin_level_2_dissolved"
			else mv $temp_dir/${t}_lines.geojson $temp_dir/${t}_lines_diff.geojson
			fi
			run_alg_dissolve ${t}_lines_diff
			cp "$temp_dir/${t}_lines_diff_dissolved.geojson" "$work_dir/${t}_proc.geojson"
			convert2spatialite "$temp_dir/${t}_lines_diff_dissolved.geojson" "$work_dir/${t}_proc.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_admin_centre_6" | "place_admin_centre_4" | "place_admin_centre_2")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_pyfieldcalc $t admin_centre_${t: -1} 2 "value='true'"
			if [ -f $work_dir/places_main.geojson ] ; then
				cp $work_dir/places_main.geojson $temp_dir
			else
				echo -e "\033[93m$work_dir/places_main.geojson not found. Please request places_main before $t. Stopping.\033[0m"
				exit 1;
			fi
			run_alg_joinattributesbylocation places_main ${t}_pyfieldcalc 2 "admin_centre_${t: -1}" 0
			cp -f $temp_dir/places_main_joinattrsloc.geojson $work_dir/places_main.geojson
			convert2spatialite "$work_dir/places_main.geojson" "$work_dir/places_main.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_of_worship_muslim" | "place_of_worship_hindu" | "place_of_worship_buddhist" | "place_of_worship_shinto" | "place_of_worship_jewish" | "place_of_worship_taoist" | "place_of_worship_sikh" | "place_of_worship_other" | "sinkhole_polygon" | "alpine_hut" | "wilderness_hut" | "memorial" | "monument" | "tower_communication" | "monastery_no_religion" | "barrier_border_control" | "cape" | "chimney" | "water_tower" | "volcano" | "volcano_dirt")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $work_dir/${t}_centroids.geojson
			convert2spatialite "$work_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			;;
		"monastery_christian")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			run_alg_buffer ${t}_centroids 0.0025
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			cp $temp_dir/${t}_centroids_buffered.geojson $work_dir/${t}_centroids_buffered.geojson
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			;;
		"place_of_worship_christian" | "place_of_worship_christian_ruins") # should be requested after "monastery_christian" and "prison"
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			if [ -f "$work_dir/prison.geojson" ] ; then
				cp "$work_dir/prison.geojson" $temp_dir
				run_alg_difference ${t}_centroids "prison"
				mv $temp_dir/${t}_centroids_diff.geojson $temp_dir/${t}_centroids.geojson
			fi
			if [ -f "$work_dir/monastery_christian_centroids_buffered.geojson" ] ; then
				cp $work_dir/monastery_christian_centroids_buffered.geojson $temp_dir/
				run_alg_difference ${t}_centroids "monastery_christian_centroids_buffered"
				run_alg_multiparttosingleparts ${t}_centroids
				run_alg_multiparttosingleparts ${t}_centroids_diff
				convert2spatialite "$temp_dir/${t}_centroids_parts.geojson" "$work_dir/${t}_centroids.sqlite"
				convert2spatialite "$temp_dir/${t}_centroids_diff_parts.geojson" "$work_dir/${t}_centroids_diff_parts.sqlite"
			else
				convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
				rm -f "$work_dir/${t}_centroids_diff_parts.sqlite" && cp "$work_dir/${t}_centroids.sqlite" "$work_dir/${t}_centroids_diff_parts.sqlite"
			fi
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			;;
		"camp_site" | "attraction" | "ruins" | "track_bridge" | "aerodrome" | "castle" | "archaeological_site" | "observatory" | "picnic_site" | "tower_cooling")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $work_dir/${t}_centroids.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			rm $work_dir/$t.osm
			;;
		"waterfall" | "weir")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_polygonstolines $t
			run_alg_centroids $t
			cp $temp_dir/${t}_centroids.geojson $work_dir/${t}_centroids.geojson
			merge_vector_layers "geojson" "LineString" ${t}_lines $t
			cp $temp_dir/${t}_lines_merged.geojson $work_dir/${t}_lines.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			convert2spatialite "$temp_dir/${t}_lines_merged.geojson" "$work_dir/${t}_lines.sqlite"
			rm $work_dir/$t.osm
			;;
		"highway_main_bridge")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			run_alg_dissolve $t 'highway'
			cp $temp_dir/${t}_centroids.geojson $work_dir/${t}_centroids.geojson
			cp $temp_dir/${t}_dissolved.geojson $work_dir/${t}_dissolved.geojson
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			convert2spatialite "$temp_dir/${t}_dissolved.geojson" "$work_dir/${t}_dissolved.sqlite"
			rm $work_dir/$t.osm
			;;
		"power_line")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			run_alg_multiparttosingleparts ${t}_dissolved
			convert2spatialite "$temp_dir/${t}_dissolved_parts.geojson" "$work_dir/${t}_dissolved.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"ford" | "railway_stop_names")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_centroids $t
			convert2spatialite "$temp_dir/${t}_centroids.geojson" "$work_dir/${t}_centroids.sqlite"
			run_alg_buffer $t 0.006
			run_alg_dissolve ${t}_buffered 'name'
			run_alg_centroids ${t}_buffered_dissolved
			convert2spatialite "$temp_dir/${t}_buffered_dissolved_centroids.geojson" "$work_dir/${t}_buffered_dissolved_centroids.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"railway_station_icons" | "railway_halt_icons")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_buffer $t 0.004
			run_alg_dissolve ${t}_buffered 'name'
			run_alg_centroids ${t}_buffered_dissolved
			run_alg_reprojectlayer ${t}_buffered_dissolved_centroids
			convert2spatialite "$temp_dir/${t}_buffered_dissolved_centroids_reproj.geojson" "$work_dir/${t}_buffered_dissolved_centroids.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"mountain_pass")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_reprojectlayer $t
			convert2spatialite "$temp_dir/${t}_reproj.geojson" "$work_dir/${t}_reproj.sqlite"
			cp "$temp_dir/${t}_reproj.geojson" $work_dir
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"building_train_station")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			run_alg_fixgeometries $t "geojson" "" "|geometrytype=Polygon" && rm -f $temp_dir/$t.geojson && mv $temp_dir/${t}_fixed.geojson $temp_dir/$t.geojson
			run_alg_centroids $t
			run_alg_reprojectlayer ${t}_centroids
			convert2spatialite "$temp_dir/${t}_centroids_reproj.geojson" "$work_dir/${t}_reproj.sqlite"
			cp "$temp_dir/${t}_centroids_reproj.geojson" "$work_dir/${t}_reproj.geojson"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"place_locality_node")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			if [[ -f "$work_dir/place_locality_way.geojson" ]] ; then
				cp $work_dir/place_locality_way.geojson $temp_dir
				run_alg_difference $t "place_locality_way"
				convert2spatialite "$temp_dir/${t}_diff.geojson" "$work_dir/${t}_diff.sqlite"
				cp "$temp_dir/${t}_diff.geojson" "$work_dir/${t}_diff.geojson"
			else
				convert2spatialite "$temp_dir/$t.geojson" "$work_dir/${t}_diff.sqlite"
				cp "$temp_dir/$t.geojson" "$work_dir/${t}_diff.geojson"
			fi
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"route_hiking")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_explodelines $t
			convert2spatialite "$temp_dir/${t}_exploded.geojson" "$work_dir/${t}_exploded.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"military")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.geojson $temp_dir
			run_alg_dissolve $t
			cp $temp_dir/${t}_dissolved.geojson $work_dir
			convert2spatialite "$temp_dir/${t}_dissolved.geojson" "$work_dir/${t}_dissolved.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;
		"glacier" | "bay_polygon" | "wetland" | "wood")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			sed -i 's/name_/name:/g' $work_dir/$t.geojson
			run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $work_dir/$t.geojson && mv $work_dir/${t}_fixed.geojson $work_dir/$t.geojson
			cp $work_dir/$t.geojson $temp_dir
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			run_alg_fixgeometries $t "sqlite" "workdir" "|geometrytype=Polygon" && rm -f $work_dir/$t.sqlite && mv $work_dir/${t}_fixed.sqlite $work_dir/$t.sqlite # If geojson with intersections processed with run_alg_fixgeometries, is converted to sqlite then it will contain intersections again
			set_projection $work_dir/$t.sqlite
			run_alg_polygonstolines $t
			convert2spatialite "$temp_dir/${t}_lines.geojson" "$work_dir/${t}_lines.sqlite"
			cp "$work_dir/$t.geojson" $temp_dir
			run_alg_buffer $t 0.002 "geojson"
			run_alg_buffer ${t}_buffered -0.002 "geojson"
			run_alg_simplifygeometries ${t}_buffered_buffered "geojson" 0 0.0002 "|geometrytype=Polygon"
			sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl.geojson
			run_grass_alg_voronoiskeleton ${t}_buffered_buffered_simpl 20 1 "geojson" "|geometrytype=Polygon"
			run_alg_simplifygeometries ${t}_buffered_buffered_simpl_skel "geojson" 0 0.002
			sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl.geojson
			run_alg_smoothgeometry ${t}_buffered_buffered_simpl_skel_simpl 10 0.25 180 "geojson"
			sed -i 's/name_/name:/g' $temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed.geojson
			run_alg_dissolve ${t}_buffered_buffered_simpl_skel_simpl_smoothed "id" "geojson"
			convert2spatialite "$temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed_dissolved.geojson" "$work_dir/${t}_names.sqlite"
			set_projection "$work_dir/${t}_names.sqlite"
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;

		"strait")
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			sed -i 's/name_/name:/g' $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			if grep -q "type\": \"Polygon" "$work_dir/$t.geojson"; then
				run_alg_fixgeometries $t "geojson" "workdir" "|geometrytype=Polygon" && rm -f $work_dir/$t.geojson && mv $work_dir/${t}_fixed.geojson $work_dir/$t.geojson
				cp "$work_dir/$t.geojson" $temp_dir
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
				convert2spatialite "$temp_dir/${t}_buffered_buffered_simpl_skel_simpl_smoothed_dissolved.geojson" "$work_dir/${t}_skel.sqlite"
				set_projection "$work_dir/${t}_skel.sqlite"
				rm $temp_dir/*.*
			fi
			rm $work_dir/$t.osm
			;;

		"island_node") # should be requested after "island"
			osmtogeojson $work_dir/$t.osm > $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			cp $work_dir/$t.sqlite $temp_dir
			if [[ -f "$work_dir/island.sqlite" ]] ; then
				suffixdiff="_diff"
				cp $work_dir/island.sqlite $temp_dir
				run_alg_difference $t island sqlite
			fi
			convert2spatialite "$temp_dir/${t}$suffixdiff.sqlite" "$work_dir/$t.sqlite"
			set_projection "$work_dir/$t.sqlite"
			rm $work_dir/$t.geojson
			rm $work_dir/$t.osm
			rm $temp_dir/*.*
			;;

		"coastline") # Create ocean polygons and merge it with water polygons. Should be requested after "water","island"
			date
			osmtogeojson_wrapper $work_dir/$t.osm $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			if [ $(wc -c <"$work_dir/$t.sqlite") -ge 70 ] ; then
				cp $work_dir/$t.sqlite $temp_dir/${t}_tmp.sqlite
				run_alg_polygonstolines ${t}_tmp "sqlite" "|geometrytype=Polygon"
				run_alg_convertgeometrytype ${t}_tmp_lines "sqlite" 2
				merge_vector_layers "sqlite" "LineString" ${t}_tmp_lines_conv ${t}_tmp
				rm -f $temp_dir/${t}_tmp.sqlite && mv $temp_dir/${t}_tmp_lines_conv_merged.sqlite $temp_dir/${t}_tmp.sqlite
				run_alg_dissolve ${t}_tmp 'natural' "sqlite"
				run_alg_simplifygeometries ${t}_tmp_dissolved "sqlite" 0 0.000050 "|geometrytype=LineString" && mv $temp_dir/${t}_tmp_dissolved_simpl.sqlite $temp_dir/${t}_dissolved_simpl.sqlite
				convert2spatialite "$project_dir/crop.geojson" "$temp_dir/crop.sqlite"
				time run_alg_splitwithlines "crop" ${t}_dissolved_simpl "sqlite"
				run_alg_fixgeometries crop_split "sqlite" && rm -f $temp_dir/crop_split.sqlite && mv $temp_dir/crop_split_fixed.sqlite $temp_dir/crop_split.sqlite
				set_projection $temp_dir/crop_split.sqlite
				run_alg_singlesidedbuffer ${t}_dissolved_simpl 0.000001 1 "sqlite"
				run_alg_buffer ${t}_dissolved_simpl_sbuffered -0.0000001 "sqlite"
				run_alg_extractbylocation crop_split ${t}_dissolved_simpl_sbuffered_buffered 5 "sqlite"
				mv $temp_dir/crop_split_extracted.sqlite $temp_dir/ocean.sqlite
				if [[ -f $work_dir/island.sqlite ]] ; then
					cp $work_dir/island.sqlite $temp_dir
					run_alg_fixgeometries island "sqlite" && rm -f $temp_dir/island.sqlite && mv $temp_dir/island_fixed.sqlite $temp_dir/island.sqlite
					run_alg_difference ocean island "sqlite"
					run_alg_difference ocean_diff ${t}_tmp "sqlite" "|geometrytype=Polygon" && rm -f "$temp_dir/ocean_diff.sqlite" && mv "$temp_dir/ocean_diff_diff.sqlite" "$temp_dir/ocean.sqlite"
				fi
				if [[ -f $work_dir/water_dissolved.sqlite ]] ; then
					cp $work_dir/water_dissolved.sqlite $temp_dir
					set_projection $temp_dir/ocean.sqlite
					merge_vector_layers "sqlite" "Polygon" water_dissolved ocean # Merge ocean with inner water
					run_alg_fixgeometries water_dissolved_merged "sqlite"
					run_alg_dissolve water_dissolved_merged_fixed 'properties' "sqlite"
					set_projection $temp_dir/water_dissolved_merged_fixed_dissolved.sqlite
					convert2spatialite "$temp_dir/water_dissolved_merged_fixed_dissolved.sqlite" "$work_dir/water_dissolved.sqlite"
				else
					set_projection "$temp_dir/ocean.sqlite"
					convert2spatialite "$temp_dir/ocean.sqlite" "$work_dir/water_dissolved.sqlite"
				fi
				rm $work_dir/$t.osm
				rm $work_dir/$t.geojson
			fi
			date
			;;
		"highway_main")
			osmium sort -o $work_dir/${t}_sorted.osm $work_dir/$t.osm && rm -f $work_dir/$t.osm && mv $work_dir/${t}_sorted.osm $work_dir/$t.osm
			osmfilter $work_dir/$t.osm --keep-ways="layer>0" -o=$work_dir/${t}_layer_1.osm
			osmfilter $work_dir/$t.osm --keep-ways="layer<0" -o=$work_dir/${t}_layer_-1.osm
			osmfilter $work_dir/$t.osm --drop-ways="layer>0 or layer<0" -o=$work_dir/${t}_new.osm && rm -f $work_dir/$t.osm && mv $work_dir/${t}_new.osm $work_dir/$t.osm
			osmtogeojson_wrapper $work_dir/$t.osm $work_dir/$t.geojson
			osmtogeojson_wrapper $work_dir/${t}_layer_1.osm $work_dir/${t}_layer_1.geojson
			# Remove orphaned nodes to reduce file size
			cat $work_dir/${t}_layer_1.geojson | jq -c '.features[] | select(.geometry.type == "LineString")' > $work_dir/${t}_layer_1_tmp.geojson && rm $work_dir/${t}_layer_1.geojson && mv $work_dir/${t}_layer_1_tmp.geojson $work_dir/${t}_layer_1.geojson
			jsonlines2json $work_dir/${t}_layer_1
			osmtogeojson_wrapper $work_dir/${t}_layer_-1.osm $work_dir/${t}_layer_-1.geojson
			cat $work_dir/${t}_layer_-1.geojson | jq -c '.features[] | select(.geometry.type == "LineString")' > $work_dir/${t}_layer_-1_tmp.geojson && rm $work_dir/${t}_layer_-1.geojson && mv $work_dir/${t}_layer_-1_tmp.geojson $work_dir/${t}_layer_-1.geojson
			jsonlines2json $work_dir/${t}_layer_-1
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			convert2spatialite "$work_dir/${t}_layer_1.geojson" "$work_dir/${t}_layer_1.sqlite"
			convert2spatialite "$work_dir/${t}_layer_-1.geojson" "$work_dir/${t}_layer_-1.sqlite"
			rm -f $work_dir/$t.osm
			rm -f $work_dir/${t}_layer_1.osm
			rm -f $work_dir/${t}_layer_-1.osm
			rm -f $temp_dir/*.*
			;;
		*)
			osmtogeojson_wrapper $work_dir/$t.osm $work_dir/$t.geojson
			convert2spatialite "$work_dir/$t.geojson" "$work_dir/$t.sqlite"
			rm $work_dir/$t.osm
			rm -f $temp_dir/*.*
			;;
	esac
	((index++))
done
# Following code is needed to cut artefacts isolines placed on water and split glacier isolines
if [[ $generate_terrain == "true" ]] && [[ $generate_terrain_isolines == "true" ]]; then
	if [ ! -f $work_dir/../isolines_full.sqlite ] ; then
		echo -e "\033[93m$work_dir/../isolines_full.sqlite not found\033[0m"
		exit 1;
	fi
	rm -f "$work_dir/../isolines_glacier.sqlite"
	if [[ -f "$work_dir/water.sqlite" ]] && [[ $(stat --printf="%s" "$work_dir/water.sqlite") -ge 70 ]] ; then
		echo -e "\e[104m=== Substracting water from isolines...\e[49m"
		cp $project_dir/isolines_full.sqlite $temp_dir/isolines_full.sqlite
		cp $work_dir/water.sqlite $temp_dir
		time run_alg_difference isolines_full "water" "sqlite"
		set_projection $temp_dir/isolines_full_diff.sqlite
		convert2spatialite "$temp_dir/isolines_full_diff.sqlite" "$temp_dir/isolines_full_tmp.sqlite"
	else
		cp $project_dir/isolines_full.sqlite $temp_dir/isolines_full_tmp.sqlite
	fi
	if [[ -f "$work_dir/glacier.sqlite" ]] && [[ $(stat --printf="%s" "$work_dir/glacier.sqlite") -ge 70 ]] ; then # should be requested after "glacier"
		echo -e "\e[104m=== Splitting isolines by glaciers...\e[49m"
		cp $work_dir/glacier.sqlite $temp_dir
		cp $temp_dir/isolines_full_tmp.sqlite $temp_dir/isolines_gl_tmp.sqlite
		cp $temp_dir/isolines_full_tmp.sqlite $temp_dir/isolines_reg_tmp.sqlite
		run_alg_intersection isolines_gl_tmp "glacier" "sqlite"
		set_projection "$temp_dir/isolines_gl_tmp_intersection.sqlite"
		run_alg_difference isolines_reg_tmp "glacier" "sqlite"
		set_projection "$temp_dir/isolines_reg_tmp_diff.sqlite"
		mv "$temp_dir/isolines_gl_tmp_intersection.sqlite" "$temp_dir/isolines_gl_tmp.sqlite"
		mv "$temp_dir/isolines_reg_tmp_diff.sqlite" "$temp_dir/isolines_reg_tmp.sqlite"
		if [[ $(stat --printf="%s" "$temp_dir/isolines_gl_tmp.sqlite") != $(stat --printf="%s" "$temp_dir/isolines_reg_tmp.sqlite") ]] ; then # if file sizes are equal
			convert2spatialite "$temp_dir/isolines_gl_tmp.sqlite" "$project_dir/isolines_glacier.sqlite" "isolines_glacier"
		fi
		rm -f "$project_dir/isolines_regular.sqlite"
		convert2spatialite "$temp_dir/isolines_reg_tmp.sqlite" "$project_dir/isolines_regular.sqlite" "isolines_regular"
	fi
fi

rm -f "$temp_dir"/*.*
rm -f "$project_dir"/crop.geojson

echo -e "\e[42m====== Data preparation finished\e[49m"

if [[ $running_in_container == "false" ]] && [[ $(command -v notify-send) == 0 ]]; then
	notify-send "QGIS-topo: data preparation finished"
fi
#sleep 60