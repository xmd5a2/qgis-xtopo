from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_power_name(name, feature, parent):
    n = name
    if 'подстанция' in n.lower(): n = re.sub("подстанция","п/c", n, flags=re.IGNORECASE)
    if n.lower().startswith('пс '): n = re.sub("пс ","п/c ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' пс'): n = re.sub(" пс"," п/c", n, flags=re.IGNORECASE)
    if 'гидроэлектростанция' in n.lower(): n = re.sub("гидроэлектростанция","ГЭС", n, flags=re.IGNORECASE)
    if n.lower().startswith('гэс '): n = re.sub("гэс ","ГЭС ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' гэс'): n = re.sub(" гэс"," ГЭС", n, flags=re.IGNORECASE)

    return n
