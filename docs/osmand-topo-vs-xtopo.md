# OsmAnd Topo vs qgis-xtopo: поддерживаемые объекты и их свойства

Дата: 2026-11-28. OsmAnd-resources, коммит `123a2680f5` (стиль `Topo`).

- OsmAnd: `rendering_styles/topo.render.xml`, ветка `master`.

- qgis-xtopo: `resources/queries/*.txt` (165 запросов) + шаблон `automap.qgz`.

- Числа объектов OSM — taginfo (глобальные данные).

Повторить: `python3 tools/compare_osmand_style.py` (`--no-taginfo` — без сетевых счётчиков).

Всего объектов в стиле OsmAnd Topo: **555** (служебных тегов OsmAnd: 24). Поддерживает xtopo **250**, ключ без нужного значения — **201**, ключ не используется — **104**.

## 1. Сводка по ключам тегов

| тег | в OsmAnd | поддержано | не поддержано | значения в xtopo |
|---|---|---|---|---|
| `abandoned:aeroway` | 3 | 0 | 3 | — |
| `abandoned:building` | 2 | 0 | 2 | — |
| `abandoned:highway` | 13 | 0 | 13 | — |
| `abandoned:landuse` | 6 | 0 | 6 | — |
| `abandoned:place` | 1 | 0 | 1 | — |
| `abandoned:railway` | 1 | 0 | 1 | — |
| `addr:flats` | 1 | 0 | 1 | — |
| `addr:housenumber` | 1 | 0 | 1 | — |
| `admin_level` | 12 | 3 | 9 | `2`, `4`, `6` |
| `aerialway` | 15 | 9 | 6 | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aeroway` | 7 | 4 | 3 | `aerodrome`, `apron`, `runway`, `taxiway` |
| `amenity` | 24 | 5 | 19 | `drinking_water`, `hunting_stand`, `monastery`, `place_of_worship`, `prison` |
| `area:highway` | 13 | 9 | 4 | `motorway`, `pedestrian`, `primary`, `residential`, `secondary`, `service`, `tertiary`, `trunk`, `unclassified` |
| `attraction` | 1 | 0 | 1 | — |
| `barrier` | 46 | 46 | 0 | любые |
| `bicycle_road` | 1 | 0 | 1 | — |
| `boundary` | 6 | 2 | 4 | `forestry_compartment`, `national_park` |
| `building` | 19 | 19 | 0 | любые |
| `club` | 1 | 0 | 1 | — |
| `craft` | 1 | 0 | 1 | — |
| `cycleway` | 8 | 0 | 8 | — |
| `cycleway:both` | 5 | 0 | 5 | — |
| `disused:building` | 2 | 0 | 2 | — |
| `embankment` | 1 | 1 | 0 | `yes` |
| `emergency` | 2 | 0 | 2 | — |
| `entrance` | 1 | 0 | 1 | — |
| `fixme_tag` | 1 | 0 | 1 | — |
| `ford` | 2 | 1 | 1 | `yes` |
| `geological` | 4 | 0 | 4 | — |
| `glacial` | 1 | 1 | 0 | `crevasse_zone` |
| `harbour` | 1 | 0 | 1 | — |
| `healthcare` | 1 | 0 | 1 | — |
| `highway` | 35 | 22 | 13 | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `historic` | 23 | 6 | 17 | `archaeological_site`, `castle`, `memorial`, `mine`, `monument`, `ruins` |
| `horse_scale` | 1 | 0 | 1 | — |
| `icn_ref` | 1 | 0 | 1 | — |
| `iwn_ref` | 1 | 0 | 1 | — |
| `junction` | 1 | 0 | 1 | — |
| `ladder` | 1 | 0 | 1 | — |
| `landuse` | 34 | 19 | 15 | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `lcn_ref` | 1 | 0 | 1 | — |
| `leisure` | 13 | 2 | 11 | `nature_reserve`, `resort` |
| `lit` | 4 | 0 | 4 | — |
| `lit_pedestrian_layer-1` | 3 | 0 | 3 | — |
| `lock` | 1 | 0 | 1 | — |
| `lwn_ref` | 1 | 0 | 1 | — |
| `man_made` | 48 | 21 | 27 | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `military` | 4 | 3 | 1 | `barracks`, `bunker`, `checkpoint` |
| `mountain_pass` | 1 | 1 | 0 | `yes` |
| `natural` | 55 | 35 | 20 | `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `heath`, `hill`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `ncn_ref` | 1 | 0 | 1 | — |
| `note_tag` | 1 | 0 | 1 | — |
| `nwn_ref` | 1 | 0 | 1 | — |
| `office` | 1 | 0 | 1 | — |
| `osm_note` | 1 | 0 | 1 | — |
| `osmand` | 4 | 0 | 4 | — |
| `osmand_aboriginal_lands` | 1 | 0 | 1 | — |
| `osmand_barrier` | 3 | 0 | 3 | — |
| `osmand_border_zone` | 1 | 0 | 1 | — |
| `osmand_change` | 1 | 0 | 1 | — |
| `osmand_legend` | 2 | 0 | 2 | — |
| `osmand_national_park` | 1 | 0 | 1 | — |
| `osmand_nature_reserve` | 1 | 0 | 1 | — |
| `osmand_protected_area` | 1 | 0 | 1 | — |
| `osmand_rungs` | 1 | 0 | 1 | — |
| `osmand_rungs_node` | 1 | 0 | 1 | — |
| `osmand_waterway` | 1 | 0 | 1 | — |
| `place` | 24 | 12 | 12 | `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `power` | 9 | 6 | 3 | `line`, `minor_line`, `plant`, `pole`, `substation`, `tower` |
| `public_transport` | 2 | 0 | 2 | — |
| `railway` | 20 | 6 | 14 | `abandoned`, `funicular`, `halt`, `narrow_gauge`, `rail`, `station` |
| `rcn_ref` | 1 | 0 | 1 | — |
| `region:type` | 1 | 1 | 0 | `mountain_area` |
| `rwn_ref` | 1 | 0 | 1 | — |
| `shop` | 1 | 0 | 1 | — |
| `sport` | 1 | 0 | 1 | — |
| `tourism` | 22 | 6 | 16 | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `waterway` | 16 | 10 | 6 | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `wood:lost` | 3 | 0 | 3 | — |

## 2. Приоритет: самые массовые неподдерживаемые объекты

| объект | объектов в OSM | где рисует OsmAnd | значок |
|---|---|---|---|
| `highway`=`crossing` | 13.8 млн | точка | crossing_traffic_signals_map, crossing_uncontrolled_map |
| `lit`=`yes` | 12 млн | линия, полигон | — |
| `power`=`generator` | 6.9 млн | точка, полигон, подпись | generator_source_solar |
| `public_transport`=`platform` | 4.2 млн | линия, точка, полигон, подпись | — |
| `highway`=`bus_stop` | 4.2 млн | точка, подпись | — |
| `lit`=`no` | 3.7 млн | линия, полигон | — |
| `amenity`=`bench` | 3.5 млн | точка, подпись | — |
| `leisure`=`swimming_pool` | 3.1 млн | полигон | — |
| `leisure`=`pitch` | 2.9 млн | полигон, подпись | — |
| `highway`=`turning_circle` | 2.6 млн | точка | — |
| `highway`=`traffic_signals` | 2 млн | точка, подпись | — |
| `leisure`=`garden` | 1.6 млн | точка, полигон, подпись | — |
| `tourism`=`information` | 1.5 млн | точка, подпись | board_type_nature, board_type_plants |
| `amenity`=`school` | 1.5 млн | подпись | — |
| `leisure`=`park` | 1.3 млн | точка, полигон, подпись | — |
| `admin_level`=`8` | 755 тыс. | линия | — |
| `amenity`=`shelter` | 702 тыс. | точка, подпись | topo_lean_to |
| `landuse`=`commercial` | 609 тыс. | подпись | — |
| `man_made`=`mast` | 535 тыс. | точка, подпись | — |
| `leisure`=`picnic_table` | 511 тыс. | точка, подпись | — |
| `admin_level`=`10` | 472 тыс. | линия | — |
| `man_made`=`utility_pole` | 457 тыс. | точка | — |
| `tourism`=`hotel` | 456 тыс. | точка, подпись | — |
| `landuse`=`retail` | 427 тыс. | подпись | — |
| `man_made`=`pipeline` | 387 тыс. | линия, подпись | — |
| `tourism`=`artwork` | 376 тыс. | полигон, подпись | memorial_statue |
| `man_made`=`silo` | 374 тыс. | полигон, подпись | — |
| `man_made`=`petroleum_well` | 343 тыс. | точка, подпись | — |
| `amenity`=`kindergarten` | 341 тыс. | подпись | — |
| `cycleway`=`lane` | 340 тыс. | линия, подпись | — |
| `man_made`=`street_cabinet` | 337 тыс. | точка, подпись | — |
| `admin_level`=`9` | 335 тыс. | линия | — |
| `power`=`portal` | 325 тыс. | линия, точка | — |
| `place`=`plot` | 324 тыс. | подпись | — |
| `natural`=`shrub` | 318 тыс. | точка, подпись | — |
| `place`=`farm` | 290 тыс. | точка, подпись | — |
| `railway`=`buffer_stop` | 283 тыс. | точка | — |
| `landuse`=`construction` | 278 тыс. | полигон, подпись | — |
| `tourism`=`viewpoint` | 266 тыс. | точка, подпись | — |
| `power`=`transformer` | 265 тыс. | точка, подпись | — |
| `man_made`=`works` | 244 тыс. | подпись | — |
| `historic`=`wayside_cross` | 242 тыс. | точка, подпись | — |
| `amenity`=`clinic` | 223 тыс. | подпись | — |
| `amenity`=`hospital` | 218 тыс. | подпись | — |
| `public_transport`=`station` | 204 тыс. | точка, полигон, подпись | — |

## 3. Полный список неподдерживаемых объектов

### 3.1. Ключ не используется xtopo вовсе

Всего: 80.

| тег | значение | объектов в OSM | где рисует OsmAnd | значок |
|---|---|---|---|---|
| `abandoned:aeroway` | `*` | — | порядок | — |
| `abandoned:aeroway` | `aerodrome` | 693 | точка, полигон, подпись | topo_aerodrome_abandoned |
| `abandoned:aeroway` | `runway` | 1 тыс. | линия, полигон, подпись | — |
| `abandoned:building` | `*` | — | полигон, подпись | — |
| `abandoned:building` | `no` | 242 | порядок | — |
| `abandoned:highway` | `*` | — | подпись | — |
| `abandoned:highway` | `footway` | 1 тыс. | линия | — |
| `abandoned:highway` | `motorway` | 953 | линия | — |
| `abandoned:highway` | `motorway_link` | 835 | линия | — |
| `abandoned:highway` | `path` | 19 тыс. | линия | — |
| `abandoned:highway` | `primary` | 3 тыс. | линия | — |
| `abandoned:highway` | `residential` | 4 тыс. | линия | — |
| `abandoned:highway` | `secondary` | 1 тыс. | линия | — |
| `abandoned:highway` | `service` | 4 тыс. | линия | — |
| `abandoned:highway` | `tertiary` | 1 тыс. | линия | — |
| `abandoned:highway` | `track` | 55 тыс. | линия | — |
| `abandoned:highway` | `trunk` | 1 тыс. | линия | — |
| `abandoned:highway` | `unclassified` | 5 тыс. | линия | — |
| `abandoned:landuse` | `*` | — | полигон, подпись | — |
| `abandoned:landuse` | `garages` | 4 | полигон | — |
| `abandoned:landuse` | `industrial` | 549 | полигон | — |
| `abandoned:landuse` | `military` | 489 | полигон | — |
| `abandoned:landuse` | `railway` | 41 | полигон | — |
| `abandoned:landuse` | `residential` | 1 тыс. | полигон | — |
| `abandoned:place` | `*` | — | полигон, подпись | — |
| `abandoned:railway` | `station` | 9 тыс. | подпись | — |
| `addr:flats` | `*` | — | подпись | — |
| `addr:housenumber` | `*` | — | подпись | — |
| `attraction` | `big_wheel` | 1 тыс. | точка, подпись | — |
| `bicycle_road` | `yes` | 23 тыс. | линия | — |
| `club` | `*` | — | точка, подпись | — |
| `craft` | `*` | — | точка, подпись | — |
| `cycleway` | `*` | — | порядок | — |
| `cycleway` | `both` | 696 | линия | — |
| `cycleway` | `lane` | 340 тыс. | линия, подпись | — |
| `cycleway` | `opposite` | 8 тыс. | линия, подпись | — |
| `cycleway` | `opposite_lane` | 2 тыс. | линия, подпись | — |
| `cycleway` | `share_busway` | 4 тыс. | линия, подпись | — |
| `cycleway` | `shared_lane` | 96 тыс. | линия | — |
| `cycleway` | `track` | 67 тыс. | линия, подпись | — |
| `cycleway:both` | `*` | — | порядок | — |
| `cycleway:both` | `lane` | 99 тыс. | линия | — |
| `cycleway:both` | `share_busway` | 1 тыс. | линия | — |
| `cycleway:both` | `shared_lane` | 40 тыс. | линия | — |
| `cycleway:both` | `track` | 17 тыс. | линия | — |
| `disused:building` | `*` | — | полигон, подпись | — |
| `disused:building` | `no` | — | порядок | — |
| `emergency` | `*` | — | точка, подпись | — |
| `emergency` | `rescue_box` | 80 | точка, подпись | — |
| `entrance` | `*` | — | подпись | — |
| `geological` | `moraine` | 3 тыс. | полигон, подпись | — |
| `geological` | `outcrop` | 2 тыс. | линия, точка, полигон, подпись | — |
| `geological` | `palaeontological_site` | 857 | точка, полигон, подпись | — |
| `geological` | `volcanic_lava_field` | 997 | полигон, подпись | — |
| `harbour` | `yes` | 5 тыс. | подпись | — |
| `healthcare` | `*` | — | точка, подпись | — |
| `horse_scale` | `*` | — | порядок | — |
| `icn_ref` | `*` | — | порядок | — |
| `iwn_ref` | `*` | — | порядок | — |
| `junction` | `yes` | 134 тыс. | точка, подпись | — |
| `ladder` | `yes` | 12 тыс. | линия, точка | — |
| `lcn_ref` | `*` | — | порядок | — |
| `lit` | `*` | — | порядок | — |
| `lit` | `interval` | 640 | линия | — |
| `lit` | `no` | 3.7 млн | линия, полигон | — |
| `lit` | `yes` | 12 млн | линия, полигон | — |
| `lock` | `yes` | 11 тыс. | линия, подпись | — |
| `lwn_ref` | `*` | — | порядок | — |
| `ncn_ref` | `*` | — | порядок | — |
| `nwn_ref` | `*` | — | порядок | — |
| `office` | `*` | — | точка, подпись | — |
| `public_transport` | `platform` | 4.2 млн | линия, точка, полигон, подпись | — |
| `public_transport` | `station` | 204 тыс. | точка, полигон, подпись | — |
| `rcn_ref` | `*` | — | порядок | — |
| `rwn_ref` | `*` | — | порядок | — |
| `shop` | `*` | — | точка | — |
| `sport` | `climbing` | 91 тыс. | точка, полигон, подпись | — |
| `wood:lost` | `*` | — | порядок | — |
| `wood:lost` | `dead_wood` | 84 | полигон, подпись | — |
| `wood:lost` | `windfall` | 884 | полигон, подпись | — |

Служебные теги OsmAnd (в OSM их нет): `fixme_tag`, `lit_pedestrian_layer-1`, `lit_pedestrian_layer-1`, `lit_pedestrian_layer-1`, `note_tag`, `osm_note`, `osmand`, `osmand`, `osmand`, `osmand`, `osmand_aboriginal_lands`, `osmand_barrier`, `osmand_barrier`, `osmand_barrier`, `osmand_border_zone`, `osmand_change`, `osmand_legend`, `osmand_legend`, `osmand_national_park`, `osmand_nature_reserve`, `osmand_protected_area`, `osmand_rungs`, `osmand_rungs_node`, `osmand_waterway`.

### 3.2. Ключ поддержан, но конкретного значения нет

Всего: 201.

| тег | значение | объектов в OSM | где рисует OsmAnd | значок | значения в xtopo |
|---|---|---|---|---|---|
| `admin_level` | `*` | — | порядок | — | `2`, `4`, `6` |
| `admin_level` | `0` | — | линия | — | `2`, `4`, `6` |
| `admin_level` | `1` | 87 | линия | — | `2`, `4`, `6` |
| `admin_level` | `10` | 472 тыс. | линия | — | `2`, `4`, `6` |
| `admin_level` | `3` | 4 тыс. | линия, подпись | — | `2`, `4`, `6` |
| `admin_level` | `5` | 50 тыс. | линия | — | `2`, `4`, `6` |
| `admin_level` | `7` | 186 тыс. | линия | — | `2`, `4`, `6` |
| `admin_level` | `8` | 755 тыс. | линия | — | `2`, `4`, `6` |
| `admin_level` | `9` | 335 тыс. | линия | — | `2`, `4`, `6` |
| `aerialway` | `*` | — | порядок | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aerialway` | `drag_lift` | 4 тыс. | линия, подпись | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aerialway` | `magic_carpet` | 3 тыс. | линия, подпись | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aerialway` | `mixed_lift` | 108 | линия, подпись | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aerialway` | `rope_tow` | 1 тыс. | линия, подпись | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aerialway` | `zip_line` | 2 тыс. | линия, подпись | — | `cable_car`, `chair_lift`, `gondola`, `goods`, `j-bar`, `platter`, `pylon`, `station`, `t-bar` |
| `aeroway` | `*` | — | порядок | — | `aerodrome`, `apron`, `runway`, `taxiway` |
| `aeroway` | `helipad` | 71 тыс. | точка, полигон, подпись | — | `aerodrome`, `apron`, `runway`, `taxiway` |
| `aeroway` | `terminal` | 10 тыс. | полигон, подпись | — | `aerodrome`, `apron`, `runway`, `taxiway` |
| `amenity` | `*` | — | точка, подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `bench` | 3.5 млн | точка, подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `bus_station` | 67 тыс. | точка, подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `cinema` | 31 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `clinic` | 223 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `college` | 72 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `ferry_terminal` | 37 тыс. | полигон | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `fire_station` | 149 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `fountain` | 204 тыс. | полигон | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `hospital` | 218 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `kindergarten` | 341 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `library` | 113 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `lounger` | 19 тыс. | точка, подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `police` | 163 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `school` | 1.5 млн | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `shelter` | 702 тыс. | точка, подпись | topo_lean_to | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `social_facility` | 183 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `theatre` | 53 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `amenity` | `university` | 58 тыс. | подпись | — | `drinking_water`, `grave_yard`, `hunting_stand`, `marketplace`, `monastery`, `place_of_worship`, `prison`, `water_point` |
| `area:highway` | `cycleway` | 11 тыс. | полигон, подпись | — | `living_street`, `motorway`, `motorway_link`, `pedestrian`, `primary`, `primary_link`, `residential`, `secondary`, `secondary_link`, `service`, `tertiary`, `tertiary_link`, `trunk`, `trunk_link`, `unclassified` |
| `area:highway` | `footway` | 100 тыс. | полигон, подпись | — | `living_street`, `motorway`, `motorway_link`, `pedestrian`, `primary`, `primary_link`, `residential`, `secondary`, `secondary_link`, `service`, `tertiary`, `tertiary_link`, `trunk`, `trunk_link`, `unclassified` |
| `area:highway` | `steps` | 8 тыс. | полигон, подпись | — | `living_street`, `motorway`, `motorway_link`, `pedestrian`, `primary`, `primary_link`, `residential`, `secondary`, `secondary_link`, `service`, `tertiary`, `tertiary_link`, `trunk`, `trunk_link`, `unclassified` |
| `area:highway` | `track` | 5 тыс. | полигон, подпись | — | `living_street`, `motorway`, `motorway_link`, `pedestrian`, `primary`, `primary_link`, `residential`, `secondary`, `secondary_link`, `service`, `tertiary`, `tertiary_link`, `trunk`, `trunk_link`, `unclassified` |
| `boundary` | `*` | — | порядок | — | `forest_compartment`, `forestry_compartment`, `land_area`, `national_park` |
| `boundary` | `aboriginal_lands` | 6 тыс. | подпись | — | `forest_compartment`, `forestry_compartment`, `land_area`, `national_park` |
| `boundary` | `border_zone` | 61 | подпись | — | `forest_compartment`, `forestry_compartment`, `land_area`, `national_park` |
| `boundary` | `protected_area` | 137 тыс. | полигон, подпись | — | `forest_compartment`, `forestry_compartment`, `land_area`, `national_park` |
| `ford` | `stepping_stones` | 18 тыс. | линия, точка, подпись | — | `yes` |
| `highway` | `bus_guideway` | 574 | подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `bus_stop` | 4.2 млн | точка, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `busway` | 40 тыс. | линия, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `crossing` | 13.8 млн | точка | crossing_traffic_signals_map, crossing_uncontrolled_map | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `elevator` | 59 тыс. | линия, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `escape` | 2 тыс. | подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `passing_place` | 70 тыс. | точка | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `raceway` | 46 тыс. | подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `services` | 33 тыс. | линия, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `traffic_signals` | 2 млн | точка, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `trailhead` | 19 тыс. | точка, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `turning_circle` | 2.6 млн | точка | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `highway` | `via_ferrata` | 5 тыс. | линия, точка, подпись | — | `bridleway`, `cycleway`, `footway`, `living_street`, `motorway`, `motorway_link`, `path`, `pedestrian`, `primary`, `primary_link`, `residential`, `road`, `secondary`, `secondary_link`, `service`, `steps`, `tertiary`, `tertiary_link`, `track`, `trunk`, `trunk_link`, `unclassified` |
| `historic` | `*` | — | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `aircraft` | 8 тыс. | точка, полигон, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `battlefield` | 1 тыс. | подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `boundary_stone` | 98 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `cannon` | 6 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `charcoal_pile` | 52 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `city_gate` | 9 тыс. | полигон, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `milestone` | 10 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `quarry` | 1 тыс. | подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `rune_stone` | 504 | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `ship` | 1 тыс. | точка, полигон, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `tank` | 1 тыс. | точка, полигон, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `technical_monument` | 1 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `tomb` | 75 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `wayside_cross` | 242 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `wayside_shrine` | 175 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `historic` | `wreck` | 5 тыс. | точка, подпись | — | `archaeological_site`, `castle`, `memorial`, `mine`, `monastery`, `monument`, `ruins` |
| `landuse` | `brownfield` | 163 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `commercial` | 609 тыс. | подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `construction` | 278 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `education` | 45 тыс. | подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `garages` | 98 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `grassland` | 147 | полигон | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `greenery` | 14 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `greenfield` | 94 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `railway` | 101 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `recreation_ground` | 195 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `religious` | 165 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `retail` | 427 тыс. | подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `salt_pond` | 21 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `village_green` | 91 тыс. | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `landuse` | `water` | 30 | полигон, подпись | — | `allotments`, `basin`, `cemetery`, `farmland`, `farmyard`, `forest`, `grass`, `greenhouse_horticulture`, `industrial`, `landfill`, `logging`, `meadow`, `military`, `orchard`, `plant_nursery`, `quarry`, `reservoir`, `residential`, `vineyard` |
| `leisure` | `beach_resort` | 15 тыс. | полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `bleachers` | 62 тыс. | полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `common` | 30 тыс. | подпись | — | `nature_reserve`, `resort` |
| `leisure` | `firepit` | 51 тыс. | точка, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `garden` | 1.6 млн | точка, полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `park` | 1.3 млн | точка, полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `picnic_table` | 511 тыс. | точка, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `pitch` | 2.9 млн | полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `stadium` | 52 тыс. | полигон, подпись | — | `nature_reserve`, `resort` |
| `leisure` | `swimming_pool` | 3.1 млн | полигон | — | `nature_reserve`, `resort` |
| `leisure` | `water_park` | 13 тыс. | подпись | — | `nature_reserve`, `resort` |
| `man_made` | `*` | — | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `cairn` | 64 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `cellar_entrance` | 3 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `crane` | 23 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `cross` | 39 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `dyke` | 38 тыс. | линия | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `flare` | 11 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `geoglyph` | 7 тыс. | точка, полигон, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `goods_conveyor` | 24 тыс. | линия, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `groyne` | 52 тыс. | линия, полигон | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `kiln` | 32 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `mast` | 535 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `petroleum_well` | 343 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `pipeline` | 387 тыс. | линия, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `pumping_station` | 39 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `reinforced_slope` | 3 тыс. | линия, полигон, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `satellite_dish` | 10 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `silo` | 374 тыс. | полигон, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `snow_fence` | 7 тыс. | линия | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `street_cabinet` | 337 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `telescope` | 5 тыс. | точка, подпись | telescope_type_optical | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `utility_pole` | 457 тыс. | точка | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `ventilation_shaft` | 4 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `wastewater_plant` | 93 тыс. | подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `watermill` | 14 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `windmill` | 12 тыс. | точка, подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `man_made` | `works` | 244 тыс. | подпись | — | `adit`, `antenna`, `breakwater`, `bridge`, `bunker_silo`, `chimney`, `cutline`, `embankment`, `lighthouse`, `mineshaft`, `monitoring_station`, `observatory`, `pier`, `reservoir_covered`, `spoil_heap`, `storage_tank`, `survey_point`, `tower`, `water_tap`, `water_tower`, `water_well` |
| `military` | `danger_area` | 2 тыс. | полигон, подпись | — | `barracks`, `bunker`, `checkpoint` |
| `natural` | `*` | — | порядок | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `anthill` | 553 | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `arch` | 1 тыс. | линия, точка, полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `birds_nest` | 8 тыс. | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `coastline_broken` | — | линия | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `coastline_line` | — | линия | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `couloir` | 144 | линия, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `crater` | 306 | линия, полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `crevasse` | 36 тыс. | линия, полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `dune` | 3 тыс. | полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `fumarole` | 375 | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `geyser` | 1 тыс. | полигон | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `gorge` | 1 тыс. | линия, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `hot_spring` | 7 тыс. | полигон | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `land` | 56 тыс. | полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `landslide` | 8 тыс. | полигон, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `saddle` | 81 тыс. | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `shrub` | 318 тыс. | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `slope` | 2 тыс. | линия | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `natural` | `termite_mound` | 3 тыс. | точка, подпись | — | `arete`, `bare_rock`, `bay`, `beach`, `cape`, `cave_entrance`, `cliff`, `coastline`, `desert`, `earth_bank`, `fell`, `glacier`, `grassland`, `gully`, `heath`, `hill`, `massif`, `meadow`, `mesa`, `mountain_range`, `mud`, `peak`, `plateau`, `reef`, `ridge`, `rock`, `sand`, `scree`, `scrub`, `shingle`, `shoal`, `sinkhole`, `spring`, `stone`, `strait`, `tree`, `tree_row`, `tundra`, `valley`, `volcano`, `water`, `wetland`, `wood` |
| `place` | `allotments` | 22 тыс. | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `atoll` | 76 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `borough` | 797 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `city_block` | 106 тыс. | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `continent` | 11 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `country` | 228 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `county` | 8 тыс. | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `farm` | 290 тыс. | точка, подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `municipality` | 25 тыс. | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `ocean` | 5 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `plot` | 324 тыс. | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `place` | `sea` | 153 | подпись | — | `archipelago`, `city`, `hamlet`, `island`, `islet`, `isolated_dwelling`, `locality`, `neighbourhood`, `quarter`, `square`, `suburb`, `town`, `village` |
| `power` | `generator` | 6.9 млн | точка, полигон, подпись | generator_source_solar | `line`, `minor_line`, `plant`, `pole`, `substation`, `tower` |
| `power` | `portal` | 325 тыс. | линия, точка | — | `line`, `minor_line`, `plant`, `pole`, `substation`, `tower` |
| `power` | `transformer` | 265 тыс. | точка, подпись | — | `line`, `minor_line`, `plant`, `pole`, `substation`, `tower` |
| `railway` | `*` | — | порядок | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `buffer_stop` | 283 тыс. | точка | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `construction` | 44 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `disused` | 196 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `light_rail` | 45 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `miniature` | 15 тыс. | подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `monorail` | 5 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `preserved` | 1 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `subway` | 101 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `tram` | 119 тыс. | линия, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `tram_stop` | 55 тыс. | линия, точка, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `traverser` | 852 | полигон | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `turntable` | 4 тыс. | полигон | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `railway` | `yard` | 6 тыс. | точка, подпись | — | `abandoned`, `dismantled`, `funicular`, `halt`, `narrow_gauge`, `rail`, `razed`, `station` |
| `tourism` | `*` | — | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `apartment` | 92 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `aquarium` | 1 тыс. | подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `artwork` | 376 тыс. | полигон, подпись | memorial_statue | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `cabin` | 880 | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `camp_pitch` | 146 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `caravan_site` | 36 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `guest_house` | 201 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `hostel` | 62 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `hotel` | 456 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `information` | 1.5 млн | точка, подпись | board_type_nature, board_type_plants | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `motel` | 49 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `museum` | 112 тыс. | подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `theme_park` | 11 тыс. | подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `viewpoint` | 266 тыс. | точка, подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `tourism` | `zoo` | 9 тыс. | подпись | — | `alpine_hut`, `attraction`, `camp_site`, `chalet`, `picnic_site`, `wilderness_hut` |
| `waterway` | `*` | — | порядок | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `waterway` | `dock` | 7 тыс. | полигон | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `waterway` | `fish_pass` | 3 тыс. | линия, подпись | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `waterway` | `lock_gate` | 24 тыс. | линия, точка, подпись | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `waterway` | `rapids` | 128 тыс. | порядок | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |
| `waterway` | `wadi` | 3 тыс. | линия, подпись | — | `canal`, `dam`, `ditch`, `drain`, `drystream`, `river`, `riverbank`, `stream`, `waterfall`, `weir` |

