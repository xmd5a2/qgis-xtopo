#!/usr/bin/python3
import math
import argparse
import sys

for i, arg in enumerate(sys.argv):
  if (arg[0] == '-') and arg[1].isdigit(): sys.argv[i] = ' ' + arg
parser = argparse.ArgumentParser()
parser.add_argument("-bbox")
args = parser.parse_args()

#def calc_srtm_tiles_list(agrs.lon_min: float, agrs.lat_min: float, agrs.lon_max: float, agrs.lat_max: float):
#def calc_srtm_tiles_list(lon_min: float, lat_min: float, lon_max: float, lat_max: float):
def calc_srtm_tiles_list(bbox):
    bbox_list=bbox.split(',')
    lon_min = math.floor(float(bbox_list[0]))  # (W,E)   left
    lat_min = math.ceil(float(bbox_list[1]))  # (N,S)   bottom
    lon_max = math.ceil(float(bbox_list[2]))  # (W,E)     right
    lat_max = math.ceil(float(bbox_list[3])) + 1  # (N,S) top
    #    print(str(lat_min) + " " + str(lat_max) + " " + str(lon_min) + " " + str(lon_max))

    lat_str_ns = "N"
    lon_str_ew = "E"
    lat_str = 0
    lon_str = 0
    tile_id = ""

    for lat in range(lat_min, lat_max):
        for lon in range(lon_min, lon_max):
            if int(lat) > 0:
                lat_str_ns = "N"
                lat_str = str(int(lat) - 1)
            elif int(lat) <= 0:
                lat_str_ns = "S"
                lat_str = str(abs(int(lat) - 1))
            if int(lon) >= 0:
                lon_str_ew = "E"
                lon_str = str(lon)
            elif int(lon) < 0:
                lon_str_ew = "W"
                lon_str = str(abs(int(lon)))
            if int(lat_str) < 10:
                lat_str = "0" + lat_str
            if int(lon_str) < 100 and int(lon_str) >= 10:
                lon_str = "0" + lon_str
            elif int(lon_str) < 10:
                lon_str = "00" + lon_str
            tile_id = tile_id + " " + lat_str_ns + lat_str + lon_str_ew + lon_str

    print(tile_id.strip())

calc_srtm_tiles_list(args.bbox)