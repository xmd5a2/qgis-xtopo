from dataclasses import dataclass, field


@dataclass
class Recipe:
    steps: list = field(default_factory=list)


def step(operation, **params):
    return operation, params


def save(suffix="", layer_name=None, target_crs=None):
    return step("save", suffix=suffix, layer_name=layer_name, target_crs=target_crs)


def load(name):
    return step("load", name=name)


def fix():
    return step("fix")


def dissolve(field=None):
    return step("dissolve", field=field)


def singleparts():
    return step("singleparts")


def simplify(tolerance):
    return step("simplify", tolerance=tolerance)


def smooth(iterations=5, offset=0.25, max_angle=180):
    return step("smooth", iterations=iterations, offset=offset, max_angle=max_angle)


def buffer(distance, dissolve_parts=False):
    return step("buffer", distance=distance, dissolve=dissolve_parts)


def centroid():
    return step("centroid")


def polygonstolines():
    return step("polygonstolines")


def explode():
    return step("explode")


def reproject(target_crs="EPSG:3857"):
    return step("reproject", target_crs=target_crs)


def difference(overlay):
    return step("difference", overlay=overlay)


def intersection(overlay):
    return step("intersection", overlay=overlay)


def merge(layers):
    return step("merge", layers=layers)


def label_axis(main_flow=False):
    """Линия подписи вдоль формы; main_flow — концы по длине фигуры."""
    return step("label_axis", main_flow=main_flow)


def split_layer():
    return step("split_layer")


def linestopolygons(drop_others=False):
    return step("linestopolygons", drop_others=drop_others)


RECIPES = {
    "water": Recipe([
        # fix() после singleparts страхует от самопересечений dissolve:
        # native:difference пропускает такой объект как ошибочный.
        fix(), save(), dissolve(), singleparts(), fix(), save("_dissolved"),
    ]),
    "water_intermittent": Recipe([
        fix(), save(), dissolve(), singleparts(), fix(), save("_dissolved"),
    ]),
    "bare_rock": Recipe([
        # В OSM встречаются самопересекающиеся мультиполигоны.
        fix(), save(),
    ]),
    "scree": Recipe([
        fix(), save(),
    ]),
    "allotments": Recipe([
        save(), load("allotments"), dissolve(), singleparts(), save("_dissolved"),
        load("allotments"), buffer(0.001), dissolve(), singleparts(),
        save("_buffered_dissolved"),
    ]),
    "water_without_riverbanks": Recipe([save()]),
    "river": Recipe([
        difference("water_without_riverbanks"), save(), simplify(0.001), smooth(), save("_smoothed"),
    ]),
    "river_intermittent": Recipe([
        difference("water_without_riverbanks"), save(), simplify(0.001), smooth(), save("_smoothed"),
    ]),
    "canal": Recipe([
        difference("water_without_riverbanks"), save(layer_name="canal_diff"),
        simplify(0.001), smooth(), save("_smoothed"),
    ]),
    "canal_intermittent": Recipe([
        difference("water_without_riverbanks"), save(layer_name="canal_intermittent_diff"),
        simplify(0.001), smooth(), save("_smoothed"),
    ]),
    "stream_intermittent": Recipe([
        difference("water_dissolved"), save(),
    ]),
    "stream": Recipe([
        difference("water_dissolved"), save(), smooth(), save("_smoothed"),
        load("stream"), merge(["stream_intermittent"]), save("_merged"),
    ]),
    "admin_level_2": Recipe([
        polygonstolines(), dissolve(), singleparts(), save("_dissolved"),
    ]),
    "admin_level_4": Recipe([
        polygonstolines(), difference("admin_level_2_dissolved"), dissolve(), singleparts(),
        save("_proc"),
    ]),
    "railway_all": Recipe([
        save(), explode(), reproject(), save("_exploded"),
    ]),
    "ridge": Recipe([
        save(), explode(), reproject(), save("_exploded"),
        simplify(0.0007), smooth(10), dissolve("name"), save("_simpl_smoothed"),
    ]),
    "route_hiking": Recipe([
        save(), explode(), save("_exploded"),
    ]),
    "power_line": Recipe([
        dissolve(), singleparts(), save("_dissolved"),
    ]),
    "highway_main_bridge": Recipe([
        save(), centroid(), save("_centroids"),
        load("highway_main_bridge"), dissolve("highway"), singleparts(), save("_dissolved"),
    ]),
    "highway_main": Recipe([
        split_layer(),
    ]),
    "waterfall": Recipe([
        save(), polygonstolines(), save("_lines"),
        load("waterfall"), centroid(), save("_centroids"),
    ]),
    "weir": Recipe([
        save(), polygonstolines(), save("_lines"),
        load("weir"), centroid(), save("_centroids"),
    ]),
    "mountain_pass": Recipe([
        # Слой остаётся в EPSG:4326: значки всех POI считаются в одной
        # системе координат, иначе расстояния вытеснения не совпадают с
        # max_distance в градусах.
        save(),
    ]),
    "building_train_station": Recipe([
        fix(), centroid(), reproject(), save("_reproj"),
    ]),
    "place_locality_node": Recipe([
        difference("place_locality_way"), singleparts(), save("_diff"),
    ]),
    "island_node": Recipe([
        difference("island"), save(),
    ]),
    "military": Recipe([
        save(), dissolve(), singleparts(), save("_dissolved"),
    ]),
    "mountain_area": Recipe([
        fix(), save(layer_name="mountain_area_fixed"),
    ]),
    "plateau": Recipe([
        # Шаблон показывает «plateau node» — узлы с тегом natural=plateau
        # (точки с названиями), поэтому исходный слой сохраняем отдельно,
        # до исправления геометрии.
        save(suffix="_original", layer_name="plateau"),
        fix(), save(layer_name="plateau_fixed"),
    ]),
    "glacier": Recipe([
        # Ледник — только замкнутый контур или мультиполигон: разомкнутые
        # way и прочую геометрию отбрасываем, иначе смешанный тип слоя
        # ломал его запись (полигоны сплющивались в линии).
        linestopolygons(drop_others=True), fix(), save(),
        polygonstolines(), save("_lines"), load("glacier"),
        label_axis(main_flow=True), save("_names"),
    ]),
    "bay_polygon": Recipe([
        fix(), save(), polygonstolines(), save("_lines"), load("bay_polygon"),
        label_axis(), save("_names"),
    ]),
    "wetland": Recipe([
        fix(), save(), polygonstolines(), save("_lines"), load("wetland"),
        label_axis(), save("_names"),
    ]),
    "wood": Recipe([
        fix(), save(), polygonstolines(), save("_lines"), load("wood"),
        label_axis(), save("_names"),
    ]),
    "strait": Recipe([
        # Шаблон ждёт два файла: strait.sqlite (исходные объекты — точки
        # проливов и линии) и strait_skel.sqlite (остовные линии
        # многоугольников проливов).
        save(),
        linestopolygons(drop_others=True), fix(),
        label_axis(), save("_skel"),
    ]),
    "coastline": Recipe([
        save(),
    ]),
    "monastery_christian": Recipe([
        centroid(), save("_centroids"), buffer(0.0025), save("_centroids_buffered"),
    ]),
    "place_of_worship_christian": Recipe([
        centroid(), save("_centroids"), difference("monastery_christian_centroids_buffered"),
        singleparts(), save("_centroids_diff_parts"),
    ]),
    "place_of_worship_christian_ruins": Recipe([
        centroid(), save("_centroids"), difference("monastery_christian_centroids_buffered"),
        singleparts(), save("_centroids_diff_parts"),
    ]),
    "ford": Recipe([
        save(), centroid(), save("_centroids"), load("ford"), buffer(0.006), dissolve("name"),
        centroid(), save("_buffered_dissolved_centroids"),
    ]),
    "railway_stop_names": Recipe([
        centroid(), save("_centroids"), load("railway_stop_names"), buffer(0.006),
        dissolve("name"), centroid(), reproject(), save("_buffered_dissolved_centroids"),
    ]),
    "railway_station_icons": Recipe([
        buffer(0.004), dissolve("name"), centroid(),
        save("_buffered_dissolved_centroids"),
    ]),
    "barrier": Recipe([
        # Замкнутые барьеры GDAL/OSM может отнести к полигонам (например,
        # стена кладбища). Для слоя «barrier way» нужны линии: превращаем
        # полигоны в их контуры.
        save(), polygonstolines(), save("_lines"),
    ]),
    "railway_halt_icons": Recipe([
        buffer(0.004), dissolve("name"), centroid(),
        save("_buffered_dissolved_centroids"),
    ]),
    # Площадные дороги (highway=* с area=yes или area:highway=*) OSM рисует
    # замкнутым контуром, но драйвер отдаёт их линиями: слой с заливкой
    # такой контур не рисует. Замкнутые линии превращаем в полигоны.
    "highway_main_area": Recipe([
        linestopolygons(), save(),
    ]),
    "place_square_highway_area": Recipe([
        linestopolygons(), save(),
    ]),
    "aerialway_station": Recipe([
        buffer(0.004), dissolve("name"), centroid(),
        save("_buffered_dissolved_centroids"),
    ]),
}


