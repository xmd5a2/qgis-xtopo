#!/usr/bin/python3
import PySimpleGUI as sg
import subprocess
import sys
import webbrowser
import locale
import os
import time
import configparser


def main():
    r_keys = ['docker', 'external']
    if os.name == "posix":
        slash_str = "/"
    else:
        slash_str = "\\"
    default_locale = "en"
    default_locale = locale.getlocale()[0][:2]

    translations = get_translations(default_locale)


    layout = [
        [sg.Text('QGIS-xtopo', font='Any 20')] +
        [sg.Text('                                                                                                                            ')] +
        [sg.Column(
            [
                    [sg.Button('RU', key="button_ru", visible=True)] +
                    [sg.Button('EN', key="button_en", visible=False)]
            ]
        ,justification='left')
        ],
        [sg.Text(translations.get('qgis_projects_folder', 'QGIS projects folder'), key="qgis_projects_folder_text",
                 size=(20, 1), justification='l', tooltip=translations.get('qgis_projects_folder_tooltip', 'Directory with your projects')),
         sg.Input(key='qgis_projects_dir', default_text=str(str(os.path.expanduser('~'))) + slash_str + "qgis_projects",
                  size=(60, 1), tooltip=translations.get('qgis_projects_folder_tooltip', 'Directory with your projects'), change_submits=True)] +
        [sg.FolderBrowse(button_text=translations.get('browse', 'Browse'), key='qgis_projects_dir_browse', tooltip=translations.get('qgis_projects_folder_browse_tooltip', 'Select projects directory'))],
        [sg.Text(translations.get('project_name', 'Project name'), key='project_name_text', size=(20, 1), justification='l', tooltip=translations.get('project_name_tooltip', 'Enter project name')),
         sg.Input(default_text='automap', key='project_name', size=(60, 1),
                  tooltip=translations.get('project_name_tooltip', 'Enter project name'))],
        [sg.Text(translations.get('bounding_box', 'Bounding box'), key='bbox_text', size=(20, 1), justification='l', tooltip=translations.get('bounding_box_tooltip', 'Bounding box or OSM url')),
         sg.Input(default_text='', key='bbox', size=(60, 1), tooltip=translations.get('bounding_box_tooltip', 'Bounding box or OSM url'))] +
        [sg.Button(key="open_osm", image_filename="docs/osm_logo.png", size=(32, 32),
                   tooltip=translations.get('open_osm_tooltip', 'Press button, find a location and copy URL. Then paste it to text field to the left.'))
         ] +
        [sg.Button(key="open_klokantech", image_filename="docs/boundingbox_logo.png", size=(32, 32),
                   tooltip=translations.get('open_klokantech_tooltip', 'Draw a rectangle and copy bounding box in CSV format. Then paste it to text field to the left.'))
         ]
        ,
        [sg.Text(translations.get('terrain_src_dir', 'Terrain source folder'), key='terrain_src_dir_text', size=(20, 1), justification='l', tooltip=translations.get('terrain_src_dir_tooltip', 'Directory with world/continent terrain')),
         sg.Input(default_text='', key='terrain_src_dir', size=(60, 1), tooltip=translations.get('terrain_src_dir_tooltip', 'Directory with world/continent terrain'))] +
        [sg.FolderBrowse(button_text=translations.get('browse', 'Browse'), key='terrain_src_dir_browse', tooltip=translations.get('terrain_src_dir_browse_tooltip', 'Select directory with world/continent terrain'))],
        [sg.Text(translations.get('overpass_instance', 'Overpass instance'), key='overpass_instance_text', size=(22, 1), justification='l',
                 tooltip=translations.get('overpass_instance_tooltip', 'Select the type of server for receiving OSM data'))] +
        [sg.Radio('docker', "RADIO1", key=r_keys[0], default=True, size=(5, 1)),
         sg.Radio('external', "RADIO1", key=r_keys[1])],
        [sg.Text(translations.get('download_terrain_tiles', 'Download terrain data'), key='download_terrain_tiles_text', size=(22, 1), justification='l',
                 tooltip=translations.get('download_terrain_tiles_tooltip', 'Automatically download SRTM30m terrain data for chosen area'))] +
        [sg.Checkbox('', key="download_terrain_tiles", size=(10, 1), default=False, change_submits=True)]
    ]
    layout += [[sg.Text(translations.get('constructed_command_line', 'Command line') + ":", key='constructed_command_line_text')],
               [sg.Text(size=(93, 6), key='command_line', text_color='yellow', font='Courier 8')]+
               [sg.Button(button_text=translations.get('copy', 'Copy'), key="copy_command_line", tooltip=translations.get('copy_command_line_tooltip', 'Copy command line to clipboard'))
                ],
               [sg.MLine(size=(90, 10), reroute_stdout=True, reroute_stderr=True, reroute_cprint=True, write_only=True,
                         font='Courier 8', autoscroll=True, key='-ML-')],
               [sg.Frame(layout=[
                   [sg.Text(translations.get('sequential_launch', 'Sequential launch'), key='sequential_launch_text', size=(14, 1), justification='l',
                            tooltip=translations.get('sequential_launch_tooltip', 'Automatically launch data preparation steps and QGIS'))] +
                   [sg.Checkbox('', key="run_chain", size=(1, 1), default=True)] +
                   [sg.Button(translations.get('start', 'Start'), key="start", tooltip=translations.get('start_tooltip', 'Initialize project')),
                    sg.Button(translations.get('populate_db', 'Populate DB'), key="populate_db", tooltip=translations.get('populate_db_tooltip', 'Populate Overpass database (populate_db)'), disabled=True),
                    sg.Button(translations.get('prepare_data', 'Prepare data'), key="prepare_data", tooltip=translations.get('prepare_data_tooltip', 'Prepare data for project (prepare_data)'), disabled=True),
                    sg.Button(translations.get('open_qgis', 'Open QGIS'), key="open_qgis", tooltip=translations.get('open_qgis_tooltip', 'Open QGIS with your project (exec_qgis)'), disabled=True),
                    sg.Button(translations.get('exit', 'Exit'), key="exit")]], title='', element_justification="left", border_width=0,
                   relief=sg.RELIEF_SUNKEN)],
               ]
    window = sg.Window('QGIS-xtopo', layout, finalize=True, icon='docs/logo_icon.png')

    if default_locale == 'ru':
        window.Elem('button_ru').update(visible=False)
        window.Elem('button_en').update(visible=True)
    if default_locale == 'en':
        window.Elem('button_ru').update(visible=True)
        window.Elem('button_en').update(visible=False)

    timer_running, counter = True, 0

    while True:
        event, values = window.read(timeout=1000)
        if event in (sg.WIN_CLOSED, 'exit'):
            break

        if timer_running:
            parameters_tuple = (
                values["project_name"], values["bbox"], values["download_terrain_tiles"], values["run_chain"],
                values["qgis_projects_dir"], values["terrain_src_dir"])
            params = compose_params(parameters_tuple, r_keys, values)
            command = command_to_run + params
            window['command_line'].update(command)
            if "qgis-xtopo" in str(subprocess.check_output("docker container ls", shell=True)):
                if ([key for key in r_keys if values[key]][0]) == 'docker':
                    window.Elem('populate_db').update(disabled=False)
                elif ([key for key in r_keys if values[key]][0]) == 'external':
                    window.Elem('populate_db').update(disabled=True)
                window.Elem('prepare_data').update(disabled=False)
                window.Elem('open_qgis').update(disabled=False)
            else:
                window.Elem('populate_db').update(disabled=True)
                window.Elem('prepare_data').update(disabled=True)
                window.Elem('open_qgis').update(disabled=True)

        if event == 'button_ru':
            translations = get_translations("ru")
            update_layout_translations(translations, window)
            window.Elem('button_ru').update(visible=False)
            window.Elem('button_en').update(visible=True)
        if event == 'button_en':
            translations = get_translations("en")
            update_layout_translations(translations, window)
            window.Elem('button_ru').update(visible=True)
            window.Elem('button_en').update(visible=False)
        if event == 'qgis_projects_dir':
            config = values["qgis_projects_dir"] + slash_str + "qgisxtopo-config" + slash_str + "config.ini"
            if os.path.isfile(config):
                print("Reading config from " + config)
                window.Elem('bbox').update(get_setting(config, "bbox"))
                window.Elem('project_name').update(get_setting(config, "project_name"))
                overpass_instance_config = get_setting(config, "overpass_instance")
                if overpass_instance_config == "docker":
                    window.Elem('docker').update(value=True)
                    window.Elem('external').update(value=False)
                if overpass_instance_config == "external":
                    window.Elem('external').update(value=True)
                    window.Elem('docker').update(value=False)
                download_terrain_tiles_config = get_setting(config, "download_terrain_tiles")
                if download_terrain_tiles_config == "true":
                    window.Elem('download_terrain_tiles').update(value=True)
                else:
                    window.Elem('download_terrain_tiles').update(value=False)
        if event == 'open_osm':
            webbrowser.open(r'https://www.openstreetmap.org')
        if event == 'open_klokantech':
            webbrowser.open(r'https://boundingbox.klokantech.com')
        if event == 'download_terrain_tiles':
            if values["download_terrain_tiles"]:
                window.Elem('terrain_src_dir').update(value='',disabled=True)
                window.Elem('terrain_src_dir_browse').update(disabled=True)
            else:
                window.Elem('terrain_src_dir').update(disabled=False)
                window.Elem('terrain_src_dir_browse').update(disabled=False)
        if event == 'populate_db':
            parameters_check_tuple = (
                values['qgis_projects_dir'], values['terrain_src_dir'], values["bbox"], values["project_name"])
            if check_parameters(parameters_check_tuple) is False:
                continue
            subprocess.Popen(['xfce4-terminal', '-H', '-e', 'bash docker_populate_db.sh'])
        if event == 'prepare_data':
            parameters_check_tuple = (
                values['qgis_projects_dir'], values['terrain_src_dir'], values["bbox"], values["project_name"])
            if check_parameters(parameters_check_tuple) is False:
                continue
            subprocess.Popen(['xfce4-terminal', '-H', '-e', 'bash docker_prepare_data.sh'])
        if event == 'open_qgis':
            subprocess.Popen(['./docker_exec_qgis.sh'])
        if event == 'start':
            parameters_check_tuple = (
                values['qgis_projects_dir'], values['terrain_src_dir'], values["bbox"], values["project_name"])
            if check_parameters(parameters_check_tuple) is False:
                continue
            if "qgis-xtopo" in str(subprocess.check_output("docker container ls", shell=True)):
                runCommand(cmd="docker stop qgis-xtopo", window=window)
            # docker run
            parameters_tuple = (
                values["project_name"], values["bbox"], values["download_terrain_tiles"], values["run_chain"],
                values["qgis_projects_dir"], values["terrain_src_dir"])
            params = compose_params(parameters_tuple, r_keys, values)
            command = command_to_run + params
            runCommand(cmd=command, window=window)
            if "qgis-xtopo" in str(subprocess.check_output("docker container ls", shell=True)):
                subprocess.Popen(
                    ['xfce4-terminal', '-H', '-e', 'docker exec --user user qgis-xtopo /app/init_docker.sh'])
            #                runCommand(cmd="docker exec -it --user user qgis-xtopo /app/init_docker.sh", window=window)

            #            subprocess.Popen(['xfce4-terminal', '-H', '-e', 'bash docker_exec_qgis.sh']).wait()

    window.close()


