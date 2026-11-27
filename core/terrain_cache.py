"""Кэш результатов обработки рельефа.

Рельеф и изолинии считаются долго — на большой площади это минуты. Поэтому
результаты сохраняются в кэш и переиспользуются, пока не изменилось:

* версия плагина;
* параметры обработки (шаг изолиний, сглаживание, метод ресемплинга);
* исходные данные (тайлы DEM, слои воды и ледников).

Ключ кэша — хеш этих данных. Сменилась версия плагина, параметры или
данные — ключ не совпадает, и рельеф считается заново.

Кэш лежит рядом с кэшем тайлов SRTM: ``<проекты>/qgisxtopo-cache/terrain``.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

# Сырой DEM переиспользуется между запусками и не зависит от воды.
DEM_FILES = (
    "merged_dem.tif",
)

# Маскированный по воде DEM и производные от него растры.  Ключ этой
# группы включает отпечатки слоёв воды: изменился берег или озеро —
# маска и отмывка пересчитываются.
RELIEF_FILES = (
    "merged_dem.tif",
    "slope.tif",
    "hillshade_composite.tif",
)

# Все файлы рельефа возвращаются из кэша жёсткой ссылкой: перед записью
# terrain.py убирает прежний файл, поэтому GDAL не может переписать
# кэш на месте.  Копирование гигабайтов при каждой подготовке больше
# не нужно.

# Версия расчёта рельефа (формат растров, сборка мозаики).
# Меняя результат, поднимайте число: иначе старый кэш отдаст устаревший
# рельеф даже после обновления плагина.  Версия плагина сюда намеренно не
# входит — обновление интерфейса или стиля не должно пересчитывать DEM.
DEM_VERSION = 3

# Версия параметров отмывки (core/terrain.py: generate_hillshade_slope).
# Меняя направление света, преувеличение, подсветку теней или правило
# укрупнения DEM, поднимайте число: иначе кэш вернёт старые растры.
# 5 — уклон и оттенок считаются по нативному DEM; укрупнение убрано.
# 6 — уклон пишется байтом (1°) в slope.tif.
HILLSHADE_STYLE_VERSION = 6

# Версия расчёта изолиний: 2 — DEM режется на тайлы с нахлёстом,
# контуры считаются параллельно, в результат попадает середина тайла.
# 5 — геометрия упрощается на min(5 м, шаг/2).
# 6 — упрощение выполняется в потоках тайлов.
# 7 — вода вычитается из готовых линий векторно, без растра-маски.
# 8 — подробные изолинии без упрощения, обзорные — отдельной копией.
# 9 — обзорная копия упрощается на 10 м и считается за один проход.
# 10 — DEM маскируется по воде, морское дно в контуры не попадает.
# 11 — в кэше сохраняются и общие слои isolines_full.
# 12 — сглаживание контуров считается фильтром на нативном DEM, без
#      временного растра вдвое подробнее.
ISOLINES_VERSION = 12

# Версия маскирования DEM по полигонам воды.  Меняется правило маски —
# поднимаем число, иначе кэш вернёт отмывку, посчитанную по морскому дну.
WATER_MASK_VERSION = 1

# Версия фильтра океанских тайлов DEM.  Меняется правило пропуска
# тайлов — старые подсказки кэша больше не должны находить рельеф,
# собранный по другому набору тайлов.
TILE_FILTER_VERSION = 1
ISOLINES_FILES = (
    "isolines_full.sqlite",
    "isolines_full_overview.sqlite",
    "isolines_regular.sqlite",
    "isolines_glacier.sqlite",
    "isolines_regular_overview.sqlite",
    "isolines_glacier_overview.sqlite",
)

# Сколько последних записей кэша оставляем в каждой группе.  Рельеф
# занимает сотни мегабайт, поэтому храним только последнюю.
KEEP_ENTRIES = 1
# Сколько подсказок «эта область уже посчитана» храним.  Подсказка
# избавляет от скачивания тайлов DEM перед проверкой кэша.
KEEP_HINTS = 16

_CHUNK = 1024 * 1024


def _digest(parts):
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return "missing"
    return digest.hexdigest()


def _file_stat(path):
    """Быстрый отпечаток файла: имя, размер и время изменения.

    Хеш содержимого на крупных охватах — это гигабайты чтения на
    каждой подготовке, а для кэша достаточно отпечатка файла.
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return f"{path.name}:missing"
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def _layer_digest(path):
    """Хест содержимого слоя (геометрия и атрибуты).

    Хеш файла не годится: SQLite-файл может отличаться байтами при той же
    геометрии. Поэтому сравниваем сами объекты слоя.
    """
    path = Path(path)
    if not path.exists():
        return "none"
    try:
        from osgeo import ogr
    except ImportError:
        return _file_sha256(path)
    digest = hashlib.sha1()
    try:
        dataset = ogr.Open(str(path))
    except Exception:  # noqa: BLE001 - битый файл считаем пустым
        return "broken"
    if dataset is None:
        return "broken"
    try:
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            definition = layer.GetLayerDefn()
            fields = [
                definition.GetFieldDefn(position).GetName()
                for position in range(definition.GetFieldCount())
            ]
            digest.update(("|".join(fields)).encode("utf-8"))
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and not geometry.IsEmpty():
                    digest.update(geometry.ExportToWkb())
                for name in fields:
                    digest.update(str(feature.GetField(name)).encode("utf-8"))
    finally:
        dataset = None
    return digest.hexdigest()