## 4. Дополнительные свойства объектов

OsmAnd различает объекты дополнительными тегами (`additional=`): `access`, `tunnel`, `bridge`, `oneway`, `surface`, `smoothness`, `tracktype`, `winter_road`, `ice_road`, `horse_scale` и др. Ниже — свойства, которые OsmAnd использует, а в слое xtopo поля нет.

| тег | значение | недостающие свойства |
|---|---|---|
| `aeroway` | `aerodrome` | `aerodrome:type=military` |
| `amenity` | `place_of_worship` | `abandoned=yes`, `religion=buddhist`, `religion=christian`, `religion=hindu`, `religion=jewish`, `religion=muslim`, `religion=shinto`, `religion=sikh`, `religion=taoist`, `ruins=yes` |
| `building` | `*` | `object_type=node` |
| `highway` | `cycleway` | `layer=-2`, `layer=2`, `layer=3` |
| `highway` | `footway` | `layer=-2`, `layer=2`, `layer=3`, `trail_visibility=bad`, `trail_visibility=horrible`, `trail_visibility=no` |
| `highway` | `motorway_link` | `covered=yes`, `tunnel=yes` |
| `highway` | `path` | `assisted_trail=yes`, `bicycle=designated`, `layer=-2`, `layer=2`, `layer=3`, `trail_visibility=bad`, `trail_visibility=horrible`, `trail_visibility=no` |
| `highway` | `primary_link` | `covered=yes`, `tunnel=yes` |
| `highway` | `road` | `layer=-2`, `layer=2`, `layer=3` |
| `highway` | `secondary_link` | `covered=yes`, `tunnel=yes` |
| `highway` | `steps` | `conveying=yes`, `layer=-2`, `layer=2`, `layer=3` |
| `highway` | `track` | `layer=-2`, `layer=2`, `layer=3`, `trail_visibility=bad`, `trail_visibility=horrible`, `trail_visibility=no` |
| `highway` | `trunk_link` | `covered=yes`, `tunnel=yes` |
| `historic` | `memorial` | `memorial=plaque`, `memorial=stolperstein` |
| `landuse` | `basin` | `basin=detention` |
| `landuse` | `cemetery` | `religion=christian`, `religion=jewish`, `religion=muslim` |
| `landuse` | `quarry` | `resource=peat`, `resource=salt` |
| `man_made` | `observatory` | `observatory:type=astronomical` |
| `man_made` | `pier` | `access=no`, `access=permit`, `access=private`, `bicycle=no` |
| `man_made` | `tower` | `tower:type=bell_tower`, `tower:type=communication`, `tower:type=cooling`, `tower:type=lighting`, `tower:type=observation` |
| `natural` | `peak` | `summit:cross=yes` |
| `natural` | `volcano` | `status=active`, `volcano:type=mud` |
| `natural` | `wetland` | `intermittent=yes`, `wetland=bog`, `wetland=fen`, `wetland=mangrove`, `wetland=marsh`, `wetland=mud`, `wetland=palsa_bog`, `wetland=reedbed`, `wetland=saltmarsh`, `wetland=string_bog`, `wetland=swamp`, `wetland=tidalflat`, `wetland=wet_meadow` |
| `place` | `city` | `capital=yes` |
| `waterway` | `stream` | `intermittent=yes` |

