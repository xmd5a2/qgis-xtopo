from qgis.core import *
from qgis.gui import *
import re

@qgsfunction(args='auto', group='Custom')
def format_water_name(name, feature, parent):
    n = name
    if n.lower() == "водопад" \
    or n.lower() == "родник" \
    or n.lower() == "бассейн" \
    or n.lower() == "водоем" \
    or n.lower() == "водоём" \
    or n.lower() == "водохранилище" \
    or n.lower() == "воложка" \
    or n.lower() == "дренажная канава" \
    or n.lower() == "дренажный канал" \
    or n.lower() == "залив" \
    or n.lower() == "заводь" \
    or n.lower() == "запруда" \
    or n.lower() == "затон" \
    or n.lower() == "канава" \
    or n.lower() == "канал" \
    or n.lower() == "рыбопитомник" \
    or n.lower() == "карьер" \
    or n.lower() == "карьеры" \
    or n.lower() == "лагуна" \
    or n.lower() == "овраг" \
    or n.lower() == "озеро" \
    or n.lower() == "протока" \
    or n.lower() == "пруд" \
    or n.lower() == "река" \
    or n.lower() == "речка" \
    or n.lower() == "ручей" \
    or n.lower() == "старица" \
    or n.lower() == "spring" \
    or n.lower() == "water" \
    or n.lower() == "stream" \
    or n.lower() == "river" \
    or n.lower() == "drain" \
    or n.lower() == "canal" \
    or n == "исток": n = ''
    if "озеро" in n.lower(): n = re.sub("озеро","оз.",n, flags=re.IGNORECASE)
    elif n.lower().startswith("озёра "): n = re.sub("^озёра ","оз.",n, flags=re.IGNORECASE)
    elif n.lower().endswith(" озёра"): n = re.sub(" озёра$"," оз.",n, flags=re.IGNORECASE)
    elif "торфопредприятия" in n.lower(): n = re.sub("торфопредприятия","торф.",n, flags=re.IGNORECASE)
    elif "торфопредприятие" in n.lower(): n = re.sub("торфопредприятие","торф.",n, flags=re.IGNORECASE)
    elif "торфоразработки" in n.lower(): n = re.sub("торфоразработки","торф.",n, flags=re.IGNORECASE)
    elif "торфоболото" in n.lower(): n = re.sub("торфоболото","торф-бол.",n, flags=re.IGNORECASE)
    elif "рыбопитомника" in n.lower(): n = re.sub("рыбопитомника","рыб.",n, flags=re.IGNORECASE)
    elif "рыбопитомник" in n.lower(): n = re.sub("рыбопитомник","рыб.",n, flags=re.IGNORECASE)

    elif "хозяйство" in n.lower(): n = re.sub("хозяйство","хоз-во",n, flags=re.IGNORECASE)
    elif n.lower().startswith("лиман "): n = re.sub("^лиман ","лим. ",n, flags=re.IGNORECASE)
    elif n.lower().endswith(" лиман"): n = re.sub(" лиман$"," лим.",n, flags=re.IGNORECASE)
    elif "заводь" in n.lower(): n = re.sub("заводь","зав.",n, flags=re.IGNORECASE)
    elif "затон " in n.lower(): n = re.sub("затон ","зат. ",n, flags=re.IGNORECASE)
    elif " затон" in n.lower(): n = re.sub(" затон"," зат.",n, flags=re.IGNORECASE)
    elif "плотина " in n.lower(): n = re.sub("плотина ","пл. ",n, flags=re.IGNORECASE)
    elif " плотина" in n.lower(): n = re.sub(" плотина"," пл.",n, flags=re.IGNORECASE)
    elif "запруда " in n.lower(): n = re.sub("запруда","",n, flags=re.IGNORECASE)
    elif n.lower().startswith("пруд"): n = re.sub("^пруд","пр.",n, flags=re.IGNORECASE)
    elif n.lower().endswith("пруд"): n = re.sub("пруд$","пр.",n, flags=re.IGNORECASE)
    elif "водохранилище" in n.lower(): n = re.sub("водохранилище","вдхр.",n, flags=re.IGNORECASE)
    elif "водоём " in n.lower(): n = re.sub("водоём ","вод. ",n, flags=re.IGNORECASE)
    elif " водоём" in n.lower(): n = re.sub(" водоём"," вод.",n, flags=re.IGNORECASE)
    elif "ручей " in n.lower(): n = re.sub("ручей ","руч. ",n, flags=re.IGNORECASE)
    elif " ручей" in n.lower(): n = re.sub(" ручей"," руч.",n, flags=re.IGNORECASE)
    elif "бассейн " in n.lower(): n = re.sub("бассейн ","басс. ",n, flags=re.IGNORECASE)
    elif " бассейн" in n.lower(): n = re.sub(" бассейн"," басс.",n, flags=re.IGNORECASE)
    elif "лагуна " in n.lower(): n = re.sub("лагуна ","лаг. ",n, flags=re.IGNORECASE)
    elif " лагуна" in n.lower(): n = re.sub(" лагуна"," лаг.",n, flags=re.IGNORECASE)
    elif "залив " in n.lower(): n = re.sub("залив ","зал. ",n, flags=re.IGNORECASE)
    elif " залив" in n.lower(): n = re.sub(" залив"," зал.",n, flags=re.IGNORECASE)
    elif "протока " in n.lower(): n = re.sub("протока ","прот. ",n, flags=re.IGNORECASE)
    elif " протока" in n.lower(): n = re.sub(" протока"," прот.",n, flags=re.IGNORECASE)
    elif "воложка " in n.lower(): n = re.sub("воложка ","вол. ",n, flags=re.IGNORECASE)
    elif " воложка" in n.lower(): n = re.sub(" воложка"," вол.",n, flags=re.IGNORECASE)
    elif "старица " in n.lower(): n = re.sub("старица ","стар. ",n, flags=re.IGNORECASE)
    elif " старица" in n.lower(): n = re.sub(" старица"," стар.",n, flags=re.IGNORECASE)
    elif "овраг " in n.lower(): n = re.sub("овраг ","овр. ",n, flags=re.IGNORECASE)
    elif " овраг" in n.lower(): n = re.sub(" овраг"," овр.",n, flags=re.IGNORECASE)
    elif n.lower().startswith("водопад "): n = re.sub("водопад ","вдп. ",n, flags=re.IGNORECASE)
    elif n.lower().startswith("водопады "): n = re.sub("водопады ","вдп. ",n, flags=re.IGNORECASE)
    elif n.lower().endswith(" водопад"): n = re.sub(" водопад"," вдп.",n, flags=re.IGNORECASE)
    elif n.lower().endswith(" водопады"): n = re.sub(" водопады"," вдп.",n, flags=re.IGNORECASE)
    elif n.lower().startswith("болото"): n = re.sub("^болото","бол.",n, flags=re.IGNORECASE)
    elif n.lower().endswith("болото"): n = re.sub("болото$","бол.",n, flags=re.IGNORECASE)
    elif n.lower().endswith("болота"): n = re.sub("болота$","бол.",n, flags=re.IGNORECASE)
    elif "урочище" in n.lower(): n = re.sub("урочище","ур.",n, flags=re.IGNORECASE)
    elif n.lower().startswith("ерик"): n = re.sub("^ерик","ер.",n, flags=re.IGNORECASE)
    elif "карьер-" in n.lower(): n = ""
    elif n.lower().startswith("карьеры"): n = re.sub("^карьеры","кар.",n, flags=re.IGNORECASE)
    elif n.lower().startswith("карьер"): n = re.sub("^карьер","кар.",n, flags=re.IGNORECASE)
    elif n.lower().endswith("карьер"): n = re.sub("карьер$","кар.",n, flags=re.IGNORECASE)
    elif n.lower().endswith("карьера"): n = re.sub("карьера$","кар.",n, flags=re.IGNORECASE)
    elif "земснаряд " in n.lower(): n = ""

    if "(залив)" in n.lower(): n = re.sub("(залив)","",n, flags=re.IGNORECASE)
    if "(пролив)" in n.lower(): n = re.sub("(пролив)","",n, flags=re.IGNORECASE)

    if n.lower().startswith('большой') and " " in n and len(n) > 8: n = re.sub("большой","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большие') and " " in n and len(n) > 8: n = re.sub("большие","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большое') and " " in n and len(n) > 8: n = re.sub("большое","Бол.", n, flags=re.IGNORECASE)
    if n.lower().startswith('большая') and " " in n and len(n) > 8: n = re.sub("большая","Бол.", n, flags=re.IGNORECASE)
    elif "малая" in n.lower() and " " in n and len(n) > 8: n = re.sub("малая","Мал.",n, flags=re.IGNORECASE)
    elif "малый" in n.lower() and " " in n and len(n) > 8: n = re.sub("малый","Мал.",n, flags=re.IGNORECASE)
    elif "малые" in n.lower() and " " in n and len(n) > 8: n = re.sub("малые","Мал.",n, flags=re.IGNORECASE)
    elif "малое" in n.lower() and " " in n and len(n) > 8: n = re.sub("малое","Мал.",n, flags=re.IGNORECASE)
    elif "старая" in n.lower() and " " in n and len(n) > 8: n = re.sub("старая","Стар.",n, flags=re.IGNORECASE)
    elif "старый" in n.lower() and " " in n and len(n) > 8: n = re.sub("старый","Стар.",n, flags=re.IGNORECASE)
    elif "старые" in n.lower() and " " in n and len(n) > 8: n = re.sub("старые","Стар.",n, flags=re.IGNORECASE)
    elif "старое" in n.lower() and " " in n and len(n) > 8: n = re.sub("старое","Стар.",n, flags=re.IGNORECASE)
    elif n.lower().startswith('новая ') and len(n) > 8: n = re.sub("^новая ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новый ') and len(n) > 8: n = re.sub("^новый ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новые ') and len(n) > 8: n = re.sub("^новые ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().startswith('новое ') and len(n) > 8: n = re.sub("^новое ","Нов. ", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новая') and len(n) > 8: n = re.sub(" новая$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новый') and len(n) > 8: n = re.sub(" новый$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новые') and len(n) > 8: n = re.sub(" новые$"," Нов.", n, flags=re.IGNORECASE)
    elif n.lower().endswith(' новое') and len(n) > 8: n = re.sub(" новое$", "Нов.", n, flags=re.IGNORECASE)
    elif "нижняя" in n.lower() and " " in n and len(n) > 8: n = re.sub("нижняя","Ниж.",n, flags=re.IGNORECASE)
    elif "нижний" in n.lower() and " " in n and len(n) > 8: n = re.sub("нижний","Ниж.",n, flags=re.IGNORECASE)
    elif "нижние" in n.lower() and " " in n and len(n) > 8: n = re.sub("нижние","Ниж.",n, flags=re.IGNORECASE)
    elif "нижнее" in n.lower() and " " in n and len(n) > 8: n = re.sub("нижнее","Ниж.",n, flags=re.IGNORECASE)
    elif "верхняя" in n.lower() and " " in n and len(n) > 8: n = re.sub("верхняя","Верх.",n, flags=re.IGNORECASE)
    elif "верхний" in n.lower() and " " in n and len(n) > 8: n = re.sub("верхний","Верх.",n, flags=re.IGNORECASE)
    elif "верхние" in n.lower() and " " in n and len(n) > 8: n = re.sub("верхние","Верх.",n, flags=re.IGNORECASE)
    elif "верхнее" in n.lower() and " " in n and len(n) > 8: n = re.sub("верхнее","Верх.",n, flags=re.IGNORECASE)

    elif "красная" in n.lower() and " " in n and len(n) > 8: n = re.sub("красная","Кр.",n, flags=re.IGNORECASE)
    elif "красный" in n.lower() and " " in n and len(n) > 8: n = re.sub("красный","Кр.",n, flags=re.IGNORECASE)
    elif "красные" in n.lower() and " " in n and len(n) > 8: n = re.sub("красные","Кр.",n, flags=re.IGNORECASE)
    elif "красное" in n.lower() and " " in n and len(n) > 8: n = re.sub("красное","Кр.",n, flags=re.IGNORECASE)
    elif "черное" in n.lower() and " " in n and len(n) > 8: n = re.sub("черное","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрное" in n.lower() and " " in n and len(n) > 8: n = re.sub("чёрное","Чёрн.",n, flags=re.IGNORECASE)
    elif "черная" in n.lower() and " " in n and len(n) > 8: n = re.sub("черная","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрная" in n.lower() and " " in n and len(n) > 8: n = re.sub("чёрная","Чёрн.",n, flags=re.IGNORECASE)
    elif "черный" in n.lower() and " " in n and len(n) > 8: n = re.sub("черный","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрный" in n.lower() and " " in n and len(n) > 8: n = re.sub("чёрный","Чёрн.",n, flags=re.IGNORECASE)
    elif "черные" in n.lower() and " " in n and len(n) > 8: n = re.sub("черные","Чёрн.",n, flags=re.IGNORECASE)
    elif "чёрные" in n.lower() and " " in n and len(n) > 8: n = re.sub("чёрные","Чёрн.",n, flags=re.IGNORECASE)
    elif "белая" in n.lower() and " " in n and len(n) > 8: n = re.sub("белая","Бел.",n, flags=re.IGNORECASE)
    elif "белый" in n.lower() and " " in n and len(n) > 8: n = re.sub("белый","Бел.",n, flags=re.IGNORECASE)
    elif "белые" in n.lower() and " " in n and len(n) > 8: n = re.sub("белые","Бел.",n, flags=re.IGNORECASE)
    elif "белое" in n.lower() and " " in n and len(n) > 8: n = re.sub("белое","Бел.",n, flags=re.IGNORECASE)

    if n.lower().startswith("неизвестный"): n = re.sub("неизвестный","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестный"): n = re.sub("неизвестный","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестное"): n = re.sub("неизвестное","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестное"): n = re.sub("неизвестное","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестная"): n = re.sub("неизвестная","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестная"): n = re.sub("неизвестная","неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().startswith("неизвестные"): n = re.sub("неизвестные","Неизвестн.",n, flags=re.IGNORECASE)
    if n.lower().endswith("неизвестные"): n = re.sub("неизвестные","неизвестн.",n, flags=re.IGNORECASE)

    if "uvala" in n.lower() and " " in n: n = re.sub("uvala","Uv.",n, flags=re.IGNORECASE)
    elif "zaliv" in n.lower() and " " in n: n = re.sub("zaliv","zal.",n, flags=re.IGNORECASE)

    if " исток" in n.lower() and "." not in n: n = re.sub(" исток"," ист.",n, flags=re.IGNORECASE)
    if "исток " in n.lower() and "." not in n: n = re.sub("исток ","ист. ",n, flags=re.IGNORECASE)
#    if (". " or " ." or "оз " or " оз") not in n.lower() and (n.lower().endswith("ое") or n.lower().endswith("ее")): n = "оз. " + n

    return n
