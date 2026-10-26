from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_glacier_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
        if len(n) <= 3 and not "л." in n.lower():
            n = "ледник " + n
        elif "ледник" in n.lower():
            n = re.sub("ледник", "л.", n, flags=re.IGNORECASE).strip()
        if not "ледн." in n.lower() and \
                not "ледник" in n.lower() and \
                not n.lower().startswith("л.") and \
                not n.lower().endswith("л.") and \
                not "пл. " in n.lower() and \
                not " пл." in n.lower() and \
                not "glacier" in n.lower():
            n = "л. " + n
        elif n.lower().startswith('glacier '):
            n = re.sub("^glacier ", "gl. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' glacier'): n = re.sub(" glacier$", " gl.", n, flags=re.IGNORECASE)
    if n.strip() == "л.": n = ""
    return n.strip()
