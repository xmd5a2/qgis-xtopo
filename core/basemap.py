"""Растровая подложка готового проекта.

Подложка не участвует в подготовке данных: это слой XYZ, который
добавляется в проект самым нижним.  Пока подложка включена, площадные
слои показывают только подписи: их полный стиль сохраняется в менеджере
стилей слоя, а активным становится стиль с null-рендерером.

Стили ``qgis_xtopo_full`` и ``qgis_xtopo_labels`` остаются в проекте:
это позволяет переключать подложку без повторного снятия копии стиля.
Они хранят только символьное оформление без подписей: подложка подписи
не меняет, а разбор XML с подписями на каждом слое занимал секунды.

Подложка даёт ещё и инверсию: надписи, дороги, скалы, ЛЭП, подъёмники и
железные дороги перекрашиваются, чтобы читаться на снимке.  Цвет текста
и цвет halo меняются местами, дороги и тропы становятся светлыми, скалы —
светло-коричневыми, ЛЭП — серыми, подъёмники — серо-белыми, железные
дороги — белыми.  Внешнее мягкое свечение буфера надписей на подложке
выключается, иначе оно остаётся светлым при чёрной обводке.  Чёрным
значкам добавляется плотная белая обводка,
сами значки остаются исходными.  Прежние цвета хранятся в свойствах слоя — по ним всё
возвращается в исходный вид.

Инверсия надписей не трогает надписи дорог и площадных дорог: надпись
дороги лежит на самой полосе, и её тип определяется по тегам объекта
(см. `core/label_tags.py`), а не по цвету надписи.  Значкам paint effect
QGIS добавляет плотную белую обводку, сами значки не перекрашиваются.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from qgis.core import (
    QgsDrawSourceEffect,
    QgsEffectStack,
    QgsHashedLineSymbolLayer,
    QgsLineSymbolLayer,
    QgsMapLayer,
    QgsMapLayerStyle,
    QgsMaskMarkerSymbolLayer,
    QgsNullSymbolRenderer,
    QgsOuterGlowEffect,
    QgsPointClusterRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsReadWriteContext,
    QgsRuleBasedRenderer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsVectorLayer,
    Qgis,
)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtXml import QDomDocument

from . import label_style, label_tags
from .resources import resource_path

SOURCE_PROPERTY = "qgis_xtopo/basemap"
OSM_VISIBLE_PROPERTY = "qgis_xtopo/basemap_osm_visible"
LABEL_INVERSION_PROPERTY = "qgis_xtopo/basemap_label_inversion"
LABEL_COLORS_PROPERTY = "qgis_xtopo/basemap_label_colors"
SYMBOL_COLORS_PROPERTY = "qgis_xtopo/basemap_symbol_colors"
ICON_EFFECTS_PROPERTY = "qgis_xtopo/basemap_icon_effects"
HILLSHADE_PROPERTY = "qgis_xtopo/basemap_hillshade"
SLOPE_PROPERTY = "qgis_xtopo/basemap_slope"
FULL_STYLE = "qgis_xtopo_full"
LABELS_STYLE = "qgis_xtopo_labels"
OSM_LAYER_NAME = "OpenStreetMap"
# `cemetery icons` — полигональный слой только по данным: он рисует
# значок, который нужен и на снимке.
EXCLUDED_LAYERS = (OSM_LAYER_NAME, "BBOX", "cemetery icons")
# Административные границы и их подписи сохраняют свои цвета на снимке:
# инверсия их не трогает.
ADMIN_LAYER_PREFIX = "admin level "
# Слои, которые на снимке только мешают: ряд деревьев рисуется зелёной
# линией, отдельные деревья — значками, линейный слой аэродрома —
# взлётными полосами и рулёжными дорожками.  Собственных подписей у них
# нет; значки аэродрома остаются — это отдельный точечный слой.
HIDDEN_SYMBOL_LAYERS = ("tree row", "tree", "aeroway runway way")

# Линейные слои, которые инверсия перекрашивает.  Ключ — имя слоя в
# шаблоне, значение — набор цветов: дороги и тропы светлые, скалы
# светло-коричневые, ЛЭП серые, подъёмники серо-белые, железные дороги
# белые.
INVERTED_SYMBOL_LAYERS = {
    "track": "road",
    "path": "road",
    "ridge, arete": "ridge",
    "power line": "power",
    "power minor line": "power",
    "aerialway cable car, gondola": "aerialway",
    "aerialway chair lift": "aerialway",
    "railway all": "rail",
    "rail tunnel": "rail",
    "railway bridge": "rail",
}

ROAD_COLOR = "#f2f2f2"
RIDGE_COLOR = "#c9a17a"
POWER_COLOR = "#b8b8b8"
AERIALWAY_CASING_COLOR = "#f2f2f2"
AERIALWAY_LINE_COLOR = "#a8a8a8"
RAIL_COLOR = "#ffffff"
ICON_GLOW_COLOR = (255, 255, 255, 255)
# Обводка без сильного размытия: широкое мягкое гало затеняло снимок.
# Значки в шаблоне имеют размер 2–3,2 мм, поэтому 0,5 мм — это
# заметная кайма, а 0,1 мм сглаживает только край.
ICON_GLOW_SPREAD_MM = 0.5
ICON_GLOW_BLUR_MM = 0.1
# Столбы ЛЭП — мелкие значки: на снимке общая обводка выглядит вокруг
# них слишком широкой, им хватает половины.
POWER_POLE_LAYERS = ("power tower", "power pole")
POWER_POLE_GLOW_SCALE = 0.5

SETTINGS_KEY = "basemap_source"
DEFAULT_SOURCE = "google"


@dataclass(frozen=True)
class BasemapSource:
    """Описание растровой подложки."""

    key: str
    name: str
    template: str
    zmax: int
    attribution: str

    @property
    def uri(self):
        """URI слоя тайлов.

        Внутренние ``&`` адреса кодируются: иначе разбор URI разорвал бы
        шаблон Google на отдельные параметры.
        """
        url = self.template.replace("&", "%26")
        return f"type=xyz&url={url}&zmax={self.zmax}&zmin=0"


SOURCES = (
    BasemapSource(
        "google",
        "Google sat",
        "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        20,
        '© <a href="https://www.google.com">Google</a>',
    ),
    BasemapSource(
        "bing",
        "Bing sat",
        "https://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1",
        19,
        '© <a href="https://www.bing.com/maps">Microsoft Bing</a>',
    ),
    BasemapSource(
        "arcgis",
        "ArcGIS sat",
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        19,
        '© <a href="https://www.esri.com">Esri</a>, Maxar, Earthstar '
        "Geographics, and the GIS User Community",
    ),
    BasemapSource(
        "osm",
        "OSM",
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        19,
        '© <a href="https://www.openstreetmap.org/copyright">'
        "OpenStreetMap</a> contributors",
    ),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}


def source(key):
    """Подложка по ключу или None."""
    return SOURCE_BY_KEY.get(key)


def settings():
    """Настройки плагина (общие для всех его частей)."""
    from . import i18n

    return i18n.settings()


def last_source(settings_object=None):
    """Последняя выбранная подложка; проект может быть и без неё."""
    try:
        value = (settings_object or settings()).value(
            SETTINGS_KEY, DEFAULT_SOURCE
        )
    except Exception:
        value = DEFAULT_SOURCE
    return value if value in SOURCE_BY_KEY else DEFAULT_SOURCE


def set_last_source(key, save=True):
    """Запоминает выбранную подложку для следующего включения."""
    key = key if key in SOURCE_BY_KEY else DEFAULT_SOURCE
    if save:
        try:
            settings().setValue(SETTINGS_KEY, key)
        except Exception:
            pass
    return key


def active_layers(project=None):
    """Слои подложки, добавленные плагином."""
    project = project or QgsProject.instance()
    return [
        layer for layer in project.mapLayers().values()
        if layer.customProperty(SOURCE_PROPERTY)
    ]


def active_layer(project=None):
    layers = active_layers(project)
    return layers[0] if layers else None


def active_source(project=None):
    layer = active_layer(project)
    if layer is None:
        return None
    return source(layer.customProperty(SOURCE_PROPERTY))


def area_layers(project=None):
    """Площадные слои, которым подложка оставляет только подписи."""
    project = project or QgsProject.instance()
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.name() in EXCLUDED_LAYERS:
            continue
        if layer.geometryType() != Qgis.GeometryType.Polygon:
            continue
        yield layer


def styled_layers(project=None):
    """Слои, у которых подложка убирает символы, оставляя подписи.

    Кроме площадных это отдельные слои без подписей: их символ на
    снимке не нужен.
    """
    project = project or QgsProject.instance()
    seen = set()
    for layer in area_layers(project):
        seen.add(layer.id())
        yield layer
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.name() not in HIDDEN_SYMBOL_LAYERS:
            continue
        if layer.id() in seen:
            continue
        yield layer


def _is_admin_layer(layer):
    """Слой административной границы или её подписей."""
    return layer.name().startswith(ADMIN_LAYER_PREFIX)


def _layer_node(project, layer):
    return project.layerTreeRoot().findLayer(layer.id())


def _layer_visible(project, layer):
    node = _layer_node(project, layer)
    return bool(node is not None and node.itemVisibilityChecked())


def _set_layer_visible(project, layer, visible):
    node = _layer_node(project, layer)
    if node is not None:
        node.setItemVisibilityChecked(bool(visible))


def _hide_osm(project):
    """Скрывает старую растровую подложку OSM, запоминая её видимость."""
    flags = []
    for layer in project.mapLayersByName(OSM_LAYER_NAME):
        flags.append("1" if _layer_visible(project, layer) else "0")
        _set_layer_visible(project, layer, False)
    return ",".join(flags)


def _restore_osm(project, flags):
    """Возвращает видимость OSM, которую запомнили при включении."""
    if not flags:
        return
    layers = project.mapLayersByName(OSM_LAYER_NAME)
    for layer, flag in zip(layers, flags.split(",")):
        _set_layer_visible(project, layer, flag == "1")


def _renderer_style(layer):
    """Стиль только с символьным оформлением, без подписей.

    Подложка меняет у слоя лишь отрисовщик, поэтому полный вид можно
    хранить без подписей. Смена стиля через менеджер разбирает весь
    XML слоя; подписи в нём занимают основное время, и на десятках
    площадных слоёв включение подложки затягивалось на секунды.
    """
    document = QDomDocument()
    error = layer.exportNamedStyle(
        document, QgsReadWriteContext(), QgsMapLayer.Symbology
    )
    if error:
        return None
    return QgsMapLayerStyle(document.toString(0))


def _replace_style(manager, name, style):
    """Кладёт стиль под именем name, заменяя прежний."""
    if style is None or not style.isValid():
        return False
    if name == manager.currentStyle():
        return False
    if name in manager.styles():
        manager.removeStyle(name)
    return manager.addStyle(name, style)


def _rename_current_style(manager, name):
    """Переименовывает текущий стиль, не применяя его.

    ``setCurrentStyle`` перед переключением заново выгружает весь стиль
    слоя и загружает новый — это и есть основная задержка. Переименование
    меняет только имя, а отрисовщик подложка подменяет сама.
    """
    current = manager.currentStyle()
    if current == name:
        return True
    if not current or current not in manager.styles():
        return False
    if name in manager.styles():
        manager.removeStyle(name)
    return manager.renameStyle(current, name)


def _apply_labels_only(project):
    """Переключает слои на стиль без символов, но с подписями."""
    for layer in styled_layers(project):
        renderer = layer.renderer()
        if renderer is None or renderer.type() == "nullSymbol":
            continue
        manager = layer.styleManager()
        style = _renderer_style(layer)
        if style is None:
            continue
        current = manager.currentStyle()
        if current == FULL_STYLE:
            # Текущий стиль хранится в менеджере только именем: сначала
            # уводим имя на «только подписи», затем сохраняем полный вид.
            _rename_current_style(manager, LABELS_STYLE)
            _replace_style(manager, FULL_STYLE, style)
        else:
            _replace_style(manager, FULL_STYLE, style)
            if current != LABELS_STYLE:
                user_style = current
                _rename_current_style(manager, LABELS_STYLE)
                # Именованный стиль пользователя остаётся в списке, но
                # без подписей: подложка их не меняет.
                if (user_style and user_style != FULL_STYLE
                        and user_style not in manager.styles()):
                    manager.addStyle(user_style, style)
        layer.setRenderer(QgsNullSymbolRenderer())


def _restore_full(project):
    """Возвращает слоям обычный стиль."""
    for layer in styled_layers(project):
        manager = layer.styleManager()
        if manager.currentStyle() == FULL_STYLE:
            continue
        if FULL_STYLE not in manager.styles():
            continue
        style = manager.style(FULL_STYLE)
        if style is None or not style.isValid():
            continue
        # Категории передаются явно: без них QGIS разбирает и пустой блок
        # подписей, сбрасывая их на слое.
        document = QDomDocument()
        loaded, _error = document.setContent(style.xmlData())[:2]
        if not loaded:
            continue
        _rename_current_style(manager, FULL_STYLE)
        layer.importNamedStyle(document, QgsMapLayer.Symbology)


def _create_layer(basemap):
    layer = QgsRasterLayer(basemap.uri, basemap.name, "wms")
    if not layer.isValid():
        return None
    layer.setCustomProperty(SOURCE_PROPERTY, basemap.key)
    return layer


def apply(key, project=None):
    """Включает подложку и убирает полигоны, оставляя подписи.

    Возвращает слой подложки или None, если слой создать не удалось.
    """
    project = project or QgsProject.instance()
    basemap = source(key)
    if basemap is None:
        return None
    existing = active_layers(project)
    if (len(existing) == 1
            and existing[0].customProperty(SOURCE_PROPERTY) == basemap.key):
        _apply_labels_only(project)
        return existing[0]
    osm_flags = ""
    inversion = ""
    hillshade = ""
    slope = ""
    if existing:
        osm_flags = existing[0].customProperty(
            OSM_VISIBLE_PROPERTY
        ) or ""
        inversion = str(
            existing[0].customProperty(LABEL_INVERSION_PROPERTY) or ""
        )
        hillshade = str(
            existing[0].customProperty(HILLSHADE_PROPERTY) or ""
        )
        slope = str(existing[0].customProperty(SLOPE_PROPERTY) or "")
        for layer in existing:
            project.removeMapLayer(layer.id())
    layer = _create_layer(basemap)
    if layer is None:
        _restore_full(project)
        _restore_osm(project, osm_flags)
        return None
    if not osm_flags:
        osm_flags = _hide_osm(project)
    layer.setCustomProperty(OSM_VISIBLE_PROPERTY, osm_flags)
    layer.setCustomProperty(LABEL_INVERSION_PROPERTY, inversion)
    layer.setCustomProperty(HILLSHADE_PROPERTY, hillshade)
    layer.setCustomProperty(SLOPE_PROPERTY, slope)
    project.addMapLayer(layer, False)
    project.layerTreeRoot().addLayer(layer)
    _apply_labels_only(project)
    return layer


def remove(project=None):
    """Выключает подложку и возвращает обычные стили слоёв."""
    project = project or QgsProject.instance()
    layers = active_layers(project)
    if not layers:
        _restore_full(project)
        return False
    osm_flags = layers[0].customProperty(OSM_VISIBLE_PROPERTY) or ""
    for layer in layers:
        project.removeMapLayer(layer.id())
    _restore_osm(project, osm_flags)
    _restore_full(project)
    return True


def sync(project=None):
    """Приводит стили открытого проекта в соответствие с подложкой.

    Нужен для слоёв, добавленных после включения подложки.
    """
    project = project or QgsProject.instance()
    basemap = active_source(project)
    if basemap is None:
        return None
    _apply_labels_only(project)
    return basemap


def _label_pairs(layer):
    """Пары «ключ правила, настройки» для правил, которые инверсия меняет.

    Настройки — копии: иначе Python удалил бы внутренний объект правила
    и стиль слоя испортился бы (см. `core/label_style.py`).

    Пропускаются надписи дорог и площадных дорог: тип объекта
    определяется по тегам (см. `core/label_tags.py`), а не по цвету
    надписи.
    """
    labeling = layer.labeling()
    filters = label_tags.rule_filters(labeling)
    layer_item = label_tags.layer_profile(layer)
    pairs = []
    for key, settings in label_style.label_settings(labeling):
        if settings is None:
            continue
        _text_color, _halo_color, halo_enabled = label_style.label_colors(
            settings
        )
        if not halo_enabled:
            continue
        rule = label_tags.profile(filters.get(key, ""))
        if label_tags.excluded(label_tags.combine(rule, layer_item)):
            continue
        pairs.append((key, settings))
    return pairs


def inverted_label_colors(text_color, halo_color):
    """Цвета текста и halo, если поменять их местами.

    Текст становится светлее прежнего halo — вплоть до белого, halo
    принимает прежний цвет текста.  Прозрачность не трогается: у текста
    и halo остаются собственные значения alpha.
    """
    text = QColor(text_color)
    halo = QColor(halo_color)
    if not text.isValid() or not halo.isValid():
        return None
    new_text = halo.lighter(150)
    new_text.setAlpha(text.alpha())
    new_halo = QColor(text)
    new_halo.setAlpha(halo.alpha())
    return new_text, new_halo


def _repaint(layer):
    layer.triggerRepaint()
    try:
        layer.emitStyleChanged()
    except Exception:
        pass


def _saved_colors(layer):
    """Сохранённые прежние цвета слоя.

    Элемент — «ключ, текст, halo, состояние свечения буфера».  Четвёртого
    элемента нет в проектах старых версий: там свечение не выключалось.
    """
    raw = layer.customProperty(LABEL_COLORS_PROPERTY)
    if not raw:
        return []
    try:
        saved = json.loads(str(raw))
    except ValueError:
        return []
    return [item for item in saved if len(item) in (3, 4)]


def _apply_saved_colors(layer, saved):
    """Возвращает слою сохранённые цвета текста, halo и свечение буфера."""
    labeling = layer.labeling()
    for item in saved:
        key, text_color, halo_color = item[:3]
        effect_enabled = item[3] if len(item) > 3 else None
        settings = label_style.settings_by_key(labeling, key)
        if settings is None:
            continue
        label_style.set_label_colors(
            settings,
            text_color=QColor(text_color),
            halo_color=QColor(halo_color),
        )
        if effect_enabled is not None:
            label_style.set_label_buffer_effect(
                settings, bool(effect_enabled)
            )
        label_style.set_label_settings(labeling, key, settings)


def invert_labels(project=None):
    """Меняет местами цвет надписей и цвет их halo.

    Внешнее свечение буфера на подложке выключается: после обмена
    цветов оно осталось бы светлым и рисовало вторую обводку.  Прежние
    цвета и состояние свечения сохраняются в свойстве слоя: по ним
    надписи возвращаются в исходный вид.  Перед обменом прежние цвета
    возвращаются, поэтому все слои получают одинаковую картину,
    даже если стиль слоя трогали стороной.  Надписи дорог с широкой
    полосой и площадных дорог не меняются (см. `core/label_tags.py`).
    """
    project = project or QgsProject.instance()
    # Прежние цвета возвращаются: слой, стиль которого поменяли
    # в стороне, иначе остался бы без инверсии.
    restored = set()
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        saved = _saved_colors(layer)
        if not saved:
            continue
        _apply_saved_colors(layer, saved)
        layer.removeCustomProperty(LABEL_COLORS_PROPERTY)
        restored.add(layer.id())
    changed = False
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if _is_admin_layer(layer):
            continue
        labeling = layer.labeling()
        saved = []
        for key, settings in _label_pairs(layer):
            text_color, halo_color, _halo_enabled = label_style.label_colors(
                settings
            )
            inverted = inverted_label_colors(text_color, halo_color)
            if inverted is None:
                continue
            new_text, new_halo = inverted
            # Внешнее свечение буфера не инвертируется: после смены
            # цвета halo оно осталось бы светлым и рисовало вторую
            # обводку. На снимке оно лишнее — выключаем до возврата.
            effect_enabled = label_style.label_buffer_effect(settings)
            if effect_enabled is not None:
                label_style.set_label_buffer_effect(settings, False)
            saved.append([
                key,
                text_color.name(QColor.HexArgb),
                halo_color.name(QColor.HexArgb),
                None if effect_enabled is None else int(effect_enabled),
            ])
            label_style.set_label_colors(
                settings, text_color=new_text, halo_color=new_halo
            )
            label_style.set_label_settings(labeling, key, settings)
        if not saved:
            # Инвертировать нечего: прежние цвета возвращены,
            # слой нужно перерисовать.
            if layer.id() in restored:
                _repaint(layer)
            continue
        layer.setCustomProperty(LABEL_COLORS_PROPERTY, json.dumps(saved))
        _repaint(layer)
        changed = True
    return changed


def restore_labels(project=None):
    """Возвращает надписям прежние цвета текста и halo."""
    project = project or QgsProject.instance()
    changed = False
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        saved = _saved_colors(layer)
        if not saved:
            continue
        _apply_saved_colors(layer, saved)
        layer.removeCustomProperty(LABEL_COLORS_PROPERTY)
        _repaint(layer)
        changed = True
    return changed


def _sub_symbol(layer):
    """Вложенный символ маркерной линии или None."""
    try:
        return layer.subSymbol()
    except (AttributeError, TypeError):
        return None


def _iter_symbol_layers(symbol, prefix=""):
    """Слои символа с путём из индексов, включая вложенные символы."""
    for index in range(symbol.symbolLayerCount()):
        layer = symbol.symbolLayer(index)
        # Символ внутри MaskMarker — маска для подписей, а не значок.
        if isinstance(layer, QgsMaskMarkerSymbolLayer):
            continue
        key = f"{prefix}{index}"
        yield key, layer
        sub = _sub_symbol(layer)
        if sub is not None:
            yield from _iter_symbol_layers(sub, key + "/")


def _iter_rule_symbols(rule):
    """Пары «ключ правила, символ» для правила и его потомков."""
    symbol = rule.symbol()
    if symbol is not None:
        yield f"rule:{rule.ruleKey()}", symbol
    for child in rule.children():
        yield from _iter_rule_symbols(child)


def _iter_renderer_symbols(renderer, prefix=""):
    """Пары «ключ, символ» отрисовщика с префиксом вложенности."""
    if renderer is None:
        return
    if isinstance(renderer, QgsRuleBasedRenderer):
        root = renderer.rootRule()
        if root is not None:
            for key, symbol in _iter_rule_symbols(root):
                yield prefix + key, symbol
    elif isinstance(renderer, QgsSingleSymbolRenderer):
        symbol = renderer.symbol()
        if symbol is not None:
            yield prefix + "single", symbol
    elif isinstance(renderer, QgsPointClusterRenderer):
        # Символ самой группы не трогаем: это счётчик с подписью.
        embedded = renderer.embeddedRenderer()
        if embedded is not None and embedded is not renderer:
            yield from _iter_renderer_symbols(embedded, prefix + "embedded/")


def _layer_symbols(layer):
    """Пары «ключ, символ» отрисовщика слоя.

    Ключ не зависит от объектов Python: он собран из ключей правил и
    пути вложенности, поэтому по нему символы находятся и после
    перезагрузки проекта.
    """
    yield from _iter_renderer_symbols(layer.renderer())


def _symbol_layers_by_key(layer):
    """Слои символов слоя по ключу «символ/путь вложенности»."""
    result = {}
    for symbol_key, symbol in _layer_symbols(layer):
        for path, symbol_layer in _iter_symbol_layers(symbol):
            result[f"{symbol_key}/{path}"] = symbol_layer
    return result


def _saved_symbol_colors(layer):
    """Сохранённые цвета слоёв символов: список «ключ, цвет, обводка»."""
    raw = layer.customProperty(SYMBOL_COLORS_PROPERTY)
    if not raw:
        return []
    try:
        saved = json.loads(str(raw))
    except ValueError:
        return []
    return [item for item in saved if len(item) == 3]


def _saved_icon_effects(layer):
    """Сохранённые значки: список «ключ, прежний путь или None».

    Прежний путь остаётся в проектах старых версий: там значкам со
    светлой заливкой подставлялась перекрашенная копия SVG.
    """
    raw = layer.customProperty(ICON_EFFECTS_PROPERTY)
    if not raw:
        return []
    try:
        saved = json.loads(str(raw))
    except ValueError:
        return []
    result = []
    for item in saved:
        if isinstance(item, str):
            # Формат до появления перекрашенных копий: только ключ.
            result.append((item, None))
        elif (isinstance(item, list) and len(item) == 2
                and isinstance(item[0], str)):
            result.append((item[0], item[1]))
    return result


def _inverted_line_color(layer_name, symbol_layer):
    """Цвет линии или маркера слоя на снимке."""
    mode = INVERTED_SYMBOL_LAYERS[layer_name]
    if mode == "aerialway":
        if (isinstance(symbol_layer, QgsLineSymbolLayer)
                and symbol_layer.width() >= 0.5):
            return QColor(AERIALWAY_CASING_COLOR)
        return QColor(AERIALWAY_LINE_COLOR)
    if mode == "ridge":
        return QColor(RIDGE_COLOR)
    if mode == "power":
        return QColor(POWER_COLOR)
    if mode == "rail":
        return QColor(RAIL_COLOR)
    return QColor(ROAD_COLOR)


def _apply_saved_symbol_colors(layer, saved):
    """Возвращает слоям символов сохранённые цвета."""
    keyed = _symbol_layers_by_key(layer)
    for key, color_name, stroke_name in saved:
        symbol_layer = keyed.get(key)
        if symbol_layer is None:
            continue
        color = QColor(color_name)
        if isinstance(symbol_layer, QgsSimpleMarkerSymbolLayer):
            symbol_layer.setColor(color)
            if stroke_name is not None:
                symbol_layer.setStrokeColor(QColor(stroke_name))
        elif isinstance(symbol_layer, QgsLineSymbolLayer):
            symbol_layer.setColor(color)


def _restore_icon_effects(layer, saved):
    """Снимает paint effect и возвращает значкам прежний путь.

    Путь возвращается проектам старых версий, где значку подставлялась
    перекрашенная копия SVG.
    """
    keyed = _symbol_layers_by_key(layer)
    changed = False
    for key, original_path in saved:
        symbol_layer = keyed.get(key)
        if symbol_layer is None:
            continue
        if (original_path is not None
                and isinstance(symbol_layer, QgsSvgMarkerSymbolLayer)):
            symbol_layer.setPath(original_path)
            changed = True
        if symbol_layer.paintEffect() is not None:
            symbol_layer.setPaintEffect(None)
            changed = True
    return changed


def _icon_glow(scale=1.0):
    """Узкая белая обводка значка; ``scale`` уменьшает её для мелких."""
    glow = QgsOuterGlowEffect()
    glow.setColor(QColor(*ICON_GLOW_COLOR))
    glow.setSpread(ICON_GLOW_SPREAD_MM * scale)
    glow.setBlurLevel(ICON_GLOW_BLUR_MM * scale)
    return glow


def _icon_effect(glow_scale=1.0):
    """Белая обводка под значком; сам значок не меняется."""
    effect = QgsEffectStack()
    # Стек рисует эффекты в порядке добавления: сначала обводку,
    # затем исходный значок поверх неё, поэтому рисунок не закрыт.
    effect.appendEffect(_icon_glow(glow_scale))
    source = QgsDrawSourceEffect()
    source.setOpacity(1.0)
    effect.appendEffect(source)
    return effect


def _svg_rgb(value):
    """RGB из цвета ``#rgb``/``#rrggbb``/``#rrggbbaa``."""
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    digits = digits[:6]
    if len(digits) != 6:
        return None
    try:
        return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None


