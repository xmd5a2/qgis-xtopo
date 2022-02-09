from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_ridge_name(name, project_lang, feature, parent):
    n = name
    if project_lang == "ru":
        if n.lower().startswith("хребет "): n = re.sub("^хребет ","хр. ",n, flags=re.IGNORECASE).strip()
        if n.lower().endswith(" хребет"): n = re.sub(" хребет$"," хр.",n, flags=re.IGNORECASE).strip()
        if n.lower().startswith("отрог "): n = re.sub("^отрог ","отр. ",n, flags=re.IGNORECASE).strip()
        if n.lower().endswith(" отрог"): n = re.sub(" отрог$"," отр.",n, flags=re.IGNORECASE).strip()
        if not "хр." in n.lower() \
        and not n.lower().startswith("отр. ") \
        and not n.lower().endswith(" отр.") \
        and not "скалы " in n.lower() \
        and not " скалы" in n.lower() \
        and not " лбы" in n.lower() \
        and not n.lower().endswith(" ребро") \
        and not n.lower().startswith("ребро ") \
        and not n.lower().endswith(" плечо") \
        and not n.lower().startswith("плечо ") \
        and not n.lower().endswith(" гребень") \
        and not n.lower().startswith("гребень "): n = "хр. " + n
    elif n.lower().startswith('ridge '): n = re.sub("^ridge ","rid. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' ridge'): n = re.sub(" ridge$"," rid.", n, flags=re.IGNORECASE)
    return n
