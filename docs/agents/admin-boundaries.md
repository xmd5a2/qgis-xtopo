# Границы admin_level: дубли линий

Статус: основные причины исправлены 2026-11-26, перепроверено на вырезе
Ферганской долины. Правки — `core/pipeline.py` (`_admin_border_members`,
`_line_geometry_union`), тесты — `AdminMemberBorderTest`
(`tests/test_pipeline_logic.py`). Проект, на котором заметили дубли:
`~/qgis_projects/automap_test` (подготовлен 16:12, HEAD `5bcb754`).

## Симптом

На границе Кыргызстан — Узбекистан — Таджикистан (район Ферганской долины)
граница рисуется двумя линиями. Identify в этой точке показывает несколько
объектов: `admin level 2 (country borders)` (в том числе
`Border Kyrgyzstan - Uzbekistan` и `Border Uzbekistan - Tajikistan`) и
`admin level 4 (regions)` (`Farg'ona Viloyati`).

Дубликаты массовые, а не единичный случай: в слое стран почти каждая линия
лежит дважды.

## Что показали данные текущего проекта

Считалось по `vector/admin_level_*.sqlite` (длины в градусах, `unary_union`
из shapely):

| слой | объектов | уникальных `@id` | лишних копий | сумма длин | длина union |
|---|---:|---:|---:|---:|---:|
| `admin_level_2_lines` | 240 | 121 | 119 | 17.2415 | 9.1113 |
| `admin_level_2_dissolved` | 131 | 121 | 10 | 17.2380 | 9.1113 |
| `admin_level_4_lines` | 164 | 147 | 17 | 11.1643 | 10.4681 |
| `admin_level_4_proc` | 148 | 147 | 1 | 11.0938 | 10.4681 |

`admin_level_2_dissolved` и `admin_level_4_proc` пересекаются на 9.1113 —
это ровно весь union слоя стран. То есть слой регионов содержит всю
государственную границу, а не только внутренние границы регионов.
`admin_level_4_proc` минус `admin_level_2_dissolved` даёт всего 1.3568.

У дубликатов совпадают `@id` и WKB: например `way/776594649` встречается
дважды с одинаковой геометрией (`FID 81` и `FID 201`). В `admin_level_2_lines`
первые ~120 объектов — исходные линии, дальше идут их точные копии.

## Воспроизведение на чистой сборке

Источник: вырез `slice_9d4b4691e9a8.osm.pbf` (он же bbox проекта),
bbox `70.2767,39.8159,71.3019,41.5353`; файл удалён при чистке temp.
Повторить можно из локальных Geofabrik PBF `kyrgyzstan`, `uzbekistan`,
`tajikistan`, передав их списком в `pbf_source.prepare_dataset`: склеивать
в один PBF не нужно, после `osmium merge` GDAL не читает дубли узлов.
Запросы только административные:

```python
array_queries = [
    "admin_level_2", "admin_level_2_relation",
    "admin_level_4", "admin_level_4_relation",
]
source_mode = "pbf"
pbf_path = ".../slice_9d4b4691e9a8.osm.pbf"
generate_terrain = False
parallel_workers = 1
```

Запуск — `qgis_xtopo.core.pipeline.run_project` под `QgsApplication`
(`QT_QPA_PLATFORM=offscreen`). В логе видно:

```
PBF: 240383 objects, 136 feature matches
admin_level_2_relation: no objects, skipped
admin_level_4_relation: no objects, skipped
admin_level_2: добавлены участки границы без тегов admin_level, объектов 121
admin_level_4: добавлены участки границы без тегов admin_level, объектов 147
admin_level_2: подписи собраны из линий границы, объектов 5
admin_level_4: подписи собраны из линий границы, объектов 7
```

Если отключить очистку `tmp` (`pipeline._clean_temp = lambda config: None`),
видны промежуточные файлы:

| файл | объектов | что это |
|---|---:|---|
| `admin_level_4_fixed_4.gpkg` | 17 | исходные admin_level=4 линии из PBF |
| `admin_level_4_difference_5.gpkg` | 17 | разность отработала |
| `admin_level_4_dissolved_6.gpkg` | 1 | растворение |
| `admin_level_4_parts_7.gpkg` | 2 | итог рецепта до fallback |
| `admin_level_4_proc.sqlite` | 148 | +147 объектов, дописанных fallback |

То есть `native:difference` не виноват: рецепт `admin_level_4` отрабатывает
и оставляет 2 объекта. Всю государственную границу в слой регионов
добавляет `_admin_border_members` уже после рецепта.