def layer_digest(path):
    """Публичный отпечаток содержимого слоя для других кэшей."""
    return _layer_digest(path)


def dem_source_digest(config):
    """Отпечаток исходных тайлов DEM.

    Считается по файлам `.hgt` (как скачаны) — их содержимое стабильно от
    запуска к запуску, в отличие от результата конвертации в `.tif`.
    Хеш содержимого не нужен: на крупном охвате это гигабайты чтения
    на каждой подготовке, поэтому достаточно имени, размера и времени
    изменения файла.
    """
    parts = []
    try:
        entries = sorted(config.terrain_input_dir.iterdir())
    except OSError:
        return _digest([])
    for path in entries:
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".hgt", ".tif"):
            continue
        parts.append(_file_stat(path))
    return _digest(parts)


def _tile_digest(required_tiles):
    if required_tiles is None:
        return "all"
    return _digest(sorted(required_tiles))


def dem_signature(config, bbox, source_digest=None, required_tiles=None):
    """Ключ кэша рельефа: версия формата, область, параметры и тайлы DEM."""
    if source_digest is None:
        source_digest = dem_source_digest(config)
    parts = [
        DEM_VERSION,
        TILE_FILTER_VERSION,
        bbox.as_key() if hasattr(bbox, "as_key") else str(bbox),
        config.resolved_terrain_source(),
        config.terrain_resample_method,
        source_digest,
        _tile_digest(required_tiles),
    ]
    return _digest(parts)


WATER_LAYER_NAMES = (
    "water_dissolved.sqlite",
    "water.sqlite",
    "water_intermittent_dissolved.sqlite",
    "water_intermittent.sqlite",
)


def _water_digest(config):
    """Отпечаток слоёв, по которым DEM маскируется.

    Считается по содержимому, а не по времени изменения: файлы слоёв
    перезаписываются при каждой подготовке, и mtime не даёт кэшу
    переиспользовать готовый рельеф даже при тех же данных.  Слои воды
    небольшие, полный обход занимает доли секунды.
    """
    return _digest([
        _layer_digest(config.vector_dir / name) for name in WATER_LAYER_NAMES
    ])


def relief_signature(config, bbox, dem_key):
    """Ключ маскированного DEM, уклона и отмывки.

    Сырой DEM от воды не зависит, а эти растры зависят: изменился берег
    или озеро — маску и отмывку надо пересчитать.
    """
    parts = [
        WATER_MASK_VERSION,
        HILLSHADE_STYLE_VERSION,
        dem_key,
        bbox.as_key() if hasattr(bbox, "as_key") else str(bbox),
        config.generate_terrain_hillshade_slope,
        _water_digest(config),
    ]
    return _digest(parts)


def _stat_digest(directory):
    """Быстрый отпечаток каталога: имена, размеры и время изменения."""
    parts = []
    try:
        entries = sorted(Path(directory).expanduser().iterdir())
    except OSError:
        return _digest(parts)
    for path in entries:
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return _digest(parts)


