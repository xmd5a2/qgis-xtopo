"""Поиск и запуск osmium-tool.

Бинарник ищется сначала в системном PATH, затем в поставке плагина
(``vendor/osmium/<платформа>-<архитектура>/osmium``).  Он нужен, чтобы
вырезать нужную область из большой выгрузки OSM до чтения её драйвером
GDAL: чтение карты целиком занимает минуты, вырезка — секунды.

Версия 1.16 и новее обязательна: в ней появилась опция стратегии
``types=any`` (в отличие от более старых версий она не отбрасывает
неполные отношения, нужные для мультиполигонов).
"""

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

MIN_VERSION = (1, 16, 0)

# Время ожидания одной команды osmium, секунды.
DEFAULT_TIMEOUT = 1800

_cache = {}


class OsmiumError(RuntimeError):
    pass


def platform_tag():
    """Имя платформы для имени каталога в поставке: linux-x86_64."""
    system = sys.platform
    if system.startswith("linux"):
        system = "linux"
    elif system.startswith("win"):
        system = "win"
    elif system == "darwin":
        system = "darwin"
    else:
        system = system.replace(".", "-")
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        machine = "x86_64"
    elif machine in ("arm64", "aarch64"):
        machine = "aarch64"
    elif machine.startswith("arm"):
        machine = "armv7l"
    return f"{system}-{machine}"


def vendor_root():
    """Каталог поставки osmium внутри пакета плагина."""
    return Path(__file__).resolve().parent.parent / "vendor" / "osmium"


def bundled_binary():
    """Путь к бинарнику из поставки плагина (может не существовать)."""
    suffix = ".exe" if sys.platform.startswith("win") else ""
    candidate = vendor_root() / platform_tag() / f"osmium{suffix}"
    return candidate if candidate.exists() else None


def ensure_executable(path, temp_dir=None):
    """Делает бинарник запускаемым и возвращает путь к нему.

    При сборке дистрибутива бит исполнения теряется, а каталог с
    плагином может быть доступен только на чтение.  Тогда бинарник
    копируется во временный каталог и запускается оттуда.
    """
    path = Path(path)
    if os.name == "nt":
        return str(path)
    if os.access(path, os.X_OK):
        return str(path)
    try:
        os.chmod(path, 0o755)
        return str(path)
    except OSError:
        pass
    if temp_dir is None:
        raise OsmiumError(f"Не удалось разрешить запуск {path}")
    target = Path(temp_dir) / f"osmium-{platform_tag()}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        os.chmod(target, 0o755)
    except OSError as exc:
        raise OsmiumError(f"Не удалось подготовить osmium: {exc}") from exc
    return str(target)


def version_of(executable):
    """Версия osmium как кортеж (major, minor, patch) или None."""
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    for token in text.split():
        parts = token.split(".")
        if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
            numbers = [int(part) for part in parts[:3]]
            while len(numbers) < 3:
                numbers.append(0)
            return tuple(numbers)
    return None


def find_osmium(temp_dir=None):
    """Путь к рабочему osmium или None, если его нет.

    Сначала проверяется поставка плагина, затем системный бинарник.
    Результат кэшируется: поиск и проверка версии стоят времени, а
    вызывать их приходится на каждый источник данных.
    """
    key = ("find", str(temp_dir or ""))
    if key in _cache:
        return _cache[key]
    result = _find_osmium(temp_dir)
    _cache[key] = result
    return result


def _find_osmium(temp_dir):
    # Сначала поставка плагина: она проверена сборкой и работает на любой
    # машине без установки.  Системный бинарник — запасной вариант
    # (например, на macOS или Windows, где поставки нет).
    candidates = []
    bundled = bundled_binary()
    if bundled:
        candidates.append(bundled)
    system = shutil.which("osmium")
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        version = version_of(candidate)
        if version is None or version < MIN_VERSION:
            continue
        try:
            return ensure_executable(candidate, temp_dir)
        except OsmiumError:
            continue
    return None


def _drain(stream, chunks):
    """Читает поток процесса до конца."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def run_process(command, timeout=None, cancel=None):
    """Запускает команду и читает её вывод.

    Вывод читается отдельными потоками: пока процесс жив, никто не
    читает канал, и при выводе больше размера буфера (~64 КБ) процесс
    встаёт на записи.  Возвращает тройку (код возврата, stdout, stderr).
    """
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        raise OsmiumError(f"Не удалось запустить команду: {exc}") from exc
    stdout_chunks = []
    stderr_chunks = []
    readers = [
        threading.Thread(
            target=_drain, args=(process.stdout, stdout_chunks), daemon=True
        ),
        threading.Thread(
            target=_drain, args=(process.stderr, stderr_chunks), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise InterruptedError("osmium cancelled")
            if deadline is not None and time.monotonic() > deadline:
                process.kill()
                raise OsmiumError("osmium не уложился в отведённое время")
            time.sleep(0.2)
    finally:
        for reader in readers:
            reader.join(timeout=10)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.returncode, b"".join(stdout_chunks), b"".join(stderr_chunks)


def run(executable, arguments, timeout=DEFAULT_TIMEOUT, cancel=None):
    """Запускает osmium и возвращает его stdout.

    ``InterruptedError`` пробрасывается наружу: отмена обработки не
    должна превращаться в ошибку источника данных.
    """
    command = [str(executable)] + [str(item) for item in arguments]
    code, stdout, stderr = run_process(command, timeout=timeout, cancel=cancel)
    if code != 0:
        message = stderr.decode("utf-8", "replace").strip()
        raise OsmiumError(f"osmium завершился с ошибкой: {message}")
    return stdout.decode("utf-8", "replace")
