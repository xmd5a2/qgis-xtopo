"""Растровая подложка готового проекта.

Подложка не участвует в подготовке данных: это слой XYZ, который
добавляется в проект самым нижним.  Пока подложка включена, площадные
слои показывают только подписи: их полный стиль сохраняется в менеджере
стилей слоя, а активным становится стиль с null-рендерером.

Стили ``qgis_xtopo_full`` и ``qgis_xtopo_labels`` остаются в проекте:
это позволяет переключать подложку без повторного снятия копии стиля.

Подложка даёт ещё и инверсию надписей: цвет текста и цвет halo
меняются местами, чтобы подписи читались на снимке.  Прежние цвета
хранятся в свойстве слоя — по ним надписи возвращаются в исходный вид.

Инверсия не трогает надписи дорог, нарисованных широкой полосой, и
надписи площадных дорог: их тип определяется по тегам объекта (см.
`core/label_tags.py`), а не по цвету надписи.
"""

import json
from dataclasses import dataclass

from qgis.core import (
    QgsNullSymbolRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    Qgis,
)
from qgis.PyQt.QtGui import QColor

from . import label_style, label_tags

SOURCE_PROPERTY = "qgis_xtopo/basemap"
OSM_VISIBLE_PROPERTY = "qgis_xtopo/basemap_osm_visible"
LABEL_INVERSION_PROPERTY = "qgis_xtopo/basemap_label_inversion"
LABEL_COLORS_PROPERTY = "qgis_xtopo/basemap_label_colors"
HILLSHADE_PROPERTY = "qgis_xtopo/basemap_hillshade"
FULL_STYLE = "qgis_xtopo_full"
LABELS_STYLE = "qgis_xtopo_labels"
OSM_LAYER_NAME = "OpenStreetMap"
EXCLUDED_LAYERS = (OSM_LAYER_NAME, "BBOX")

SETTINGS_KEY = "basemap_source"
DEFAULT_SOURCE = "google"


@dataclass(frozen=True)
class BasemapSource:
    """Описание спутниковой подложки."""

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


def _store_style(manager, name, layer):
    """Сохраняет текущий стиль слоя в менеджере стилей под именем name.

    ``manager.style(name)`` возвращает копию сохранённого стиля: правка
    копии ничего не меняет, поэтому готовый стиль заменяется целиком.
    """
    style = manager.style(name)
    style.readFromLayer(layer)
    current = manager.currentStyle()
    if name not in manager.styles():
        manager.addStyle(name, style)
        return
    manager.removeStyle(name)
    manager.addStyle(name, style)
    if current == name:
        # Удаление сбросило текущий стиль на первый в списке: возвращаем
        # обновлённый, иначе слой получит чужой рендерер.
        manager.setCurrentStyle(name)
    elif manager.currentStyle() != current and current in manager.styles():
        manager.setCurrentStyle(current)


def _apply_labels_only(project):
    """Переключает площадные слои на стиль без символов, но с подписями."""
    for layer in area_layers(project):
        renderer = layer.renderer()
        if renderer is None or renderer.type() == "nullSymbol":
            continue
        manager = layer.styleManager()
        if manager.currentStyle() == LABELS_STYLE:
            continue
        if FULL_STYLE in manager.styles():
            if manager.currentStyle() != LABELS_STYLE:
                _store_style(manager, FULL_STYLE, layer)
        else:
            manager.addStyleFromLayer(FULL_STYLE)
        original = layer.renderer().clone()
        layer.setRenderer(QgsNullSymbolRenderer())
        try:
            if LABELS_STYLE in manager.styles():
                _store_style(manager, LABELS_STYLE, layer)
            else:
                manager.addStyleFromLayer(LABELS_STYLE)
        finally:
            if original is not None:
                layer.setRenderer(original)
        manager.setCurrentStyle(LABELS_STYLE)


def _restore_full(project):
    """Возвращает площадным слоям обычный стиль."""
    for layer in area_layers(project):
        manager = layer.styleManager()
        if (FULL_STYLE in manager.styles()
                and manager.currentStyle() == LABELS_STYLE):
            manager.setCurrentStyle(FULL_STYLE)


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

    Пропускаются надписи дорог, нарисованных широкой полосой, и
    площадных дорог: тип объекта определяется по тегам (см.
    `core/label_tags.py`), а не по цвету надписи.
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
    """Сохранённые прежние цвета слоя: список «ключ, текст, halo»."""
    raw = layer.customProperty(LABEL_COLORS_PROPERTY)
    if not raw:
        return []
    try:
        saved = json.loads(str(raw))
    except ValueError:
        return []
    return [item for item in saved if len(item) == 3]


def _apply_saved_colors(layer, saved):
    """Возвращает слою сохранённые цвета текста и halo."""
    labeling = layer.labeling()
    for key, text_color, halo_color in saved:
        settings = label_style.settings_by_key(labeling, key)
        if settings is None:
            continue
        label_style.set_label_colors(
            settings,
            text_color=QColor(text_color),
            halo_color=QColor(halo_color),
        )
        label_style.set_label_settings(labeling, key, settings)


def invert_labels(project=None):
    """Меняет местами цвет надписей и цвет их halo.

    Прежние цвета сохраняются в свойстве слоя: по ним надписи
    возвращаются в исходный вид.  Перед обменом прежние цвета
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
            saved.append([
                key,
                text_color.name(QColor.HexArgb),
                halo_color.name(QColor.HexArgb),
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


def label_inversion_enabled(project=None):
    """Включена ли инверсия надписей."""
    layer = active_layer(project)
    if layer is None:
        return False
    return str(layer.customProperty(LABEL_INVERSION_PROPERTY)) == "1"


def set_label_inversion(enabled, project=None):
    """Запоминает состояние инверсии надписей в слое подложки.

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