def dem_hint(config, bbox, required_tiles=None):
    """Ключ «эта область уже посчитана» без скачивания тайлов.

    Полный ключ :func:`dem_signature` включает хеш содержимого тайлов DEM,
    поэтому его нельзя вычислить до загрузки.  Подсказка строится только
    по настройкам и области: если готовый рельеф уже лежит в кэше, тайлы
    скачивать незачем.  Для ручного каталога и локальных тайлов в ключ
    входит отпечаток файлов, чтобы подмена данных не осталась незамеченной.
    """
    source = config.resolved_terrain_source()
    parts = [
        DEM_VERSION,
        TILE_FILTER_VERSION,
        bbox.as_key() if hasattr(bbox, "as_key") else str(bbox),
        source,
        config.terrain_resample_method,
        config.download_terrain_tiles,
        config.get_terrain_tiles,
        _tile_digest(required_tiles),
    ]
    if source == "manual":
        parts.append(_stat_digest(config.terrain_src_dir))
    elif not config.download_terrain_tiles:
        parts.append(_stat_digest(config.terrain_input_dir))
    return _digest(parts)


def _hint_dir(config):
    return config.terrain_result_cache_dir / "dem-hints"


def _hint_path(config, hint):
    return _hint_dir(config) / f"{hint}.json"


def _prune_hints(directory):
    """Убирает подсказки на уже удалённые записи кэша."""
    try:
        entries = [item for item in directory.iterdir() if item.is_file()]
    except OSError:
        return
    entries.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for item in entries[KEEP_HINTS:]:
        item.unlink(missing_ok=True)
    dem_dir = directory.parent / "dem"
    for item in entries[:KEEP_HINTS]:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            item.unlink(missing_ok=True)
            continue
        signature = str(data.get("signature") or "")
        if not signature or not (dem_dir / signature).is_dir():
            item.unlink(missing_ok=True)


