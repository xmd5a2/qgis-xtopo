QGIS-topo v0.1.20201027
================

Topographic rendering style for QGIS and data preparation scripts

QGIS-topo это набор инструментов, предназначенных для упрощения создания карт, пригодных для печати.
Проект состоит из двух частей:

1. Скрипты для подготовки данных для проекта ГИС QGIS из OpenStreetMap и данных рельефа, локальный Overpass сервер
2. Проект QGIS с топографическим картостилем

В docker-контейнере запускаются скрипты для подготовки данных. Также возможно запустить QGIS прямо из контейнера. Скрипты получают данные OSM через [Overpass API](https://wiki.openstreetmap.org/wiki/RU:Overpass_API). Для удобства возможно использовать Overpass сервер внутри docker контейнера.

### 1. Установка
  1. Установить [docker](https://docs.docker.com/get-docker/)
     * Если у вас Linux то нужно настроить запуск docker не из под root. Все дальнейшие действия не требуют прав суперпользователя.
  2. Скачать образ docker с dockerhub
   ```
   docker pull xmd5a2/qgis-topo:latest
   ```
  3. _**Этот шаг нужен только для удобства запуска. Его можно пропустить если вы хотите запускать docker из командной строки**_.

     Скачать [репозиторий qgis-topo с github](https://github.com/xmd5a2/qgis-topo/archive/master.zip) и распаковать.
     Либо клонировать репозиторий:
     ```
     git clone https://github.com/xmd5a2/qgis-topo
     ```
     В текущем каталоге будет создан каталог **qgis-topo**, откуда необходимо запускать все последующие скрипты, отмеченные `таким образом`. 
      
      _**{фигурными скобками} отмечены переменные, задаваемые пользователем**_
      
### 2. Инициализация
   1. Первичная инициализация (2 варианта)
      1. Через `docker_run`:
         1. Настроить пути к каталогу проекта **{qgis_projects_dir}** (обязательно) и к источнику данных рельефа **{terrain_src_dir}** (опционально) в скрипте `docker_run`. Пути не должны содержать пробелов!
         2. Запустить начальную инициализацию проекта через `docker_run`.
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
   3. Повторно запустить `docker_run` для инициализации каталога конкретного проекта **{qgis_projects_dir}/{project_name}/**, указанного в предыдущем шаге.
      * Либо выполнить
        ```
        docker exec -it --user user qgis-topo /app/init_docker.sh
        ``` 
        В каталог **{qgis_projects_dir}** будет скопирован каталог **icons**, содержащий иконки для проекта QGIS.
        В каталог проекта **{qgis_projects_dir}/{project_name}/** будет скопирован проект QGIS (по умолчанию **automap.qgz**)
        `docker_run` не перезаписывает уже существующие данные.
### 3. Подготовка данных
   1. Если будет использоваться сервер Overpass внутри docker контейнера (рекомендуемый способ):
      1. Получить экстракт данных OSM. Поддерживаются форматы pbf, o5m, osm.bz2, osm. Есть поддержка нескольких экстрактов одновременно. Варианты:
         * Через [Protomaps](https://protomaps.com/) (рекомендуемый способ)
         * Через [download.geofabrik.de](https://download.geofabrik.de/)
         * Через JOSM
      2. Поместить полученные файлы в каталог **{qgis_projects_dir}/{project_name}/osm_data/**
      3. Заполнить локальную базу данных Overpass. Варианты:
         * Выполнить `docker_populate_db`
         * Вручную
           ```
           docker exec -it --user user qgis-topo /app/populate_db.sh
           ```
   2. Получить данные рельефа (опционально)
      Доступно два варианта использования данных:
      * Скачать полный набор тайлов для всего мира. Поддерживаются только тайлы размером 1х1 градус. Они должны иметь имена, соответствующие общепринятым правилам наименования SRTM. Например: N51E005.tif. Поддерживаются форматы GeoTIFF (tif) и HGT (hgt), а также zip файлы с файлами этих форматов (один архив на файл). Рекомендуемый источник данных: [torrent](https://rutracker.org/forum/viewtopic.php?t=5393970). Данные должны находиться в каталоге **{terrain_src_dir}**, указанном в шаге 2.1.i.a при запуске docker контейнера.
      * В случае отсутствия полных данных о рельефе возможно получить список необходимых файлов через `docker_query_srtm_tiles_list`, либо вручную
        ```
        docker exec -it --user user qgis-topo /app/query_srtm_tiles_list.sh
        ```
        предварительно задав параметр **{bbox}** (шаг 4.1). Затем необходимо скачать и поместить их в каталог **{qgis_projects_dir}/{project_name}/input_terrain/**. Список источников указан выше.

### 4. Настройка **config.ini**
  **{qgis_projects_dir}/qgistopo-config/config.ini**
   * **{project_name}**: имя каталога проекта. Не должно содержать пробелов. Уже должно быть настроено в шаге 5 **(обязательно!)**
   * **{bbox}**: границы зоны охвата в формате **lon_min,lat_min,lon_max,lat_max**. Удобно получать **{bbox}** через https://boundingbox.klokantech.com/ **(обязательно!)**
   * **{array_queries}**: список запросов к Overpass, которые будут выполнены в шаге **ХХХ**. По умолчанию указаны все возможные запросы, но вы можете сократить этот список по своему усмотрению. Обратите внимание что некоторые запросы зависят от других. Эти зависимости описаны в **{qgis_projects_dir}/qgistopo-config/config.ini** (Query dependencies). Обычно не требуется менять этот список.
   * **{overpass_instance}**: определяет какой сервер Overpass использовать **(обязательно!)**:
      * **docker** : внутри docker (рекомендуется)
      * **external** : находится в интернете и доступен по http(s)
        * **{overpass_endpoint_external}**: адрес сервера (в кавычках). Список доступных серверов здесь: [ссылка1](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances), [ссылка2](https://wiki.openstreetmap.org/wiki/RU:Overpass_API#.D0.92.D0.B2.D0.B5.D0.B4.D0.B5.D0.BD.D0.B8.D0.B5). Некоторые сервера не позволяют делать много запросов в течение короткого времени и блокируют вас.
      * **local** : установлен на локальной машине
        * **{overpass_endpoint_local}**: путь к файлу **osm3s_query**. Например: "/path/to/your/overpass/osm3s_query --db-dir=/path/to/overpass_db" (с кавычками)
      * **ssh** : через ssh. Например: "ssh user@server '/path/to/overpass/osm3s/bin/osm3s_query'" (с кавычками)
   * **{generate_terrain}=true/false**: обработка рельефа
      * **{get_terrain_tiles}=true/false**: использовать каталог **{terrain_src_dir}** с данными рельефа. Каталог **{qgis_projects_dir}/{project_name}/input_terrain/** будет очищен.
      * **{generate_terrain_hillshade_slope}=true/false**: создавать карту затенения рельефа и карту уклонов
        * **{terrain_resample_method}=cubicspline/lanczos**: метод масштабирования исходных данных рельефа. **cubicspline** даёт менее детальную картинку, но без артефактов, а **lanczos** напротив, более детальную, но с артефактами в виде колец
      * **{generate_terrain_isolines}**=**true/false**: создавать изолинии высот (изогипсы)
        * **{isolines_step}=10/25/50/100/200**: шаг изолиний
        * **{smooth_isolines}=true/false**: сглаживание изолиний. Обычно не требуется.
      * **{manual_coastline_processing}=true/false**: ручная обработка береговой линии (**coastline**) мирового океана. Дело в том что автоматическая обработка занимает достаточно много времени. Тем больше, чем более детальна береговая линия. Иногда требуется получить карту большой области со сложным берегом и ждать времени нет. В таком случае вы можете включить эту опцию и обработать береговую линию вручную в JOSM. Рекомендуется отдельно запросить её, оставив в массиве **{array_queries}** только **coastline** и запустить `prepare_data` (шаг 5). В нужный момент скрипт остановит выполнение и попросит вручную достроить береговую линию до полигона, включающего в себя мировой океан в пределах заданного **bbox**, избегая пересечений и неправильной геометрии. Путь к файлу: **{qgis_projects_dir}/{project_name}/vector/coastline.osm**. После этого следует в окне выполнения `prepare_data` нажать любую клавишу.

### 5. Получение и обработка данных OSM, обработка рельефа
  Сделав все необходимые приготовления вы можете запустить скрипт
  ####    `prepare_data`
  Он сгенерирует карту затенения рельефа, карту уклонов, изолинии высот. Также сделает необходимые запросы к серверу Overpass и обработает их с помощью алгоритмов QGIS и GRASS. Векторные данные (данные OSM и изолинии высот) находятся в **{qgis_projects_dir}/{project_name}/vector**, а растровые (карта затенения рельефа, карта уклонов) в **{qgis_projects_dir}/{project_name}/raster**

### 6. Запуск QGIS
  Проект QGIS можно открыть двумя способами
  * С помощью QGIS, установленной внутри docker контейнера (рекомендуется)
    * `docker_exec_qgis`
    * Вручную: `xhost +local:docker && docker exec -it --user user qgis-topo qgis`
    
      Далее **Project - Open - /home/user/qgis_projects/{project_name}/{project_name}.qgz**
  * Локально установленной QGIS
