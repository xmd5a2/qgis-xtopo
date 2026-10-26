from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_locality_name(name, project_lang, feature, parent):
    n = name

    if n.lower() == 'сарай' or n.lower() == 'сараи': n = "сар."
    if n.lower().startswith('развалины '): n = re.sub("^развалины ","разв. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' развалины'): n = re.sub(" развалины$"," разв.", n, flags=re.IGNORECASE)
    if n.lower().startswith("водопады "): n = re.sub("водопады ","вдп. ",n, flags=re.IGNORECASE)
    if n.lower().endswith(" водопады"): n = re.sub(" водопады"," вдп.",n, flags=re.IGNORECASE)
    if n.lower().startswith("водопад "): n = re.sub("водопад ","вдп. ",n, flags=re.IGNORECASE)
    if n.lower().endswith(" водопад"): n = re.sub(" водопад"," вдп.",n, flags=re.IGNORECASE)

    if project_lang == "ru" and \
    "ур." not in n and \
    "хозяйств" not in n.lower() and \
    "леснич" not in n.lower() and \
    "охотни" not in n.lower() and \
    "охот." not in n.lower() and \
    "лагер" not in n.lower() and \
    "пионер" not in n.lower() and \
    "погост" not in n.lower() and \
    "корд." not in n.lower() and \
    "погран" not in n.lower() and \
    "прист." not in n.lower() and \
    "пос. " not in n.lower() and \
    "поляна" not in n.lower() and \
    "долина" not in n.lower() and \
    "ферма" not in n.lower() and \
    "сан." not in n.lower() and \
    "б. " not in n.lower() and \
    " б." not in n.lower() and \
    "плато" not in n.lower() and \
    "бол." not in n and \
    "торф." not in n.lower() and \
    "б.о." not in n.lower() and \
    "д.о." not in n.lower() and \
    "т/б" not in n.lower() and \
    "ДОЛ " not in n and \
    n != "лет." and \
    n != "зим." and \
    not (n.lower().endswith(" вал")) and \
    "лесопилка" not in n.lower(): n = "ур. " + n

    return n