def _drawing_colors(text, properties=("fill", "stroke")):
    """Цвета свойств SVG, которые он действительно использует."""
    names = "|".join(properties)
    colors = set()
    class_colors = {}
    for block in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S):
        for name, body in re.findall(
            r"\.([A-Za-z0-9_-]+)\s*\{([^}]*)\}", block
        ):
            values = re.findall(
                rf"(?:{names})\s*:\s*(#[0-9a-fA-F]{{3,8}})", body
            )
            if values:
                class_colors.setdefault(name, set()).update(values)
    for used in re.findall(r'class="([^"]*)"', text):
        for name in used.split():
            colors.update(class_colors.get(name, ()))
    for style in re.findall(r'style="([^"]*)"', text):
        colors.update(re.findall(
            rf"(?:{names})\s*:\s*(#[0-9a-fA-F]{{3,8}})", style
        ))
    colors.update(re.findall(
        rf'\b(?:{names})="(#[0-9a-fA-F]{{3,8}})"', text
    ))
    return colors


@lru_cache(maxsize=None)
def _svg_is_black(path):
    """True, если в SVG нет цветных элементов, кроме чёрных и серых."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    dark = False
    for value in _drawing_colors(text):
        rgb = _svg_rgb(value)
        if rgb is None:
            continue
        if max(rgb) - min(rgb) > 24 and max(rgb) > 40:
            return False
        if max(rgb) < 120:
            dark = True
    return dark


def _icon_source(path):
    """Файл значка в ресурсах плагина по пути из стиля слоя."""
    return Path(resource_path("icons")) / Path(path).name


def _is_black_icon(path):
    """Чёрно-серый значок из ресурсов плагина."""
    return _svg_is_black(str(_icon_source(path)))


def _is_black_marker(symbol_layer):
    """Чёрный маркер без цветной заливки и обводки."""
    color = symbol_layer.color()
    if color.alpha() == 0:
        return False
    channels = (color.red(), color.green(), color.blue())
    if max(channels) > 80 or max(channels) - min(channels) > 24:
        return False
    stroke = symbol_layer.strokeColor()
    if stroke.alpha() > 0:
        stroke_channels = (stroke.red(), stroke.green(), stroke.blue())
        if (max(stroke_channels) - min(stroke_channels) > 24
                and max(stroke_channels) > 40):
            return False
    return True


def _invert_layer_symbols(layer):
    """Перекрашивает линии и маркеры одного слоя."""
    saved = []
    for key, symbol_layer in _symbol_layers_by_key(layer).items():
        if not isinstance(
            symbol_layer,
            (QgsSimpleLineSymbolLayer, QgsHashedLineSymbolLayer,
             QgsSimpleMarkerSymbolLayer),
        ):
            continue
        target = _inverted_line_color(layer.name(), symbol_layer)
        color = symbol_layer.color()
        target.setAlpha(color.alpha())
        stroke = symbol_layer.strokeColor()
        saved.append([key, color.name(QColor.HexArgb),
                      stroke.name(QColor.HexArgb)])
        symbol_layer.setColor(target)
        if isinstance(symbol_layer, QgsSimpleMarkerSymbolLayer):
            if stroke.alpha() > 0:
                target.setAlpha(stroke.alpha())
                symbol_layer.setStrokeColor(target)
    if not saved:
        return False
    layer.setCustomProperty(SYMBOL_COLORS_PROPERTY, json.dumps(saved))
    _repaint(layer)
    return True


def _outline_layer_icons(layer):
    """Добавляет чёрным значкам слоя плотную белую обводку."""
    point_layer = layer.geometryType() == Qgis.GeometryType.Point
    glow_scale = (
        POWER_POLE_GLOW_SCALE if layer.name() in POWER_POLE_LAYERS else 1.0
    )
    saved = []
    for key, symbol_layer in _symbol_layers_by_key(layer).items():
        if isinstance(symbol_layer, QgsSvgMarkerSymbolLayer):
            if not _is_black_icon(symbol_layer.path()):
                continue
        elif (
            isinstance(symbol_layer, QgsSimpleMarkerSymbolLayer)
            and point_layer
        ):
            if not _is_black_marker(symbol_layer):
                continue
        else:
            continue
        effect = symbol_layer.paintEffect()
        if effect is not None and effect.enabled():
            # Включённый чужой эффект не затираем: значок останется как есть.
            continue
        saved.append([key, None])
        symbol_layer.setPaintEffect(_icon_effect(glow_scale))
    if not saved:
        return False
    layer.setCustomProperty(ICON_EFFECTS_PROPERTY, json.dumps(saved))
    _repaint(layer)
    return True


def invert_symbols(project=None):
    """Перекрашивает дороги, скалы, ЛЭП, подъёмники и железные дороги.

    Заодно обводит чёрные значки.

    Прежние цвета и наложенные эффекты хранятся в свойствах слоя: по
    ним символы возвращаются в исходный вид.  Перед перекраской прежние
    цвета возвращаются, поэтому все слои получают одинаковую картину,
    даже если стиль слоя трогали стороной.
    """
    project = project or QgsProject.instance()
    restore_symbols(project)
    changed = False
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if _is_admin_layer(layer):
            continue
        if layer.name() in INVERTED_SYMBOL_LAYERS:
            changed = _invert_layer_symbols(layer) or changed
        changed = _outline_layer_icons(layer) or changed
    return changed


def restore_symbols(project=None):
    """Возвращает линиям и значкам прежние цвета."""
    project = project or QgsProject.instance()
    changed = False
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        restored = False
        saved = _saved_symbol_colors(layer)
        if saved:
            _apply_saved_symbol_colors(layer, saved)
            layer.removeCustomProperty(SYMBOL_COLORS_PROPERTY)
            restored = True
        keys = _saved_icon_effects(layer)
        if keys:
            _restore_icon_effects(layer, keys)
            layer.removeCustomProperty(ICON_EFFECTS_PROPERTY)
            restored = True
        if restored:
            _repaint(layer)
            changed = True
    return changed


def label_inversion_enabled(project=None):
    """Включена ли инверсия надписей и символов."""
    layer = active_layer(project)
    if layer is None:
        return False
    return str(layer.customProperty(LABEL_INVERSION_PROPERTY)) == "1"


def set_label_inversion(enabled, project=None):
    """Запоминает состояние инверсии в слое подложки.

    Состояние хранится в проекте, поэтому после открытия файла галка
    останется в том же виде, в каком её оставили.
    """
    layer = active_layer(project)
    if layer is None:
        return False
    layer.setCustomProperty(
        LABEL_INVERSION_PROPERTY, "1" if enabled else "0"
    )
    return True


def hillshade_state(project=None):
    """Была ли отмывка включена до подложки.

    Подложка отмывку выключает, а после её выключения отмывка
    возвращается в прежний вид.  Состояние хранится в слое подложки,
    поэтому оно переживает и смену источника, и перезагрузку проекта.
    """
    layer = active_layer(project)
    if layer is None:
        return False
    return str(layer.customProperty(HILLSHADE_PROPERTY)) == "1"


def set_hillshade_state(enabled, project=None):
    """Запоминает состояние отмывки в слое подложки."""
    layer = active_layer(project)
    if layer is None:
        return False
    layer.setCustomProperty(HILLSHADE_PROPERTY, "1" if enabled else "0")
    return True


def slope_state(project=None):
    """Была ли раскраска уклонов включена до подложки.

    Возвращает пару (крутые склоны, равнинные участки).  Подложка
    выключает обе раскраски, а после её выключения они возвращаются
    в прежний вид.  Состояние хранится в слое подложки, поэтому
    переживает смену источника и перезагрузку проекта.
    """
    layer = active_layer(project)
    if layer is None:
        return False, False
    raw = str(layer.customProperty(SLOPE_PROPERTY) or "")
    return raw[:1] == "1", raw[1:2] == "1"


def set_slope_state(steep, slight, project=None):
    """Запоминает состояние раскраски уклонов в слое подложки."""
    layer = active_layer(project)
    if layer is None:
        return False
    layer.setCustomProperty(
        SLOPE_PROPERTY,
        ("1" if steep else "0") + ("1" if slight else "0"),
    )
    return True
