# OsmAnd: правила `rendering_types.xml`, похожие на исправления данных

Дата проверки: 2026-11-27.

Источник: `/home/xmd5a/git/OsmAnd-resources/obf_creation/rendering_types.xml`,
коммит `7149c8f02` (2026-11-27). Файл на момент проверки не изменён.

## Что и как проверялось

- Разобраны только активные `entity_convert` (XML парсился `lxml`, закомментированные
  правила не учитывались).
- Отобраны правила, которые выглядят как исправление ошибочных, устаревших,
  конфликтующих или нестандартных тегов, а не как обычная регистрация типов.
- Числа `N` — глобальное количество объектов или пар тегов по данным
  taginfo/Overpass на 2026-11-27. Для комбинаций это число пар тегов, а не
  обязательно число объектов, к которым правило применится после ограничений
  `apply_to`; в спорных случаях это указано.
- `overpass.osm.ch` — региональная база (Швейцария), для глобальных чисел не
  использовалась. Глобальные счётчики брались из taginfo и `maps.mail.ru`
  (последний работает нестабильно, часть запросов падала по таймауту).
- `taginfo tag/combinations` не показывает очень редкие комбинации (порог),
  поэтому для редких правил использовался Overpass.

## A. Явные исправления ошибочных/устаревших данных

- **775** `man_made=bridge` + `highway=footway` → удалить `man_made=bridge` — **531, актуально**
- **777** `bridge` + `highway=proposed` → удалить `bridge` — **10 447, актуально**
- **799** `tunnel` + `highway=proposed` → удалить `tunnel` — **2 721, актуально**
- **2221** `highway=bus_stop` + `public_transport=platform` → удалить `highway=bus_stop` — **3 695 941, актуально**
- **2235** `route` + `abandoned=yes` (relation) → удалить `route` — **204, актуально**
- **2735** `sidewalk` + `demolished:highway` → удалить `sidewalk` — **189, актуально**
- **3164** `horse_scale` + `highway=cycleway` → удалить `horse_scale` — **169, актуально**
- **3801** `barrier=toll_booth` + `toll=no` → удалить `barrier` — **37, актуально, единичные**
- **3865** `waterway=river` на node → удалить — **2 518 нод, актуально**
- **3866** `waterway=stream` на node → удалить — **5 452 ноды, актуально**
- **4491** `man_made=pipeline` + `pipeline=substation` → удалить `man_made=pipeline` — **927, актуально**
- **4493** `landuse=industrial` + `pipeline=substation` → удалить `landuse=industrial` — **17 301, актуально**
- **5188** `tourism=attraction` + `natural=water` → удалить `tourism=attraction` — **1 347, актуально**
- **5189** `tourism=yes` + `natural=water` → удалить `tourism=yes` — **63, актуально, единичные**
- **5205** `tourism=artwork` + `historic=memorial` → удалить `tourism=artwork` — **7 658, актуально**
- **5206** `tourism=artwork` + `historic=monument` → удалить `tourism=artwork` — **852, актуально**
- **5210** `railway` + `roller_coaster=track` → удалить `railway` — **10, актуально, единичные**
- **5211** `attraction=roller_coaster` + `roller_coaster=track` → удалить `attraction` — **438, актуально**
- **5260** `tourism=information` + `information=guidepost` → удалить `tourism=information` — **681 078, актуально** (валидное сочетание, скорее дедуп)
- **5385** `religion` + `denomination=jehovahs_witness` → удалить `religion` — **14 092, спорно:** удаляет и `religion=christian`; неверный `religion=jehovahs_witness` — только **3**
- **5491** `historic=quarry` + `natural` → удалить `historic=quarry` — **121, актуально**
- **5532** `landuse=landfill` + `man_made=spoil_heap` → удалить `landuse=landfill` — **1 803, актуально**
- **5612** `natural=cliff` + `waterway=waterfall` → удалить `natural=cliff` — **482, актуально**
- **5636** `natural=bare_rock` + `geological=volcanic_lava_field` → удалить `natural` — **832, актуально**
- **5637** `natural=scree` + `geological=volcanic_lava_field` → удалить `natural` — **116, актуально**
- **5669** `natural=scrub` + `man_made=clearcut` → удалить `natural=scrub` — **38 645, актуально**
- **5689** `natural=valley` + `waterway` → удалить `natural=valley` — **9 794, актуально**
- **5690** `natural=gully` + `waterway` → удалить `natural=gully` — **7 743, актуально**
- **5780** `natural=wood` + `wood:lost` → удалить `natural=wood` — **45, актуально, единичные**
- **6081** `healthcare=pharmacy` + `amenity=pharmacy` → удалить `healthcare=pharmacy` — **329 180, актуально**
- **6407** `place=locality` + `historic` → удалить `place=locality` — **9 335, актуально**
- **6408** `place=allotments` + `landuse=allotments` → удалить `place=allotments` — **18 148, актуально**
- **6409** `place=locality` + `natural=dune` → удалить `place=locality` — **1 365, актуально**
- **6506** `admin_level` + `amenity=place_of_worship` → удалить `admin_level` — **100, актуально, единичные**
- **6507** `admin_level` + `building` → удалить `admin_level` — **18 245, актуально**
- **6508** `admin_level` + `natural=wood` → удалить `admin_level` — **34, актуально, единичные**
- **6509** `admin_level` + `boundary=religious_administration` → удалить `admin_level` — **13 121, актуально**

