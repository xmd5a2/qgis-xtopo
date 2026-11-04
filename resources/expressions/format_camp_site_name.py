from qgis.core import *
from qgis.gui import *
import re

# Названия, из которых подпись не нужна: они описывают место, а не имя.
CAMP_HIDDEN_NAMES = (
    "место для", "места под ", "примерное", "место под ", "палаточный лагерь",
    "палаточный лаг.", "кемпинг", "вода внизу",
    "воды нет", "проблем", "скорпионы", "с видом", "с отличным",
    "с хорошим", "кострище", "квартир", "коттедж", "домик",
)


@qgsfunction(args='auto', group='Custom')
def format_camp_site_name(name, feature, parent):
    n = name

    hidden = False
    for marker in CAMP_HIDDEN_NAMES:
        if marker in n.lower():
            hidden = True
            break
    if n.lower() in ("стоянка", "стоянки"):
        hidden = True
    if hidden: n = ""
    if "лагерь" in n.lower(): n = re.sub("лагерь","лаг.", n, flags=re.IGNORECASE)
    if "турприют" in n.lower(): n = re.sub("турприют","", n, flags=re.IGNORECASE)

    return " ".join(n.split())
