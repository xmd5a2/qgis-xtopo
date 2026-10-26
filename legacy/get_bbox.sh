IFS=',' read -r -a array_bbox <<< $(python3 $(pwd)/process_bbox.py -bbox_str "$bbox")

if [[ $(echo ${array_bbox[0]} | grep Invalid) ]] ; then
	echo ${array_bbox[0]}
	exit 1;
fi
lon_min=${array_bbox[0]}
lat_min=${array_bbox[1]}
lon_max=${array_bbox[2]}
lat_max=${array_bbox[3]}

# Reconstruct bbox if OSM link is given
if [[ $bbox == *"openstreetmap"* ]] ; then
	bbox=$lon_min,$lat_min,$lon_max,$lat_max
fi