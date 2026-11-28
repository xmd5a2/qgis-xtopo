# Что осталось добавить из OsmAnd Topo

Дата: 2026-11-28. Источник: `osmandapp/OsmAnd-resources`, `rendering_styles/topo.render.xml`, коммит `123a2680f5`.

Полное сравнение с таблицами по каждому ключу — [`osmand-topo-vs-xtopo.md`](osmand-topo-vs-xtopo.md). Обновить список:

```sh
python3 tools/compare_osmand_style.py
```

## Уже добавлено

`bus_station`; `tourism=viewpoint`, `highway=trailhead`, `tourism=information` (`information=guidepost` отдельным слоем); `artwork`, `museum`, `zoo`, `aquarium`, `theme_park`; `hotel`, `hostel`, `guest_house`, `motel`, `apartment`; `caravan_site`, `camp_pitch`; `tourism=cabin` в слое `wilderness hut`; линии `ladder=yes`, `ford=stepping_stones`, `highway=raceway`, `highway=escape`.

## 1. Приоритет по массовости

Самые массовые объекты OsmAnd Topo, которых в xtopo пока нет (числа — глобальные счётчики OSM по taginfo).

| Объект | Объектов в OSM |
|---|---|
| `highway`=`crossing` | 13.8 млн |
| `lit`=`yes` | 12 млн |
| `power`=`generator` | 6.9 млн |
| `public_transport`=`platform` | 4.2 млн |
| `highway`=`bus_stop` | 4.2 млн |
| `lit`=`no` | 3.7 млн |
| `amenity`=`bench` | 3.5 млн |
| `leisure`=`swimming_pool` | 3.1 млн |
| `leisure`=`pitch` | 2.9 млн |
| `highway`=`turning_circle` | 2.6 млн |
| `highway`=`traffic_signals` | 2 млн |
| `leisure`=`garden` | 1.6 млн |
| `amenity`=`school` | 1.5 млн |
| `leisure`=`park` | 1.3 млн |
| `admin_level`=`8` | 755 тыс. |
| `amenity`=`shelter` | 702 тыс. |
| `landuse`=`commercial` | 609 тыс. |
| `man_made`=`mast` | 535 тыс. |
| `leisure`=`picnic_table` | 511 тыс. |
| `admin_level`=`10` | 472 тыс. |
| `man_made`=`utility_pole` | 457 тыс. |
| `landuse`=`retail` | 427 тыс. |
| `man_made`=`pipeline` | 387 тыс. |
| `man_made`=`silo` | 374 тыс. |
| `man_made`=`petroleum_well` | 343 тыс. |
| `amenity`=`kindergarten` | 341 тыс. |
| `cycleway`=`lane` | 340 тыс. |
| `man_made`=`street_cabinet` | 337 тыс. |
| `admin_level`=`9` | 335 тыс. |
| `power`=`portal` | 325 тыс. |

## 2. Полностью отсутствующие ключи

### Дороги, улицы и транспорт

- `bicycle_road=yes` — 23 тыс.
- `cycleway=*` — любые значения
- `cycleway=both` — 696
- `cycleway=lane` — 340 тыс.
- `cycleway=opposite` — 8 тыс.
- `cycleway=opposite_lane` — 2 тыс.
- `cycleway=share_busway` — 4 тыс.
- `cycleway=shared_lane` — 96 тыс.
- `cycleway=track` — 67 тыс.
- `cycleway:both=*` — любые значения
- `cycleway:both=lane` — 99 тыс.
- `cycleway:both=share_busway` — 1 тыс.
- `cycleway:both=shared_lane` — 40 тыс.
- `cycleway:both=track` — 17 тыс.
- `emergency=*` — любые значения
- `emergency=rescue_box` — 80
- `entrance=*` — любые значения
- `junction=yes` — 134 тыс.
- `lit=*` — любые значения
- `lit=interval` — 640
- `lit=no` — 3.7 млн
- `lit=yes` — 12 млн
- `lock=yes` — 11 тыс.
- `public_transport=platform` — 4.2 млн
- `public_transport=station` — 204 тыс.

### POI, туризм и услуги

- `addr:flats=*` — любые значения
- `addr:housenumber=*` — любые значения
- `attraction=big_wheel` — 1 тыс.
- `club=*` — любые значения
- `craft=*` — любые значения
- `harbour=yes` — 5 тыс.
- `healthcare=*` — любые значения
- `office=*` — любые значения
- `shop=*` — любые значения
- `sport=climbing` — 91 тыс.

### Земля, природа и вода

