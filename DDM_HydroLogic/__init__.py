# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""QGIS plugin entry point."""


class _DDMHydroLogicLoadFailurePlugin:
    """Minimal plugin object returned when startup dependencies are missing."""

    def __init__(self, iface, message):
        self.iface = iface
        self.message = message

    def initGui(self):  # pylint: disable=invalid-name
        try:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(self.iface.mainWindow(), "DDM HydroLogic could not start", self.message)
        except Exception:
            # If Qt itself is unavailable, QGIS will still shows the original Python console traceback.
            pass

    def unload(self):
        pass


def _friendly_startup_error(exc):
    name = getattr(exc, "name", None) or ""
    filename = getattr(exc, "filename", None) or ""
    detail = str(exc)
    missing = name or filename or detail
    return (
        "DDM HydroLogic could not be loaded because a required Python module or plugin file is missing.\n\n"
        f"Reported issue: {missing}\n\n"
        "What to do:\n"
        "1. Delete the existing DDM_HydroLogic plugin folder from your QGIS profile.\n"
        "2. Reinstall the latest DDM HydroLogic ZIP using Plugins > Manage and Install Plugins > Install from ZIP.\n"
        "3. If the missing item is a third-party Python package such as numpy, install or repair the QGIS/OSGeo4W Python package that provides it.\n"
        "4. Restart QGIS after reinstalling.\n\n"
        f"Technical detail: {detail}"
    )


def classFactory(iface):  # pylint: disable=invalid-name
    """Load DDM HydroLogic plugin."""
    try:
        from .plugin import DDMHydroLogicPlugin
        return DDMHydroLogicPlugin(iface)
    except (ModuleNotFoundError, ImportError, FileNotFoundError) as exc:
        return _DDMHydroLogicLoadFailurePlugin(iface, _friendly_startup_error(exc))
