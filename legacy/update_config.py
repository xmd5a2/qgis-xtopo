#!/usr/bin/python3
import argparse
import sys

for i, arg in enumerate(sys.argv):
    if (arg[0] == '-') and arg[1].isdigit(): sys.argv[i] = ' ' + arg
parser = argparse.ArgumentParser()
parser.add_argument("-str_in")
args = parser.parse_args()


def main(str_in):
    input_list = []
    try:
        input_list = str_in.split("|")
    except IndexError:
        pass
    k_list = []
    v_list = []
    for i in range(len(input_list)):
        tmp_list = input_list[i].split("==")
        try:
            k_list.append(tmp_list[0])
        except IndexError:
            pass
        try:
            v_list.append(tmp_list[1])
        except IndexError:
            pass
    settings_dict = {k: v for k, v in zip(k_list, v_list)}
    if settings_dict.get("bbox"):
        amp_bbox_pos = settings_dict.get("bbox").rfind("&")
        if amp_bbox_pos > 1 and "map=" in settings_dict.get("bbox"):
            settings_dict.update({"bbox": str(settings_dict.get("bbox"))[0:amp_bbox_pos]})
            if "\"" in settings_dict.get("bbox")[0] and "\"" not in settings_dict.get("bbox")[-1]:
                settings_dict.update({"bbox": str(settings_dict.get("bbox") + "\"")})
    config_path = settings_dict.get("config_dir") + "/config.ini"
    settings_dict.pop("config_dir", None)
    update_config(settings_dict, config_path)


def update_config(settings_dict, config_path):
    if settings_dict is None:
        settings_dict = {}
    try:
        with open(config_path, 'r') as config:
            filedata = config.read().splitlines()
    except FileNotFoundError:
        print("Config " + settings_dict.get("config_dir") + " not found")
        exit(1)

    key_to_delete = ''
    for i in range(len(filedata)):
        for key in settings_dict:
            if key + "=" in filedata[i] and filedata[i].strip().startswith(key) and not filedata[i].startswith('#') and \
                    filedata[i].strip()[0:1] != "#":
                filedata[i] = key + "=" + settings_dict.get(key)
                key_to_delete = key
                continue
            else:
                if i == len(filedata) - 1:
                    filedata.append(key + "=" + settings_dict.get(key))
                    key_to_delete = key
        settings_dict.pop(key_to_delete, None)

    filedata = list_to_string_file(filedata)
    with open(config_path, 'w') as file:
        file.write(filedata)


def list_to_string_file(list_str):
    str1 = ""
    for i in list_str:
        str1 += i + "\n"
    return str1


if __name__ == '__main__':
    main(args.str_in)