- `abandoned:aeroway=*` — любые значения
- `abandoned:aeroway=aerodrome` — 693
- `abandoned:aeroway=runway` — 1 тыс.
- `abandoned:building=*` — любые значения
- `abandoned:building=no` — 242
- `abandoned:highway=*` — любые значения
- `abandoned:highway=footway` — 1 тыс.
- `abandoned:highway=motorway` — 953
- `abandoned:highway=motorway_link` — 835
- `abandoned:highway=path` — 19 тыс.
- `abandoned:highway=primary` — 3 тыс.
- `abandoned:highway=residential` — 4 тыс.
- `abandoned:highway=secondary` — 1 тыс.
- `abandoned:highway=service` — 4 тыс.
- `abandoned:highway=tertiary` — 1 тыс.
- `abandoned:highway=track` — 55 тыс.
- `abandoned:highway=trunk` — 1 тыс.
- `abandoned:highway=unclassified` — 5 тыс.
- `abandoned:landuse=*` — любые значения
- `abandoned:landuse=garages` — 4
- `abandoned:landuse=industrial` — 549
- `abandoned:landuse=military` — 489
- `abandoned:landuse=railway` — 41
- `abandoned:landuse=residential` — 1 тыс.
- `abandoned:place=*` — любые значения
- `abandoned:railway=station` — 9 тыс.
- `disused:building=*` — любые значения
- `disused:building=no`
- `geological=moraine` — 3 тыс.
- `geological=outcrop` — 2 тыс.
- `geological=palaeontological_site` — 857
- `geological=volcanic_lava_field` — 997
- `wood:lost=*` — любые значения
- `wood:lost=dead_wood` — 84
- `wood:lost=windfall` — 884

### Маршруты и прочее

- `horse_scale=*` — любые значения
- `icn_ref=*` — любые значения
- `iwn_ref=*` — любые значения
- `lcn_ref=*` — любые значения
- `lwn_ref=*` — любые значения
- `ncn_ref=*` — любые значения
- `nwn_ref=*` — любые значения
- `rcn_ref=*` — любые значения
- `rwn_ref=*` — любые значения

## 3. Ключ есть, но нужных значений нет

### Дороги, улицы и транспорт

- `admin_level`: `0`, `1` — 87, `10` — 472 тыс., `3` — 4 тыс., `5` — 50 тыс., `7` — 186 тыс., `8` — 755 тыс., `9` — 335 тыс.
- `aerialway`: `drag_lift` — 4 тыс., `magic_carpet` — 3 тыс., `mixed_lift` — 108, `rope_tow` — 1 тыс., `zip_line` — 2 тыс.
- `aeroway`: `helipad` — 71 тыс., `terminal` — 10 тыс.
- `area:highway`: `cycleway` — 11 тыс., `footway` — 100 тыс., `steps` — 8 тыс., `track` — 5 тыс.
- `highway`: `bus_guideway` — 574, `bus_stop` — 4.2 млн, `busway` — 40 тыс., `crossing` — 13.8 млн, `elevator` — 59 тыс., `passing_place` — 70 тыс., `services` — 33 тыс., `traffic_signals` — 2 млн, `turning_circle` — 2.6 млн, `via_ferrata` — 5 тыс.
- `railway`: `buffer_stop` — 283 тыс., `construction` — 44 тыс., `disused` — 196 тыс., `light_rail` — 45 тыс., `miniature` — 15 тыс., `monorail` — 5 тыс., `preserved` — 1 тыс., `subway` — 101 тыс., `tram` — 119 тыс., `tram_stop` — 55 тыс., `traverser` — 852, `turntable` — 4 тыс., `yard` — 6 тыс.

### POI, туризм и услуги

- `amenity`: `bench` — 3.5 млн, `cinema` — 31 тыс., `clinic` — 223 тыс., `college` — 72 тыс., `ferry_terminal` — 37 тыс., `fire_station` — 149 тыс., `fountain` — 204 тыс., `hospital` — 218 тыс., `kindergarten` — 341 тыс., `library` — 113 тыс., `lounger` — 19 тыс., `police` — 163 тыс., `school` — 1.5 млн, `shelter` — 702 тыс., `social_facility` — 183 тыс., `theatre` — 53 тыс., `university` — 58 тыс.
- `historic`: `aircraft` — 8 тыс., `battlefield` — 1 тыс., `boundary_stone` — 98 тыс., `cannon` — 6 тыс., `charcoal_pile` — 52 тыс., `city_gate` — 9 тыс., `milestone` — 10 тыс., `quarry` — 1 тыс., `rune_stone` — 504, `ship` — 1 тыс., `tank` — 1 тыс., `technical_monument` — 1 тыс., `tomb` — 75 тыс., `wayside_cross` — 242 тыс., `wayside_shrine` — 175 тыс., `wreck` — 5 тыс.
- `military`: `danger_area` — 2 тыс.

