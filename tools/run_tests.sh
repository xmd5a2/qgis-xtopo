#!/bin/sh
# Прогон тестов плагина.
#
# Пакет плагина регистрируется под именем qgis_xtopo, поэтому верхний
# уровень поиска модулей указывается на корень репозитория (-t .).
set -e
cd "$(dirname "$0")/.."
exec env QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s tests -t . "$@"
