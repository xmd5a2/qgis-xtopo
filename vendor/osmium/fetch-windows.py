#!/usr/bin/env python3
"""Получение бинарника osmium-tool для Windows x86_64 из conda-forge.

Скрипт сам подбирает свежую совместимую сборку: смотрит индекс conda-forge,
берёт последнюю сборку osmium-tool для win-64 (не ниже 1.16) и ровно те
версии её зависимостей, которые требует эта сборка.  Поэтому обновление
на стороне conda-forge не ломает получение бинарника.

Результат кладётся в vendor/osmium/win-x86_64/:

    osmium.exe                 инструмент
    boost_program_options.dll  разбор параметров командной строки
    zlib.dll, libbz2.dll, libexpat.dll, lz4.dll   сжатие данных PBF

Использование:

    python3 vendor/osmium/fetch-windows.py --dest vendor/osmium/win-x86_64
    python3 vendor/osmium/fetch-windows.py --dest ... --update-pin
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

CHANNEL = "conda-forge"
API = "https://api.anaconda.org/package/{channel}/{name}"
DOWNLOAD = "https://conda.anaconda.org/{channel}/{basename}"

# Имя пакета conda-forge -> файл, который берём из него.
SOURCES = {
    "osmium-tool": "osmium.exe",
    "libboost": "boost_program_options.dll",
    "libzlib": "zlib.dll",
    "bzip2": "libbz2.dll",
    "libexpat": "libexpat.dll",
    "lz4-c": "lz4.dll",
}
REQUIRED_FILES = tuple(SOURCES.values())

# Ниже 1.16 нет опции types=any — с ней теряются неполные отношения.
MIN_VERSION = (1, 16, 0)

# Каталог, куда складываются скачанные пакеты conda.
CACHE = Path(os.environ.get("XTOPO_CONDA_CACHE", Path.home() / ".cache" / "xtopo-conda"))


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------
# Версии и ограничения


def version_key(text: str):
    """Ключ для сравнения версий: (major, minor, patch, pre).

    ``1.91.0a0`` — предварительный релиз, поэтому он меньше ``1.91.0``.
    """
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    if not match:
        return (0, 0, 0, 0)
    numbers = [int(part or 0) for part in match.groups()]
    pre = 0 if re.search(r"(a|b|rc)\d*$", text or "") else 1
    return (numbers[0], numbers[1], numbers[2], pre)


def satisfies(version: str, spec: str) -> bool:
    """Проверка ограничения вида ``>=1.90.0,<1.91.0a0``."""
    if not spec:
        return True
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        match = re.match(r"^(>=|<=|==|!=|>|<)?\s*(.+)$", item)
        if not match:
            continue
        operator = match.group(1) or "=="
        bound = version_key(match.group(2).strip())
        current = version_key(version)
        if operator == ">=" and not current >= bound:
            return False
        if operator == "<=" and not current <= bound:
            return False
        if operator == ">" and not current > bound:
            return False
        if operator == "<" and not current < bound:
            return False
        if operator == "==" and not current == bound:
            return False
        if operator == "!=" and current == bound:
            return False
    return True


def parse_depends(depends) -> dict:
    """``['libboost >=1.90.0,<1.91.0a0', ...]`` -> {имя: ограничение}."""
    result = {}
    for item in depends or []:
        parts = str(item).split(None, 1)
        result[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return result


# --------------------------------------------------------------------------
# Индекс conda-forge


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "qgis-xtopo/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def win64_builds(name: str):
    """Все сборки пакета для win-64, новые сверху."""
    data = fetch_json(API.format(channel=CHANNEL, name=name))
    builds = []
    for entry in data.get("files", []):
        attrs = entry.get("attrs") or {}
        if attrs.get("subdir") != "win-64":
            continue
        builds.append(
            {
                "name": name,
                "basename": entry.get("basename"),
                "version": attrs.get("version") or entry.get("version") or "",
                "build": attrs.get("build") or "",
                "number": attrs.get("build_number") or 0,
                "depends": attrs.get("depends") or [],
                "size": entry.get("size") or 0,
            }
        )
    builds.sort(
        key=lambda item: (version_key(item["version"]), item["number"], item["basename"]),
        reverse=True,
    )
    return builds


def pick(name: str, spec: str = ""):
    """Последняя сборка пакета, удовлетворяющая ограничению."""
    for build in win64_builds(name):
        if satisfies(build["version"], spec):
            return build
    return None


def resolve_packages():
    """Набор пакетов: osmium-tool и ровно те зависимости, что ему нужны."""
    osmium = None
    lowest = version_key(".".join(map(str, MIN_VERSION)))
    for build in win64_builds("osmium-tool"):
        key = version_key(build["version"])
        # Ветка 1.x: команды и опции osmium-tool проверены на ней.
        if lowest <= key < (2, 0, 0, 0):
            osmium = build
            break
    if osmium is None:
        raise SystemExit("Не найдена сборка osmium-tool для win-64 версии 1.16 и новее")

    constraints = parse_depends(osmium["depends"])
    packages = {"osmium-tool": osmium}
    for name in SOURCES:
        if name == "osmium-tool":
            continue
        build = pick(name, constraints.get(name, ""))
        if build is None:
            raise SystemExit(f"Не найдена сборка {name} для win-64 ({constraints.get(name, '')})")
        packages[name] = build
    return packages


# --------------------------------------------------------------------------
# Загрузка и распаковка


def download(basename: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / Path(basename).name
    if target.exists() and target.stat().st_size > 0:
        return target
    url = DOWNLOAD.format(channel=CHANNEL, basename=basename)
    log(f"  скачиваем {basename}")
    temporary = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as response, open(temporary, "wb") as out:
        shutil.copyfileobj(response, out)
    temporary.replace(target)
    return target


def extract_package(archive: Path, destination: Path) -> None:
    """Распаковка .conda (zip + tar.zst) или .tar.bz2 в каталог."""
    if zipfile.is_zipfile(archive):
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(temporary)
            inner = sorted(Path(temporary).glob("pkg-*.tar.zst"))
            if inner:
                extract_tar(inner[0], destination)
            else:
                inner = sorted(Path(temporary).glob("pkg-*.tar.bz2"))
                if not inner:
                    raise SystemExit(f"В пакете {archive.name} нет payload")
                extract_tar(inner[0], destination)
    else:
        extract_tar(archive, destination)


def extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zst"):
        # zstd в модуле tar есть только начиная с Python 3.14.
        subprocess.run(
            ["tar", "--zstd", "-xf", str(archive), "-C", str(destination)],
            check=True,
        )
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(destination)


# --------------------------------------------------------------------------
# Проверки


def pe_machine(path: Path) -> int:
    """Тип машины из заголовка PE: 0x8664 — x86-64."""
    with open(path, "rb") as handle:
        data = handle.read(1024)
        if data[:2] != b"MZ":
            return 0
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        handle.seek(offset)
        header = handle.read(6)
        if header[:4] != b"PE\0\0":
            return 0
        return struct.unpack_from("<H", header, 4)[0]


def run_version(executable: Path) -> str:
    """Запуск ``--version`` и чтение номера версии."""
    command = [str(executable), "--version"]
    if os.name != "nt" and shutil.which("wine"):
        command = ["wine", str(executable), "--version"]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    return result.stdout.decode("utf-8", "replace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Основной сценарий


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        default=str(Path(__file__).resolve().parent / "win-x86_64"),
        help="каталог для бинарника и библиотек",
    )
    parser.add_argument(
        "--update-pin",
        action="store_true",
        help="записать найденные версии и хеши в windows-build.json",
    )
    arguments = parser.parse_args()

    destination = Path(arguments.dest).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    log("==> Подбираем сборки conda-forge")
    packages = resolve_packages()
    for name, build in packages.items():
        log(f"  {name} {build['version']} ({build['basename']})")

    log("==> Скачиваем и распаковываем")
    with tempfile.TemporaryDirectory() as temporary:
        extracted = Path(temporary)
        for name, build in packages.items():
            archive = download(build["basename"], CACHE)
            target = extracted / name
            extract_package(archive, target)
            source = target / "Library" / "bin" / SOURCES[name]
            if not source.exists():
                source = target / SOURCES[name]
            if not source.exists():
                raise SystemExit(f"В пакете {build['basename']} нет файла {SOURCES[name]}")
            shutil.copy2(source, destination / SOURCES[name])

    missing = [name for name in REQUIRED_FILES if not (destination / name).exists()]
    if missing:
        raise SystemExit(f"Не получены файлы: {', '.join(missing)}")

    executable = destination / "osmium.exe"
    machine = pe_machine(executable)
    if machine != 0x8664:
        raise SystemExit(f"osmium.exe — не 64-разрядный PE (machine={machine:#x})")

    log("==> Проверяем запуск")
    text = run_version(executable)
    match = re.search(r"osmium version ([\d.]+)", text)
    if not match:
        raise SystemExit(f"Не удалось получить версию osmium:\n{text.strip()}")
    log(f"  osmium {match.group(1)}")

    log("==> Контрольные суммы")
    digests = {}
    for name in REQUIRED_FILES:
        digests[name] = sha256(destination / name)
        log(f"  {name}  {digests[name]}")

    record = {
        "channel": CHANNEL,
        "packages": {
            name: {"version": build["version"], "build": build["basename"]}
            for name, build in packages.items()
        },
        "sha256": digests,
    }
    pin = Path(__file__).resolve().parent / "windows-build.json"
    if pin.exists():
        previous = json.loads(pin.read_text(encoding="utf-8"))
        changed = [
            name
            for name in REQUIRED_FILES
            if previous.get("sha256", {}).get(name) != digests[name]
        ]
        if changed:
            log("==> Внимание: сборка в conda-forge обновилась, файлы изменились:")
            for name in changed:
                log(f"    {name}")
            log("    проверьте изменения и обновите vendor/osmium/windows-build.json")
    if arguments.update_pin:
        pin.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log(f"==> Записано: {pin}")
    log(f"==> Готово: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
