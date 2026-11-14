# AGENTS.md — qgis-xtopo

Памятка проекта. Подробные правила разложены по файлам `docs/agents/` — перед
правкой соответствующей части прочитайте нужный (указатели в конце).

## Что это

- Плагин QGIS (не приложение и не библиотека), минимум QGIS 3.28. Готовит
  данные OSM и рельефа (SRTM) и собирает проект QGIS из шаблона `automap.qgz`.
  Старый Docker-пайплайн лежит в `legacy/` только для справки.
- Корень репозитория — корень пакета: при установке каталог называется
  `qgis_xtopo`, поэтому импорты в тестах идут как `qgis_xtopo.core...`
  (`tests/__init__.py` регистрирует пакет).
- Плагин самодостаточен: только QGIS и его Python, без pip-зависимостей и
  системных утилит. Исключение — поставляемый `vendor/osmium/`; без него
  плагин обязан работать, только медленнее (`core/osmium_prefilter.py`).
- **Исходники QGIS лежат рядом — `/home/xmd5a/git/qgis`** (клон
  `github.com/qgis/qgis`, рабочее дерево чистое). Это справочник: по нему
  смотрят код QGIS, при необходимости обновляют — `git pull` там делать
  можно. В upstream ничего не отправляем: QGIS запрещает вклад с помощью
  ИИ (QEP 408), поэтому правки делаем только в своём плагине.

## Команды

```sh
./tools/run_tests.sh                      # все тесты, offscreen, ~20 с
./tools/run_tests.sh tests.test_project   # только выбранный модуль (можно несколько)
python3 tools/build_plugin.py             # dist/qgis_xtopo-<версия>.zip + dist/qgis-xtopo-ready/
python3 tools/build_plugin.py --install   # + установка в профиль QGIS
./tools/run_qgis.sh                       # сборка + установка + QGIS с открытой панелью
WINEPREFIX=~/.cache/wineprefix-inno ./installer/build-installer.sh  # Windows-инсталлятор
QT_QPA_PLATFORM=offscreen python3 tools/check_pbf_coverage.py       # сверка полноты с osmium
```

- После изменения поведения: тесты зелёные → `build_plugin.py --install` →
  `installer/build-installer.sh`. Инсталлятор собирается всегда, даже если
  правка не про Windows: он содержит ту же копию плагина.
- При смене версии правьте `metadata.txt` **и** `#define MyAppVersion`
  в `installer/qgis-xtopo.iss`.
- Тесты — `unittest`. `tools/run_tests.py` запускает каждый модуль отдельным
  процессом: QGIS не переносит два приложения в одном процессе.
  `QT_QPA_PLATFORM=offscreen` уже выставлен. Запускайте имена модулей, а не
  отдельные классы: часть тестов создаёт `QgsApplication` в `setUpClass`, и
  класс, запущенный первым, может упасть на `QFontDatabase`.
- Интеграционные тесты с PBF пропускаются, пока `QGIS_XTOPO_TEST_PBF` не
  указывает на локальный `.pbf`. `check_pbf_coverage.py` дополнительно ждёт
  выгрузки Geofabrik в `~/qgis_projects/qgisxtopo-cache/geofabrik`.
- CI тесты не запускает: workflows собирают только Windows-инсталлятор и
  бинарники osmium.

## Структура

- `core/pipeline.py` — оркестрация. Источники OSM (`geofabrik.py`,
  `overpass_*.py`, `sources.py`) → вырезка `osmium_prefilter.py` → чтение
  `pbf_source.py` → раскладка по слоям (`query_recipes.py`,
  `resources/queries/`) → рельеф `terrain.py` → чистка пустых слоёв.
  `core/project.py` собирает проект из шаблона в начале и удаляет пустые слои
  в конце.
- `core/terrain_cache.py` — кэш DEM/изолиний; `core/transliteration.py` +
  `map_language.py` — язык карты; `core/i18n.py` + `resources/lang/en.json` —
  язык интерфейса; `core/expressions.py` — регистрация выражений QGIS;
  `core/measure.py` — размеры рамки; `core/peak_placement.py` — правило
  проверки пиков; `core/peak_filter.py` — его применение к слою `peak`;
  `core/poi_icons.py` — вытеснение значков; `core/cpu.py` — число потоков.
- `gui/dockwidget.py` — вкладки «Данные/Слои/Рельеф»; `gui/projectpanel.py` —
  вкладка открытого проекта, атлас и слои.
- `automap.qgz` лежит только в корне; путь в коде — `core/resources.py:
  template_path()`. При сборке копируется в пакет как `resources/automap.qgz`.
  Рядом не оставлять `*.bak*`.

## Ограничения

- **Git**: после каждой законченной feature (не тестовой!) делать коммит с
  осмысленным комментарием на английском. Без явной просьбы не делать `push`,
  `checkout`, `reset --hard`, `stash`.
