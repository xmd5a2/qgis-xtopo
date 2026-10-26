from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_cemetery_name(name, project_lang, feature, parent):
    n = name
    if "кладбище" == n.lower(): n = ""

    return n
