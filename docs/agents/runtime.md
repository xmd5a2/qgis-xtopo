# Проверка в реальном QGIS и Windows-инсталлятор

Справочная часть `AGENTS.md`.

## Создание и очистка проекта

Проект `.qgz` — это архив с одним qgs на десятки мегабайт (после
унификации масок значков — 74 МБ). Его подготовка и очистка идут на
каждой сборке, поэтому горячий путь нельзя писать «в лоб»:

- **Блоки `maplayer` ищутся через `str.find`** (`project._maplayer_spans`),
  а не регулярным выражением с `.*?`: на 74 МБ regex идёт около 0,4 с,
  поиск подстроки — 0,02 с. Границы блоков те же.
- **Все правки qgs собираются в один список** `(начало, конец, текст)` и
  применяются одним проходом (`project._apply_edits`). Каждый отдельный
  `str.replace` или срез `text[:start] + ... + text[end:]` копирует все
  74 МБ; десять таких правок стоили около секунды.
- **Переменные проекта правятся по маленьким спискам**
  `variableNames`/`variableValues` (`_variable_edits`), а не повторным
  поиском по всему тексту для каждой переменной.
- **Растровые слои патчатся выборочно** (`_raster_edits`): в шаблоне их
  8 из 277, остальные блоки не копируются.
- **Значки POI разбирают шаблон по блокам** (`poi_icons._template_data`):
  порядок слоёв берётся из тегов `layer-tree-layer`, дальше через
  `_maplayer_spans` разбираются только блоки `maplayer`. Полный
  `iterparse` 74 МБ идёт дольше, а остальной текст проекта (стили,
  макеты) для значков не нужен.
- **Шаблон разбирается один раз на процесс, а не на поток.**
  `field_subset.template_references` держит результат за блокировкой с
  повторной проверкой: `lru_cache` при одновременном холодном вызове
  запускает функцию в каждом потоке, и восемь слоёв по очереди разбирали
  один и тот же проект (около 19 с вместо 1,5 с). Внутри разбора
  выражения кэшируются (`_fields_in_expression`), а значения атрибутов
  ищутся `str.find`, а не регулярным выражением по 68 МБ. Прогрев
  (`pipeline._warm_template_data`) идёт в фоновом потоке одновременно со
  слоями; без него первый же слой сам разбирал шаблон, а значки — ещё раз
  в конце.
- **Архив пишется с `ARCHIVE_COMPRESSLEVEL = 1`.** Уровень 6 тратит
  около 0,7 с на запись, уровень 1 — 0,27 с; архив больше примерно на
  2 МБ и читается быстрее. Важно: `ZipFile.writestr(ZipInfo, data)`
  игнорирует `compresslevel` архива — уровень надо передавать в
  `writestr` явно, иначе используется уровень по умолчанию.

Проверка после правок — сравнить распакованный qgs со старым кодом
байт-в-байт: оптимизация не должна менять содержимое проекта.

## Проверка в реальном QGIS

- Offscreen-тесты не покрывают регистрацию плагина и раскладку окон.
- **Пустой `processing/` в корне репозитория** (его создаёт QGIS) Python
  принимает за namespace-пакет: `import processing` проходит, а
  `processing.core` — нет. `pipeline._load_processing()` ставит путь QGIS в
  начало `sys.path` и сбрасывает уже импортированный пустой пакет. Каталог в
  `.gitignore`, удалять его не обязательно.
- Не трогать работающий QGIS (`pgrep -x qgis.bin`); запускать с временным
  профилем: `qgis --profiles-path /mnt/data/temp/qgis-profile --code script.py`.
- Панель открывается при `QGIS_XTOPO_OPEN=1`; показ — по
  `iface.initializationCompleted`, иначе восстановление окон перезапишет
  `show()`.
- Плагин включается секцией `[PythonPlugins]` в `QGIS/QGIS3.ini` профиля (не
  `[Plugins]`). В `qgis.utils` QGIS 3.44 нет `enabled_plugins`.

### Offscreen-рендер карты

Для проверки стиля на реальных данных:

