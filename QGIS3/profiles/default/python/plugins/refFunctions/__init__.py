# -*- coding: utf-8 -*-
"""
/***************************************************************************
 multiPrint
                                 A QGIS plugin
 print multiple print composer views
                             -------------------
        begin                : 2014-06-24
        copyright            : (C) 2014 by enrico ferreguti
        email                : enricofer@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""

def classFactory(iface):
    # load multiPrint class from file multiPrint
    from .reffunctions import refFunctions
    return refFunctions(iface)