`admin_level_2_dissolved` устроен так же: 10 частей после рецепта плюс
121 дописанный объект.

## Причины

### 1. Разные типы id при поиске отсутствующих членов отношений

Исправлено.

`core/pipeline.py`, `_admin_border_members` (~1051–1055). Было:

```python
member_ids = set(entry.get("ways", ()))   # int: 776594649
existing = _line_feature_ids(lines_path)  # str: "way/776594649"
missing = sorted(member_ids - existing)   # всегда все члены
```

`_parse_admin_relation_tags` кладёт в `ways` целые числа
(`int(value)` из `w123@`), а `_line_feature_ids` возвращает строки `@id`
вида `way/123`. Пересечение всегда пустое, поэтому в конец слоя
дописываются **все** члены отношения, включая уже имеющиеся линии.
Это и даёт почти двойной слой стран и точные WKB-копии.

Теперь числовая часть `@id` разбирается через `_way_id_from_value`, и
`missing` считается по целым id. После правки `admin_level_2_lines`
остаётся ровно 121 объект.

### 2. Для регионов fallback дописывает государственную границу

Исправлено: из дописываемых участков вычитается объединение уже
записанных линий уровня и `admin_level_2_dissolved`.

Даже после исправления id нельзя дописывать в `admin_level_4_proc` все
отсутствующие члены отношений: в отношение региона входят участки
государственной границы. У них может не быть `admin_level=4` (например,
река по границе), поэтому они попадают в «missing» и возвращают в слой
регионов линии, которые уже есть в `admin_level_2_dissolved`.

В текущем проекте так и получилось: 147 из 148 объектов `admin_level_4_proc`
дописаны fallback, и все они лежат на границе страны.

Теперь участки вычитаются из `admin_level_2_dissolved` (и из уже
записанных линий уровня), а не добавляются как есть. Настоящие дырки
внутренних границ регионов остаются заполненными: отсутствующий участок
может быть общим для двух регионов одной страны.

### 3. Порядок операций: fallback идёт после dissolve/difference

`_admin_border_members` пишет в уже готовые `_lines` и
`_dissolved`/`_proc`, то есть **после** `dissolve`, `singleparts` и
`difference`. Поэтому дописанные линии:

- не проходят через union/dissolve и могут дублировать существующие;
- не вычитаются из регионов;
- не попадают в другие проверки рецепта.

Сейчас это закрыто маской: дописывается только разность участка и уже
записанных линий, поэтому наложения на готовые слои не возникает и
повторный dissolve не нужен.

### 4. Общий риск: admin_level_2 берёт и ways, и relations

`resources/queries/admin_level_2.txt` (и `admin_level_4.txt`) содержит
`way[...]` и `relation[...]`. Когда GDAL отдаёт полное отношение страны,
`polygonstolines` превращает его контур в линии, и граница дублируется:
один раз как way с `admin_level=2`, второй раз как контур отношения. У общей
границы двух стран контур добавляет ещё одну копию от каждого отношения.
В этом PBF отношения неполные, поэтому `*_relation` пуст и сработал
fallback; но на полной выгрузке страны проблема проявится и без fallback.

Надёжный вариант — строить линии только из way и членов отношений
(через osmium), а отношения использовать только для названий/подписей.
Либо после сборки всех источников делать line union, а не `native:dissolve`
по отдельным объектам. Проверить, нужно ли вообще оставлять
`relation[...]` в запросах линий; `*_relation` уже вынесен в отдельный
запрос.

Запросы решено не менять: маска перед дописыванием снимает наложения
контура отношения на way, а `_dissolved`/`_proc` и так собираются через
dissolve, где совпадающие линии сливаются. Отдельный line union в рецепте
не понадобился.

### 5. Fallback запускается только при пустом слое названий

`Pipeline.run` вызывает `_admin_relation_names`, `_admin_border_members` и
`_admin_name_fallback` только если `_admin_fallback_needed()` — то есть
один из `admin_level_*_relation.sqlite` пуст. Если отношения в выгрузке
есть, дырки от участков без тегов (`admin_level` у реки) не заполняются.
Это отдельный вопрос, но при переделке fallback его стоит решить вместе:
заполнение пропущенных членов не должно зависеть от наличия подписей.

## Что сделано

`_admin_border_members` теперь:

- сравнивает члены отношения с линиями по числовому way id
  (`_way_id_from_value`), а не по строке `@id`;