- **Стиль — только в `automap.qgz`**. Инструментов правки шаблона и переноса
  стиля в готовые проекты нет (не возвращать `tools/patch_template_*.py`).
  `core/project.py: repair_project` при открытии лишь убирает пустые слои,
  чинит шрифт и оборачивает `overlay_*` в `try(...)`, но стиль не обновляет.
  Изменили стиль — переготовьте данные.
- **Пустые слои** вырезаются при сборке; обращение `overlay_*` к отсутствующему
  слою даёт NULL и скрывает объекты. Поэтому такие фильтры обёрнуты в
  `try(..., запасное значение)` (`patch_overlay_filters`).
- **Потоки**: перед удалением панели все QThread останавливаются
  (`shutdown_workers`); свой сигнал `finished` в QThread не объявлять.
  Подробности — `docs/agents/ui.md`.
- **`ProcessingRunner` даёт каждому рабочему потоку свой
  `QgsProcessingContext` с отдельным `QgsProject`.** Общий
  `QgsProject.instance()` нельзя: контекст лениво кэширует область
  выражений, и параллельные вызовы гоняются за её пересборку (segfault в
  `createExpressionContextScope`). Проект, созданный в рабочем потоке,
  освобождается там же (`ProcessingRunner.release`): иначе PyQt отложит
  удаление до завершения потока, а там `thread_local` регулярное выражение
  в `QgsProject::writeEntry` уже разрушено — снова segfault.
- **Сеть**: запросы только через `core/net.py` (предохранитель от обрыва
  связи), прокси — `core/proxy.py`. Подробности — `docs/agents/ui.md`.
- **Слои обрабатываются параллельно, по умолчанию 3/4 ядер с потолком 8**
  (`core/cpu.py: cpu_workers`, `Pipeline._worker_count`). На 16 ядрах 12 слоёв
  оказались медленнее восьми: конкуренция за диск и память. На четырёх ядрах
  будет 3 воркера, на восьми — 6. `parallel_workers` в `config.ini` задаёт
  число явно. Остальные CPU-задачи (osmium, проверка пиков) тоже берут 3/4
  ядер; у проверки пиков потолок 4 — больше потоков упираются в GIL и
  работают медленнее. Загрузка окон COG — сетевая, у неё свой фиксированный
  лимит.
- **Тяжёлые слои запускаются первыми**: порядок подготовки — по размеру
  исходных данных (`Pipeline._source_size`). Иначе лес и ледники (самые
  долгие слои) стартуют последними и затягивают конец подготовки.
- **Кэш Geofabrik не обновляется сам.** Валидный локальный PBF (`ensure_maps`)
  используется как есть: ежедневная регенерация выгрузок не причина
  перекачивать карты. Свежую выгрузку получают удалением файла из
  `<проекты>/qgisxtopo-cache/geofabrik`.
- **Рельеф в pipeline — две стадии.** `prepare_terrain_raster` (скачивание
  тайлов, сборка DEM, затенение) не зависит от слоёв и в `run_project`
  работает в потоке `xtopo-terrain` параллельно `Pipeline.run`; ошибки из
  потока собираются в список и всплывают после `join`, отмена проверяется там
  же, в `finally` поток дожидается. `prepare_terrain_isolines` ждёт ключ кэша
  DEM от первой стадии и идёт после слоёв — контуры режутся по воде и
  ледникам (`_water_mask_paths`, `_glacier_polygons` читают `vector_dir`).
  `prepare_terrain` остался как последовательная обёртка обеих стадий.
- **Рабочие данные пользователя** (`~/qgis_projects/...`, кэш Geofabrik) не
  использовать как тестовые полигоны; чтение и offscreen-открытие допустимы.
- `dist/`, `output/`, `installer/payload/`, `__pycache__/`,
  `resources/automap.qgz` — артефакты, не редактировать.

## Стиль изменений

- В этом проекте комментарии, docstring'и и сообщения — на русском, как в
  окружающем коде (это переопределяет общее правило про английский).
- UI-строки пишутся по-русски; английский — в `resources/lang/en.json`
  (словарь «русская строка → английская», работает в обе стороны). Уже
  построенные виджеты переводит `gui/translation.py`.
- Тесты добавлять в существующие тематические файлы. Запуск — только через
  `./tools/run_tests.sh`.
- После изменения поведения обновляйте docstring'и и README (разделы
  «Установка» и «Разработка»).

## Указатели по темам

| Что правите | Файл |
|---|---|
| Порядок слоёв, варианты slope/hillshade, правила по масштабу, изолинии | `docs/agents/styles.md` |
| Язык карты, подписи, сокращения, вытеснение значков POI | `docs/agents/labels.md` |
| Источники рельефа, мозаика, COG, Sonny, кэш DEM/изолиний | `docs/agents/terrain.md` |
| GDAL, драйвер OSM, osmium, потери данных | `docs/agents/drivers.md` |
| Правило проверки пиков, инструмент, кэш вердиктов | `docs/agents/peaks.md` |
| Панель, QThread, прокси, сеть, окно охвата | `docs/agents/ui.md` |
| Проверка в реальном QGIS, offscreen-рендер, Windows-инсталлятор | `docs/agents/runtime.md` |
