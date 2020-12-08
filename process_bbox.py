#!/usr/bin/python3
import argparse
import sys
import math

for i, arg in enumerate(sys.argv):
    if (arg[0] == '-') and arg[1].isdigit(): sys.argv[i] = ' ' + arg
parser = argparse.ArgumentParser()
parser.add_argument("-bbox_str")
args = parser.parse_args()


def num(s):
    try:
        return float(s)
    except ValueError:
        throw_error()


def parse_link(link):
    if "map=" not in link:
        throw_error()
    map_str = link[link.find('map=') + 4:len(link)]
    if map_str.find('&') > 0:
        location = map_str[0:map_str.find('&')].split("/")
    else:
        location = map_str.split("/")
    try:
        calc_bbox = bbox_from_location(location[1], location[2], location[0])
    except IndexError:
        calc_bbox = ""
        throw_error()
    if calc_bbox:
        return calc_bbox


def bbox_from_location(lat, lon, zoom):
    lat = num(lat)
    lon = num(lon)
    zoom = num(zoom)
    if zoom < 6:
        return ""
    scale_meters_per_pixel = 0.0003

    lat_rad = math.radians(lat)
    meters_per_pixel = 10000000 * math.cos(lat_rad) / (2 ** (zoom + 8))
    earth_meters_per_picture_meters = meters_per_pixel / scale_meters_per_pixel

    meters_north = earth_meters_per_picture_meters / 2
    meters_east = earth_meters_per_picture_meters / 2

    meters_per_degree_lat = 111111.0
    meters_per_degree_lon = 111111.0 * math.cos(lat_rad)

    degrees_north = meters_north / meters_per_degree_lat
    degrees_east = 1.6 * (meters_east / meters_per_degree_lon)

    north = round(lat + degrees_north, 3)
    south = round(lat - degrees_north, 3)
    east = round(lon + degrees_east, 3)
    west = round(lon - degrees_east, 3)
    if north >= 90:
        north = 90
    if south <= -90:
        south = -90
    if east >= 180:
        east = 179.999
    if west <= -180:
        west = -179.999
    north_str = str(north)
    south_str = str(south)
    east_str = str(east)
    west_str = str(west)
    link_bbox = west_str + "," + south_str + "," + east_str + "," + north_str
    print(link_bbox)
    return link_bbox


def check_bbox(bbox_str):
    bbox_list = bbox_str.replace(" ", "").split(',')
    if len(bbox_list) != 4:
        throw_error()
    else:
        lon_min = bbox_list[0]  # (W) left
        lat_min = bbox_list[1]  # (S) bottom
        lon_max = bbox_list[2]  # (E) right
        lat_max = bbox_list[3]  # (N) top
        if num(lon_min) > num(lon_max) or num(lat_min) > num(lat_max) or num(lat_max) >= 90 or num(lat_min) <= -90 or \
                num(lon_min) <= -180 or num(lon_max) >= 180:
            throw_error()
        result = str(lon_min) + "," + str(lat_min) + "," + str(lon_max) + "," + str(lat_max)
        print(result)
        return result


def throw_error():
    print(
        "\033[91mInvalid bbox format. Use openstreetmap.org link or comma separated left bottom right top (see https://github.com/xmd5a2/qgis-xtopo).\033[0m")
    return 1


def prepare_bbox(bbox_str):
    try:
        if "openstreetmap" in bbox_str:
            parse_link(bbox_str)
            return parse_link(bbox_str)
        else:
            check_bbox(bbox_str)
            return check_bbox(bbox_str)
    except TypeError:
        return


prepare_bbox(args.bbox_str)