- строит маску из объединения уже записанных линий (`_line_geometry_union`,
  `QgsGeometry.unaryUnion`) и вычитает её из каждого дописываемого участка;
  для `admin_level_4` в маску входит и `admin_level_2_dissolved`;
- пропускает участок, если после вычитания от него ничего не осталось.

Маска закрывает и случай полного отношения из пункта 4: контур отношения
даёт те же линии, что и входящие в него way, поэтому дописываемые члены
по ней обрезаются. Запросы `admin_level_*.txt` не менялись.

### Замеры на вырезе Ферганской долины

Тот же bbox `70.2767,39.8159,71.3019,41.5353`, три локальных PBF
(`kyrgyzstan`, `uzbekistan`, `tajikistan`) в одном `prepare_dataset`.
Длины в градусах, `union` — shapely `unary_union`.

| слой | до: объектов / `sum` / `union` | после: объектов / `sum` / `union` |
|---|---|---|
| `admin_level_2_lines` | 240 / 17.2415 / 9.1113 | 121 / 9.1113 / 9.1113 |
| `admin_level_2_dissolved` | 131 / 17.2380 / 9.1113 | 13 / 9.1113 / 9.1113 |
| `admin_level_4_lines` | 164 / 11.1643 / 10.4681 | 26 / 1.3568 / 1.3568 |
| `admin_level_4_proc` | 148 / 11.0938 / 10.4681 | 11 / 1.3568 / 1.3568 |

Дописано fallback: level 2 — 2 объекта вместо 121, level 4 — 9 вместо 147.
`admin_level_4_proc ∩ admin_level_2_dissolved` уменьшилось с 9.1113 до 0,
`admin_level_4_proc - admin_level_2_dissolved` осталось 1.3568: внутренние
границы регионов не потеряны. У `admin_level_2_dissolved` 13 объектов при
3 уникальных `@id` — это разбиение одного way на части (11 LineString с
одним `@id`), а не дубли: `sum` ровно равен `union`.

Подписи из линий: level 2 — по-прежнему 5 объектов, но площадь полос
вдвое меньше (раньше дубли линий добавляли две наложенные полосы);
level 4 — 3 объекта вместо 7, потому что полосы вдоль государственной
границы в слой регионов больше не попадают.

Проект собран из `automap.qgz` и отрендерен offscreen: в «до» вдоль всей
государственной границы поверх красной линии стран шла синяя линия
регионов, в «после» на границе страны остаётся только красная, синие —
внутренние границы регионов.

### Тесты

`AdminMemberBorderTest` (`tests/test_pipeline_logic.py`):

- `test_member_with_same_way_id_is_not_appended` — id `way/123`;
- `test_member_covered_by_relation_outline_is_not_appended` — контур
  отношения уже содержит участок;
- `test_level_4_member_on_country_border_is_not_appended` — граница страны
  не возвращается в регионы;
- `test_member_is_trimmed_to_missing_part` — из участка вычитается только
  уже имеющаяся часть.

## Что осталось

1. Fallback по-прежнему запускается только при пустом слое названий
   (`_admin_fallback_needed`): если отношения в выгрузке есть, участки без
   `admin_level` у рек не заполняются. Заполнение не должно зависеть от
   наличия подписей, но сначала нужно оценить стоимость `osmium getid` на
   больших выгрузках.
2. На полной выгрузке страны проверить промежуточный `_lines`: в нём
   останутся и way, и контуры отношений. На готовые `_dissolved`/`_proc`
   это не влияет (dissolve сливает совпадающие линии, маска снимает
   наложения), но `sum/union` у `_lines` будет больше 1.
3. После правки поведения пересобрать плагин и Windows-инсталлятор
   (`python3 tools/build_plugin.py --install`,
   `WINEPREFIX=~/.cache/wineprefix-inno ./installer/build-installer.sh`).

## Полезные команды проверки

```bash
# объекты и уникальные id
python3 - <<'PY'
from osgeo import ogr
from collections import Counter
p='/path/vector/admin_level_2_dissolved.sqlite'
ds=ogr.Open(p); lyr=ds.GetLayer(0)
ids=Counter(f.GetField('@id') for f in lyr)
print(sum(ids.values()), len(ids), sum(v-1 for v in ids.values() if v>1))
PY
```

Длины и пересечения считались shapely по WKB из OGR:
`unary_union(...)`, `.length`, `.intersection`, `.difference`.

Временные файлы воспроизведения (`/tmp/opencode/...`) после сохранения
этой заметки удалены. Всё нужное для повтора — исходный PBF, bbox и
запросы из раздела «Воспроизведение на чистой сборке».
