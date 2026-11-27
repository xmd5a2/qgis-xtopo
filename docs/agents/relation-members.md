# Way-члены отношений, потерянные драйвером GDAL

Состояние на 27.11.2026: **реализовано, проверено и закоммичено**
(`c3c084e`; сообщение журнала добавлено в каталог переводов).
Продолжение — с раздела «Что осталось».

## Симптом

В проекте `~/qgis_projects/automap_test` не виден центральный водоём
резервуара Төрткүл (Кыргызстан): по краям нарисована штриховка
`water intermittent`, а центр пустой. Ссылка на объект —
<https://www.openstreetmap.org/way/24989382>.

## Причина

1. Way 24989382 (`natural=water`, `water=reservoir`, без `intermittent`)
   входит в отношение `19683895` («Төрткүл суу сактагычы», `natural=water`,
   `water=reservoir`, **`intermittent=yes`**) с ролью `outer`. Второй
   член — way `1370077167`, тоже `outer`, и way 24989382 лежит внутри него.
2. Драйвер GDAL собирает мультиполигон: вложенный `outer` становится
   **дыркой**. В `water_intermittent_dissolved.sqlite` отношение лежит
   одним полигоном с кольцами `[240, 171]`, где 171 — это way 24989382.
3. Внешние (`outer`) way-члены отношений драйвер отдельно не отдаёт:
   `OGROSMDataSource::NotifyRelation` удаляет их из `polygons_standalone`.
   Поэтому way 24989382 нет ни в одном `vector/*.sqlite` проекта, и его
   собственные теги (постоянная вода) теряются.
4. Итог: отношение целиком ушло в `water_intermittent` (из-за
   `intermittent=yes`), дырка ничем не залита, а `water` для этого места
   пуст. На 1:3M и мельче объект дополнительно скрывается порогом
   `area_geodesic`.

На OSM это, скорее всего, ошибка ролей: way 24989382 должен быть `inner`
(постоянная вода внутри пересыхающего водохранилища) либо отдельным
полигоном вне отношения. Но плагин теперь восстанавливает такие объекты
и без правки OSM.

## Что сделано

Изменения в коммите `c3c084e`: `core/pbf_source.py`,
`tests/test_pbf_source.py`; сообщение журнала добавлено в
`resources/lang/en.json`.

В `core/pbf_source.py`:

- `RELATION_MEMBER_LAYERS = ("water", "water_intermittent")` — слои, для
  которых включено восстановление.
- `_unescape_opl`, `_opl_tags`, `_parse_opl_objects` — потоковый разбор
  OPL от osmium (узлы, way, отношения, роли членов).
- `_relation_member_filters` — фильтры `osmium tags-filter` из
  положительных условий запросов воды (`r/natural=water` и т.п.);
  отрицательные условия (`intermittent!=yes`) отбрасываются, точное
  совпадение проверяет тот же `QueryIndex`.
- `_ring_geometry` — полигон из замкнутого кольца.
- `_recover_relation_member_ways` — основной шаг: osmium
  `tags-filter -f opl -o ...` по отношениям, затем для каждого `outer`
  way-члена строится полигон, проверяется по `QueryIndex` и дописывается
  в NDJSON тех слоёв, которые **не** покрывает отношение. Дубликаты
  отсекаются общим `seen` по `(слой, @id)`, геометрия — по `bbox`.
- Вызов добавлен в конец `_prepare_dataset` (до закрытия `handles`),
  пути берутся из `read_paths` (распакованные копии источников), а не из
  исходных `.pbf.gz`. `warnings` инициализируется до `try`.

Почему так: `tags-filter` по умолчанию включает члены отношений, поэтому
отдельный `osmium getid` не нужен. OPL пишется в файл, а не в память:
на большой выгрузке вывод в память занял бы сотни мегабайт. Без osmium
шаг молча пропускается — основной путь чтения PBF от него не зависит.

В `tests/test_pbf_source.py`:

- класс `RelationMemberWaysTest`: разбор OPL, фильтры, восстановление
  way в свой слой, отсутствие дубликата слоя отношения, повторный вызов
  без дублей;
- в `MultiFileDatasetTest._prepare` замокан `find_osmium` → `None`,
  чтобы фейковые PBF не запускали osmium.

## Проверки

- `./tools/run_tests.sh tests.test_pbf_source` — OK, 42 теста;
  `./tools/run_tests.sh` — OK, 28 модулей (после добавления сообщения в
  `resources/lang/en.json`).
