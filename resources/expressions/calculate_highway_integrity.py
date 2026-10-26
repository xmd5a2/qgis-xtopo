from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def calculate_highway_integrity(highway, surface, smoothness, tracktype, feature, parent):
    result = 0
    if surface:
        surface = surface.lower()
    if smoothness:
        smoothness = smoothness.lower()
    if tracktype:
        tracktype = tracktype.lower()

    if surface == "paved" or surface == "concrete" or surface ==  "concrete:lanes" or surface == "concrete:plates" or \
        surface == "sett" or surface == "paving_stones" or surface == "metal" or surface == "wood":
        result += 3
    elif surface == "fine_gravel" or surface == "grass_paver":
        result += 4
    elif surface == "compacted":
        result += 8
    elif surface == 'ground' or surface == "earth" or surface == "pebblestone":
        result += 9
    elif surface == "grass":
        result += 10
    elif surface == "cobblestone":
        result += 11
    elif surface == "gravel":
        result += 12
    elif surface == "stone" or surface == "rock" or surface == "rocky":
        result += 13
    elif surface == "unpaved" or surface == "dirt":
        result += 14
    elif surface == "salt" or surface == "ice" or surface == "snow":
        result += 15
    elif surface == "sand":
        result += 16
    elif surface == "mud":
        result += 18

    if smoothness == "excellent":
        if (highway == "track" or highway == "path") and surface == "":
            result = 7
        else: result -= 5
    elif smoothness == "very_good":
        if (highway == "track" or highway == "path") and surface == "":
            result = 6
        else: result -= 4
    elif smoothness == "good":
        if (highway == "track" or highway == "path") and surface == "":
            result = 8
        else: result -= 2
    elif smoothness == "intermediate":
        if (highway == "track" or highway == "path") and surface == "":
            result = 9
    elif smoothness == "bad":
        if (highway == "track" or highway == "path") and surface == "":
            result = 9
        elif surface == "asphalt": result += 7
        else: result += 6
    elif smoothness == "very_bad":
        if (highway == "track" or highway == "path") and surface == "":
            result = 12
        elif surface == "asphalt": result += 12
        else: result += 7
    elif smoothness == "horrible":
        if (highway == "track" or highway == "path") and surface == "":
            result = 15
        elif surface == "asphalt": result += 19
        else: result += 9
    elif smoothness == "very_horrible":
        if (highway == "track" or highway == "path") and surface == "":
            result = 18
        elif surface == "asphalt": result += 22
        else: result += 11
    elif smoothness == "impassable":
        if (highway == "track" or highway == "path") and surface == "":
            result = 24
        elif surface == "asphalt": result += 26
        else: result += 12

    if surface == "":
        if tracktype == "grade1": result += 1
        elif tracktype == "grade2": result += 3
        elif tracktype == "grade3": result += 7
        elif tracktype == "grade4": result += 10
        elif tracktype == "grade5": result += 15

    if (highway == "motorway" or highway == "motorway_link" or highway == "trunk" or highway == "trunk_link" or
        highway == "primary" or highway == "primary_link" or highway == "secondary" or highway == "secondary_link" or
        highway == "tertiary" or highway == "tertiary_link" or highway == "unclassified" or highway == "residential" or
        highway == "service" or highway == "pedestrian" or highway == "living_street" or
        highway == "footway" or highway == "cycleway") and \
        surface == "" and smoothness == "":
        result = 100

    if (highway == "track" or highway == "path") and (surface == "" and smoothness == "" and tracktype == ""):
        result = 100
    if result < 0:
        result = 0

    max_integrity = 30
    result = (result * 10) / max_integrity

    return int(result)
