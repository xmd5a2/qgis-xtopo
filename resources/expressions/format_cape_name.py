from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_cape_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
#         if len(n) <= 3 and not "м." in n.lower():
#             n = "м " + n
        if n.lower().startswith("мыс "):
            n = re.sub("мыс ","м. ", n, flags=re.IGNORECASE).strip()
        elif n.lower().endswith(" мыс"):
            n = re.sub(" мыс"," м.", n, flags=re.IGNORECASE).strip()
        if not n.lower().startswith("мыс ") and \
                not n.lower().endswith(" мыс") and \
                not n.lower().startswith("м.") and \
                not n.lower().endswith(" м."):
            n = "м. " + n
    if n.strip() == "м.": n = ""

    return n
