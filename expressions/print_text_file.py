from qgis.core import *
from qgis.gui import *

@qgsfunction(args='auto', group='Custom')
def print_text_file(filename, prefix, project_home, feature, parent):
    if os.path.isfile(project_home+"/"+filename):
        f = open(project_home+"/"+filename, 'r')
        file_contents = prefix+f.read().replace("\n","")
        f.close()
    return file_contents
