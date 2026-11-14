"""Оценка доступной памяти и лимиты параллельных задач.

На машине с 8 ГБ нельзя запускать столько же процессов osmium, сколько
на 32 ГБ: каждая вырезка крупной карты занимает несколько гигабайт.
Поэтому число одновременных задач ограничивается не только ядрами, но и
бюджетом памяти.  На машинах с большим запасом памяти ограничение не
мешает: там срабатывает потолок по ядрам.

Linux отдаёт точные значения в ``/proc/meminfo``, Windows — через
``GlobalMemoryStatusEx``.  Остальные системы используют общий размер
физической памяти и консервативную оценку свободной.
"""

import ctypes
import os
import sys
import threading
from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB

# Резерв для QGIS, системы и роста уже занятых структур.
RESERVE_BYTES = int(1.5 * GIB)

# Ниже этого бюджета не опускаемся: иначе даже одна задача не запустится.
MIN_BUDGET = 512 * MIB

# Если определить память не удалось, считаем машину 8-гигабайтной.
FALLBACK_TOTAL = 8 * GIB

# osmium extract строит индекс узлов всей карты: на выгрузке 46–119 МБ
# замер дал 3,6–3,9 ГБ.  tags-filter читает файл потоково и почти не
# занимает памяти.
OSMIUM_EXTRACT_PER_BYTE = 55.0
OSMIUM_EXTRACT_BASE = int(1.5 * GIB)
OSMIUM_EXTRACT_MIN = int(1.5 * GIB)
OSMIUM_EXTRACT_MAX = 6 * GIB
OSMIUM_FILTER_MIN = 128 * MIB
OSMIUM_FILTER_MAX = 512 * MIB

# Оценка памяти одного потока обработки слоёв и одного тайла изолиний.
LAYER_WORKER_BYTES = int(1.5 * GIB)
CONTOUR_WORKER_BYTES = 768 * MIB


def _cgroup_memory():
    """Лимит памяти cgroup v2/v1, если он меньше физической памяти."""
    try:
        path_line = next(
            line for line in Path("/proc/self/cgroup").read_text(
                encoding="ascii"
            ).splitlines() if line.startswith("0::")
        )
        relative = path_line.split("::", 1)[1].strip().lstrip("/")
        group = Path("/sys/fs/cgroup") / relative
        limit_text = (group / "memory.max").read_text(encoding="ascii").strip()
        current = int((group / "memory.current").read_text(encoding="ascii"))
        if limit_text != "max":
            limit = int(limit_text)
            stats = {}
            try:
                for line in (group / "memory.stat").read_text(
                    encoding="ascii"
                ).splitlines():
                    key, value = line.split()
                    stats[key] = int(value)
            except (OSError, ValueError):
                stats = {}
            if "anon" in stats:
                # Файловый кэш ядро может освободить, поэтому в доступное
                # не включаем только анонимную память и ядро.
                used = stats["anon"] + stats.get("kernel", 0)
                used += max(
                    0, stats.get("file", 0) - stats.get("inactive_file", 0)
                )
            else:
                used = current
            return limit, max(0, limit - used)
    except (OSError, ValueError, StopIteration):
        pass
    try:
        limit = int(
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text(
                encoding="ascii"
            )
        )
        current = int(
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").read_text(
                encoding="ascii"
            )
        )
        # В cgroup v1 «без лимита» записано огромное число.
        if limit < 1 << 60:
            return limit, max(0, limit - current)
    except (OSError, ValueError):
        pass
    return None, None


def _linux_memory():
    """(всего, доступно) из /proc/meminfo с учётом лимита cgroup."""
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            if not rest:
                continue
            values[key.strip()] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None, None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = (
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
        )
    limit, limit_available = _cgroup_memory()
    if limit is not None:
        total = min(total, limit) if total else limit
        if limit_available is not None:
            available = (
                min(available, limit_available) if available else limit_available
            )
    return total, available


