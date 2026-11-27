# Границы admin_level: дубли линий и fallback

Справочная часть `AGENTS.md`. Здесь — как собираются линии границ, откуда
берутся дубли и какие инварианты удерживает fallback. Чтение отношений,
стороны границы и подписи описаны в `drivers.md` и `labels.md`; код —
`core/pipeline.py` (`_admin_relation_names`, `_admin_border_members`,
`_admin_name_fallback`), тесты — `AdminMemberBorderTest` и
`AdminNameFallbackTest` (`tests/test_pipeline_logic.py`).

## Слои и порядок сборки

Запросы `resources/queries/admin_level_2.txt` и `admin_level_4.txt` берут и
`way`, и `relation`. Рецепт каждого уровня сохраняет `*_lines` до
`dissolve`, а слой отрисовки строит уже из растворённых линий:

- `admin_level_2`: `_lines` → `dissolve` → `admin_level_2_dissolved`;
- `admin_level_4`: `_lines` → `difference(admin_level_2_dissolved)` →
  `dissolve` → `admin_level_4_proc`.

В `*_lines` попадают и way с `admin_level`, и контуры отношений, которые
`polygonstolines` делает из полигонов. Поэтому `sum` длин `*_lines` может
быть больше `union`: это две формы одной границы, а не ошибка. Для
отрисовки важны `_dissolved`/`_proc`: в них совпадающие линии слиты, а
государственная граница из регионов вычтена.

После слоёв `Pipeline.run` при `_admin_fallback_needed()` вызывает
`_admin_relation_names`, `_admin_border_members` и
`_admin_name_fallback`. `_admin_border_members` дописывает участки-члены
отношений без тегов `admin_level` (например, реку по границе): GDAL
выбрасывает неполное отношение целиком, и такого участка нет ни в
`_lines`, ни в `_dissolved`/`_proc`. Без osmium или без названий
отношений дописывание не выполняется.

## Причины дублей

Симптом — государственная граница нарисована дважды, а в слое регионов
присутствует вся граница страны.

### id члена отношения и `@id` линии сравнивались в разных типах

`_parse_admin_relation_tags` хранит id членов как целые числа, а
`_line_feature_ids` возвращает строки `@id` вида `way/123`. Пересечение
всегда пустое:

```python
member_ids = set(entry.get("ways", ()))   # int: 123
existing = _line_feature_ids(lines_path)  # str: "way/123"
missing = sorted(member_ids - existing)   # все члены
```

В конец слоя дописывались все члены отношения, включая уже нарисованные,
поэтому слой стран почти удваивался точными WKB-копиями. Теперь числовая
часть `@id` разбирается `_way_id_from_value`, и `missing` считается по
целым id.

### В отношение региона входят участки государственной границы

Участок государственной границы может не иметь `admin_level=4` (река по
границе страны). После исправления id он всё равно попадает в `missing` и
возвращает в `admin_level_4_proc` линию, которая уже есть в
`admin_level_2_dissolved`.

Поэтому маска для дописываемых участков собирается из уже записанных
линий уровня, а для `admin_level_4` — ещё и из
`admin_level_2_dissolved`. Маска объединяется через
`_line_geometry_union` (`QgsGeometry.unaryUnion`), из каждого участка
вычитается `Difference`, а пустой остаток пропускается. Настоящие дырки
внутренних границ регионов при этом не теряются: отсутствующий участок
может быть общим для двух регионов одной страны.

### Fallback работает после dissolve/difference

`_admin_border_members` пишет в уже готовые `*_lines` и
`_dissolved`/`_proc`, то есть после `dissolve`, `singleparts` и
`difference`. Дописанные линии не проходят рецепт, поэтому без маски они
могли бы наложиться на существующие. Маска закрывает это, и повторный
`dissolve` для дописанных участков не нужен.

### `admin_level` берёт и way, и контур отношения

Полное отношение страны `polygonstolines` превращает в контур, и граница
дублируется: один раз как way с `admin_level=2`, второй раз как контур
отношения. У общей границы двух стран контур добавляет ещё одну копию от
каждого отношения.

