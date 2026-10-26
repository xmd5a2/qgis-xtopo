def classFactory(iface):
    from .plugin import QgisXtopoPlugin

    return QgisXtopoPlugin(iface)