### Земля, природа и вода

- `boundary`: `aboriginal_lands` — 6 тыс., `border_zone` — 61, `protected_area` — 137 тыс.
- `landuse`: `brownfield` — 163 тыс., `commercial` — 609 тыс., `construction` — 278 тыс., `education` — 45 тыс., `garages` — 98 тыс., `grassland` — 147, `greenery` — 14 тыс., `greenfield` — 94 тыс., `railway` — 101 тыс., `recreation_ground` — 195 тыс., `religious` — 165 тыс., `retail` — 427 тыс., `salt_pond` — 21 тыс., `village_green` — 91 тыс., `water` — 30
- `leisure`: `beach_resort` — 15 тыс., `bleachers` — 62 тыс., `common` — 30 тыс., `firepit` — 51 тыс., `garden` — 1.6 млн, `park` — 1.3 млн, `picnic_table` — 511 тыс., `pitch` — 2.9 млн, `stadium` — 52 тыс., `swimming_pool` — 3.1 млн, `water_park` — 13 тыс.
- `man_made`: `cairn` — 64 тыс., `cellar_entrance` — 3 тыс., `crane` — 23 тыс., `cross` — 39 тыс., `dyke` — 38 тыс., `flare` — 11 тыс., `geoglyph` — 7 тыс., `goods_conveyor` — 24 тыс., `groyne` — 52 тыс., `kiln` — 32 тыс., `mast` — 535 тыс., `petroleum_well` — 343 тыс., `pipeline` — 387 тыс., `pumping_station` — 39 тыс., `reinforced_slope` — 3 тыс., `satellite_dish` — 10 тыс., `silo` — 374 тыс., `snow_fence` — 7 тыс., `street_cabinet` — 337 тыс., `telescope` — 5 тыс., `utility_pole` — 457 тыс., `ventilation_shaft` — 4 тыс., `wastewater_plant` — 93 тыс., `watermill` — 14 тыс., `windmill` — 12 тыс., `works` — 244 тыс.
- `natural`: `anthill` — 553, `arch` — 1 тыс., `birds_nest` — 8 тыс., `coastline_broken`, `coastline_line`, `couloir` — 144, `crater` — 306, `crevasse` — 36 тыс., `dune` — 3 тыс., `fumarole` — 375, `geyser` — 1 тыс., `gorge` — 1 тыс., `hot_spring` — 7 тыс., `land` — 56 тыс., `landslide` — 8 тыс., `saddle` — 81 тыс., `shrub` — 318 тыс., `slope` — 2 тыс., `termite_mound` — 3 тыс.
- `place`: `allotments` — 22 тыс., `atoll` — 76, `borough` — 797, `city_block` — 106 тыс., `continent` — 11, `country` — 228, `county` — 8 тыс., `farm` — 290 тыс., `municipality` — 25 тыс., `ocean` — 5, `plot` — 324 тыс., `sea` — 153
- `power`: `generator` — 6.9 млн, `portal` — 325 тыс., `transformer` — 265 тыс.
- `waterway`: `dock` — 7 тыс., `fish_pass` — 3 тыс., `lock_gate` — 24 тыс., `rapids` — 128 тыс., `wadi` — 3 тыс.

## 4. Свойства объектов, которых нет в слоях

OsmAnd различает объекты по этим тегам, а xtopo их не сохраняет (или отбирает значение при выгрузке, не оставляя поля).

| Свойство | Значения в OsmAnd | Состояние |
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
| `information` | `information=audioguide`, `information=board`, `information=guidepost`, `information=map`, `information=office`, `information=route_marker`, `information=terminal` | зашито в запрос (значение отбирается при выгрузке) |
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

## 5. С чего продолжить

1. Массовые дорожные и транспортные POI: `highway=crossing`, `traffic_signals`, `bus_stop`, `turning_circle`, `public_transport=platform`.
2. `lit=yes/no` и `access` как свойства дорог: без них нельзя приглушать закрытые и неосвещённые дороги.
3. Полигоны отдыха: `leisure=park`, `garden`, `pitch`, `swimming_pool`.
4. Железные дороги: `railway=tram`, `subway`, `light_rail`, `tram_stop`.
5. Административные границы: `admin_level=8/9/10`.
6. Остальные `landuse`, `natural`, `historic`, `man_made` и `shop`/`office` по таблицам выше.
7. Свойства: `access`, `trail_visibility`, `wetland`, `covered`, `crossing`, `bicycle`, `via_ferrata_scale`, `information`/`board_type`.
