FROM ubuntu:20.04 AS builder

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends --no-install-suggests -y \
        autoconf \
        automake \
        bash \
        bzip2 \
        ca-certificates \
        curl \
        expat \
        fcgiwrap \
        g++ \
        libexpat1-dev \
        liblz4-1 \
        liblz4-dev \
        libtool \
        m4 \
        make \
        osmium-tool \
        python3 \
        python3-venv \
        supervisor \
        wget \
        zlib1g \
        zlib1g-dev \
        osmctools \
        unzip

ADD http://dev.overpass-api.de/releases/osm-3s_v0.7.56.3.tar.gz /app/src.tar.gz

RUN mkdir -p /app/src \
    && cd /app/src \
    && tar -x -z --strip-components 1 -f ../src.tar.gz \
    && autoscan \
    && aclocal \
    && autoheader \
    && libtoolize \
    && automake --add-missing  \
    && autoconf \
    && CXXFLAGS='-O2' CFLAGS='-O2' ./configure --prefix=/app --enable-lz4 \
    && make -j $(grep -c ^processor /proc/cpuinfo) dist install clean \
    && mkdir -p /db/diffs /app/etc \
    && cp -r /app/src/rules /app/etc/rules \
    && rm -rf /app/src /app/src.tar.gz

FROM ubuntu:20.04

RUN adduser user --disabled-password

RUN apt-get update && \
    apt-get install --no-install-recommends --no-install-suggests -y \
    software-properties-common \
    gnupg \
    wget \
    && wget -qO - https://qgis.org/downloads/qgis-2020.gpg.key | gpg --no-default-keyring --keyring gnupg-ring:/etc/apt/trusted.gpg.d/qgis-archive.gpg --import \
    && chmod a+r /etc/apt/trusted.gpg.d/qgis-archive.gpg \
    && add-apt-repository "deb http://qgis.org/ubuntu focal main"

RUN apt-get install --no-install-recommends --no-install-suggests --fix-missing -y \
    make \
    curl \
    unzip \
    zip \
    lbzip2 \
    bc \
    jq \
    xvfb \
    python3 \
    nodejs \
    npm \
    osmium-tool \
    gdal-bin \
    python3-gdal \
    fonts-noto \
    fonts-roboto \
    fonts-wqy-microhei \
    ttf-dejavu \
    gsfonts \
    ttf-ubuntu-font-family \
    qgis \
    qgis-plugin-grass \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g osmtogeojson

RUN apt-get purge -y software-properties-common \
   gnupg \
   npm \
   && apt clean -y \
   && apt autoremove -y

RUN mkdir /app \
   && mkdir /app/icons/ \
   && mkdir /app/queries/ \
   && mkdir -p /app/QGIS3/profiles/default/python/expressions/ \
   && mkdir /app/osm-3s/

RUN chown user /app

COPY --from=builder /app /app/osm-3s/

COPY --from=builder /usr/bin/osmconvert /usr/bin/osmfilter /usr/bin/

COPY elevation-1.0.6.tar.gz /app/

RUN cd /app && tar -xvf elevation-1.0.6.tar.gz && cd elevation-1.0.6 && python3 setup.py install

COPY QGIS3/ /app/QGIS3/

COPY expressions/ /app/QGIS3/profiles/default/python/expressions/

COPY icons/ /app/icons/

COPY queries/ /app/queries/

COPY config.ini set_dirs.ini automap.qgz init_docker.sh prepare_data.sh populate_db.sh exec_qgis.sh get_bbox.sh README.md \
	run_alg.py replace_bbox_xml.py process_bbox.py calc_srtm_tiles_list.py update_config.py /app/

RUN cd /app && unzip -j automap.qgz && rm automap.qgd

RUN chmod +x /app/*.sh

# Disable deprecation warnings in utils.py
RUN sed -i "s/warnings.simplefilter('default')/warnings.simplefilter('ignore')/g" /usr/lib/python3/dist-packages/qgis/utils.py

USER user