## B. Нестандартные значения и ключи

- **228** `access=restricted` → `private` — **4 280, актуально**
- **229–271** `vehicle/motor_vehicle/motorcar/motorcycle/foot/bicycle/boat=permit` → `private` — **~89 000, актуально**
- **260** `bicycle=official` → `designated` — **1 519, актуально**
- **283/284** `horse=official` / `motorhome=official` → `yes` — **286 / 2**, второе почти устарело
- **4561** `pump:status=out_of_order` → `broken` — **374, актуально**
- **4565/4566** `operational_status=non-operational/out_of_order` → `broken`/`closed` — **557 / 792, актуально**
- **4546** `man_made=communications_tower` → `tower` + `tower:type=communication` — **7 070, актуально**
- **5092** `craft=electronics` → `electronics_repair` — **235, актуально**
- **4784** `service:vehicle:repairs` → `service:vehicle:car_repair` — **3 558, актуально**
- **5218** `tourism=camping` → `camp_site` — **4, почти устарело**
- **3763** `barrier=field_boundary` → `fence` — **3 169, актуально**
- **6531** `boundary=forest_compartment` → `forestry_compartment` — **25 796, но направление обратное вики:** `forest_compartment` — approved, `forestry_compartment` — deprecated (**908**); правило переводит корректный тег в устаревший
- **6533** `boundary=protected_area` + `protect_class=25` → `landuse=military` — **8, актуально, единичные**
- **6514/6520/6525** `type=boundary` → `multipolygon` для `administrative/aboriginal_lands/border_zone` — **785 968 / 4 044 / 31**; нормализация, не ошибка
- **2238** `route`-отношение без `type=route` → `route=no_type_route` — **44, актуально, единичные**
- **2851/2852** `building=apartments/dormitory` → `residential` — **8 181 060 / 76 874**; нормализация
- **5510** `landuse=plantation` → `farmland` — **6 342**; нормализация
- **2854–2861** синонимы POI: `student_accomodation` **27**, `market` **137**, `convent` **174**, `community_hall` **150**, `social_club` **790**, `border_control` **297**, `craft=printing` **120**, `craft=photography` **6**
- **4860** `shop=printing` → `copyshop` — **2 169, актуально**
- **2853** `amenity=dormitory` → `student_accommodation` — **1 037, актуально**

## C. Мусорные значения

- **1019** `owner=UNKNOWN` → удалить — **0, устарело**
- **315** `maxspeed=.` → удалить — **0, устарело**
- **3746** `traffic_calming=no` → удалить — **13 451, актуально**
- **5026** `shop=no` → удалить — **3 035, актуально**
- **4445–4459** `generator:output:*=no` → удалить — **17, почти устарело**

## D. Дубли/приоритет рендера (не обязательно ошибка данных)

