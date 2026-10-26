from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_spoil_heap_name(name, feature, parent):
    n = name

    if n.lower() == 'террикон': n = ''
    if n.lower() == 'терриконы': n = ''
    if n.lower() == 'терикон': n = ''
    if n.lower() == 'терикони': n = ''
    if n.lower() == 'террикон шахты': n = ''
    if n.lower() == 'отвал': n = ''
    if n.lower() == 'отвалы': n = ''
    if n.lower() == 'отв.': n = ''
    if n.lower() == 'отвал шахты': n = ''
    if n.lower() == 'свалка': n = ''
    if n.lower() == 'шламоотвал': n = ''
    if n.lower().startswith('отвал '): n = re.sub("^отвал ","отв. ", n, flags=re.IGNORECASE)
    if n.lower().startswith('отвалы '): n = re.sub("^отвалы ","отв. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' отвал'): n = re.sub(" отвал$"," отв.", n, flags=re.IGNORECASE)
    if n.lower().endswith(' отвалы'): n = re.sub(" отвалы$"," отв.", n, flags=re.IGNORECASE)
    if n.lower().startswith('террикон '): n = re.sub("^террикон ","тер. ", n, flags=re.IGNORECASE)
    if n.lower().startswith('терриконы '): n = re.sub("^терриконы ","тер. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' террикон'): n = re.sub(" террикон$"," тер.", n, flags=re.IGNORECASE)
    if n.lower().endswith(' терриконы'): n = re.sub(" терриконы$"," тер.", n, flags=re.IGNORECASE)
    if " шахты " in n.lower(): n = re.sub(" шахты "," шах. ", n, flags=re.IGNORECASE)
    if " шахти " in n.lower(): n = re.sub(" шахти "," шах. ", n, flags=re.IGNORECASE)
    if n.lower().startswith('шахта '): n = re.sub("^шахта ","шах. ", n, flags=re.IGNORECASE)
    return n
