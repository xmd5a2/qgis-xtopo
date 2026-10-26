from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_sand_desert_name(name, feature, parent):
    n = name

    if n.lower() == 'пески': n = ''
    if n.lower() == 'песок': n = ''
    if n.lower() == 'пустыня': n = ''
    if n.lower() == 'пустыни': n = ''
    if n.lower() == 'бархан': n = ''
    if n.lower() == 'барханы': n = ''
    if n.lower() == 'дюны': n = ''
    if n.lower() == 'дюна': n = ''
    if n.lower().startswith('возвышенность '): n = re.sub("^возвышенность ","возвыш. ", n, flags=re.IGNORECASE)
    if n.lower().startswith('пески '): n = re.sub("^пески ","пес. ", n, flags=re.IGNORECASE)
    return n