### 4.1. Свойства, которых нет ни в одном слое xtopo

| свойство | значения в OsmAnd Topo | как учтено в xtopo |
|---|---|---|
| `access` | `access=no`, `access=permit`, `access=private` | **не хранится** |
| `aerodrome:type` | `aerodrome:type=military` | **не хранится** |
| `artwork_type` | `artwork_type=statue` | **не хранится** |
| `assisted_trail` | `assisted_trail=yes` | **не хранится** |
| `basin` | `basin=detention` | **не хранится** |
| `bicycle` | `bicycle=designated`, `bicycle=no` | **не хранится** |
| `board_type` | `board_type=nature`, `board_type=plants`, `board_type=wildlife` | **не хранится** |
| `bridge` | `bridge=yes` | зашито в запрос (значение отбирается при выгрузке) |
| `climbing_crag` | `climbing_crag=crag` | **не хранится** |
| `conveying` | `conveying=yes` | **не хранится** |
| `covered` | `covered=yes` | **не хранится** |
| `crossing` | `crossing=traffic_signals`, `crossing=uncontrolled`, `crossing=unmarked` | **не хранится** |
| `generator:source` | `generator:source=wind` | **не хранится** |
| `horse_scale_highway` | `horse_scale_highway=bridleway`, `horse_scale_highway=footway`, `horse_scale_highway=path`, `horse_scale_highway=residential`, `horse_scale_highway=service`, `horse_scale_highway=track` | **не хранится** |
| `information` | `information=audioguide`, `information=board`, `information=guidepost`, `information=map`, `information=office`, `information=route_marker`, `information=terminal` | **не хранится** |
| `memorial` | `memorial=plaque`, `memorial=stolperstein` | **не хранится** |
| `nr_high_density` | `nr_high_density=yes` | **не хранится** |
| `object_type` | `object_type=node` | **не хранится** |
| `observatory:type` | `observatory:type=astronomical` | зашито в запрос (значение отбирается при выгрузке) |
| `plant:source` | `plant:source=solar` | **не хранится** |
| `religion` | `religion=buddhist`, `religion=christian`, `religion=hindu`, `religion=jewish`, `religion=muslim`, `religion=shinto`, `religion=sikh`, `religion=taoist` | зашито в запрос (значение отбирается при выгрузке) |
| `resource` | `resource=peat`, `resource=salt` | **не хранится** |
| `shelter_type` | `shelter_type=lean_to` | **не хранится** |
| `status` | `status=active` | **не хранится** |
| `summit:cross` | `summit:cross=yes` | **не хранится** |
| `telescope:type` | `telescope:type=optical` | **не хранится** |
| `tower:type` | `tower:type=bell_tower`, `tower:type=communication`, `tower:type=cooling`, `tower:type=lighting`, `tower:type=observation` | зашито в запрос (значение отбирается при выгрузке) |
| `trail_visibility` | `trail_visibility=bad`, `trail_visibility=horrible`, `trail_visibility=no` | **не хранится** |
| `via_ferrata_scale` | `via_ferrata_scale=0`, `via_ferrata_scale=1`, `via_ferrata_scale=2`, `via_ferrata_scale=3`, `via_ferrata_scale=4`, `via_ferrata_scale=5`, `via_ferrata_scale=6` | **не хранится** |
| `volcano:type` | `volcano:type=mud` | зашито в запрос (значение отбирается при выгрузке) |
| `wetland` | `wetland=bog`, `wetland=fen`, `wetland=mangrove`, `wetland=marsh`, `wetland=mud`, `wetland=palsa_bog`, `wetland=reedbed`, `wetland=saltmarsh`, `wetland=string_bog`, `wetland=swamp`, `wetland=tidalflat`, `wetland=wet_meadow` | **не хранится** |

