"""Калибровка скорости конкретного ПК для оценки времени подготовки.

Оценка в панели раньше использовала фиксированные «20 тыс. объектов в
секунду» и «0,5 мс на объект».  На другом процессоре, диске и числе
ядер это давало ошибку в разы.  Теперь:

* скорость чтения PBF измеряется той же пробой, что показывает число
  объектов (байты в секунду);
* процессор проходит короткий детерминированный бенчмарк, результат
  кэшируется в QSettings;
* число одновременных слоёв берётся из фактического memory-aware
  ограничения (`Pipeline._worker_count`);
* рельеф оценивается отдельно по площади охвата, выбранному набору DEM
  и наличию готового кэша.

Абсолютные константы оставлены от прежней калибровки, а измерения дают
поправочные коэффициенты.  Так оценка остаётся в привычных единицах, но
подстраивается под машину.
"""

import time

from .cpu import cpu_workers
from .memory import LAYER_WORKER_BYTES, worker_cap
from .pbf_source import parse_tags

# Эталонные значения прежней формулы (калибровка по реальным выгрузкам).
REFERENCE_READ_RATE = 20_000.0
REFERENCE_LAYER_RATE = 2_000.0
REFERENCE_LAYER_OVERHEAD = 0.05
REFERENCE_FIXED_OVERHEAD = 5.0
REFERENCE_WORKERS = 8

# Грубая оценка до скачивания PBF: секунд на мегабайт выгрузки.
REFERENCE_SECONDS_PER_MB = 0.45

# Оценка рельефа: фиксированная часть (загрузка тайлов, мозаика,
# репроекция) и работа на км² для 30-м DEM и 3DEP.
TERRAIN_FIXED_SECONDS = 7.5
TERRAIN_SECONDS_PER_KM2 = 0.012
TERRAIN_3DEP_SECONDS_PER_KM2 = 0.040
TERRAIN_CACHED_SECONDS = 1.5

# Общий хвост после слоёв: создание проекта, значки POI, пересечения
# полигонов, очистка.  Он не зависит от размера выгрузки, но заметен
# на маленьких охватах.
POSTPROCESS_SECONDS = 9.0

# Эталон бенчмарка на машине, где калибровалась формула.
REFERENCE_CPU_SECONDS = 0.0223

# Скорость чтения PBF-пробы на той же машине, байт/с.
REFERENCE_PROBE_BYTES = 30 * 1024 * 1024

CPU_MIN = 0.35
CPU_MAX = 3.0
READ_MIN = 0.5
READ_MAX = 2.0
PARALLEL_MIN = 0.6
PARALLEL_MAX = 1.5

CPU_ROUNDS = 5_000
CPU_REPEATS = 3
CPU_MAX_AGE = 30 * 24 * 60 * 60

SETTINGS_GROUP = ("qgis-xtopo", "qgis-xtopo")
SETTINGS_CPU = "performance/cpu_score"
SETTINGS_CPU_TIME = "performance/cpu_checked"

_CPU_SAMPLES = (
    '"name"=>"Object 123", "highway"=>"residential", '
    '"building"=>"yes", "addr:street"=>"Main"',
    '"name"=>"Река", "waterway"=>"river", "intermittent"=>"no", '
    '"width"=>"12"',
    '"natural"=>"peak", "name"=>"Peak", "ele"=>"1234", '
    '"prominence"=>"300"',
)


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _cpu_workload(rounds=CPU_ROUNDS):
    """Нагрузка, похожая на разбор тегов OSM в слоях."""
    total = 0
    for index in range(rounds):
        tags = parse_tags(_CPU_SAMPLES[index % len(_CPU_SAMPLES)])
        total += len(tags) + (index & 7)
    return total


def measure_cpu_score():
    """Относительная скорость процессора: 1.0 — эталонная машина."""
    best = None
    for _ in range(CPU_REPEATS):
        started = time.perf_counter()
        _cpu_workload()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    if not best or best <= 0:
        return 1.0
    return _clamp(REFERENCE_CPU_SECONDS / best, CPU_MIN, CPU_MAX)


def cpu_score(store=None):
    """Скорость ПК из кэша QSettings, при необходимости измеряет заново."""
    if store is None:
        try:
            from qgis.PyQt.QtCore import QSettings

            store = QSettings(*SETTINGS_GROUP)
        except Exception:  # noqa: BLE001 - без QSettings остаётся замер
            return measure_cpu_score()
    try:
        raw = store.value(SETTINGS_CPU, None)
        checked = float(store.value(SETTINGS_CPU_TIME, 0) or 0)
        if raw not in (None, "") and time.time() - checked < CPU_MAX_AGE:
            return _clamp(float(raw), CPU_MIN, CPU_MAX)
    except (TypeError, ValueError):
        pass
    score = measure_cpu_score()
    try:
        store.setValue(SETTINGS_CPU, f"{score:.6f}")
        store.setValue(SETTINGS_CPU_TIME, str(time.time()))
    except Exception:  # noqa: BLE001 - кэш не обязателен
        pass
    return score


