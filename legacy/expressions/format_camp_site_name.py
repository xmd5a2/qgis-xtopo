from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_camp_site_name(name, feature, parent):
    n = name

    if "место для" in n.lower() or "места под " in n.lower() or "примерное" in n.lower() or "место под " in n.lower() or "палаточный лагерь" in n.lower() or "кемпинг" in n.lower() or n.lower() == "стоянка" or n.lower() == "стоянки" or "вода внизу" in n.lower() or "воды нет" in n.lower() or "проблем" in n.lower() or "скорпионы" in n.lower() or "с видом" in n.lower() or "с отличным" in n.lower() or "с хорошим" in n.lower() or "кострище" in n.lower() or "квартир" in n.lower() or "коттедж" in n.lower() or "домик" in n.lower(): n = ""
    if "лагерь" in n.lower(): n = re.sub("лагерь","лаг.", n, flags=re.IGNORECASE)
    if "турприют" in n.lower(): n = re.sub("турприют","", n, flags=re.IGNORECASE)

    return n
