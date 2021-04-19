#!/bin/bash
project_dir=$(pwd)
cp -f calc_srtm_tiles_list.py gui/
cp -f process_bbox.py gui/
cd gui
pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui-linux.spec
docker run -v "$(pwd):/src/" cdrx/pyinstaller-windows "pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui-win.spec"
# upx -9 -o dist/qgis-xtopo-gui-upx.exe dist/qgis-xtopo-gui.exe
# rm -f dist/qgis-xtopo-gui.exe
# mv dist/qgis-xtopo-gui-upx.exe dist/qgis-xtopo-gui.exe
rm -f calc_srtm_tiles_list.py
rm -f process_bbox.py