"""Сторож скорости сетевых загрузок.

Общий порог для рельефа и выгрузок OSM: источник, который за минуту не
отдал и 100 КБ/с, считается слишком медленным.  Для карт и вырезов это
повод заменить источник, для рельефа — взять набор ниже по приоритету.
Ожидание ответа сервера и сборка выреза в окно скорости не входят:
оно начинается с первого принятого байта.
"""

import threading
import time


SLOW_DOWNLOAD_RATE = 100 * 1024
SLOW_DOWNLOAD_WINDOW = 60.0


class SlowDownload(RuntimeError):
    """Источник отдаёт данные медленнее порога."""


class Speed:
    """Скорость загрузки с усреднением по окну времени."""

    def __init__(self, window=0.5):
        self.window = window
        self.total = 0
        self.speed = 0.0
        self._mark = 0
        self._time = time.monotonic()
        self._lock = threading.Lock()

    def add(self, size):
        """Добавляет байты. True, если скорость пересчитана."""
        with self._lock:
            self.total += size
            now = time.monotonic()
            elapsed = now - self._time
            if elapsed < self.window:
                return False
            current = (self.total - self._mark) / elapsed
            self.speed = current if not self.speed else 0.5 * self.speed + 0.5 * current
            self._mark = self.total
            self._time = now
            return True


class SpeedGuard:
    """Сторож скорости скачивания источника.

    Скорость оценивается окнами по :data:`SLOW_DOWNLOAD_WINDOW` секунд.
    Байты приходят из читающего цикла (:meth:`add`); для растровых
    наборов, где нет отдельного счётчика, меряется прирост файлов слоя
    через ``measure``.  Сторож с ``measure`` стартует при создании,
    сторож без него — с первого байта в :meth:`add`.
    """

    def __init__(self, measure=None, window=SLOW_DOWNLOAD_WINDOW,
                 threshold=SLOW_DOWNLOAD_RATE):
        self._measure = measure
        self._window = window
        self._threshold = threshold
        self._lock = threading.Lock()
        self._extra = 0
        self._base = self._measure_bytes()
        self._mark_bytes = 0
        self._mark_time = time.monotonic() if measure is not None else None
        self.slow = False
        self.speed = 0.0

    def _measure_bytes(self):
        if self._measure is None:
            return 0
        try:
            return int(self._measure() or 0)
        except OSError:
            return 0

    def _total(self):
        return self._extra + max(0, self._measure_bytes() - self._base)

    def add(self, size):
        """Добавляет прочитанные байты; :class:`SlowDownload` — медленно."""
        with self._lock:
            if self._mark_time is None:
                self._mark_time = time.monotonic()
            self._extra += size
        self.check()
        if self.slow:
            raise SlowDownload(self.reason())

    def check(self):
        """True — последнее окно оказалось медленнее порога."""
        with self._lock:
            if self.slow:
                return True
            if self._mark_time is None:
                return False
            now = time.monotonic()
            elapsed = now - self._mark_time
            if elapsed <= 0 or elapsed < self._window:
                return False
            total = self._total()
            self.speed = max(0, total - self._mark_bytes) / elapsed
            self._mark_time = now
            self._mark_bytes = total
            if self.speed < self._threshold:
                self.slow = True
            return self.slow

    def reason(self):
        """Короткая причина для журнала: измеренная скорость и порог."""
        return (
            f"{self.speed / 1024:.0f} КБ/с < "
            f"{self._threshold / 1024:.0f} КБ/с"
        )
