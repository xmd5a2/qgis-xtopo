#!/bin/sh
# Сборка, установка плагина и запуск QGIS с открытой панелью плагина.
#
# Использование: tools/run_qgis.sh [аргументы qgis]
set -e
cd "$(dirname "$0")/.."
python3 tools/build_plugin.py --install
export QGIS_XTOPO_OPEN=1
exec qgis "$@"
