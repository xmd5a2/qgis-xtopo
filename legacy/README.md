# Legacy

Здесь лежит прежняя реализация проекта — до появления плагина QGIS:

* `Dockerfile`, `docker_*.sh`, `init_docker.sh`, `prepare_data.sh`,
  `populate_db.sh`, `build_gui.sh`, `exec_qgis.sh`, `get_bbox.sh` — сборка и
  запуск Docker-образа с QGIS и Overpass;
* `gui/` — отдельное приложение на Python (сборка карты и запуск пайплайна);
* `core`-скрипты `calc_srtm_tiles_list.py`, `process_bbox.py`,
  `replace_bbox_xml.py`, `run_alg.py`, `update_config.py`;
* `expressions/`, `queries/`, `icons/`, `config.ini`, `set_dirs.ini`,
  `crop_template.geojson` — исходные копии ресурсов, которые сейчас лежат в
  `resources/` плагина;
* `QGIS3/` — профиль QGIS, использовавшийся в Docker;
* `docs/` — скриншоты и описание прежнего интерфейса.

Текущий плагин из этого каталога ничего не использует и не обновляется.
Каталог сохранён как история проекта.
