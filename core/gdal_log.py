"""Сбор предупреждений GDAL во время подготовки проекта.

GDAL пишет о проблемах в данных прямо в консоль QGIS: незамкнутые кольца
в отношениях OSM, объекты с типом геометрии, не совпадающим с типом слоя,
самопересечения контуров. Отдельные сообщения бесполезны — их десятки, а
причина одна. Поэтому перехватываем их и показываем в журнале плагина
сводкой: сколько и о чём.

Ошибки (CE_Failure и выше) перехватчик не глотает: они и дальше превращаются
в исключения, а текст дополнительно дублируется в stderr, чтобы ничего не
потерялось.
"""

import sys
import threading
from collections import Counter

from . import i18n

# Категории предупреждений: начало сообщения -> описание для журнала.
_CATEGORIES = (
    (
        "Non closed ring detected",
        "незамкнутые кольца в данных OSM (GDAL закрывает их сам)",
    ),
    (
        "organizePolygons() received an unexpected geometry",
        "некорректные многоугольники в данных OSM",
    ),
    (
        "A geometry of type",
        "тип геометрии объекта не совпадает с типом слоя",
    ),
    (
        "Self-intersection at or near point",
        "контур пересекает сам себя",
    ),
)


def _categorize(message):
    text = message.strip()
    for prefix, description in _CATEGORIES:
        if text.startswith(prefix):
            return description
    return text.split(":")[0][:80] or "прочее"


class GdalLog:
    """Счётчик предупреждений GDAL.

    Счётчики общие для всех потоков, а обработчик устанавливается в том
    потоке, где GDAL действительно вызывается: GDAL различает обработчики
    по потокам.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = Counter()
        self._errors = Counter()
        self._last_return = None

    def _handle(self, err_class, err_no, message):
        try:
            from osgeo import gdal
        except ImportError:  # pragma: no cover - GDAL всегда доступен в QGIS
            gdal = None
        failure = gdal is not None and err_class >= gdal.CE_Failure
        with self._lock:
            if failure:
                self._errors[_categorize(message)] += 1
            else:
                self._counts[_categorize(message)] += 1
        if failure:
            # Настоящая ошибка не должна молчать: GDAL превратит её в
            # исключение, но текст полезно иметь и в консоли.
            sys.stderr.write(f"GDAL ERROR {err_no}: {message.strip()}\n")
            self._last_return = gdal.CE_None
            return self._last_return
        # GDAL вызывает все обработчики в стеке, поэтому сообщение уходит и
        # в консоль QGIS. CE_Fatal обрывает цепочку: текст остаётся только в
        # сводке журнала.
        self._last_return = gdal.CE_Fatal
        return self._last_return

    def counts(self):
        with self._lock:
            return dict(self._counts)

    def error_counts(self):
        with self._lock:
            return dict(self._errors)

    def summary(self):
        """Строки сводки для журнала плагина."""
        lines = []
        for description, count in sorted(self.counts().items(), key=lambda item: -item[1]):
            lines.append(
                i18n.trf("GDAL: {} — {}", i18n.tr(description), count)
            )
        for description, count in sorted(self.error_counts().items(), key=lambda item: -item[1]):
            lines.append(
                i18n.trf(
                    "GDAL: ошибок «{}» — {}",
                    i18n.tr(description), count,
                )
            )
        return lines

    def reset(self):
        with self._lock:
            self._counts.clear()
            self._errors.clear()

    def install(self):
        """Установить обработчик в текущем потоке."""
        from osgeo import gdal

        gdal.PushErrorHandler(self._handle)
        return self

    def restore(self):
        from osgeo import gdal

        gdal.PopErrorHandler()


# Единственный счётчик на время подготовки проекта.
LOG = GdalLog()
