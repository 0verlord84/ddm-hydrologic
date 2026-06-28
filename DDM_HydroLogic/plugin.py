# -*- coding: utf-8 -*-
# DDM_HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Plugin bootstrap for DDM HydroLogic."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .compat import qt_enum
from .interactive_dock import DDMHydroLogicDock


class DDMHydroLogicPlugin:
    """Exposes the interactive DDM HydroLogic dock tool."""

    MENU_NAME = "&DDM HydroLogic"

    def __init__(self, iface):
        self.iface = iface
        self.open_dock_action = None
        self.dock = None
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):  # pylint: disable=invalid-name
        """Called by QGIS when the plugin is enabled."""
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))

        self.open_dock_action = QAction(icon, "DDM HydroLogic", self.iface.mainWindow())
        self.open_dock_action.setObjectName("DDMHydroLogicInteractiveAction")
        self.open_dock_action.setWhatsThis("Open the interactive DDM HydroLogic panel.")
        self.open_dock_action.setStatusTip("Open the interactive DDM HydroLogic panel")
        self.open_dock_action.triggered.connect(self.show_dock)

        # The workflow starts from a raster DEM and produces vector outputs, so
        # the tool is offered from both the Raster and Vector menus.
        self.iface.addToolBarIcon(self.open_dock_action)
        self.iface.addPluginToRasterMenu(self.MENU_NAME, self.open_dock_action)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.open_dock_action)

    def unload(self):
        """Called by QGIS when the plugin is disabled."""
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

        if self.open_dock_action is not None:
            self.iface.removePluginRasterMenu(self.MENU_NAME, self.open_dock_action)
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.open_dock_action)
            self.iface.removeToolBarIcon(self.open_dock_action)
            self.open_dock_action.deleteLater()
            self.open_dock_action = None

    def show_dock(self):
        """Opens or raises the interactive DDM HydroLogic dock."""
        if self.dock is None:
            self.dock = DDMHydroLogicDock(self.iface)
            self.iface.addDockWidget(qt_enum(Qt, "DockWidgetArea", "RightDockWidgetArea"), self.dock)
        self.dock.show()
        self.dock.raise_()