POINT_CENTROID = {
    "camp_site", "attraction", "ruins", "track_bridge", "aerodrome", "castle",
    "archaeological_site", "observatory", "picnic_site", "tower_cooling",
    "power_plant", "power_substation",
}
POINT_CENTROID_EXTRA = {
    "place_of_worship_muslim", "place_of_worship_hindu", "place_of_worship_buddhist",
    "place_of_worship_shinto", "place_of_worship_jewish", "place_of_worship_taoist",
    "place_of_worship_sikh", "place_of_worship_other", "sinkhole_polygon",
    "alpine_hut", "wilderness_hut", "memorial", "monument", "tower_communication",
    "monastery_no_religion", "barrier_border_control", "cape", "chimney",
    "water_tower", "volcano", "volcano_dirt", "mineshaft", "mineshaft_abandoned",
}
POINT_ONLY = {
    "peak", "hill", "spring", "water_point", "cave_entrance", "places_main",
    "place_admin_centre_6", "place_admin_centre_4", "place_admin_centre_2",
    "hunting_stand", "lighthouse", "power_pole", "power_tower", "aerialway_pylon",
    "survey_point", "tree", "stone", "rock", "sinkhole", "bay_point", "cape",
}


def recipe_for(name):
    if name in RECIPES:
        return RECIPES[name]
    if name in POINT_CENTROID:
        return Recipe([save(), centroid(), save("_centroids")])
    if name in POINT_CENTROID_EXTRA:
        return Recipe([save(), centroid(), save("_centroids")])
    if name in POINT_ONLY:
        return Recipe([save()])
    return Recipe([save()])
