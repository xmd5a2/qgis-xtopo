"""Растровая подложка готового проекта.

Подложка не участвует в подготовке данных: это слой XYZ, который
добавляется в проект самым нижним.  Пока подложка включена, площадные
слои показывают только подписи: их полный стиль сохраняется в менеджере
стилей слоя, а активным становится стиль с null-рендерером.

Стили ``qgis_xtopo_full`` и ``qgis_xtopo_labels`` остаются в проекте:
это позволяет переключать подложку без повторного снятия копии стиля.
"""

from dataclasses import dataclass

from qgis.core import (
    QgsNullSymbolRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    Qgis,
)

SOURCE_PROPERTY = "qgis_xtopo/basemap"
OSM_VISIBLE_PROPERTY = "qgis_xtopo/basemap_osm_visible"
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
    if existing:
        osm_flags = existing[0].customProperty(
            OSM_VISIBLE_PROPERTY
        ) or ""
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
