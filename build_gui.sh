#!/bin/bash
project_dir=$(pwd)
cd gui
pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui.py
docker run -v "$(pwd):/src/" cdrx/pyinstaller-windows "pyinstaller --onefile -i /src/images/logo_icon.ico --noconsole --clean qgis-xtopo-gui.py"