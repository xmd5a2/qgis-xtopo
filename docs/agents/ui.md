# Панель, потоки и сеть

Справочная часть `AGENTS.md`. Код — `gui/dockwidget.py`,
`gui/projectpanel.py`, `gui/extentdialog.py`, `core/net.py`, `core/proxy.py`.

## Потоки

- **Перед удалением панели все QThread останавливаются.** QThread нельзя
  уничтожать, пока он работает, иначе QGIS падает с «QThread: Destroyed
  while thread is still running». `XtopoDockWidget.shutdown_workers` отменяет и
  ждёт pipeline, PBF, Geofabrik, DEM, Sonny, прокси и серверы; вызывается из
  `closeEvent` (`final=False`, панель можно открыть снова), `unload`,
  `destroyed` и `aboutToQuit`. Новые фоновые потоки добавлять только с
  отменой и ожиданием в этом методе.
- **Не объявлять в QThread свой сигнал `finished`** — он перекрывает встроенный,
  по которому `deleteLater` удаляет поток; удаление до возврата из `run()`
  даёт то же падение. У `PipelineWorker` сигнал называется `completed`;
  удаление повешено на встроенный `QThread.finished`. Проверка —
  `test_pipeline_worker_does_not_shadow_qthread_finished` в
  `tests/test_ui_panels.py`.
- **Поток поиска — не ребёнок окна**: окно закрывается во время запроса, поток
  должен доработать и удалиться сам (родитель — `QgsApplication`); на
  `aboutToQuit` его ждёт `shutdown_nominatim_workers`.

## Интерфейс

- **Штатные инструменты QGIS** (измерение, identify) вызываются через `QAction`
  по `objectName`: `mActionMeasure`, `mActionMeasureArea`,
  `mActionMeasureAngle`, `mActionMeasureBearing`, `mActionIdentify`. В
  Python-обёртках QGIS 3.44 классов измерения нет
  (`gui/projectpanel.py: _trigger_qgis_action`). Закрытие окна
  `QgsMeasureDialog` не снимает инструмент с холста, поэтому панель ловит
  `QEvent.Close` и включает `QgsMapToolPan` (`_connect_measure_dialogs`).
- **Подписи размеров** — `core/measure.py` по эллипсоиду WGS84; в окне охвата
  они берутся у `MeasuredCanvas.set_frame_provider` при каждой отрисовке. Шкала
  масштаба считается от ширины видимой области, а не от знаменателя масштаба
  (`gui/extentdialog.py: scale_bar`).
- **Окно выбора охвата**: поиск места по Nominatim (`core/nominatim.py`) и
  выбор подложки предпросмотра (`BASEMAPS` в `gui/extentdialog.py`). Подложка
  меняет только карту в окне, в готовую карту не попадает. Nominatim: не
  больше одного запроса в секунду, обязателен User-Agent, ответы кэшируются по
  строке запроса (`core/nominatim.py`).
- **Enter в окне охвата**: в строке поиска он запускает поиск, а не закрывает
  окно. `QLineEdit` отдаёт Enter родителю, поэтому `keyPressEvent` окна
  проверяет фокус и не применяет охват (`gui/extentdialog.py`). Shortcut Enter
  живёт на холсте (`Qt.WidgetShortcut`), иначе он перехватывал бы ввод в
  строке поиска.
- **Панель ≤ 600 px по высоте** — тест `test_dock_is_compact`
  (`tests/test_pipeline_integration.py`). Сжимайте spacing/высоту, а не тест.
- **Строка статуса — `WrappedLabel`, высота не фиксирована**: длинный прогресс
  Geofabrik с ETA переносится и не обрезается. Прогресс (`Загрузка карт:`) не
  проходит через `_short_status`, иначе обрежется хвост с расчётным временем.

## Сеть и прокси

- **Сетевой предохранитель — `core/net.py`.** Все сетевые запросы плагина идут
  через urllib-opener с обёрткой `GuardedOpener`: ошибка соединения ставит
  глобальный запрет на ~20 с (`OFFLINE_TTL`), пока он действует, запросы падают
  сразу; успешный запрос или `net.reset()` (кнопка обновления, смена прокси)
  запрет снимают. Ответ сервера с кодом ошибки (HTTPError) запрета не ставит.
  Opener подставляет `DEFAULT_TIMEOUT`, если вызывающий код таймаут не задал.
  Ставится в `proxy.apply()` и для `dem_sources._OPENER`.
- **Прокси — `core/proxy.py`.** Кнопка в углу вкладок открывает диалог;
  `proxy.apply()` ставит opener urllib (через предохранитель), opener
  dem_sources и `QNetworkProxy`. Статус значка: `proxy.probe()` (TCP +
  приветствие SOCKS5 с авторизацией, без внешней сети) в фоновом
  `ProxyCheckWorker`, панель проверяет при показе и раз в 30 с; пока панель
  скрыта, проверки нет. Живой прокси в тестах — только через
  `QGIS_XTOPO_TEST_PROXY`, без него тест пропускается.
- **Значок прокси рисуется сам** (`_proxy_state_icon` в `gui/dockwidget.py`):
  контур глобуса цветом текста и маленькая точка состояния в правом нижнем
  углу — зелёная/красная; без прокси точки нет. Не заливать стандартную иконку
  сети цветом состояния: в части стилей глиф плотный, и после SourceIn
  остаётся сплошное пятно. Проверка — `ProxyIconTest` в
  `tests/test_ui_panels.py`.
