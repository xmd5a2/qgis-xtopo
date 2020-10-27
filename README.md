QGIS-topo v0.1.20201016
================

Topographic rendering style for QGIS and data preparation scripts

QGIS-topo это набор инструментов, предназначенных для упрощения создания карт, пригодных для печати.
Проект состоит из двух частей:

1. Скрипты для подготовки данных для проекта QGIS из OpenStreetMap и данных рельефа, локальный Overpass сервер
2. Проект QGIS с топографическим картостилем

В docker-контейнере запускаются скрипты для подготовки данных. Также возможно запустить QGIS прямо из контейнера. Скрипты получают данные OSM через [Overpass API](https://wiki.openstreetmap.org/wiki/RU:Overpass_API). Для удобства возможно использовать Overpass сервер внутри docker контейнера.

## Процесс получения карты состоит из следующих шагов:
### 1. Установка
  1. Установить docker
  2. Получить образ docker с dockerhub
   ```
   docker pull xmd5a2/qgis-topo:latest
   ```
  3. _**Этот шаг нужен только для удобства запуска. Его можно пропустить если вы хотите запускать docker из командной строки**_.

     Скачать [репозиторий qgis-topo с github одним файлом](https://github.com/xmd5a2/qgis-topo/archive/master.zip) и распаковать.
     Либо клонировать репозиторий:
     ```
     git clone https://github.com/xmd5a2/qgis-topo
     ```
     В текущем каталоге будет создан каталог **qgis-topo**, откуда необходимо запускать все последующие скрипты, отмеченные *курсивом*. 
      
      _**{фигурными скобками} отмечены переменные, задаваемые пользователем**_
      
### 2. Инициализация
   1. Первичная инициализация (2 варианта)
      1. Через *docker_run*:
         1. Настроить пути к каталогу проекта **{qgis_projects_dir}** (обязательно) и к источнику данных рельефа **{terrain_src_dir}** (опционально) в скрипте *docker_run*
         2. Запустить начальную инициализацию проекта через *docker_run*.
         Если не существует каталог **{qgis_projects_dir}**, указанный в шаге **a**, то он будет создан.
      2. Вручную. Каталоги должны существовать.
         ```
         docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
         --mount type=bind,source=*/путь/к/каталогу/с/проектами*,target=/mnt/qgis_projects \
         --mount type=bind,source=*/путь/к/каталогу/с/данными/рельефа*,target=/mnt/terrain \
         xmd5a2/qgis-topo:latest
         docker exec -it --user user qgis-topo /app/init_docker.sh
         ```
   2. В файле **{qgis_projects_dir}/qgistopo-config/config.ini** указать имя проекта **{project_name}**
   3. Повторно запустить *docker_run* для инициализации каталога конкретного проекта **{qgis_projects_dir}/{project_name}/**, указанного в предыдущем шаге.
      * Либо выполнить
        ```
        docker exec -it --user user qgis-topo /app/init_docker.sh
        ``` 
### 3. Подготовка данных
   1. Если будет использоваться сервер Overpass внутри docker контейнера (рекомендуемый способ):
      1. Получить экстракт данных OSM. Поддерживаются форматы pbf, o5m, osm.bz2, osm. Варианты:
         * Через [Protomaps](https://protomaps.com/) (рекомендуемый способ)
         * Через [download.geofabrik.de](https://download.geofabrik.de/)
         * Через JOSM
      2. Поместить полученные файлы в каталог **{qgis_projects_dir}/{project_name}/osm_data/**
      3. Заполнить локальную базу данных Overpass. Варианты:
         * Выполнить *docker_populate_db*
         * Вручную
           ```
           docker exec -it --user user qgis-topo /app/populate_db.sh
           ```
   2. Получить данные рельефа (опционально)
      Доступно два варианта использования данных:
      * Скачать полный набор тайлов для всего мира. Поддерживаются только тайлы размером 1х1 градус. Они должны иметь имена, соответствующие общепринятым правилам наименования SRTM. Например: N51E005.tif. Поддерживаются форматы GeoTIFF (tif) и HGT (hgt), а также zip файлы с файлами этих форматов (один архив на файл). Рекомендуемый источник данных: [torrent](https://rutracker.org/forum/viewtopic.php?t=5393970). Данные должны находиться в каталоге **{terrain_src_dir}**, указанном в шаге 2.1.i.a при запуске docker контейнера.
      * В случае отсутствия полных данных о рельефе возможно получить список необходимых файлов через *docker_query_srtm_tiles_list*, либо вручную
        ```
        docker exec -it --user user qgis-topo /app/query_srtm_tiles_list.sh
        ```
        предварительно задав параметр **{bbox}** (шаг 4.1). Затем необходимо скачать и поместить их в каталог **{qgis_projects_dir}/{project_name}/input_terrain/**. Список источников указан выше.

### 4. Настройка config.ini
   1. Настроить параметры для получения данных в файле **{qgis_projects_dir}/qgistopo-config/config.ini**.
         * **{project_name}**: имя каталога проекта. Не должно содержать пробелов. Уже должно быть настроено в шаге 5 **(обязательно!)**
         * **{bbox}**: границы зоны охвата в формате **lon_min,lat_min,lon_max,lat_max**. Удобно получать **{bbox}** через https://boundingbox.klokantech.com/ **(обязательно!)**
         * **{array_queries}**: список запросов к Overpass, которые будут выполнены в шаге **ХХХ**. По умолчанию указаны все возможные запросы, но вы можете сократить этот список по своему усмотрению. Обратите внимание что некоторые запросы зависят от других. Эти зависимости описаны в **{qgis_projects_dir}/qgistopo-config/config.ini** (Query dependencies). Обычно не требуется менять этот список.
         * **{overpass_instance}**: определяет какой сервер Overpass использовать:
            * *docker* : внутри docker (рекомендуется)
            * *external* : находится в интернете и доступен по http
              * **{overpass_endpoint_external}**: адрес сервера. Список доступных серверов здесь: [ссылка1](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances), [ссылка2](https://wiki.openstreetmap.org/wiki/RU:Overpass_API#.D0.92.D0.B2.D0.B5.D0.B4.D0.B5.D0.BD.D0.B8.D0.B5). Некоторые сервера не позволяют делать много запросов в течение короткого времени и блокируют вас.
            * *local* : установлен на локальной машине
              * **{overpass_endpoint_local}**: путь к файлу **osm3s_query**. Например: "/path/to/your/overpass/osm3s_query --db-dir=/path/to/overpass_db" (с кавычками)
            * *ssh* : через ssh. Например: "ssh user@server '/path/to/overpass/osm3s/bin/osm3s_query'" (с кавычками)
         * **{generate_terrain}**:
