#!/bin/bash
project_dir=$(pwd)
cp -f calc_srtm_tiles_list.py gui/
cp -f process_bbox.py gui/
cd gui
# pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui.py
docker run -v "$(pwd):/src/" cdrx/pyinstaller-windows "pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui.spec"
rm -f calc_srtm_tiles_list.py
rm -f process_bbox.py