command_to_run = r'docker '


def update_layout_translations(translations, window):
    window.Elem('qgis_projects_folder_text').update(translations.get('qgis_projects_folder'))
    window.Elem('project_name_text').update(translations.get('project_name'))
    window.Elem('bbox_text').update(translations.get('bounding_box'))
    window.Elem('terrain_src_dir_text').update(translations.get('terrain_src_dir'))
    window.Elem('overpass_instance_text').update(translations.get('overpass_instance'))
    window.Elem('download_terrain_tiles_text').update(translations.get('download_terrain_tiles'))
    window.Elem('constructed_command_line_text').update(translations.get('constructed_command_line') + ":")
    window.Elem('copy_command_line').update(translations.get('copy'))
    window.Elem('sequential_launch_text').update(translations.get('sequential_launch'))
    window.Elem('start').update(text=translations.get('start'))
    window.Elem('populate_db').update(text=translations.get('populate_db'))
    window.Elem('prepare_data').update(text=translations.get('prepare_data'))
    window.Elem('open_qgis').update(text=translations.get('open_qgis'))
    window.Elem('exit').update(text=translations.get('exit'))


def get_setting(path, setting):
    with open(path) as f:
        for num, line in enumerate(f, 1):
            value = None
            if setting + "=" in line:
                line = line.strip()
                value = line[len(setting) + 1:].replace('"', "").replace("'", "").lower()
                break
    return str(value)


