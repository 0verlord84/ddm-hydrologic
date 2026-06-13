# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Compatibility helpers for QGIS 3/PyQt5 and QGIS 4/PyQt6 enum names.

QGIS 4 uses PyQt6-style scoped enums in several places. These helpers allow the
same plugin code to run against older flat enum names and newer scoped names.
"""


def qt_enum(parent, scoped_group_name, member_name):
    """Return a Qt enum member from scoped PyQt6 or flat PyQt5-style names."""
    scoped_group = getattr(parent, scoped_group_name, None)
    if scoped_group is not None and hasattr(scoped_group, member_name):
        return getattr(scoped_group, member_name)
    if hasattr(parent, member_name):
        return getattr(parent, member_name)
    raise AttributeError(f"Could not resolve Qt enum {scoped_group_name}.{member_name}")


def enum_member(parent, scoped_group_name, member_name):
    """Return a generic scoped enum member, falling back to a flat member."""
    scoped_group = getattr(parent, scoped_group_name, None)
    if scoped_group is not None and hasattr(scoped_group, member_name):
        return getattr(scoped_group, member_name)
    if hasattr(parent, member_name):
        return getattr(parent, member_name)
    raise AttributeError(f"Could not resolve enum {scoped_group_name}.{member_name}")
