# Поставка osmium-tool

В каталоге лежит бинарник [osmium-tool](https://github.com/osmcode/osmium-tool) —
инструмента для работы с выгрузками OpenStreetMap.  Плагин использует его,
чтобы вырезать нужную область из большой выгрузки до чтения её драйвером
GDAL: чтение карты целиком занимает минуты, вырезка — секунды.

Без osmium плагин работает как и раньше, просто медленнее.

## Состав

| Путь | Описание |
|------|----------|
| `linux-x86_64/osmium` | статический бинарник для Linux x86_64, osmium-tool 1.19.0 |
| `win-x86_64/osmium.exe` | бинарник для Windows x86_64, osmium-tool 1.19.1 |
| `win-x86_64/*.dll` | зависимости osmium.exe (boost, zlib, bzip2, expat, lz4) |
| `LICENSE` | текст GNU GPL версии 3 — лицензия osmium-tool |
| `build-linux.sh` | воспроизводимая сборка бинарника для Linux |
| `fetch-windows.sh`, `fetch-windows.py` | получение готовой сборки для Windows из conda-forge |
| `windows-build.json` | зафиксированные версии пакетов и хеши поставки для Windows |
| `build-windows.ps1` | альтернатива: сборка бинарника для Windows из исходников |

Имя файла бинарника должно быть ровно `osmium` (`osmium.exe`): osmium
разбирает имя файла программы и воспринимает его как имя команды.

## Версия и происхождение

| Параметр | Значение |
|----------|----------|
| osmium-tool | 1.19.0 (`https://github.com/osmcode/osmium-tool/archive/refs/tags/v1.19.0.tar.gz`) |
| libosmium | 2.20.0 (`https://github.com/osmcode/libosmium/archive/refs/tags/v2.20.0.tar.gz`, только заголовки) |
| protozero | 1.7.1 (пакет `libprotozero-dev` Ubuntu 24.04) |
| boost program_options | 1.83.0 (пакет `libboost-program-options1.83-dev` Ubuntu 24.04) |
| zlib / bzip2 / expat / lz4 | из Ubuntu 24.04, статические библиотеки |
| Сборка | Ubuntu 24.04, GCC 13, CMake 3.31.6 |
| Linux sha256 | `77fdcba2f48eddca2e2e5a5b9532f9301f53aa41eb465ab1c96ad4050c0b5389` |

### Windows x86_64

Бинарник для Windows берётся готовым из [conda-forge](https://conda-forge.org/)
(канал `conda-forge`, подкаталог `win-64`) — там osmium-tool собирается
регулярно, и версия всегда новее, чем в нашей собственной сборке.

Скрипт `fetch-windows.sh` (обёртка над `fetch-windows.py`) **сам** подбирает
сборки: берёт последнюю версию osmium-tool для win-64 не ниже 1.16 и ровно
те версии зависимостей, которые эта сборка требует. Поэтому обновление
пакетов на стороне conda-forge не ломает получение бинарника: скрипт
подстроится сам. Найденные версии и хеши фиксируются в
`windows-build.json`; если после обновления файлы изменились, скрипт
об этом предупредит, а запись обновляется флагом `--update-pin`.

| Пакет conda-forge | Версия | Что берём |
|-------------------|--------|-----------|
| `osmium-tool` | 1.19.1 | `osmium.exe` |
| `libboost` | 1.90.0 | `boost_program_options.dll` |
| `libzlib` | 1.3.2 | `zlib.dll` |
| `bzip2` | 1.0.8 | `libbz2.dll` |
| `libexpat` | 2.9.0 | `libexpat.dll` |
| `lz4-c` | 1.10.0 | `lz4.dll` |

Все эти библиотеки нужны `osmium.exe` для сжатия данных PBF и разбора
параметров командной строки. Они кладутся рядом с `osmium.exe`: Windows
ищет зависимые DLL сначала в каталоге самой программы.

Библиотеки Microsoft (`msvcp140.dll`, `vcruntime140.dll`,
`vcruntime140_1.dll`) в поставку **не** входят: они присутствуют в любой
системе, где установлен QGIS, и подмена их может конфликтовать с
рантаймом самого QGIS.

Контрольные суммы файлов (sha256) лежат в `windows-build.json`; тест
`tests/test_osmium.py: test_windows_bundle_matches_pin` сверяет их с
файлами в поставке.

Проверка: бинарник для Windows под wine даёт вырезку, побайтово
совпадающую с бинарником для Linux (проверено на выгрузке Georgia,
bbox 43.3323,42.8372–43.6356,43.0155: 55 891 узел, 1 289 линий,
68 отношений, одинаковый размер файла 480 466 байт).

Минимально поддерживаемая версия osmium — 1.16: в ней появилась опция
стратегии `types=any`, без которой теряются неполные отношения.

## Лицензия

osmium-tool распространяется по GNU GPL версии 3 — так же, как и сам плагин.
Текст лицензии лежит в `LICENSE`.  Согласно §6 GPL-3 исходный код
доступен по адресам выше (теги `v1.19.0` и `v2.20.0`), рецепт сборки —
в `build-linux.sh` и `build-windows.ps1`.

Список файлов, входящих в бинарник, и состав статических библиотек можно
воспроизвести командами из `build-linux.sh`.

Windows-бинарник взят из conda-forge, там же лежат и его зависимости.
Их лицензии свободные и совместимы с GPL-3: Boost — BSL-1.0,
zlib — Zlib, bzip2 — bzip2-1.0.6, Expat — MIT, LZ4 — BSD-2-Clause.
Тексты лицензий этих библиотек лежат в самих пакетах conda-forge
(каталог `info/licenses/` в каждом `.conda`).

## Пересборка

```sh
WORK_DIR=/tmp/osmium-repro DEST_DIR=vendor/osmium ./vendor/osmium/build-linux.sh
```

Скрипт не требует прав администратора: пакеты Ubuntu скачиваются через
`apt-get download` и распаковываются в отдельный каталог, CMake
устанавливается в виртуальное окружение через `pip`.

Windows-бинарник получается из готовых пакетов conda-forge:

```sh
DEST_DIR=vendor/osmium ./vendor/osmium/fetch-windows.sh
```

Скрипт сам выбирает актуальные сборки, скачивает их, распаковывает
`osmium.exe` и его зависимости, проверяет, что файл 64-разрядный и
запускается, и печатает контрольные суммы.  Чтобы зафиксировать новую
сборку в репозитории:

```sh
DEST_DIR=vendor/osmium ./vendor/osmium/fetch-windows.sh --update-pin
```

