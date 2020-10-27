QGIS-topo v0.1.20201016
================

Topographic rendering style for QGIS and data preparation scripts

QGIS-topo это набор инструментов, предназначенных для упрощения создания карт, пригодных для печати.
Проект состоит из двух частей:

1. Скрипты для подготовки данных для проекта QGIS из OpenStreetMap и данных рельефа, локальный Overpass сервер
2. Проект QGIS с топографическим картостилем

В docker-контейнере запускаются скрипты для подготовки данных. Также возможно запустить QGIS прямо из контейнера. Скрипты получают данные OSM через [Overpass API](https://wiki.openstreetmap.org/wiki/RU:Overpass_API). Для удобства возможно использовать Overpass сервер внутри docker контейнера.

Процесс получения карты состоит из следующих шагов:
1. Установить docker
2. Получить образ docker с dockerhub
   ```
   docker pull xmd5a2/qgis-topo:latest
   ```
   В текущем каталоге будет создан каталог **qgis-topo**, откуда необходимо запускать все последующие скрипты, отмеченные *курсивом*.
3. Инициализация (2 варианта)
   1. Через *docker_run*:
      1. Настроить пути к каталогу проекта **qgis_projects_dir** и к источнику данных рельефа **terrain_src_dir** (второе опционально) в скрипте docker_run
      2. Запустить начальную инициализацию проекта через *docker_run*.
   2. Вручную:
   ```
   docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
   --mount type=bind,source=*/путь/к/каталогу/с/проектами*,target=/mnt/qgis_projects \
   --mount type=bind,source=*/путь/к/каталогу/с/данными/рельефа*,target=/mnt/terrain \
   xmd5a2/qgis-topo:latest
   docker exec -it --user user qgis-topo /app/init_docker.sh
   ```
   Если не существует каталог **qgis_projects_dir**, указанный в предыдущем шаге, то он будет создан.
5. В файле **qgis_projects_dir/qgistopo-config/config.ini** указать имя проекта **project_name**
6. Повторно запустить *docker_run* для инициализации каталога конкретного проекта **qgis_projects_dir/project_name**, указанного в предыдущем шаге.
7. Подготовка к созданию проекта
   1. Если будет использоваться сервер Overpass в docker контейнере (рекомендуемый способ):
      1. Получить экстракт данных OSM. Поддерживаются форматы pbf, o5m, osm.bz2, osm.
         1. Через [Protomaps](https://protomaps.com/) (рекомендуемый способ)
         2. Через [download.geofabrik.de](https://download.geofabrik.de/)
         3. Через JOSM
      2. Заполнить локальную базу данных Overpass через *docker_populate_db*
   2. Получить данные рельефа (опционально)
      1. В случае отсутствия полных данных о рельефе возможно узнать имена необходимых тайлов рельефа через *docker_query_srtm_tiles_list*. Затем вручную переписать их в каталог **qgis_projects_dir/project_name/input_terrain**
   4. Настроить параметры для получения данных в файле **qgis_projects_dir/qgistopo-config/config.ini**.
      1. **project_name**: имя каталога проекта. Не должно содержать пробелов. Уже должно быть настроено в шаге 5. **(обязательно!)**
      2. **bbox**: границы зоны охвата в формате **lon_min,lat_min,lon_max,lat_max** **(обязательно!)**
      3. **array_queries**:
8.
