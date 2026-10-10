# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Helpers shared across the plugin: QGIS 3/PyQt5 and QGIS 4/PyQt6 enum names,
logging of errors that are safe to ignore, and memory checks."""

import os
import sys

# A handful of messages per site is enough to diagnose a problem; anything more
# would flood the log panel when a call fails inside a per-cell loop.
_LOG_LIMIT = 5
_logged = {}

OUT_OF_MEMORY = (
    "QGIS ran out of memory. Draw a mask polygon around the catchment to process a smaller area, "
    "or close other programs, then try again."
)


def log_ignored(context=""):
    """Record a non-fatal error in the QGIS log and carry on.

    For calls that can fail on some QGIS builds without stopping a whole export.
    The detail goes to the log panel so the failure can still be traced. Running
    out of memory is never carried on from: the error is raised again.
    """
    detail = sys.exc_info()[1]
    if isinstance(detail, MemoryError):
        raise detail
    seen = _logged.get(context, 0)
    if seen >= _LOG_LIMIT:
        return
    _logged[context] = seen + 1
    try:
        from qgis.core import QgsMessageLog
        QgsMessageLog.logMessage(f"{context}: {detail}".strip(": "), "DDM HydroLogic")
    except Exception:
        return


def failure_text(what, exc):
    """Message for a failed operation, with plain advice when memory ran out."""
    if isinstance(exc, MemoryError):
        return OUT_OF_MEMORY
    return f"{what}:\n\n{exc}"


def free_memory_bytes():
    """Physical memory available to new work, in bytes, or None where it cannot be read."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys)
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        return None
    except Exception:
        return None


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
