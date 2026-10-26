from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def format_resort_name(name, resort, abandoned, feature, parent):
    n = name
    r = resort
    short_name = ""
    abandoned_string = ""
    if "база отдыха" in n.lower(): short_name = "б.о."
    if "БО " in n: short_name = "б.о."
    if r == "recreation_center": short_name = "б.о."
    if "ДОЛ" in n or "лагерь" in n.lower() or "пионерлаг" in n.lower(): short_name = "дет.лаг."
    if r == "kids_camp": short_name = "дет.лаг."
    if "турбаза" in n.lower(): short_name = "т/б"
    if r == "tourist_camp": short_name = "т/б"
    if "санатор" in n.lower(): short_name = "сан."
    if r == "sanatorium": short_name = "сан."
    if "пансион" in n.lower(): short_name = "пансион."
    if r == "pension": short_name = "пансион."
    if r == "hunting": short_name = "охот."

    if abandoned == "yes": abandoned_string = "\n(забр.)"

    result = short_name+abandoned_string
    return short_name+abandoned_string