## 5. Обратная разница: объекты xtopo, которых нет в OsmAnd Topo

Всего: 34 — данные готовим, но этот стиль их не рисует.

| тег | значение | слои xtopo |
|---|---|---|
| `amenity` | `grave_yard` | `cemetery` |
| `amenity` | `marketplace` | `marketplace` |
| `amenity` | `water_point` | `water_point` |
| `area` | `yes` | `highway_main_area`, `place_square_highway_area` |
| `area:highway` | `living_street` | `highway_main_area` |
| `area:highway` | `motorway_link` | `highway_main_area` |
| `area:highway` | `primary_link` | `highway_main_area` |
| `area:highway` | `secondary_link` | `highway_main_area` |
| `area:highway` | `tertiary_link` | `highway_main_area` |
| `area:highway` | `trunk_link` | `highway_main_area` |
| `boundary` | `forest_compartment` | `forest_compartment` |
| `boundary` | `land_area` | `island` |
| `bridge` | `no` | `highway_main` |
| `building` | `church` | `place_of_worship_christian_ruins` |
| `building` | `train_station` | `building_train_station` |
| `disused:amenity` | `place_of_worship` | `place_of_worship_buddhist`, `place_of_worship_christian`, `place_of_worship_christian_ruins`, `place_of_worship_hindu`, `place_of_worship_jewish`, `place_of_worship_muslim`, `place_of_worship_other`, `place_of_worship_shinto`, `place_of_worship_sikh`, `place_of_worship_taoist` |
| `historic` | `monastery` | `monastery_christian`, `monastery_no_religion` |
| `name` | `*` | `ridge_names` |
| `name:en` | `*` | `ridge_names` |
| `name:ru` | `*` | `ridge_names` |
| `natural` | `arete` | `ridge` |
| `natural` | `gully` | `earth_bank_gully` |
| `natural` | `massif` | `mountain_area` |
| `natural` | `meadow` | `grass` |
| `natural` | `mesa` | `plateau` |
| `natural` | `mountain_range` | `mountain_area` |
| `natural` | `mud` | `mud` |
| `natural` | `tundra` | `tundra` |
| `place` | `archipelago` | `island` |
| `railway` | `dismantled` | `railway_disused` |
| `railway` | `razed` | `railway_disused` |
| `ridge` | `yes` | `ridge` |
| `tunnel` | `no` | `canal`, `highway_main`, `river`, `stream` |
| `volcano:type` | `dirt` | `volcano_dirt` |