def compose_params(parameters, r_keys, values):
    project_name = parameters[0]
    bbox = parameters[1]
    download_terrain_tiles = parameters[2]
    run_chain = parameters[3]
    qgis_projects_dir = parameters[4]
    terrain_src_dir = parameters[5]

    params = 'run -dti --rm '
    if project_name:
        params += f"-e PROJECT_NAME_EXT={project_name} "
    if bbox:
        params += f'-e BBOX_STR="{bbox}" '
    params += f"-e OVERPASS_INSTANCE={([key for key in r_keys if values[key]][0])} "
    if download_terrain_tiles:
        params += f"-e download_terrain_tiles={str(download_terrain_tiles).lower()} "
    if run_chain:
        params += f"-e RUN_CHAIN={str(run_chain).lower()} "
    lang = locale.getlocale(locale.LC_CTYPE)
    params += f"-e DISPLAY "
    if os.name == "posix":
        params += f"-v /tmp/.X11-unix:/tmp/.X11-unix "
    params += f"-e LANG={lang[0]}.{lang[1]} "
    params += f"--name qgis-xtopo "
    if qgis_projects_dir:
        params += f"--mount type=bind,source={qgis_projects_dir},target=/mnt/qgis_projects "
    if terrain_src_dir:
        params += f"--mount type=bind,source={terrain_src_dir},target=/mnt/terrain "
    if os.path.isfile('config_debug.ini'):
        params += 'qgis-xtopo'
    else:
        params += 'xmd5a2/qgis-xtopo:latest'
    return params