def _windows_memory():
    """(всего, доступно) через GlobalMemoryStatusEx."""
    try:
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("load", wintypes.DWORD),
                ("total", ctypes.c_ulonglong),
                ("available", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)
        ):
            return None, None
        return int(status.total), int(status.available)
    except (AttributeError, OSError, ValueError):
        return None, None


def _portable_memory():
    """Общий объём памяти и консервативная оценка доступной."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        total = int(pages) * int(size)
        # На macOS и BSD нет простого аналога MemAvailable.
        return total, int(total * 0.7)
    except (AttributeError, OSError, ValueError):
        return None, None


def memory_state():
    """Пара (всего байт, доступно байт); ``None`` — определить не удалось."""
    if sys.platform.startswith("linux"):
        total, available = _linux_memory()
        if total:
            return total, available
    if os.name == "nt":
        total, available = _windows_memory()
        if total:
            return total, available
    total, available = _portable_memory()
    if total:
        return total, available
    return None, None


def memory_budget(total=None, available=None, extra_reserve=0):
    """Сколько памяти можно отдать параллельным задачам.

    ``extra_reserve`` — память, которая уже нужна другой стадии
    (например, параллельно строящемуся рельефу).
    """
    detected_total, detected_available = memory_state()
    if total is None:
        total = detected_total or FALLBACK_TOTAL
    if available is None:
        available = detected_available
    if available is None:
        available = int(total * 0.7)
    reserve = max(RESERVE_BYTES, int(total * 0.08)) + max(0, int(extra_reserve))
    budget = min(int(available) - reserve, int(total * 0.85))
    return max(MIN_BUDGET, budget)


def worker_cap(per_worker, cpu_limit=None, budget=None):
    """Сколько воркеров помещается в бюджет памяти и в потолок ядер."""
    if budget is None:
        budget = memory_budget()
    per_worker = max(1, int(per_worker))
    workers = max(1, int(budget) // per_worker)
    if cpu_limit is not None:
        workers = min(workers, max(1, int(cpu_limit)))
    return max(1, workers)


def osmium_extract_bytes(path):
    """Оценка памяти ``osmium extract`` для исходной карты."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    estimate = int(size * OSMIUM_EXTRACT_PER_BYTE) + OSMIUM_EXTRACT_BASE
    return max(OSMIUM_EXTRACT_MIN, min(OSMIUM_EXTRACT_MAX, estimate))


def osmium_filter_bytes(path):
    """Оценка памяти ``osmium tags-filter``: потоковое чтение."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    estimate = max(OSMIUM_FILTER_MIN, int(size * 4))
    return min(OSMIUM_FILTER_MAX, estimate)


def gdal_cache_bytes():
    """Размер кэша GDAL: четверть бюджета, но не больше 2 ГБ."""
    return max(256 * MIB, min(2 * GIB, memory_budget() // 4))


def gdal_cache_mb():
    """GDAL_CACHEMAX в мегабайтах."""
    return max(1, gdal_cache_bytes() // MIB)


class MemoryLimiter:
    """Пропускает задачи, пока их суммарная оценка помещается в бюджет.

    Если одна задача требует больше бюджета, она запускается одна:
    иначе крупную карту невозможно было бы обработать вообще.
    """

    def __init__(self, budget):
        self.budget = max(1, int(budget))
        self.used = 0
        self.peak = 0
        self._condition = threading.Condition()

    def acquire(self, amount):
        amount = max(1, int(amount))
        with self._condition:
            while self.used and self.used + amount > self.budget:
                self._condition.wait()
            self.used += amount
            self.peak = max(self.peak, self.used)
        return amount

    def release(self, amount):
        with self._condition:
            self.used = max(0, self.used - max(0, int(amount)))
            self._condition.notify_all()

    def reservation(self, amount):
        """Контекстный менеджер для одной задачи."""
        return _Reservation(self, amount)


class _Reservation:
    def __init__(self, limiter, amount):
        self.limiter = limiter
        self.amount = amount

    def __enter__(self):
        self.limiter.acquire(self.amount)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.limiter.release(self.amount)
        return False
