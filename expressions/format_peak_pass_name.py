from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_peak_pass_name(name, feature, parent):
    n = name
    if n.lower() == 'сопка': n = ""
    if n.lower() == 'вершина': n = ""
    if n.lower() == 'пик': n = ""
    if n.lower() == 'скала': n = ""
    if n.lower() == 'гора': n = ""
    if n.lower().startswith('пик '): n = re.sub("^пик ","", n, flags=re.IGNORECASE)
    if n.lower().endswith(' пик'): n = re.sub(" пик$","", n, flags=re.IGNORECASE)
    if ' пик ' in n.lower(): n = re.sub(" пик "," ", n, flags=re.IGNORECASE)
    if n.lower().startswith('п.'): n = re.sub("^п.","", n, flags=re.IGNORECASE)
    if n.lower().startswith('скала '): n = re.sub("^скала ","", n, flags=re.IGNORECASE)
    if n.lower().endswith(' скала'): n = re.sub(" скала$","", n, flags=re.IGNORECASE)
    if n.lower().startswith('гора '): n = re.sub("^гора ","", n, flags=re.IGNORECASE)
    if n.lower().endswith(' гора'): n = re.sub(" гора$","", n, flags=re.IGNORECASE)
    if n.lower().startswith('г. '): n = re.sub("^г. ","г. ", n, flags=re.IGNORECASE)
    if n.lower().startswith('сопка '): n = re.sub("^сопка ","соп. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' сопка'): n = re.sub(" сопка$"," соп.", n, flags=re.IGNORECASE)
    if n.lower().startswith('соп.'): n = re.sub("^соп.","соп.", n, flags=re.IGNORECASE)
    if n.lower().endswith(' соп.'): n = re.sub(" соп.$"," соп.", n, flags=re.IGNORECASE)
    if 'плато' in n.lower() and " " in n: n = re.sub("плато","пл.", n, flags=re.IGNORECASE)
    if 'перевал' in n.lower() and " " in n: n = re.sub("перевал","", n, flags=re.IGNORECASE)
    if 'пер.' in n.lower(): n = re.sub("пер.","", n, flags=re.IGNORECASE)
    if 'седловина' in n.lower() and " " in n: n = re.sub("седловина","седл.", n, flags=re.IGNORECASE)
    if '-летия' in n.lower() and " " in n: n = re.sub("-летия","-лет.", n, flags=re.IGNORECASE)
    if ' летия' in n.lower() and " " in n: n = re.sub(" летия"," лет.", n, flags=re.IGNORECASE)
    if n.lower().startswith('mount '): n = re.sub("^mount ","Mt. ", n, flags=re.IGNORECASE)
    if n.lower().endswith(' mount'): n = re.sub(" mount$"," Mt.", n, flags=re.IGNORECASE)

    if n.lower().startswith('большой') and " " in n: n = re.sub("большой","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большие') and " " in n: n = re.sub("большие","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большая') and " " in n: n = re.sub("большая","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большое') and " " in n: n = re.sub("большое","Бол.", n, flags=re.IGNORECASE)

    if 'дополнительная' in n.lower() and " " in n: n = re.sub("дополнительная","Доп.", n, flags=re.IGNORECASE)
    elif 'дополнительный' in n.lower() and " " in n: n = re.sub("дополнительный","Доп.", n, flags=re.IGNORECASE)
    elif 'дополнительные' in n.lower() and " " in n: n = re.sub("дополнительные","Доп.", n, flags=re.IGNORECASE)
    elif 'дополнительное' in n.lower() and " " in n: n = re.sub("дополнительное","Доп.", n, flags=re.IGNORECASE)
    elif 'центральная' in n.lower() and " " in n: n = re.sub("центральная","Центр.", n, flags=re.IGNORECASE)
    elif 'центральный' in n.lower() and " " in n: n = re.sub("центральный","Центр.", n, flags=re.IGNORECASE)
    elif 'центральные' in n.lower() and " " in n: n = re.sub("центральные","Центр.", n, flags=re.IGNORECASE)
    elif 'центральное' in n.lower() and " " in n: n = re.sub("центральное","Центр.", n, flags=re.IGNORECASE)
    elif 'советская' in n.lower() and " " in n: n = re.sub("советская","Сов.", n, flags=re.IGNORECASE)
    elif 'советский' in n.lower() and " " in n: n = re.sub("советский","Сов.", n, flags=re.IGNORECASE)
    elif 'советские' in n.lower() and " " in n: n = re.sub("советские","Сов.", n, flags=re.IGNORECASE)
    elif 'советское' in n.lower() and " " in n: n = re.sub("советское","Сов.", n, flags=re.IGNORECASE)
    elif 'советского' in n.lower() and " " in n: n = re.sub("советского","Сов.", n, flags=re.IGNORECASE)
    elif 'восточная' in n.lower() and " " in n: n = re.sub("восточная","Вост.", n, flags=re.IGNORECASE)
    elif 'восточный' in n.lower() and " " in n: n = re.sub("восточный","Вост.", n, flags=re.IGNORECASE)
    elif 'восточные' in n.lower() and " " in n: n = re.sub("восточные","Вост.", n, flags=re.IGNORECASE)
    elif 'восточное' in n.lower() and " " in n: n = re.sub("восточное","Вост.", n, flags=re.IGNORECASE)
    elif 'северная' in n.lower() and " " in n: n = re.sub("северная","Сев.", n, flags=re.IGNORECASE)
    elif 'северный' in n.lower() and " " in n: n = re.sub("северный","Сев.", n, flags=re.IGNORECASE)
    elif 'северные' in n.lower() and " " in n: n = re.sub("северные","Сев.", n, flags=re.IGNORECASE)
    elif 'северное' in n.lower() and " " in n: n = re.sub("северное","Сев.", n, flags=re.IGNORECASE)
    elif 'западная' in n.lower() and " " in n: n = re.sub("западная","Зап.", n, flags=re.IGNORECASE)
    elif 'западный' in n.lower() and " " in n: n = re.sub("западный","Зап.", n, flags=re.IGNORECASE)
    elif 'западные' in n.lower() and " " in n: n = re.sub("западные","Зап.", n, flags=re.IGNORECASE)
    elif 'западное' in n.lower() and " " in n: n = re.sub("западное","Зап.", n, flags=re.IGNORECASE)
    elif 'верхняя' in n.lower() and " " in n: n = re.sub("верхняя","Верх.", n, flags=re.IGNORECASE)
    elif 'верхний' in n.lower() and " " in n: n = re.sub("верхний","Верх.", n, flags=re.IGNORECASE)
    elif 'верхние' in n.lower() and " " in n: n = re.sub("верхние","Верх.", n, flags=re.IGNORECASE)
    elif 'верхнее' in n.lower() and " " in n: n = re.sub("верхнее","Верх.", n, flags=re.IGNORECASE)
    elif 'снежная' in n.lower() and " " in n: n = re.sub("снежная","Снеж.", n, flags=re.IGNORECASE)
    elif 'снежный' in n.lower() and " " in n: n = re.sub("снежный","Снеж.", n, flags=re.IGNORECASE)
    elif 'снежные' in n.lower() and " " in n: n = re.sub("снежные","Снеж.", n, flags=re.IGNORECASE)
    elif 'снежное' in n.lower() and " " in n: n = re.sub("снежное","Снеж.", n, flags=re.IGNORECASE)
    elif 'озерная' in n.lower() and " " in n: n = re.sub("озерная","Озерн.", n, flags=re.IGNORECASE)
    elif 'озерный' in n.lower() and " " in n: n = re.sub("озерный","Озерн.", n, flags=re.IGNORECASE)
    elif 'озерные' in n.lower() and " " in n: n = re.sub("озерные","Озерн.", n, flags=re.IGNORECASE)
    elif 'озерное' in n.lower() and " " in n: n = re.sub("озерное","Озерн.", n, flags=re.IGNORECASE)
    elif 'средняя' in n.lower() and " " in n: n = re.sub("средняя","Сред.", n, flags=re.IGNORECASE)
    elif 'средний' in n.lower() and " " in n: n = re.sub("средний","Сред.", n, flags=re.IGNORECASE)
    elif 'средние' in n.lower() and " " in n: n = re.sub("средние","Сред.", n, flags=re.IGNORECASE)
    elif 'среднее' in n.lower() and " " in n: n = re.sub("среднее","Сред.", n, flags=re.IGNORECASE)
    elif 'высокая' in n.lower() and " " in n: n = re.sub("высокая","Высок.", n, flags=re.IGNORECASE)
    elif 'высокий' in n.lower() and " " in n: n = re.sub("высокий","Высок.", n, flags=re.IGNORECASE)
    elif 'высокие' in n.lower() and " " in n: n = re.sub("высокие","Высок.", n, flags=re.IGNORECASE)
    elif 'высокое' in n.lower() and " " in n: n = re.sub("высокое","Высок.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('красная') and " " in n: n = re.sub("^красная","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('красный') and " " in n: n = re.sub("^красный","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('красные') and " " in n: n = re.sub("^красные","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('красное') and " " in n: n = re.sub("^красное","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('красная') and " " in n: n = re.sub("красная$","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('красный') and " " in n: n = re.sub("красный$","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('красные') and " " in n: n = re.sub("красные$","Кр.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('красное') and " " in n: n = re.sub("красное$","Кр.", n, flags=re.IGNORECASE)
    elif ' ложная' in n.lower(): n = re.sub(" ложная"," Ложн.", n, flags=re.IGNORECASE)
    elif 'ложная ' in n.lower(): n = re.sub("ложная ","Ложн. ", n, flags=re.IGNORECASE)
    elif ' ложный' in n.lower(): n = re.sub(" ложный"," Ложн.", n, flags=re.IGNORECASE)
    elif 'ложный ' in n.lower(): n = re.sub("ложный ","Ложн. ", n, flags=re.IGNORECASE)
    elif ' ложные' in n.lower(): n = re.sub(" ложные"," Ложн.", n, flags=re.IGNORECASE)
    elif 'ложные ' in n.lower(): n = re.sub("ложные ","Ложн. ", n, flags=re.IGNORECASE)
    elif ' ложное' in n.lower(): n = re.sub(" ложное"," Ложн.", n, flags=re.IGNORECASE)
    elif 'ложное ' in n.lower(): n = re.sub("ложное ","Ложн. ", n, flags=re.IGNORECASE)
    elif 'нижняя' in n.lower() and " " in n: n = re.sub("нижняя","Ниж.", n, flags=re.IGNORECASE)
    elif 'нижний' in n.lower() and " " in n: n = re.sub("нижний","Ниж.", n, flags=re.IGNORECASE)
    elif 'нижние' in n.lower() and " " in n: n = re.sub("нижние","Ниж.", n, flags=re.IGNORECASE)
    elif 'нижнее' in n.lower() and " " in n: n = re.sub("нижнее","Ниж.", n, flags=re.IGNORECASE)
    elif 'низкая' in n.lower() and " " in n: n = re.sub("низкая","Низк.", n, flags=re.IGNORECASE)
    elif 'низкий' in n.lower() and " " in n: n = re.sub("низкий","Низк.", n, flags=re.IGNORECASE)
    elif 'низкие' in n.lower() and " " in n: n = re.sub("низкие","Низк.", n, flags=re.IGNORECASE)
    elif 'низкое' in n.lower() and " " in n: n = re.sub("низкое","Низк.", n, flags=re.IGNORECASE)
    elif 'южная' in n.lower() and " " in n: n = re.sub("южная","Юж.", n, flags=re.IGNORECASE)
    elif 'южный' in n.lower() and " " in n: n = re.sub("южный","Юж.", n, flags=re.IGNORECASE)
    elif 'южные' in n.lower() and " " in n: n = re.sub("южные","Юж.", n, flags=re.IGNORECASE)
    elif 'южное' in n.lower() and " " in n: n = re.sub("южное","Юж.", n, flags=re.IGNORECASE)
    elif 'малая' in n.lower() and " " in n: n = re.sub("малая","Мал.", n, flags=re.IGNORECASE)
    elif 'малый' in n.lower() and " " in n: n = re.sub("малый","Мал.", n, flags=re.IGNORECASE)
    elif 'малые' in n.lower() and " " in n: n = re.sub("малые","Мал.", n, flags=re.IGNORECASE)
    elif 'малое' in n.lower() and " " in n: n = re.sub("малое","Мал.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('ледовая') and " " in n: n = re.sub("^ледовая","лед.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('ледовый') and " " in n: n = re.sub("^ледовый","лед.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('ледовые') and " " in n: n = re.sub("^ледовые","лед.", n, flags=re.IGNORECASE)
    elif n.lower().startswith('ледовое') and " " in n: n = re.sub("^ледовое","лед.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('ледовая') and " " in n: n = re.sub("ледовая$","лед.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('ледовый') and " " in n: n = re.sub("ледовый$","лед.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('ледовые') and " " in n: n = re.sub("ледовые$","лед.", n, flags=re.IGNORECASE)
    elif n.lower().endswith('ледовое') and " " in n: n = re.sub("ледовое$","лед.", n, flags=re.IGNORECASE)

    return n