def layer_worker_count(layers):
    """Сколько слоёв реально пойдёт одновременно на этой машине."""
    total = max(1, int(layers or 1))
    requested = max(1, min(cpu_workers(limit=8), total))
    return max(
        1, min(requested, worker_cap(LAYER_WORKER_BYTES, cpu_limit=requested))
    )


def parallel_factor(workers):
    """Поправка на число воркеров относительно эталонных восьми."""
    return _clamp(float(workers) / REFERENCE_WORKERS, PARALLEL_MIN, PARALLEL_MAX)


def read_score(bytes_per_second):
    """Поправка на скорость диска/чтения PBF относительно эталона."""
    if not bytes_per_second or bytes_per_second <= 0:
        return 1.0
    return _clamp(
        float(bytes_per_second) / REFERENCE_PROBE_BYTES, READ_MIN, READ_MAX
    )


def estimate_terrain_seconds(area_km2, source="auto", cpu=None,
                             workers=None, cached=False, hillshade=True):
    """Оценка подготовки растрового рельефа в секундах.

    ``area_km2`` — площадь охвата, ``source`` — ключ выбранного набора
    DEM.  Для 3DEP (1/3″) работы примерно втрое больше на ту же площадь.
    Если готовый рельеф уже лежит в кэше, остаётся только восстановить
    файлы.  ``workers`` — фактическое число параллельных задач; на
    машинах с малым объёмом памяти их меньше, и рельеф идёт медленнее.
    """
    speed = cpu_score() if cpu is None else _clamp(float(cpu), CPU_MIN, CPU_MAX)
    if cached:
        return TERRAIN_CACHED_SECONDS / speed
    if workers is None:
        workers = layer_worker_count(REFERENCE_WORKERS)
    rate = (
        TERRAIN_3DEP_SECONDS_PER_KM2
        if source == "3dep"
        else TERRAIN_SECONDS_PER_KM2
    )
    seconds = TERRAIN_FIXED_SECONDS + max(0.0, float(area_km2 or 0.0)) * rate
    if not hillshade:
        seconds *= 0.8
    # При нехватке памяти воркеров меньше восьми; зависимость слабее
    # линейной, потому что часть работы последовательная.
    memory_factor = (REFERENCE_WORKERS / max(1, workers)) ** 0.35
    return seconds / speed * min(1.6, max(1.0, memory_factor))


def estimate_map_seconds(megabytes, cpu=None, workers=None,
                         terrain_seconds=0.0):
    """Грубая оценка подготовки по объёму карт, до скачивания PBF.

    ``megabytes`` — суммарный размер выгрузок Geofabrik.  Формула
    калибрована на эталонной машине; процессор и число воркеров дают
    поправку, как и в точной оценке после пробы.
    """
    megabytes = max(0.0, float(megabytes or 0.0))
    speed = cpu_score() if cpu is None else _clamp(float(cpu), CPU_MIN, CPU_MAX)
    if workers is None:
        workers = layer_worker_count(8)
    return (
        max(
            megabytes * REFERENCE_SECONDS_PER_MB
            / (speed * parallel_factor(workers)),
            max(0.0, float(terrain_seconds or 0.0)),
        )
        + POSTPROCESS_SECONDS / speed
    )


def estimate_preparation_seconds(total, matched, extended_ratio, layers,
                                 bytes_per_second=0.0, cpu=None,
                                 workers=None, terrain_seconds=0.0):
    """Оценка подготовки OSM и рельефа в секундах для конкретной машины.

    ``total`` — объектов во входных выгрузках, ``matched`` — сколько из
    них попадает в выбранные слои, ``extended_ratio`` — доля выгрузки,
    которую придётся прочитать.  ``bytes_per_second`` — измеренная
    скорость PBF-пробы; ``cpu`` и ``workers`` можно передать явно для
    тестов и для оценки до запуска пробы.  ``terrain_seconds`` —
    отдельная оценка рельефа (``estimate_terrain_seconds``): рельеф
    считается параллельно слоям, поэтому берётся максимум, а не сумма.
    """
    share = max(0.0, min(1.0, float(extended_ratio or 0.0))) or 1.0
    speed = cpu_score() if cpu is None else _clamp(float(cpu), CPU_MIN, CPU_MAX)
    if workers is None:
        workers = layer_worker_count(layers)
    parallel = parallel_factor(workers)
    read = total * share / (REFERENCE_READ_RATE * read_score(bytes_per_second))
    layer = matched / (REFERENCE_LAYER_RATE * speed * parallel)
    overhead = (
        layers * REFERENCE_LAYER_OVERHEAD / speed
        + REFERENCE_FIXED_OVERHEAD / speed
    )
    return (
        max(read + layer + overhead, max(0.0, float(terrain_seconds or 0.0)))
        + POSTPROCESS_SECONDS / speed
    )
