# Activate refFunctions plugin on QGIS startup
from PyQt5.QtCore import QSettings
packageName = 'refFunctions'
QSettings().setValue( "PythonPlugins/" + packageName, True )