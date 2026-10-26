#!/usr/bin/python3
# Replace bbox inside QGIS project by current bbox
import xml.etree.ElementTree as ET
import argparse
import sys
from pyproj import Transformer

for i, arg in enumerate(sys.argv):
    if (arg[0] == '-') and arg[1].isdigit(): sys.argv[i] = ' ' + arg
parser = argparse.ArgumentParser()
parser.add_argument("-bbox")
parser.add_argument("-file")
args = parser.parse_args()

bbox_list = args.bbox.split(',')
lon_min = bbox_list[0]
lat_min = bbox_list[1]
lon_max = bbox_list[2]
lat_max = bbox_list[3]

def mytransform(lat, lon):
    return Transformer.from_crs("EPSG:4326", "EPSG:3857").transform(lat, lon)

coords_list_min = mytransform(lat_min, lon_min)
coords_list_max = mytransform(lat_max, lon_max)

tree = ET.parse(args.file)
root = tree.getroot()
# replace bounding box with new coordinates

tree.find('.//mapcanvas/extent/xmin').text = str(coords_list_min[0])
tree.find('.//mapcanvas/extent/ymin').text = str(coords_list_min[1])
tree.find('.//mapcanvas/extent/xmax').text = str(coords_list_max[0])
tree.find('.//mapcanvas/extent/ymax').text = str(coords_list_max[1])

tree.write(args.file, encoding='UTF-8', xml_declaration=True)
