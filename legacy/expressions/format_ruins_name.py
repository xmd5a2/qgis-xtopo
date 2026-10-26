from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_ruins_name(name, feature, parent):
    n = name

    if "руины" == n.lower() or "разрушенное здание" == n.lower() or "развалины" in n.lower() or "заброшенн" in n.lower() or "ruins" == n.lower() or "ancient ruins" == n.lower() or " строения" in n.lower() or " строение" in n.lower(): n = ""

    return n
