from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def get_ele(ele, ele_calc, feature, parent):
    result=""
    if int(ele_calc) > -500: result=ele_calc
    if int(ele) > -500: result="\n"+str(ele)+" м"
    return result
