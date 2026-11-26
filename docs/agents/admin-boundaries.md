# Границы admin_level: дубли линий

Статус: расследование не закончено, правки в код не внесены.
Дата: 2026-11-26. Проект, на котором замечено: `~/qgis_projects/automap_test`
(подготовлен 16:12, HEAD `5bcb754`).

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

Источник: `~/qgis_projects/qgisxtopo-cache/geofabrik/slice_9d4b4691e9a8.osm.pbf`
(это ровно bbox проекта), bbox `70.2767,39.8159,71.3019,41.5353`.
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

`core/pipeline.py`, `_admin_border_members` (~1051–1055):

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

Простое исправление — сравнивать в одном формате, например
`{f"way/{way_id}" for way_id in member_ids}` или разбирать числовую часть
`@id`. После этого `admin_level_2_lines` должен остаться ровно 121 объект.

### 2. Для регионов fallback дописывает государственную границу

Даже после исправления id нельзя дописывать в `admin_level_4_proc` все
отсутствующие члены отношений: в отношение региона входят участки
государственной границы. У них может не быть `admin_level=4` (например,
река по границе), поэтому они попадают в «missing» и возвращают в слой
регионов линии, которые уже есть в `admin_level_2_dissolved`.

В текущем проекте так и получилось: 147 из 148 объектов `admin_level_4_proc`
дописаны fallback, и все они лежат на границе страны.

Для уровня 4 дописываемые участки нужно вычитать из
`admin_level_2_dissolved` (или из `admin_level_2_lines`), а не добавлять
как есть. При этом настоящие дырки внутренних границ регионов должны
остаться заполненными: отсутствующий участок может быть общим для двух
регионов одной страны.

### 3. Порядок операций: fallback идёт после dissolve/difference

`_admin_border_members` пишет в уже готовые `_lines` и
`_dissolved`/`_proc`, то есть **после** `dissolve`, `singleparts` и
`difference`. Поэтому дописанные линии:

- не проходят через union/dissolve и могут дублировать существующие;
- не вычитаются из регионов;
- не попадают в другие проверки рецепта.

Даже при исправленном сравнении id это надо учитывать: fallback должен
либо дописывать исходный слой до рецепта, либо после дописывания повторять
union/difference для затронутых слоёв.

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

### 5. Fallback запускается только при пустом слое названий

`Pipeline.run` вызывает `_admin_relation_names`, `_admin_border_members` и
`_admin_name_fallback` только если `_admin_fallback_needed()` — то есть
один из `admin_level_*_relation.sqlite` пуст. Если отношения в выгрузке
есть, дырки от участков без тегов (`admin_level` у реки) не заполняются.
Это отдельный вопрос, но при переделке fallback его стоит решить вместе:
заполнение пропущенных членов не должно зависеть от наличия подписей.

## Что делать дальше

1. Исправить сравнение id (`_admin_border_members` / `_line_feature_ids`) —
   это минимальная правка, убирающая массовые точные копии.
2. Для `admin_level_4` вычитать из дописываемых участков
   `admin_level_2_dissolved`; проверить на этом же PBF, что
   `admin_level_4_proc ∩ admin_level_2_dissolved` после fallback пуст
   (кроме допустимых точечных касаний), а внутренние границы регионов
   не потеряны.
3. Решить, откуда берутся линии admin2: ways + члены отношений или
   ways + контуры отношений. Если контуры остаются, после сборки нужен
   union линий, иначе на полных отношениях дубли вернутся.
4. Добавить тест: после сборки admin-слоёв нет повторяющихся `@id`,
   `sum(length)` близка к `union.length`, а `admin_level_4_proc` не
   содержит участков `admin_level_2_dissolved`.
5. Пересобрать проект на `slice_9d4b4691e9a8.osm.pbf` и проверить
   визуально район Ферганской долины + посчитать длины тем же скриптом.

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