```python
import sys
sys.path.insert(0, '.')
import tests
from tests import qgis_app
app = qgis_app()
from qgis.core import (
    QgsProject, QgsMapSettings, QgsMapRendererCustomPainterJob,
    QgsRectangle, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)
from qgis.PyQt.QtGui import QImage, QPainter, QColor
from qgis.PyQt.QtCore import QSize
from qgis_xtopo.core.resources import template_path

project = QgsProject.instance()
project.read(str(template_path()))  # только str, не Path
layer = next(
    item for item in project.mapLayers().values()
    if item.name() == 'railway all'
)
# В шаблоне путь ./vector/...; подменяем на реальный файл.
layer.setDataSource('/path/to/vector/railway_all.sqlite',
                    layer.name(), 'ogr')
layer.reload()

settings = QgsMapSettings()
settings.setLayers([layer])
settings.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:3857'))
settings.setExtent(QgsCoordinateTransform(
    QgsCoordinateReferenceSystem('EPSG:4326'),
    settings.destinationCrs(), project.transformContext(),
).transformBoundingBox(QgsRectangle(8.3, 46.8, 8.6, 47.0)))
settings.setOutputSize(QSize(1000, 700))
settings.setBackgroundColor(QColor('white'))

image = QImage(settings.outputSize(), QImage.Format_ARGB32)
image.fill(QColor('white'))
painter = QPainter(image)
job = QgsMapRendererCustomPainterJob(settings, painter)
job.start()
job.waitForFinished()
painter.end()
image.save('/mnt/data/temp/map.png')
```

`setExtent` ждёт extent уже в целевой CRS, поэтому пересчитывать его нужно
через `QgsCoordinateTransform`. У `QgsRectangle` нет `combine` и `united` —
объединять через `combineExtentWith`. Готовый PNG класть в `/mnt/data/temp`, не
в репозиторий. Рабочие данные пользователя можно читать, но не изменять.

`waitForFinished()` в примере безопасен только для простого слоя без
выражений. Для проекта целиком он блокирует event loop, а `overlay_nearest`,
`layer_property`, `raster_value` и `raster_statistic` из потока рендера ждут
главный поток — это дедлок. Готовые обёртки с ожиданием через `QEventLoop` —
`qgis-render-profile/profile_render.py` и `qgis-xtopo-bench/render_project.py`.

## Windows-инсталлятор

- **QGIS для новых пользователей ставится официальным сетевым установщиком
  OSGeo4W** (`installer/scripts/install-qgis.ps1`): пакет `qgis-ltr` с
  зависимостями (~410 МБ, против 580 МБ у standalone-MSI) в
  `%LOCALAPPDATA%\OSGeo4W`; если он не сработал — запасной путь через
  standalone-MSI. Раскладка OSGeo4W отличается от standalone: приложение —
  `bin\qgis-ltr-bin.exe` (в standalone — `bin\qgis-bin.exe`), запускать надо
  через `bin\qgis-ltr.bat` (он задаёт переменные окружения, без них
  `qgis_app.dll` не грузится), значок — в `apps\qgis-ltr\icons\QGIS.ico`.
  Проверка установки — `installer/scripts/check-qgis-install.py`, прогон в CI —
  `.github/workflows/verify-qgis-install.yml` (вручную: качает ~410 МБ).
- **Вопрос в инсталляторе задаётся из `NextButtonClick`, а не из
  `CurPageChanged` и не из `InitializeSetup`.** Из `CurPageChanged` после
  ответа мастер сразу завершается, а из `InitializeSetup` вопрос появляется до
  окна мастера и прячется за другими окнами. Рабочее место — нажатие «Далее»
  на странице выбора каталога (`wpSelectDir`): окно уже видно, мастер после
  ответа продолжает работу.
- **Успех установки QGIS проверяется по факту, а не по коду возврата.** В wine
  `powershell.exe` — заглушка: молча ничего не делает и выходит с кодом 0,
  поэтому установщик считал, что QGIS установлен, и молчал. Теперь инсталлятор
  после запуска скрипта проверяет, появился ли QGIS, и есть ли журнал скрипта,
  и показывает ошибку с указанием причины.
- Установка не требует прав администратора: всё ставится в профиль
  пользователя (`PrivilegesRequired=lowest`).
