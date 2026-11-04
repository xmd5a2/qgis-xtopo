#!/usr/bin/env bash
#
# Сборка статического бинарника osmium-tool для Linux x86_64.
#
# Что делает скрипт (без root, без sudo, без apt install):
#   1. apt-get download — скачивает .deb-пакеты с зависимостями в $WORK_DIR/debs;
#   2. dpkg-deb -x      — распаковывает их в самодельный sysroot ($WORK_DIR/sysroot);
#   3. pip (venv)       — ставит cmake и ninja в $WORK_DIR/venv;
#   4. собирает osmium-tool 1.19.0 + libosmium 2.20.0 со статической линковкой;
#   5. кладёт готовый бинарник в vendor/osmium/linux-x86_64/osmium.
#
# Пакеты взяты из Ubuntu 24.04 (noble) — сборка рассчитана именно на него.
#
# Использование:
#   ./vendor/osmium/build-linux.sh              # сборка в /tmp, результат в vendor/
#   WORK_DIR=/tmp/mydir ./vendor/osmium/build-linux.sh
#
set -euo pipefail

OSMIUM_TOOL_VERSION="1.19.0"
LIBOSMIUM_VERSION="2.20.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEST_DIR="${DEST_DIR:-${SCRIPT_DIR}/linux-x86_64}"
WORK_DIR="${WORK_DIR:-/tmp/osmium-tool-build}"
JOBS="${JOBS:-$(nproc)}"

SYSROOT="${WORK_DIR}/sysroot"
VENV="${WORK_DIR}/venv"
CMAKE="${VENV}/bin/cmake"

# Пакеты: заголовки boost/protozero/nlohmann + статические .a (zlib, expat,
# bz2, lz4) и статический boost::program_options.
DEBS=(
    "libboost1.83-dev"
    "libboost-program-options1.83-dev"
    "libprotozero-dev"
    "nlohmann-json3-dev"
    "liblz4-dev"
    "zlib1g-dev"
    "libexpat1-dev"
    "libbz2-dev"
)

echo "==> Рабочий каталог: ${WORK_DIR}"
mkdir -p "${WORK_DIR}/debs" "${SYSROOT}"

echo "==> Скачиваем .deb-пакеты"
cd "${WORK_DIR}/debs"
apt-get download "${DEBS[@]}"

echo "==> Распаковываем пакеты в sysroot"
for deb in ./*.deb; do
    dpkg-deb -x "${deb}" "${SYSROOT}"
done

echo "==> Ставим cmake и ninja в venv"
if [ ! -x "${CMAKE}" ]; then
    python3 -m venv "${VENV}"
    # cmake < 4: libosmium 2.20 объявляет cmake_minimum_required(VERSION 2.8.12),
    # а CMake 4 полностью отказался от поддержки версий старше 3.5.
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet "cmake==3.31.6" ninja
fi

echo "==> Скачиваем исходники"
cd "${WORK_DIR}"
OSMIUM_TARBALL="osmium-tool-${OSMIUM_TOOL_VERSION}.tar.gz"
LIBOSMIUM_TARBALL="libosmium-${LIBOSMIUM_VERSION}.tar.gz"

if [ ! -f "${OSMIUM_TARBALL}" ]; then
    curl -fsSL -o "${OSMIUM_TARBALL}" \
        "https://github.com/osmcode/osmium-tool/archive/refs/tags/v${OSMIUM_TOOL_VERSION}.tar.gz"
fi
if [ ! -f "${LIBOSMIUM_TARBALL}" ]; then
    curl -fsSL -o "${LIBOSMIUM_TARBALL}" \
        "https://github.com/osmcode/libosmium/archive/refs/tags/v${LIBOSMIUM_VERSION}.tar.gz"
fi

rm -rf "osmium-tool-${OSMIUM_TOOL_VERSION}" "libosmium-${LIBOSMIUM_VERSION}"
tar xzf "${OSMIUM_TARBALL}"
tar xzf "${LIBOSMIUM_TARBALL}"

echo "==> Конфигурируем osmium-tool"
rm -rf "${WORK_DIR}/build"
"${CMAKE}" -S "osmium-tool-${OSMIUM_TOOL_VERSION}" -B "${WORK_DIR}/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_TESTING=OFF \
    -DCMAKE_PREFIX_PATH="${SYSROOT}/usr" \
    -DBOOST_ROOT="${SYSROOT}/usr" \
    -DBoost_USE_STATIC_LIBS=ON \
    -DOSMIUM_INCLUDE_DIR="${WORK_DIR}/libosmium-${LIBOSMIUM_VERSION}/include" \
    -DPROTOZERO_INCLUDE_DIR="${SYSROOT}/usr/include" \
    -DCMAKE_FIND_LIBRARY_SUFFIXES=".a" \
    -DCMAKE_EXE_LINKER_FLAGS="-static"

echo "==> Собираем"
"${CMAKE}" --build "${WORK_DIR}/build" -j "${JOBS}"

echo "==> Устанавливаем бинарник"
mkdir -p "${DEST_DIR}"
install -m 755 "${WORK_DIR}/build/src/osmium" "${DEST_DIR}/osmium"
strip --strip-unneeded "${DEST_DIR}/osmium" || true

echo "==> Проверка"
file "${DEST_DIR}/osmium"
if ldd "${DEST_DIR}/osmium" 2>&1 | grep -qv "not a dynamic executable\|не является динамическим"; then
    echo "ОШИБКА: бинарник зависит от разделяемых библиотек:" >&2
    ldd "${DEST_DIR}/osmium" >&2
    exit 1
fi
"${DEST_DIR}/osmium" --version

echo "==> Готово: ${DEST_DIR}/osmium"
