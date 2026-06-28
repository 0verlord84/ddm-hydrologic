# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Custom map tools for the interactive DDM HydroLogic plugin."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.PyQt.QtWidgets import QToolTip
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import QgsPointXY, QgsWkbTypes

from .compat import enum_member, qt_enum


class DrawOutletLineTool(QgsMapTool):
    """Captures a user-drawn line: left click adds vertices, right click finishes."""

    lineFinished = pyqtSignal(list)
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, enum_member(QgsWkbTypes, "GeometryType", "LineGeometry"))
        self.rubber_band.setColor(QColor(220, 0, 0, 235))
        self.rubber_band.setWidth(5)
        self.setCursor(QCursor(qt_enum(Qt, "CursorShape", "CrossCursor")))

    def activate(self):
        super().activate()
        self.reset()

    def deactivate(self):
        self.reset()
        super().deactivate()

    def reset(self):
        self.points = []
        self.rubber_band.reset(enum_member(QgsWkbTypes, "GeometryType", "LineGeometry"))

    def canvasPressEvent(self, event):  # pylint: disable=invalid-name
        point = QgsPointXY(event.mapPoint())
        if event.button() == qt_enum(Qt, "MouseButton", "LeftButton"):
            self.points.append(point)
            self.rubber_band.addPoint(point, True)
        elif event.button() == qt_enum(Qt, "MouseButton", "RightButton"):
            if len(self.points) >= 2:
                self.lineFinished.emit(list(self.points))
            else:
                self.cancelled.emit()
            self.reset()

    def canvasMoveEvent(self, event):  # pylint: disable=invalid-name
        if not self.points:
            return
        self.rubber_band.reset(enum_member(QgsWkbTypes, "GeometryType", "LineGeometry"))
        for point in self.points:
            self.rubber_band.addPoint(point, False)
        self.rubber_band.addPoint(QgsPointXY(event.mapPoint()), True)

    def keyPressEvent(self, event):  # pylint: disable=invalid-name
        if event.key() == qt_enum(Qt, "Key", "Key_Escape"):
            self.cancelled.emit()
            self.reset()


class DrawMaskPolygonTool(QgsMapTool):
    """Captures a user-drawn polygon: left click adds vertices, right click finishes."""

    polygonFinished = pyqtSignal(list)
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
        if hasattr(self.rubber_band, "setStrokeColor"):
            self.rubber_band.setStrokeColor(QColor(0, 180, 255, 235))
        else:
            self.rubber_band.setColor(QColor(0, 180, 255, 235))
        if hasattr(self.rubber_band, "setFillColor"):
            self.rubber_band.setFillColor(QColor(0, 180, 255, 55))
        self.rubber_band.setWidth(2)
        self.hover_tip = "Right-click to finish"
        self._finish_tip_enabled = False
        self.setCursor(QCursor(qt_enum(Qt, "CursorShape", "CrossCursor")))

    def activate(self):
        super().activate()
        self._finish_tip_enabled = True
        self.reset()

    def deactivate(self):
        self._finish_tip_enabled = False
        QToolTip.hideText()
        self.reset()
        super().deactivate()

    def reset(self):
        self.points = []
        self.rubber_band.reset(enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))

    def _redraw(self, moving_point=None):
        self.rubber_band.reset(enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
        pts = list(self.points)
        if moving_point is not None:
            pts.append(QgsPointXY(moving_point))
        if not pts:
            return
        for point in pts:
            self.rubber_band.addPoint(QgsPointXY(point), False)
        if len(pts) >= 2:
            self.rubber_band.addPoint(QgsPointXY(pts[0]), True)
        else:
            self.rubber_band.addPoint(QgsPointXY(pts[0]), True)

    def canvasPressEvent(self, event):  # pylint: disable=invalid-name
        point = QgsPointXY(event.mapPoint())
        if event.button() == qt_enum(Qt, "MouseButton", "LeftButton"):
            self.points.append(point)
            self._redraw()
        elif event.button() == qt_enum(Qt, "MouseButton", "RightButton"):
            self._finish_tip_enabled = False
            QToolTip.hideText()
            if len(self.points) >= 3:
                self.polygonFinished.emit(list(self.points))
            else:
                self.cancelled.emit()
            self.reset()

    def canvasMoveEvent(self, event):  # pylint: disable=invalid-name
        if self._finish_tip_enabled:
            try:
                QToolTip.showText(self.canvas.mapToGlobal(event.pos()), self.hover_tip, self.canvas)
            except Exception:
                pass
        if not self.points:
            return
        self._redraw(QgsPointXY(event.mapPoint()))

    def keyPressEvent(self, event):  # pylint: disable=invalid-name
        if event.key() == qt_enum(Qt, "Key", "Key_Escape"):
            self._finish_tip_enabled = False
            QToolTip.hideText()
            self.cancelled.emit()
            self.reset()