- Полная подготовка без рельефа на вырезе
  `slice_9d4b4691e9a8.osm.pbf`, bbox `70.55,39.90,70.85,40.10`:
  36.4 с, пик RSS 1631 МБ; в журнале
  `PBF: восстановлены way-члены отношений: объектов 1`.
  - `vector/water.sqlite` — 3 объекта, среди них `way/24989382`
    (`water=reservoir`);
  - `water_intermittent_dissolved.sqlite` по-прежнему содержит кольцо
    отношения (это правильно);
  - рендер участка: центр резервуара залит синим, по краям осталась
    штриховка intermittent. PNG:
    `/mnt/data/temp/xtopo-fixcheck/after.png`; сравнение «до/после» —
    `/mnt/data/temp/xtopo-fixcheck/confirm.png`.
- Важно: в `water_dissolved.sqlite` `@id` теряется на `dissolve`, поэтому
  проверять наличие way нужно в `water.sqlite` или по рендеру.

Артефакты на этой машине (не в репозитории):

- вырез: `~/qgis_projects/qgisxtopo-cache/geofabrik/slice_9d4b4691e9a8.osm.pbf`;
- стенд: `/mnt/data/temp/xtopo-fixcheck/projects/fixcheck/`;
- рендер «после»: `/mnt/data/temp/xtopo-fixcheck/after.png`;
- рендер «до» (старый `automap_test`): `/mnt/data/temp/xtopo-fixcheck/before.png`;
- сравнение с подписями: `/mnt/data/temp/xtopo-fixcheck/confirm.png`.

## Что осталось

1. ~~Прогнать все тесты~~ — выполнено 27.11.2026, 28 из 28 модулей.
2. ~~Проверить установку и инсталлятор~~ — выполнено 27.11.2026:
   `build_plugin.py --install` поставил 0.18.0, инсталлятор собран в
   `dist/qgis-xtopo-setup-0.18.0.exe`.
3. ~~Закоммитить изменения~~ — коммит `c3c084e`; сообщение журнала
   добавлено в каталог переводов отдельным коммитом.
4. При желании расширить `RELATION_MEMBER_LAYERS`, если такие же потери
   найдутся в других площадных слоях. Сейчас восстановление ограничено
   водой.
5. Пересобрать рабочий проект пользователя (`automap_test`): готовый
   проект не чинится, данные должны быть подготовлены заново.

## Как воспроизвести на другом ПК

Пути ниже — с этой машины (`/home/xmd5a/...`, `/mnt/data/temp/...`);
на другом ПК замените корень репозитория и рабочий каталог на свои.

```sh
# 1. Вырез и охват
PBF=~/qgis_projects/qgisxtopo-cache/geofabrik/slice_9d4b4691e9a8.osm.pbf
BBOX=70.55,39.90,70.85,40.10

# 2. Подготовка без рельефа
QT_QPA_PLATFORM=offscreen python3 \
  ~/.config/opencode/skills/qgis-xtopo-bench/bench_run.py \
  --root /home/xmd5a/git/qgis-xtopo --label fixcheck --bbox "$BBOX" \
  --pbf "$PBF" --work /mnt/data/temp/xtopo-fixcheck \
  --out /mnt/data/temp/xtopo-fixcheck/bench.json --no-terrain \
  --project-name fixcheck

# 3. Проверка данных
sqlite3 /mnt/data/temp/xtopo-fixcheck/projects/fixcheck/vector/water.sqlite \
  'select "@id", water from water where "@id" = "way/24989382";'

# 4. Рендер центра резервуара (EPSG:3857)
QT_QPA_PLATFORM=offscreen python3 \
  ~/.config/opencode/skills/qgis-xtopo-bench/render_project.py \
  --project /mnt/data/temp/xtopo-fixcheck/projects/fixcheck/fixcheck.qgz \
  --output /mnt/data/temp/xtopo-fixcheck/after.png \
  --extent 7866200,4863200,7874200,4867000 \
  --width 1600 --no-basemap --timeout 100

# 5. Тесты
./tools/run_tests.sh tests.test_pbf_source
./tools/run_tests.sh
```

Если изменится исходный вырез (обновится OSM), проверка та же: в
`water.sqlite` должен быть `way/24989382`, а на рендере — синий центр и
штриховка по краям.
