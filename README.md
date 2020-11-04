QGIS-topo v0.1.20201027
================
![GitHub Logo](/docs/splash.png)

Topographic rendering style for QGIS and data preparation scripts

QGIS-topo это набор инструментов, предназначенных для создания карт, пригодных для печати. Цель проекта **не** в автоматическом создании готовых карт, а в попытке максимально упростить этот процесс.

### Системные требования

  Сильно зависят от объёма загружаемых данных. Для области размером 20х20 км в среднем будет достаточно ПК, удовлетворяющего минимальным требованиям.
  
  * Минимальные
     * Процессор: Intel Core-i5 / AMD Ryzen 5
     * Оперативная память: 8 Гб
     * Свободное место на диске: 5 Гб
   
  * Рекомендуемые
     * Процессор: Intel Core-i7 / AMD Ryzen 7
     * Оперативная память: 16+ Гб
     * Свободное место на диске: 20+ Гб

Проект состоит из двух частей:

1. Скрипты для подготовки данных для проекта ГИС [QGIS](https://qgis.org/ru/site/) из [OpenStreetMap](https://www.openstreetmap.org/#map=19/29.84546/-95.37745) и данных рельефа, локальный [Overpass API](https://wiki.openstreetmap.org/wiki/RU:Overpass_API) сервер.
2. Проект QGIS с топографическим картостилем.

Всё необходимое для работы находится в контейнере *docker*, размещённом на [DockerHub](https://hub.docker.com/repository/docker/xmd5a2/qgis-topo). В нём запускаются скрипты для подготовки данных. Также возможно запустить QGIS прямо из контейнера. Скрипты получают данные OSM через Overpass API. Рекомендуется использовать Overpass сервер, встроенный в *docker* контейнер.

### Содержание
   1. [Установка](https://github.com/xmd5a2/qgis-topo#1-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D0%B0)
   2. [Инициализация](https://github.com/xmd5a2/qgis-topo#2-%D0%B8%D0%BD%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F)
   3. [Подготовка данных](https://github.com/xmd5a2/qgis-topo#3-%D0%BF%D0%BE%D0%B4%D0%B3%D0%BE%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85)
   4. [Настройка](https://github.com/xmd5a2/qgis-topo#4-%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B0)
   5. [Получение и обработка данных OSM, обработка рельефа](https://github.com/xmd5a2/qgis-topo#5-%D0%BF%D0%BE%D0%BB%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-osm-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D1%80%D0%B5%D0%BB%D1%8C%D0%B5%D1%84%D0%B0)
   6. [Запуск QGIS](https://github.com/xmd5a2/qgis-topo#6-%D0%B7%D0%B0%D0%BF%D1%83%D1%81%D0%BA-qgis)
   7. [Работа с QGIS](https://github.com/xmd5a2/qgis-topo#7-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0-%D1%81-qgis)
   8. [Удаление QGIS-topo](https://github.com/xmd5a2/qgis-topo#8-%D1%83%D0%B4%D0%B0%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5-qgis-topo)

### 1. Установка
  1. [Установить docker](https://docs.docker.com/get-docker/)
     * Если у вас Linux то нужно настроить запуск *docker* из под обычного пользователя. Все дальнейшие действия **не требуют прав суперпользователя (root)**.
  2. _**Этот шаг нужен только для удобства запуска. Его можно пропустить если вы хотите запускать docker из командной строки**_. Требуется **wget**.

     ```
     mkdir -p qgis-topo&&a=(run prepare_data populate_db query_srtm_tiles_list exec_qgis clean)&&command -v wget >/dev/null 2>&1&&for f in "${a[@]}";do wget -nv -nc https://github.com/xmd5a2/qgis-topo/raw/master/docker_${f}.sh -P ./qgis-topo;done&&cd qgis-topo && chmod +x *.sh     
     ```
     В текущем каталоге будет создан каталог **qgis-topo**, откуда необходимо запускать все последующие скрипты, либо использовать команды `прямого запуска docker` (приведены в справке как альтернативный вариант).
     
### 2. Инициализация
   Для инициализации QGIS-topo необходимо:
   * **Каталог, в котором будут храниться данные проекта**. Его размер может достигать 10-20 Гб и более, в зависимости от размера обрабатываемой области, поэтому убедитесь что на устройстве достаточно свободного места. Перед следующим шагом вы должны создать этот каталог **самостоятельно**. Путь не должен содержать пробелов!
   * **(опционально) Каталог, в котором хранятся исходные данные рельефа (полный набор для всего мира)**. Если у вас уже скачаны эти данные то путь к этому каталогу необходимо указать при инициализации через `docker_run` ниже. Путь не должен содержать пробелов! Не уверены - пропускайте.
      
   Возможны два варианта. Вам необходимо выбрать один из них.
   * С помощью скрипта `docker_run` в каталоге **qgis-topo**, скачанном на шаге **[1.2](https://github.com/xmd5a2/qgis-topo#1-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D0%B0)**. Нужно указать минимум **2** параметра. Имена параметров (-n, -d) и их значения разделяются пробелами. Подставьте **имя_проекта** (без пробелов) и свой **путь_к_каталогу_с_проектами**.
   
        ```
        ./docker_run.sh -n имя_проекта -d путь_к_каталогу_с_проектами
        ```
      Поддерживаются необязательные параметры (их можно задать позже):
      * **границы зоны охвата (bbox)**: `-b lon_min,lat_min,lon_max,lat_max` (помощь по формированию рамки находится в разделе [**4**](https://github.com/xmd5a2/qgis-topo#4-%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B0))
      * Если вы хотите использовать **внешний Overpass сервер**, то добавьте в конец команды строку `-e true`
        Преимущества и недостатки такого использования рассмотрены в разделе [**3**](https://github.com/xmd5a2/qgis-topo#3-%D0%BF%D0%BE%D0%B4%D0%B3%D0%BE%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85).
      * Если вы собираетесь использовать **полный набор данных рельефа**, то нужно добавить в конец команды строку `-t путь_к_каталогу_с_данными_рельефа`
        Если каталог с проектами не существует то он будет создан.
        
        Пример:`./docker_run.sh -n automap -d ~/qgis_projects -b 21.8,36.8,22.4,37.2 -t ~/terrain -e true`
        
   или
   * Непосредственный запуск *docker*. Подставьте **имя_проекта** (без пробелов) и свои пути к каталогам. Если вы не используете **путь_к_каталогу_с_данными_рельефа** то **уберите** из следующей команды строчку `--mount type=bind,source=**путь_к_каталогу_с_данными_рельефа**,target=/mnt/terrain \`. Параметры ` -e BBOX_STR=**границы зоны охвата** -e OVERPASS_INSTANCE_EXTERNAL=**использовать_внешний_overpass(true)**` также опциональны.

     ```
     docker run -dti --rm -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix --name qgis-topo \
      -e PROJECT_NAME_EXT=**имя_проекта** -e BBOX_STR=**границы зоны охвата** -e OVERPASS_INSTANCE_EXTERNAL=**использовать_внешний_overpass(true)** \
      --mount type=bind,source=**путь_к_каталогу_с_проектами**,target=/mnt/qgis_projects \
      --mount type=bind,source=**путь_к_каталогу_с_данными_рельефа**,target=/mnt/terrain \
      xmd5a2/qgis-topo:latest
     docker exec -it --user user qgis-topo /app/init_docker.sh
     ```
  Скрипт создаст в каталоге с проектами каталог с указанным именем. В каталог c вашими проектами будет скопирован каталог **icons**, содержащий иконки для проекта QGIS. В подкаталог с именем вашего проекта будет скопирован проект QGIS (по умолчанию **automap.qgz**). `docker_run` не перезаписывает уже существующие данные, поэтому можно не опасаться что запуск команды что-то удалит. Поэтому если вы хотите **восстановить данные проекта по умолчанию**, то удалите их и выполните инструкции в разделе [**2**](https://github.com/xmd5a2/qgis-topo#2-%D0%B8%D0%BD%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F) снова.

### 3. Подготовка данных
   Поддерживается два основных варианта получения данных от сервера Overpass:
   1. С помощью сервера внутри *docker* контейнера **(рекомендуемый способ)**
   2. С помощью внешнего сервера

   Работа с **Overpass сервером, установленным внутри *docker***, является самым надёжным вариантом подготовки данных для QGIS-topo. Однако она требует ручного получения данных OSM и заполнения базы данных. Этот процесс может занимать существенное время.
   
   Существует возможность получения данных OSM с **внешних серверов Overpass**. Такой вариант не требует подготовки базы данных и может обеспечить быстрый старт. Подробная настройка такого варианта использования описана в разделе [**4**](https://github.com/xmd5a2/qgis-topo#4-%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B0) (переменные **overpass_instance=external** и **overpass_endpoint_external**).

   Если будет использоваться сервер Overpass внутри *docker* контейнера, то нужно заполнить его собственную базу данных данными, полученными из OSM. База данных хранится на вашем ПК в каталоге **путь_к_каталогу_с_проектами/overpass_db**. Она нужна только на время работы скрипта `prepare_data` в разделе [**5**](https://github.com/xmd5a2/qgis-topo#5-%D0%BF%D0%BE%D0%BB%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-osm-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D1%80%D0%B5%D0%BB%D1%8C%D0%B5%D1%84%D0%B0).
   1. Заполнить базу данных Overpass
      1. Скачать экстракт данных OSM. Чем обширнее область тем больше файл экстракта, что замедляет обработку данных. Поддерживаются форматы pbf, o5m, osm.bz2, osm.
         * Через [Protomaps](https://protomaps.com/extracts) **(рекомендуемый способ)**
           1. Выбрать интересующую область на карте с помощью кнопок **Rectangle Selection (Прямоугольное выделение)** либо **Polygon Selection (Полигональное выделение)**.
           2. Задать имя экстракта (поле **Name this area**) (опционально)
           3. После окончания подготовки данных сервером Protomaps нужно скачать их (**Download PBF**)
           
          или
         * Через [download.geofabrik.de](https://download.geofabrik.de/). Скачайте страну/область с наименьшим приемлемым охватом. Например: не стоит качать всю центральную Россию если вам нужна только Московская область.
         
          или
         * Через JOSM
      2. Поместить полученные файлы в каталог **путь_к_каталогу_с_проектами/имя_проекта/osm_data/**. Поддерживается несколько экстрактов одновременно.
      3. Заполнить базу данных Overpass в *docker*.
         * `./docker_populate_db.sh` **(рекомендуемый способ)**
         
         или
         * `docker exec -it --user user qgis-topo /app/populate_db.sh`
         
   2. Получить данные рельефа (опционально)
   
      Доступно два варианта использования данных:
      * Использовать фрагментарные данные (к примеру, несколько фрагментов нужной области). Их нужно поместить в каталог **путь_к_каталогу_с_проектами/имя_проекта/input_terrain/**.
        Некоторые из подходящих источников данных рельефа:
        * [CGIAR-CSI SRTM](http://srtm.csi.cgiar.org/srtmdata/) **(рекомендуется)**

        Если вы собираетесь использовать фрагменты данных рельефа, которые имеют имена, соответствующие общепринятым правилам наименования SRTM (например *N51E005.tif*), то для облегчения поиска данных возможно получить список необходимых тайлов для текущего проекта:
        ```
        ./docker_query_srtm_tiles_list.sh
        ```
        
          или
        ```
        docker exec -it --user user qgis-topo /app/query_srtm_tiles_list.sh
        ```
        предварительно задав в **config.ini** параметр **bbox** (раздел [**4**](https://github.com/xmd5a2/qgis-topo#4-%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B0)).

      или
      * Использовать полный набор тайлов для всего мира. Поддерживаются только тайлы размером 1х1 градус. Они должны иметь имена, соответствующие общепринятым правилам наименования SRTM. Например: *N51E005.tif*. Поддерживаются форматы GeoTIFF (tif) и HGT (hgt), а также zip архивы с файлами в этих форматах (один архив на файл). Данные должны находиться в каталоге с данными рельефа, который был задан в шаге [**2**](https://github.com/xmd5a2/qgis-topo#2-%D0%B8%D0%BD%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F) при запуске *docker* контейнера.
      
        Некоторые из подходящих источников данных рельефа:
        * [Рельеф Земли (составлено из SRTM 30м,90m, EU-DEM, LIDAR data, ALOS DEM, ArcticDEM, MERIT DEM) [2020]](https://rutracker.org/forum/viewtopic.php?t=5393970) (torrent)

  #### Если вы хотите использовать рельеф то не забудьте задать для опции **generate_terrain** значение **true** в **config.ini** (раздел [**4**](https://github.com/xmd5a2/qgis-topo#4-%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B0)).

### 4. Настройка
  Конфигурационный файл проекта называется **config.ini**. Он находится здесь: **путь_к_каталогу_с_проектами/qgistopo-config/config.ini**. В нём задаются параметры, которые используются в шаге [**5**](https://github.com/xmd5a2/qgis-topo#5-%D0%BF%D0%BE%D0%BB%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-osm-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D1%80%D0%B5%D0%BB%D1%8C%D0%B5%D1%84%D0%B0).
  
  Обычно требуется менять только переменную **bbox**. Но если вы хотите использовать рельеф (раздел [**3.2**](https://github.com/xmd5a2/qgis-topo#3-%D0%BF%D0%BE%D0%B4%D0%B3%D0%BE%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85)) то нужно для опции **generate_terrain** установить значение **true**.
  
   * **project_name**: имя каталога проекта. Не должно содержать пробелов. Задаётся в разделе [**2**](https://github.com/xmd5a2/qgis-topo#2-%D0%B8%D0%BD%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F) при запуске *docker* контейнера. Менять не требуется.
   * **bbox**: границы зоны охвата в формате **lon_min,lat_min,lon_max,lat_max**. Удобно получать **bbox** через https://boundingbox.klokantech.com/ . В левом нижнем углу страницы нужно выбрать формат **CSV**. **(обязательно!)**
   * **array_queries**: список запросов к Overpass, которые будут выполнены на шаге [**5**](https://github.com/xmd5a2/qgis-topo#5-%D0%BF%D0%BE%D0%BB%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-osm-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D1%80%D0%B5%D0%BB%D1%8C%D0%B5%D1%84%D0%B0). По умолчанию указаны все возможные запросы, но вы можете сократить этот список по своему усмотрению. Обратите внимание что некоторые запросы зависят от других. Эти зависимости описаны в **config.ini (Query dependencies)**. Обычно не требуется менять этот список.
   * **overpass_instance**: определяет какой сервер Overpass использовать **(обязательно!)**:
      * **docker**: внутри docker **(рекомендуется)**. Шаг [**3.1**](https://github.com/xmd5a2/qgis-topo#3-%D0%BF%D0%BE%D0%B4%D0%B3%D0%BE%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85) необходим только совместно с этой опцией.
      * **external**: находится в интернете и доступен по http(s)
        * **overpass_endpoint_external**: адрес сервера (в кавычках). Список доступных серверов здесь: [ссылка1](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances), [ссылка2](https://wiki.openstreetmap.org/wiki/RU:Overpass_API#.D0.92.D0.B2.D0.B5.D0.B4.D0.B5.D0.BD.D0.B8.D0.B5).
        Использование удалённого сервера является самым простым способом получить данные OSM. Однако из-за различных ограничений результат не всегда оказывается стабильным. Некоторые сервера не позволяют делать много запросов в течение короткого времени и блокируют вас. Можете попробовать следующие сервера:
          * "https://overpass.kumi.systems/api/interpreter"
      * **local**: установлен на локальной машине
        * **overpass_endpoint_local**: путь к файлу **osm3s_query**. Например: "/path/to/your/overpass/osm3s_query --db-dir=/path/to/overpass_db" (с кавычками)
      * **ssh**: через ssh. Например: "ssh user@server '/path/to/overpass/osm3s/bin/osm3s_query'" (с кавычками)
   * **generate_terrain=true/false**: обработка рельефа. Эта переменная влияет **на все вложенные переменные**, относящиеся к рельефу. Если она установлена в **false** то рельеф обрабатываться не будет.
      * **get_terrain_tiles=true/false**: использовать каталог с данными рельефа, который был задан в шаге [**2**](https://github.com/xmd5a2/qgis-topo#2-%D0%B8%D0%BD%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F). Если этот параметр установлен в **false** то будет использоваться каталог **путь_к_каталогу_с_проектами/имя_проекта/input_terrain**, куда вы самостоятельно можете скопировать данные рельефа для области проекта. Их имена могут быть произвольными. Поддерживаются форматы GeoTIFF (tif) и HGT (hgt), а также zip архивы с файлами в этих форматах.
      * **generate_terrain_hillshade_slope=true/false**: создавать карту затенения рельефа (отмывку) и карту уклонов
        * **terrain_resample_method=cubicspline/lanczos**: метод масштабирования исходных данных рельефа. **cubicspline** даёт менее детальную картинку без артефактов, а **lanczos** напротив, более детальную, но с артефактами в виде колец
      * **generate_terrain_isolines=true/false**: создавать изолинии высот (изогипсы)
        * **isolines_step=10/25/50/100/200**: шаг изолиний
        * **smooth_isolines=true/false**: сглаживание изолиний. Обычно не требуется.
   * **manual_coastline_processing=true/false**: ручная обработка береговой линии (**coastline**) мирового океана. Дело в том что автоматическая обработка занимает достаточно много времени. Тем больше, чем более детальна береговая линия. Иногда требуется получить карту большой области со сложным берегом и ждать времени нет. В таком случае вы можете включить эту опцию и обработать береговую линию вручную в JOSM. Рекомендуется отдельно запросить её, оставив в массиве **array_queries** только **coastline** и запустить скрипт `docker_prepare_data` (шаг [**5**](https://github.com/xmd5a2/qgis-topo#5-%D0%BF%D0%BE%D0%BB%D1%83%D1%87%D0%B5%D0%BD%D0%B8%D0%B5-%D0%B8-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-osm-%D0%BE%D0%B1%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%BA%D0%B0-%D1%80%D0%B5%D0%BB%D1%8C%D0%B5%D1%84%D0%B0)). В нужный момент скрипт остановит выполнение и попросит вручную достроить береговую линию до полигона, включающего в себя мировой океан в пределах заданного **bbox**, избегая пересечений и неправильной геометрии. Путь к файлу: **путь_к_каталогу_с_проектами/имя_проекта/vector/coastline.osm**. После этого следует в окне выполнения `docker_prepare_data` нажать любую клавишу.

### 5. Получение и обработка данных OSM, обработка рельефа
  Сделав все необходимые приготовления нужно запустить скрипт
  ```
  ./docker_prepare_data.sh
  ```
  
  или
  ```
  docker exec -it --user user qgis-topo /app/prepare_data.sh
  ```
  Он сгенерирует карту затенения рельефа, карту уклонов, изолинии высот. Также сделает необходимые запросы к серверу Overpass и обработает их с помощью алгоритмов QGIS и GRASS. Векторные данные (данные OSM и изолинии высот) находятся в каталоге **путь_к_каталогу_с_проектами/имя_проекта/vector**, а растровые (карта затенения рельефа, карта уклонов) в каталоге **путь_к_каталогу_с_проектами/имя_проекта/raster**

### 6. Запуск QGIS
  Полученный проект QGIS можно открыть двумя способами:
  * С помощью QGIS, установленной внутри *docker* контейнера (рекомендуется):
    * `./docker_exec_qgis.sh`
    
      или
    * `xhost +local:docker && docker exec -it --user user qgis-topo qgis`
    
      Далее в меню **Project - Open - /home/user/qgis_projects/имя_проекта/имя_проекта.qgz**
  * Локально установленной QGIS

### 7. Работа с QGIS

### 8. Удаление QGIS-topo
   1. Остановка контейнера *docker* и удаление образа
      ```
      ./docker_clean.sh
      ```
      
      или
      ```
      docker stop qgis-topo
      docker rmi xmd5a2/qgis-topo
       ```
      Каталог с проектами не удаляется автоматически во избежание потери данных. Если он вам больше не нужен то удалите его вручную.
      
   2. Удалите каталог со скриптами, скачанный в шаге [**1.2**](https://github.com/xmd5a2/qgis-topo#1-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D0%B0).
   3. Удалите *docker* средствами вашей ОС