def check_parameters(parameters):
    qgis_projects_dir = parameters[0]
    terrain_src_dir = parameters[1]
    bbox = parameters[2]
    project_name = parameters[3]
    if len(qgis_projects_dir) < 5:
        sg.Popup('Projects directory value is empty', title="Error")
        return False
    if not os.path.isdir(qgis_projects_dir):
        try:
            os.makedirs(qgis_projects_dir, exist_ok=True)
        except OSError:
            sg.Popup("Directory " + qgis_projects_dir + " can not be created")
            return False
    if len(project_name) == 0:
        sg.Popup('Project name is empty', title="Error")
        return False
    if len(terrain_src_dir) != 0:
        if not os.path.isdir(terrain_src_dir):
            sg.Popup('Terrain source folder does not exist', title="Error")
            return False
    if len(bbox) == 0:
        sg.Popup('Bounding box is empty', title="Error")
        return False
    if len(bbox) >= 7:
        if "openstreetmap" not in bbox:
            if check_bbox(bbox) is False:
                throw_bbox_error()
                return False
    else:
        throw_bbox_error()
        return False
    return True


def num(s):
    try:
        return float(s)
    except ValueError:
        return False


def check_bbox(bbox_str):
    bbox_list = bbox_str.replace(" ", "").split(',')
    if len(bbox_list) != 4:
        return False
    else:
        lon_min = bbox_list[0]  # (W) left
        lat_min = bbox_list[1]  # (S) bottom
        lon_max = bbox_list[2]  # (E) right
        lat_max = bbox_list[3]  # (N) top
        if num(lon_min) > num(lon_max) or num(lat_min) > num(lat_max) or num(lat_max) >= 90 or num(lat_min) <= -90 or \
                num(lon_min) <= -180 or num(lon_max) >= 180:
            return False
    return True


def throw_bbox_error():
    sg.Popup("Use openstreetmap.org link or comma separated left bottom right top", title="Invalid bbox format")


def runCommand(cmd, timeout=None, window=None):
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = ''
    for line in p.stdout:
        line = line.decode(errors='replace' if (sys.version_info) < (3, 5) else 'backslashreplace').rstrip()
        output += line
        print(line)
        window.refresh() if window else None

    retval = p.wait(timeout)
    return (retval, output)


def get_translations(lang):
    path = 'translations_' + lang + '.txt'
    if not os.path.exists(path):
        create_config(path)
    config = configparser.ConfigParser(delimiters='=', )
    config.read(path)
    translations = {k: v for k, v in config.items('main')}
    return translations


# def get_translation(translation_en, lang):
#     config_path = 'translations_' + lang + '.txt'
#     config_translations = get_config(config_path)
#     value = config_translations.get("main", translation_en)
#     return value


if __name__ == '__main__':
    sg.theme('DarkGreen6')
    main()