Маска снимает наложения дописываемых участков, а `_dissolved`/`_proc`
собираются через `dissolve`, где совпадающие линии сливаются. Поэтому
запросы `admin_level_*.txt` оставлены без изменений, и отдельный line
union в рецепте не понадобился. `*_relation` — отдельный запрос для
названий; если когда-нибудь убирать `relation[...]` из запросов линий,
проверять нужно и подписи.

### Fallback зависит от наличия названий

`Pipeline.run` вызывает fallback, только если `_admin_fallback_needed()`:
хотя бы один `admin_level_*_relation.sqlite` отсутствует или пуст. Если
отношения в выгрузке есть, участки без `admin_level` не заполняются.
Заполнение не должно зависеть от наличия подписей, но перед изменением
нужно оценить стоимость `osmium getid -r` на больших выгрузках.

## Инварианты

- Член отношения не дописывается, если его числовой way id уже есть в
  `*_lines`.
- Участок, уже покрытый `*_lines` (в том числе контуром отношения), не
  дописывается; для `admin_level_4` не дописывается и участок
  государственной границы.
- От дописываемого участка остаётся только часть, которой ещё нет; пустой
  остаток пропускается.
- В `admin_level_4_proc` не должно быть государственной границы:
  пересечение с `admin_level_2_dissolved` по длине нулевое, а
  `admin_level_4_proc - admin_level_2_dissolved` содержит внутренние
  границы регионов.
- Повтор `@id` в растворённом слое сам по себе не дубль: один way может
  быть разбит на несколько `LineString`. Проверять нужно `sum` и `union`,
  а не число объектов.

## Диагностика

Если слой регионов содержит государственную границу, виноват не
`native:difference`: рецепт `admin_level_4` вычитает
`admin_level_2_dissolved` до `dissolve`, и в норме её там нет. Смотреть
нужно fallback. Промежуточные файлы рецепта можно сохранить, отключив
очистку `config.temp_dir` (`pipeline._clean_temp`).

## Проверка

`AdminMemberBorderTest` (`tests/test_pipeline_logic.py`):

- `test_member_with_same_way_id_is_not_appended` — id `way/123`;
- `test_member_covered_by_relation_outline_is_not_appended` — контур
  отношения уже содержит участок;
- `test_level_4_member_on_country_border_is_not_appended` — граница страны
  не возвращается в регионы;
- `test_member_is_trimmed_to_missing_part` — из участка вычитается только
  уже имеющаяся часть.

`AdminNameFallbackTest` проверяет сборку названий из линий. Порядок слоёв
и подписи — `AdminBoundaryOrderTest` и `OpenBorderAdminLabelTest`
(`tests/test_template_styles.py`).

Объекты и уникальные `@id`:

```bash
python3 - <<'PY'
from collections import Counter
from osgeo import ogr

path = "/path/vector/admin_level_2_dissolved.sqlite"
dataset = ogr.Open(path)
layer = dataset.GetLayer(0)
ids = Counter(feature.GetField("@id") for feature in layer)
print(sum(ids.values()), len(ids), sum(v - 1 for v in ids.values() if v > 1))
PY
```

Длины и пересечения считаются shapely по WKB из OGR: `unary_union(...)`,
`.length`, `.intersection`, `.difference`. Для `_dissolved`/`_proc`
`sum` должен совпадать с `union` в пределах точности; у `*_lines` `sum`
может быть больше `union` из-за контуров отношений. У
`admin_level_4_proc` пересечение с `admin_level_2_dissolved` по длине
должно быть нулевым.

## Что осталось

1. Fallback по-прежнему запускается только при пустом слое названий
   (`_admin_fallback_needed`). Если отношения в выгрузке есть, участки без
   `admin_level` у рек не заполняются. Заполнение не должно зависеть от
   наличия подписей, но сначала нужно оценить стоимость `osmium getid` на
   больших выгрузках.
2. На полной выгрузке страны `*_lines` содержит и way, и контуры
   отношений: `sum/union > 1` — ожидаемое состояние, а не регрессия.
   На готовые `_dissolved`/`_proc` это не влияет: совпадающие линии слиты,
   а наложения сняты маской.