- **2990–2994** `building` на POI-нодах → удалить `building` — **актуально, массово**
- **4467–4477** `addr:housenumber` на POI-нодах → удалить — **актуально, массово**
- **2744/2745** `sidewalk:left/right=separate` + `foot=use_sidepath` → удалить sidewalk — **22 928 / 102 672, дедуп**
- **3688** `cycleway` + `cycleway:both` → удалить `cycleway` — **15 241, дедуп**
- **3644** `cycleway:oneway=yes` → удалить — **1 737; тег валидный, скорее скрытие**
- **4344/4345/4346/4400** дубли `public_transport=station/platform` — **89 088 / 919 / 19 159 / 22 840, дедуп**
- **4562** `amenity=drinking_water` + `man_made=water_well` → удалить `amenity` — **17 996, дедуп**
- **4804** `shop=vacant` → удалить — **84 936, скрытие пустующих**
- **4899** `craft` + `shop` → удалить `craft` — **22 825, приоритет**
- **2181/2182** `traffic_signals=blink_mode/emergency` → удалить `highway=traffic_signals` — **788 / 3 090, скрытие**
- **955** `name:signed` → удалить — **43 654, валидный тег**
- **4604/4605, 5930/5931, 6647/6648** приватные бассейны/парковки → удалить — **скрытие приватных объектов**
- **6713–6715** удаление `landuse/leisure/natural` при `osmand_climbing` — **внутренняя очистка**

## E. Устарело полностью

- **5857** `cuisine=vegan` + `diet:vegan` → удалить `cuisine=vegan` — **0** (`cuisine=vegan` в OSM отсутствует)
- **5241** `tourism=hanami` + `natural=tree` → удалить `tourism=hanami` — **0** (сам тег есть у **67** объектов, но не на деревьях)

## F. Спорные правила, которые стоит пересмотреть

- **6531** `forest_compartment` → `forestry_compartment`: направление противоречит вики;
  корректных объектов **25 796**, устаревших — **908**.
- **5385** удаление `religion` при `denomination=jehovahs_witness`: задевает **14 092**
  объекта, хотя неверный `religion=jehovahs_witness` только **3**; скорее всего,
  правило должно проверять значение `religion`.
- **5260**, **5205/5206**, **5188**, **5612**, **5689/5690**, **5491**, **6407/6409**:
  это валидные сочетания тегов, правило выбирает один объект для рендера. Не ошибка
  данных, но и не «исправление» в строгом смысле.
- **4562**, **4804**, **4899**, **4344–4400**, **2744/2745**, **3688**, **955**,
  **2181/2182**, **3644**: то же — дедупликация/скрытие, а не исправление ошибки.

## Что осталось проверить

- Точные глобальные числа только по нодам для **2990–2994** (`building` на POI-нодах)
  и **4467–4477** (`addr:housenumber` на POI-нодах). Taginfo показывает миллионы
  сочетаний, но включает way/relation; Overpass-запросы по нодам на `maps.mail.ru`
  упирались в таймаут.
- Глобальное число для **3001** (`building` + `aeroway=terminal`) — не получено.
- Разбивка по типам объектов для дедуп-правил `public_transport` (сейчас числа
  taginfo — общие пары тегов).
- Решение по **6531** (оставить как внутреннюю нормализацию OsmAnd или поменять
  направление) и по **5385** (добавить `from_value="jehovahs_witness"`).
- Проверить, не появились ли новые значения после 2026-11-27.

## Как перепроверить на другом ПК

Файл: `/home/xmd5a/git/OsmAnd-resources/obf_creation/rendering_types.xml`.

Активные правила (без закомментированных) удобно извлекать так:

```python
import lxml.etree as ET
root = ET.parse('obf_creation/rendering_types.xml').getroot()
for r in root.iter('entity_convert'):
    a = r.attrib
    if a.get('notosm') == 'true':
        continue
    print(r.sourceline, a)
```

Одиночные теги:

```sh
curl -sS 'https://taginfo.openstreetmap.org/api/4/tag/stats?key=boundary&value=forest_compartment'
```

Комбинации:

```sh
curl -sS 'https://taginfo.openstreetmap.org/api/4/tag/combinations?key=highway&value=bus_stop&rp=1000'
```

Overpass (глобальный, нестабильный):

```sh
curl -sS --max-time 100 -A 'Mozilla/5.0' \
  --data-urlencode 'data=[out:json][timeout:90];nwr["highway"="bus_stop"]["public_transport"="platform"];out count;' \
  'https://maps.mail.ru/osm/tools/overpass/api/interpreter'
```

`https://overpass.osm.ch/api/interpreter` — только Швейцария, для глобальных
чисел не подходит. `https://overpass-api.de` часто отвечает 406/502.
