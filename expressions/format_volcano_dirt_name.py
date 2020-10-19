from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_volcano_dirt_name(name, feature, parent):
    n = name
    if n.lower() == 'грязевой вулкан': n = ''
    if n.lower() == 'грязевая сопка': n = ''
    if 'грязевой вулкан' in n.lower(): n = re.sub("грязевой вулкан","гряз. вулкан", n, flags=re.IGNORECASE)
    if 'грязевая сопка' in n.lower(): n = re.sub("грязевая сопка","гряз. сопка", n, flags=re.IGNORECASE)

    return n