def store_dem_hint(config, hint, signature, used=(), fallback=""):
    """Запоминает, какой полный ключ соответствует области и настройкам."""
    path = _hint_path(config, hint)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(
            {
                "signature": signature,
                "used": list(used or ()),
                "fallback": fallback or "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    _prune_hints(path.parent)


def restore_dem_by_hint(config, hint):
    """Готовый рельеф по подсказке или None.

    Возвращает данные подсказки: полный ключ, наборы мозаики и сообщение
    о замене тайлов.  Если запись кэша успела удалиться, подсказка тоже
    убирается.
    """
    path = _hint_path(config, hint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    signature = str(data.get("signature") or "")
    if not signature or not restore_dem(config, signature):
        path.unlink(missing_ok=True)
        return None
    return data


def isolines_signature(config, bbox, dem_key):
    """Ключ кэша изолиний.

    Кроме рельефа учитываются слои, по которым изолинии режутся: постоянная
    и пересыхающая вода и ледники. Изменились данные — изолинии
    пересчитываются заново.
    """
    parts = [
        dem_key,
        config.isolines_step,
        getattr(config, "isolines_units", "m"),
        config.smooth_isolines,
        bbox.as_key() if hasattr(bbox, "as_key") else str(bbox),
        # Слои, по которым режутся изолинии: отпечаток содержимого вместо
        # времени изменения — файлы перезаписываются при каждой сборке,
        # а данные при том же охвате не меняются.
        _layer_digest(config.vector_dir / "water_dissolved.sqlite"),
        _layer_digest(config.vector_dir / "water.sqlite"),
        _layer_digest(config.vector_dir / "water_intermittent_dissolved.sqlite"),
        _layer_digest(config.vector_dir / "water_intermittent.sqlite"),
        _layer_digest(config.vector_dir / "glacier.sqlite"),
        # Нарезка DEM на тайлы изменилась: старые изолинии считались
        # одним проходом и переиспользовать их нельзя.
        ISOLINES_VERSION,
    ]
    return _digest(parts)


def _group_dir(config, group, signature):
    return config.terrain_result_cache_dir / group / signature


def _prune(directory, keep=KEEP_ENTRIES):
    """Оставляет несколько последних записей кэша."""
    try:
        entries = [item for item in directory.iterdir() if item.is_dir()]
    except OSError:
        return
    entries.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for item in entries[keep:]:
        shutil.rmtree(item, ignore_errors=True)


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _restore_item(item, target):
    """Восстанавливает файл кэша без лишнего копирования.

    Крупные растры занимают гигабайты, и каждый прогон переписывал их
    заново: восстановление рельефа занимало больше 20 секунд.  Все файлы
    связываются с кэшем жёсткой ссылкой — места на диске не расходуется,
    а данные те же.  Перед записью terrain.py удаляет прежний файл,
    поэтому кэш не может быть перезаписан через ссылку.
    """
    if target.exists():
        if _same_file(item, target):
            return
        try:
            source_stat = item.stat()
            target_stat = target.stat()
            if (source_stat.st_size == target_stat.st_size
                    and source_stat.st_mtime_ns == target_stat.st_mtime_ns):
                return
        except OSError:
            pass
        # Прежний файл мог быть жёсткой ссылкой на другую запись кэша
        # (например, сырой DEM под маскированным).  Копирование поверх
        # изменило бы и её, поэтому ссылку сначала убираем.
        target.unlink(missing_ok=True)
    try:
        os.link(item, target)
        return
    except OSError:
        pass
    shutil.copy2(item, target)


def _restore(config, group, signature, names, target_dir, required):
    source = _group_dir(config, group, signature)
    if not source.is_dir():
        return False
    for name in required:
        if not (source / name).exists():
            return False
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        item = source / name
        if item.exists():
            _restore_item(item, target_dir / name)
    # Запись только что использована: время изменения обновляется, иначе
    # вытеснение по mtime удаляло бы её первой.
    _touch(source)
    return True


def _touch(directory):
    """Обновляет время изменения каталога записи кэша."""
    try:
        os.utime(directory, None)
    except OSError:
        pass


def _store(config, group, signature, names, source_dir):
    target = _group_dir(config, group, signature)
    target.mkdir(parents=True, exist_ok=True)
    stored = False
    for name in names:
        item = source_dir / name
        if not item.exists():
            continue
        destination = target / name
        if _same_file(item, destination):
            stored = True
            continue
        # Ссылка вместо копии: файл только читается, а перед следующей
        # записью terrain.py удаляет его из каталога проекта.
        destination.unlink(missing_ok=True)
        try:
            os.link(item, destination)
        except OSError:
            shutil.copy2(item, destination)
        stored = True
    if stored:
        _prune(target.parent)
    return stored


def dem_required(config):
    """Сырые растры, которые должны быть в кэше при текущих настройках."""
    return ("merged_dem.tif",)


def relief_required(config):
    """Растры маскированного рельефа, нужные для текущих настроек."""
    names = ["merged_dem.tif"]
    if config.generate_terrain_hillshade_slope:
        names.extend(["slope.tif", "hillshade_composite.tif"])
    return tuple(names)


def dem_cached(config, bbox):
    """Есть ли готовый рельеф для области, без копирования в проект.

    Проверяет ту же подсказку, что и :func:`restore_dem_by_hint`, но
    ничего не восстанавливает: панель оценки не должна трогать файлы
    проекта при каждом пересчёте времени.
    """
    hint = dem_hint(config, bbox)
    try:
        data = json.loads(
            _hint_path(config, hint).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    signature = str(data.get("signature") or "")
    if not signature:
        return False
    source = _group_dir(config, "dem", signature)
    return all((source / name).exists() for name in dem_required(config))


def restore_dem(config, signature):
    """Возвращает готовые растры рельефа из кэша (True — удалось)."""
    return _restore(
        config, "dem", signature,
        DEM_FILES, config.raster_dir, dem_required(config),
    )


def store_dem(config, signature):
    """Сохраняет сырые растры рельефа в кэш."""
    return _store(config, "dem", signature, DEM_FILES, config.raster_dir)


def restore_relief(config, signature):
    """Возвращает маскированный DEM, уклон и отмывку из кэша."""
    return _restore(
        config, "relief", signature,
        RELIEF_FILES, config.raster_dir, relief_required(config),
    )


def store_relief(config, signature):
    """Сохраняет маскированный DEM, уклон и отмывку в кэш."""
    return _store(
        config, "relief", signature, RELIEF_FILES, config.raster_dir,
    )


def restore_isolines(config, signature):
    """Возвращает готовые изолинии из кэша (True — удалось)."""
    return _restore(
        config, "isolines", signature,
        ISOLINES_FILES, config.vector_dir,
        ("isolines_regular.sqlite", "isolines_regular_overview.sqlite"),
    )


def store_isolines(config, signature):
    """Сохраняет изолинии в кэш."""
    return _store(config, "isolines", signature, ISOLINES_FILES, config.vector_dir)
