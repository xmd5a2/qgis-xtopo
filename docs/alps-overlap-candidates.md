# Кандидаты пересечений полигонов: Альпы

Статус: **список одобрен, пары добавлены в `DEFAULT_PAIRS`**.
Дата: 2026-11-25.

## Охват

Координаты из поля «Охват» плагина (порядок `lon_min,lat_min,lon_max,lat_max`):

```
7.838,46.4661,12.0026,47.7296
```

Это восточная Швейцария, Лихтенштейн, Тироль, север Италии и юг Баварии/
Баден-Вюртемберга. `alps`-выгрузка Geofabrik покрывает только 88,5% охвата,
поэтому нужен набор из девяти выгрузок:

```
austria
freiburg-regbez
liechtenstein
nord-est
nord-ovest
oberbayern
schwaben
switzerland
tuebingen-regbez
```

## Как получен список

Проект `alps_poly` подготовлен с `resolve_polygon_overlaps=False` (чтобы
скан видел и те пересечения, которые уже вырезаются), без рельефа. В
`array_queries` вошли только площадные слои (57 запросов): линии, дороги и
ручьи исключены — на них предыдущая полная сборка не уложилась в лимит.

Скан:

- выборка до 400 объектов на слой, площадь объекта не меньше 100 м²;
- пара считается скрытой, если `order(верхний) < order(нижний)`, то есть
  верхний слой стоит выше в дереве;
- пересечение должно покрывать не менее 90% площади нижнего объекта;
- `building` и `barrier polygon` как верхние слои не проверялись:
  здания слишком мелкие, у `barrier polygon` заливка `alpha=0`;
- `place locality polygon` имеет `nullSymbol`, `military_dissolved` —
  `alpha=27`, `cemetery` — `alpha=54`, `resort` — `alpha=62`,
  `camp site polygon` — `alpha=92`; они не скрывают объекты полностью.

Оценки в таблице — по выборке, не точные. Все пары из таблицы добавлены
в `core/polygon_overlaps.py:DEFAULT_PAIRS`; уже существовавшие
`farmland` → `landuse_residential`, `farmland` → `industrial` и
`wood` → `landuse_residential` не дублировались. Точный прогон на копиях
слоёв ещё предстоит.

## Кандидаты

| Верхний → нижний | Оценка | Комментарий |
|---|---:|---|
| `wood` → `bare_rock` | ~573 | лес непрозрачный (`#aeefa2`, alpha 255), скалы в лесу скрыты; в OsmAnd скалы выше леса |
| `scree` → `bare_rock` | ~573 | осыпь — SVG-штриховка, порядок обратный OsmAnd |
| `scrub` → `bare_rock` | ~172 | кустарник непрозрачный |
| `wood` → `shingle` | ~80 | галечник под лесом |
| `grass` → `shingle` | ~40 | галечник под луком |
| `wood` → `industrial` | ~43 | промзона под лесом |
| `farmland` → `industrial` | ~43 | промзона под полями |
| `farmland` → `greenhouse horticulture` | ~28 | теплицы под полями |
| `farmyard` → `greenhouse horticulture` | ~25 | теплицы под фермами |
| `landuse residential` → `sand` | ~18 | песок под застройкой |
| `scrub` → `plant nursery` | ~11 | питомник под кустарником |
| `wood` → `plant nursery` | ~9 | питомник под лесом |
| `farmland` → `plant nursery` | ~7 | питомник под полями |
| `farmland` → `landuse residential` | ~16 | застройка под полями |
| `wood` → `landuse residential` | ~16 | застройка под лесом |

## Что не предлагается

- `wetland` → `water` (~563): болото намеренно остаётся выше воды
  (`LandcoverLayerOrderTest.test_wetland_stays_above_water`).
- `place locality polygon` — `nullSymbol`, все его пары геометрические.
- `military_dissolved`, `cemetery`, `resort`, `camp site polygon` —
  полупрозрачные заливки, объекты под ними видны.
- `building` и `barrier polygon` — см. ограничения скана выше.

В raw-проекте также проявились уже обработанные пары: `farmland`→`farmyard`
(~260), `grass`→`landuse_residential` (~203), `farmland`→`orchard` (~165),
`glacier`→`bare_rock` (~115), `glacier`→`scree` (~67),
`grass`→`industrial` (~65), `water`→`shingle` (~60),
`grass`→`vineyard` (~48), `grass`→`sand` (~30), `water`→`beach` (~10),
`grass`→`beach` (~9). Это подтверждает, что скан находит известные случаи;
отдельных действий по ним не нужно.

## Как продолжить на другой машине

1. Подготовить охват `7.838,46.4661,12.0026,47.7296` с
   `resolve_polygon_overlaps=False`, без рельефа, только площадными
   запросами. Geofabrik выберет девять выгрузок из списка выше.
2. Повторить скан по слоям проекта (условия в разделе «Как получен
   список»). Проект `alps_poly` и скрипт скана были разовыми и в
   репозиторий не входят; на новой машине нужен свой прогон.
3. Пары из таблицы уже добавлены. Если при новом скане появятся ещё
   пары:
   - добавить их в `DEFAULT_PAIRS` в `core/polygon_overlaps.py`;
   - при необходимости добавить имя файла в `LAYER_FILES` (например,
     `water_intermittent` → `water_intermittent_dissolved`);
   - расширить `PolygonOverlapTest.test_landcover_pairs_are_configured`
     и `test_alps_pairs_are_cut`;
   - обновить `docs/agents/drivers.md`;
   - выполнить `./tools/run_tests.sh`, затем
     `python3 tools/build_plugin.py --install` и
     `WINEPREFIX=...` (см. `AGENTS.md`) `./installer/build-installer.sh`.

## Кандидаты на проверку после добавления

- Лес и кустарник выше скал: проверить, не станет ли слишком много дырок в
  `wood`/`scrub` на лесных массивах Альп; при необходимости ограничить
  пары только `bare_rock`.
- `scree` → `bare_rock`: обе заливки — SVG-штриховки; визуально сравнить
  до/после, возможно, пару не добавлять.
- `wood`/`farmland` → `industrial`: промзоны в лесу/полях могут быть
  реальным пересечением landuse, а не ошибкой порядка; проверить на
  конкретных объектах.
