# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Dock widget for the interactive DDM HydroLogic workflow."""

import gc
import os

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QDoubleSpinBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsFillSymbol,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsSingleSymbolRenderer,
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand

from .compat import enum_member, qt_enum
from .hydrology_engine import D8HydrologyEngine, HydrologyBuildError, HydrologyCancelled
from .map_tools import DrawOutletLineTool, DrawMaskPolygonTool
from .rorb_catg_exporter import RorbCatgExportError, write_rorb_catg_from_engine
from .rorb_catg_importer import load_rorb_catg_layers
from .tuflow_exporter import TUFLOW_LAYERS, TuflowExportError, write_tuflow_from_engine
from .wbnm_2025_exporter import Wbnm2025ExportError, write_wbnm_2025_from_engine
from .xprafts_exporter import XpRaftsExportError, write_xprafts_from_engine


def _writer_no_error_code():
    """Returns the vector-writer success code for QGIS 3 or QGIS 4."""
    if hasattr(QgsVectorFileWriter, "NoError"):
        return QgsVectorFileWriter.NoError
    writer_error = getattr(QgsVectorFileWriter, "WriterError", None)
    if writer_error is not None and hasattr(writer_error, "NoError"):
        return writer_error.NoError
    return 0


class DDMHydroLogicDock(QDockWidget):
    """Interactive dock for DEM flow paths and subcatchment preview/export."""

    def __init__(self, iface, parent=None):
        super().__init__("DDM HydroLogic", parent or iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.engine = None
        self.click_tool = None
        self.draw_tool = None
        self.mask_tool = None
        self.mask_layer = None
        self.outlet_cells = []
        self.current_assignments = {}
        self.selected_highlight_cells = set()
        self.selected_catchment_groups = {}
        self.selection_polygon_geom = None
        self.selection_polygon_band = None
        self.selection_polygon_bands = []
        self._catchment_geom_cache = {}
        self.outlet_line_band = None
        self.outlet_line_points = []
        self.outlet_line_layer_geom = None
        self.dem_layer_ids = []
        self.abort_requested = False
        self.active_operation = None

        try:
            QgsProject.instance().layersWillBeRemoved.connect(self._handle_project_layers_will_be_removed)
        except Exception:
            # Signal names/signatures can shift across QGIS/PyQt builds.
            # The layer validity checks below still protect the workflow.
            pass

        self._build_ui()
        self.refresh_dem_layers()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        dem_group = QGroupBox("DEM and flow path processing")
        dem_layout = QFormLayout(dem_group)
        dem_caution = QLabel(
            "CAUTION: the lower the accumulation number, the longer it gets. "
            "Example: for a 5m DEM a value of 10,000 is a good starting point."
        )
        dem_caution.setWordWrap(True)
        dem_layout.addRow(dem_caution)

        self.dem_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh DEM list")
        dem_row = QHBoxLayout()
        dem_row.addWidget(self.dem_combo, 1)
        dem_row.addWidget(self.refresh_btn)
        dem_layout.addRow("1. DEM", dem_row)

        self.min_acc_spin = QSpinBox()
        self.min_acc_spin.setRange(1, 100000000)
        self.min_acc_spin.setValue(10000)
        self.min_acc_spin.setToolTip(
            "Minimum accumulation, in DEM cells, to display as flow-path line features. "
            "Use 1 for every cell-sized path. Larger values are much lighter on QGIS."
        )
        dem_layout.addRow("2. Display flow paths from flow accumulation", self.min_acc_spin)

        self.mask_btn = QPushButton("Create polygon")
        self.mask_btn.setToolTip(
            "Optional. Left-click to digitise a polygon on the map canvas. "
            "The polygon is used as the boundary for all processes."
        )
        dem_layout.addRow("3. Mask layer", self.mask_btn)

        self.build_btn = QPushButton("Compute")
        dem_layout.addRow("4. Create flow paths", self.build_btn)
        layout.addWidget(dem_group)

        interact_group = QGroupBox("Interactive flow path selection")
        interact_layout = QVBoxLayout(interact_group)
        self.flow_selection_hint = QLabel(
            "Left-click on flow path to highlight upstream contributing catchment. "
            "CTRL+left-click to de-select a subcatchment. "
            "Clear temporary highlight/preview to re-start the selection process."
        )
        self.flow_selection_hint.setWordWrap(True)
        interact_layout.addWidget(self.flow_selection_hint)

        self.click_btn = QPushButton("5. Click on flow paths")
        self.clear_btn = QPushButton("Clear temporary highlight/preview")
        click_row = QHBoxLayout()
        click_row.addWidget(self.click_btn, 1)
        click_row.addWidget(self.clear_btn, 1)
        interact_layout.addLayout(click_row)
        layout.addWidget(interact_group)

        sub_group = QGroupBox("Subcatchment processing")
        sub_layout = QFormLayout(sub_group)
        self.draw_subcatchment_hint = QLabel(
            "Left-click to draw a line across a flow path, right-click to finish. "
            "Select the desired minimum subcatchment size and hit Process subcatchments to visualise them."
        )
        self.draw_subcatchment_hint.setWordWrap(True)
        sub_layout.addRow(self.draw_subcatchment_hint)

        self.draw_btn = QPushButton("6. Draw outlet line")
        self.clear_outlet_btn = QPushButton("Clear outlet line")
        outlet_row = QHBoxLayout()
        outlet_row.addWidget(self.draw_btn, 1)
        outlet_row.addWidget(self.clear_outlet_btn, 1)
        sub_layout.addRow(outlet_row)

        self.min_subcatchment_spin = QDoubleSpinBox()
        self.min_subcatchment_spin.setRange(0.0, 1000000000000.0)
        self.min_subcatchment_spin.setDecimals(0)
        self.min_subcatchment_spin.setSingleStep(100.0)
        self.min_subcatchment_spin.setValue(100000.0)
        self.min_subcatchment_spin.setSuffix(" m²")
        if hasattr(self.min_subcatchment_spin, "setGroupSeparatorShown"):
            self.min_subcatchment_spin.setGroupSeparatorShown(True)
        self.min_subcatchment_spin.setToolTip(
            "Minimum target subcatchment area in square metres. The plugin converts this to DEM cells, "
            "then creates as many minimum-size subcatchments as the selected upstream area can hydrologically support. If no outlet line exists, the plugin asks before processing the whole DEM."
        )
        sub_layout.addRow("7. Minimum subcatchment size", self.min_subcatchment_spin)

        self.process_subcatchments_btn = QPushButton("8. Process subcatchments")
        self.clear_subcatchments_btn = QPushButton("Clear subcatchments")
        process_row = QHBoxLayout()
        process_row.addWidget(self.process_subcatchments_btn, 1)
        process_row.addWidget(self.clear_subcatchments_btn, 1)
        sub_layout.addRow(process_row)
        layout.addWidget(sub_group)

        export_group = QGroupBox("Final output")
        export_layout = QVBoxLayout(export_group)
        self.export_btn = QPushButton("Export flow paths and subcatchments to GeoPackage")
        export_layout.addWidget(self.export_btn)
        self.rorb_export_btn = QPushButton("Export to RORB GE (.catg)")
        self.rorb_export_btn.setToolTip(
            "Creates a first-pass RORB GE/RORBwin .catg file from the current flow-path and subcatchment outputs, "
            "then loads the generated RORB nodes and links into a temporary RORB group in QGIS."
        )
        export_layout.addWidget(self.rorb_export_btn)

        self.wbnm_export_btn = QPushButton("Export to WBNM 2025 (.wbn)")
        self.wbnm_export_btn.setToolTip(
            "Creates a first-pass WBNM 2025 .wbn runfile from current subcatchments, topology, areas and flowpaths. "
            "Rainfall, structures and losses are left as dummy/default values for later editing in WBNM."
        )
        export_layout.addWidget(self.wbnm_export_btn)

        self.xprafts_export_btn = QPushButton("Export to XP-RAFTS (.xpx)")
        self.xprafts_export_btn.setToolTip(
            "Creates a first-pass XP-RAFTS .xpx exchange file from current subcatchments, topology and areas. "
            "Import it in XP-RAFTS (File > Import > XPX). Roughness, slope, routing and storms are left as defaults to edit there."
        )
        export_layout.addWidget(self.xprafts_export_btn)

        self.tuflow_export_btn = QPushButton("Export TUFLOW files (.shp)")
        self.tuflow_export_btn.setToolTip(
            "Writes seven TUFLOW region shapefiles (2d_code, 2d_loc, 2d_mat, 2d_soil, 2d_rf, 2d_po, 2d_qnl) "
            "into a chosen folder. Each holds the subcatchments merged into one polygon, "
            "(same CRS as DEM)."
        )
        export_layout.addWidget(self.tuflow_export_btn)
        layout.addWidget(export_group)

        self.abort_btn = QPushButton("Abort current plugin process and clear memory")
        self.abort_btn.setEnabled(False)
        self.abort_btn.setToolTip("Requests cancellation at the next safe processing checkpoint and clears unnecessary temporary data/layers.")
        layout.addWidget(self.abort_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status_label = QLabel("No DEM processed yet.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(root)
        self.setWidget(scroll)

        self.refresh_btn.clicked.connect(self.refresh_dem_layers)
        self.mask_btn.clicked.connect(self.activate_mask_tool)
        self.build_btn.clicked.connect(self.build_flow_paths)
        self.click_btn.clicked.connect(self.activate_click_tool)
        self.draw_btn.clicked.connect(self.activate_draw_tool)
        self.clear_outlet_btn.clicked.connect(self.clear_outlet_line)
        self.min_subcatchment_spin.valueChanged.connect(self._subcatchment_parameters_changed)
        self.process_subcatchments_btn.clicked.connect(self.process_subcatchments)
        self.clear_subcatchments_btn.clicked.connect(self.clear_subcatchments)
        self.export_btn.clicked.connect(self.export_outputs)
        self.rorb_export_btn.clicked.connect(self.export_rorb_catg)
        self.wbnm_export_btn.clicked.connect(self.export_wbnm_2025)
        self.xprafts_export_btn.clicked.connect(self.export_xprafts)
        self.tuflow_export_btn.clicked.connect(self.export_tuflow)
        self.clear_btn.clicked.connect(self.clear_temporary_layers)
        self.abort_btn.clicked.connect(self.request_abort)

    # ------------------------------------------------------------------
    # DEM and graph creation
    # ------------------------------------------------------------------
    def refresh_dem_layers(self):
        self.dem_combo.clear()
        self.dem_layer_ids = []
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                self.dem_combo.addItem(layer.name(), layer.id())
                self.dem_layer_ids.append(layer.id())
        if not self.dem_layer_ids:
            self.dem_combo.addItem("No raster DEM layers loaded", "")

    def selected_dem_layer(self):
        layer_id = self.dem_combo.currentData()
        if not layer_id:
            return None
        layer = QgsProject.instance().mapLayer(layer_id)
        return layer if isinstance(layer, QgsRasterLayer) else None

    def _dem_projected_crs_warning_passed(self, dem_layer):
        """Warns before processing DEMs that are not in a projected CRS."""
        try:
            crs = dem_layer.crs()
        except Exception:
            crs = None

        is_valid = False
        is_projected = False
        crs_name = "Unknown CRS"
        epsg_code = "unknown"

        if crs is not None:
            try:
                is_valid = bool(crs.isValid())
            except Exception:
                is_valid = False
            try:
                crs_name = crs.description() or crs.authid() or "Unknown CRS"
            except Exception:
                crs_name = "Unknown CRS"
            try:
                authid = crs.authid() or ""
                if authid.upper().startswith("EPSG:"):
                    epsg_code = authid.split(":", 1)[1]
                elif authid:
                    epsg_code = authid
            except Exception:
                epsg_code = "unknown"
            try:
                if hasattr(crs, "isProjected"):
                    is_projected = bool(crs.isProjected())
                else:
                    is_projected = is_valid and not bool(crs.isGeographic())
            except Exception:
                try:
                    is_projected = is_valid and not bool(crs.isGeographic())
                except Exception:
                    is_projected = False

        if is_valid and is_projected:
            return True

        response = QMessageBox.question(
            self,
            "DEM CRS warning",
            f"It looks like your DEM is in {crs_name} EPSG: {epsg_code}. "
            "The areas and lengths calculations will not be accurate - reprojection is recommended. "
            "Do you still want to proceed?",
            enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
            enum_member(QMessageBox, "StandardButton", "No"),
        )
        if response == enum_member(QMessageBox, "StandardButton", "Yes"):
            return True

        self._reset_after_unprojected_crs_cancel()
        return False

    def _reset_after_unprojected_crs_cancel(self):
        """Cancels Compute and clears any temporary outputs."""
        try:
            self._remove_mask_layer_if_present()
            self._remove_all_plugin_temporary_layers(include_flow=True, include_highlight=True, include_subcatchments=True)
            if self.engine is not None:
                self._remove_layer_if_present("flow_layer")
                self._remove_layer_if_present("highlight_layer")
                self._remove_layer_if_present("subcatchment_layer")
        except Exception:
            pass
        self.engine = None
        self.outlet_cells = []
        self.current_assignments = {}
        self.selected_highlight_cells = set()
        self.selected_catchment_groups = {}
        self.selection_polygon_geom = None
        self._clear_selection_polygon_overlay()
        self._clear_outlet_line_overlay()
        gc.collect()
        self.progress.setValue(0)
        self.status_label.setText("Flow-path processing cancelled. Analysis mask and temporary plugin outputs were cleared; reproject the DEM or select Yes to proceed with the current CRS.")

    def _canvas_crs_uri(self):
        crs = self.canvas.mapSettings().destinationCrs()
        authid = crs.authid()
        if authid:
            return authid
        return crs.toWkt()

    def _mask_layer_is_available(self):
        layer = getattr(self, "mask_layer", None)
        layer_id = self._layer_id_safe(layer)
        if not layer_id:
            self.mask_layer = None
            return False
        if QgsProject.instance().mapLayer(layer_id) is None:
            self.mask_layer = None
            return False
        try:
            if not layer.isValid() or layer.featureCount() < 1:
                self.mask_layer = None
                return False
        except RuntimeError:
            self.mask_layer = None
            return False
        except Exception:
            return False
        return True

    def _remove_mask_layer_if_present(self):
        """Removes the current mask layer and any stale layers."""
        project = QgsProject.instance()
        layer_id = self._layer_id_safe(getattr(self, "mask_layer", None))
        if layer_id and project.mapLayer(layer_id) is not None:
            try:
                project.removeMapLayer(layer_id)
            except Exception:
                pass
        self.mask_layer = None

        for layer in list(project.mapLayers().values()):
            try:
                if layer.name().startswith("DDM HydroLogic mask polygon"):
                    project.removeMapLayer(layer.id())
            except Exception:
                continue

    def activate_mask_tool(self):
        """Activates a canvas tool to digitise the optional mask poly."""
        dem_layer = self.selected_dem_layer()
        if dem_layer is None:
            QMessageBox.warning(self, "DDM HydroLogic", "Load/select a DEM raster before creating a mask polygon.")
            return
        self.mask_tool = DrawMaskPolygonTool(self.canvas)
        self.mask_tool.polygonFinished.connect(self._handle_mask_polygon)
        self.mask_tool.cancelled.connect(self._handle_mask_polygon_cancelled)
        self.canvas.setMapTool(self.mask_tool)
        self.status_label.setText(
            "Digitise the mask polygon: left-click on the canvas to add vertices. "
            "The canvas tooltip appears only while this tool is active. Right-click to finish."
        )

    def _deactivate_mask_tool(self):
        """Stops the mask digitising tool and hide its temporary tooltip."""
        try:
            from qgis.PyQt.QtWidgets import QToolTip
            QToolTip.hideText()
        except Exception:
            pass
        try:
            if self.mask_tool is not None and self.canvas.mapTool() == self.mask_tool:
                self.canvas.unsetMapTool(self.mask_tool)
        except Exception:
            pass

    def _handle_mask_polygon_cancelled(self):
        self._deactivate_mask_tool()
        self.status_label.setText("Digitisation cancelled.")

    def _handle_mask_polygon(self, points):
        """Creates a temporary mask layer from digitised canvas points."""
        self._deactivate_mask_tool()
        if not points or len(points) < 3:
            self.status_label.setText("Mask polygon was not created because fewer than 3 vertices were supplied.")
            return
        try:
            self._remove_mask_layer_if_present()
            self._remove_all_plugin_temporary_layers(include_flow=True, include_highlight=True, include_subcatchments=True)
            if self.engine is not None:
                self._remove_layer_if_present("flow_layer")
                self._remove_layer_if_present("highlight_layer")
                self._remove_layer_if_present("subcatchment_layer")
            self.engine = None
            self.outlet_cells = []
            self.current_assignments = {}
            self.selected_highlight_cells = set()
            self.selected_catchment_groups = {}
            self._clear_selection_polygon_overlay()
            self._clear_outlet_line_overlay()

            ring = [QgsPointXY(p) for p in points]
            if ring[0] != ring[-1]:
                ring.append(QgsPointXY(ring[0]))
            geom = QgsGeometry.fromPolygonXY([ring])
            if geom.isNull() or geom.isEmpty():
                raise RuntimeError("The drawn polygon geometry is empty.")
            try:
                if not geom.isGeosValid():
                    fixed = geom.buffer(0, 1)
                    if not fixed.isNull() and not fixed.isEmpty():
                        geom = fixed
            except Exception:
                pass

            layer = QgsVectorLayer(
                f"Polygon?crs={self._canvas_crs_uri()}",
                "DDM HydroLogic mask polygon - temporary",
                "memory",
            )
            provider = layer.dataProvider()
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geom)
            provider.addFeatures([feature])
            layer.updateExtents()
            symbol = QgsFillSymbol.createSimple({
                "color": "0,180,255,45",
                "outline_color": "0,130,210",
                "outline_width": "0.60",
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            QgsProject.instance().addMapLayer(layer)
            self.mask_layer = layer
            self.progress.setValue(100)
            self.status_label.setText(
                "Mask polygon created. Existing temporary flow paths/highlights/subcatchments were cleared. "
                "Press Compute to process only the DEM cells inside the mask."
            )
            self.canvas.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not create mask polygon:\n\n{exc}")
            self.status_label.setText("Mask polygon creation failed.")

    def _mask_is_definitely_outside_dem_extent(self, dem_layer):
        """Returns True when the temporary mask polygon does not intersect the DEM extent.

        This is a lightweight preflight check used before the heavier rasterised
        mask/NoData inspection.  The rasterised check still catches masks that
        touch the raster extent but only cover NoData collar cells.
        """
        try:
            if dem_layer is None or not dem_layer.isValid() or not self._mask_layer_is_available():
                return False
            geoms = []
            for feat in self.mask_layer.getFeatures():
                geom = feat.geometry()
                if geom is not None and not geom.isNull() and not geom.isEmpty():
                    geoms.append(QgsGeometry(geom))
            if not geoms:
                return False
            mask_geom = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
            if mask_geom is None or mask_geom.isNull() or mask_geom.isEmpty():
                return False
            try:
                if self.mask_layer.crs() != dem_layer.crs():
                    tr = QgsCoordinateTransform(self.mask_layer.crs(), dem_layer.crs(), QgsProject.instance())
                    mask_geom.transform(tr)
            except Exception:
                return False
            try:
                return not dem_layer.extent().intersects(mask_geom.boundingBox())
            except Exception:
                return False
        except Exception:
            return False

    def _analysis_mask_warning_passed(self, dem_layer):
        """Runs separate mask-outside and NoData preflight warnings before Compute."""
        if not self._mask_layer_is_available():
            return True
        if self._mask_is_definitely_outside_dem_extent(dem_layer):
            response = QMessageBox.question(
                self,
                "Mask polygon outside DEM",
                "CAUTION: the mask polygon is outside the DEM. Do you still wish to continue?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response == enum_member(QMessageBox, "StandardButton", "Yes"):
                return True
            self._remove_mask_layer_if_present()
            self.status_label.setText("Compute cancelled. The mask layer was cleared.")
            return False
        checker = D8HydrologyEngine(
            dem_layer,
            lambda pct, msg: None,
            self._abort_has_been_requested,
            mask_layer=self.mask_layer,
        )
        try:
            info = checker.inspect_analysis_mask_quality()
        except HydrologyBuildError:
            info = {
                "mask_cells": int(getattr(checker, "mask_raster_cell_count", 0) or 0),
                "valid_cells": int(getattr(checker, "mask_cell_count", 0) or 0),
                "nodata_cells": int(getattr(checker, "mask_nodata_cell_count", 0) or 0),
                "nodata_cell_ids": list(getattr(checker, "mask_nodata_cell_ids", []) or []),
            }
        except Exception:
            return True

        mask_cells = int(info.get("mask_cells", 0) or 0)
        valid_cells = int(info.get("valid_cells", 0) or 0)
        nodata_cells = int(info.get("nodata_cells", 0) or 0)

        # Case 1: the mask does not capture any DEM cells/valid DEM cells. Keep
        # this warning separate from the NoData warning.
        if mask_cells <= 0 or valid_cells <= 0:
            response = QMessageBox.question(
                self,
                "Mask polygon outside DEM",
                "CAUTION: the mask polygon is outside the DEM. Do you still wish to continue?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response == enum_member(QMessageBox, "StandardButton", "Yes"):
                return True
            self._remove_mask_layer_if_present()
            self.status_label.setText("Compute cancelled. The mask layer was cleared.")
            return False

        # Case 2: the mask includes NoData cells. Create the red warning layer
        # before asking whether to continue, so the user can review.
        if nodata_cells > 0:
            self._create_nodata_problem_layer(checker, info)
            response = QMessageBox.question(
                self,
                "NoData values detected",
                'WARNING: NoData values have been detected. This may output unrealistic results - please review the "NoData - problematic areas" layer. It is recommended to quality check the DEM. Do you still wish to continue?',
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response == enum_member(QMessageBox, "StandardButton", "Yes"):
                return True
            self.status_label.setText('Compute cancelled. The mask layer was kept and the "NoData - problematic areas" layer was left for review.')
            return False

        self._remove_nodata_problem_layer_if_present()
        return True

    def _dem_nodata_warning_passed(self, dem_layer):
        """Warns when an unmasked DEM contains NoData cells before Compute."""
        # When a mask exists, _analysis_mask_warning_passed already checks the
        # analysis-relevant NoData cells and shows the same warning. Avoid a
        # duplicate whole-raster warning that would mostly show the DEM collar.
        if self._mask_layer_is_available():
            return True
        checker = D8HydrologyEngine(
            dem_layer,
            lambda pct, msg: None,
            self._abort_has_been_requested,
            mask_layer=None,
        )
        try:
            checker._read_dem()
            flat_valid = checker.valid.reshape(-1)
            nodata_cell_ids = [int(i) for i, ok in enumerate(flat_valid) if not bool(ok)]
        except Exception:
            return True
        if not nodata_cell_ids:
            self._remove_nodata_problem_layer_if_present()
            return True
        info = {"nodata_cell_ids": nodata_cell_ids}
        self._create_nodata_problem_layer(checker, info)
        response = QMessageBox.question(
            self,
            "NoData values detected",
            'WARNING: NoData values have been detected. This may output unrealistic results - please review the "NoData - problematic areas" layer. It is recommended to quality check the DEM. Do you still wish to continue?',
            enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
            enum_member(QMessageBox, "StandardButton", "No"),
        )
        if response == enum_member(QMessageBox, "StandardButton", "Yes"):
            return True
        self.status_label.setText('Compute cancelled. The "NoData - problematic areas" layer was left for review.')
        return False

    def _remove_nodata_problem_layer_if_present(self):
        """Removes stale NoData warning polygons created by this plugin."""
        project = QgsProject.instance()
        for layer in list(project.mapLayers().values()):
            try:
                if layer.name() in ("NoData - problematic areas", "DDM HydroLogic mask NoData warning - temporary") or layer.name().startswith("DDM HydroLogic mask NoData warning"):
                    project.removeMapLayer(layer.id())
            except Exception:
                continue

    def _create_nodata_problem_layer(self, checker, info):
        """Create a red temporary polygon showing NoData cells intersecting the mask."""
        try:
            self._remove_nodata_problem_layer_if_present()
            geoms = []
            cell_ids = [int(c) for c in info.get("nodata_cell_ids", []) or []]
            if cell_ids and getattr(checker, "geotransform", None) is not None:
                geom = checker.dissolve_cells_to_geometry(cell_ids)
                if geom is not None and not geom.isNull() and not geom.isEmpty():
                    geoms.append(geom)
            if not geoms:
                return
            geom = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
            crs_uri = checker._layer_crs_uri() if checker is not None else self._canvas_crs_uri()
            layer = QgsVectorLayer(f"MultiPolygon?crs={crs_uri}", "NoData - problematic areas", "memory")
            provider = layer.dataProvider()
            provider.addAttributes([QgsField("warning", QVariant.String)])
            layer.updateFields()
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes(["NoData cells intersect the analysis mask"])
            provider.addFeatures([feat])
            layer.updateExtents()
            symbol = QgsFillSymbol.createSimple({
                "color": "255,0,0,90",
                "outline_color": "255,0,0",
                "outline_width": "0.90",
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            QgsProject.instance().addMapLayer(layer)
            self.canvas.refresh()
        except Exception:
            return

    def _create_mask_nodata_warning_layer(self, checker, info):
        """Creates the NoData warnig polygon layer."""
        return self._create_nodata_problem_layer(checker, info)

    def build_flow_paths(self):
        dem_layer = self.selected_dem_layer()
        if dem_layer is None:
            QMessageBox.warning(self, "DDM HydroLogic", "Load a DEM raster first, then try again.")
            return

        if not self._dem_projected_crs_warning_passed(dem_layer):
            return

        if not self._analysis_mask_warning_passed(dem_layer):
            return

        if not self._dem_nodata_warning_passed(dem_layer):
            return

        min_acc = int(self.min_acc_spin.value())
        self.abort_requested = False
        self.active_operation = "build"
        self._set_busy(True)
        try:
            self._remove_all_plugin_temporary_layers(include_flow=True)
            self._remove_layer_if_present("flow_layer")
            self._remove_layer_if_present("highlight_layer")
            self._remove_layer_if_present("subcatchment_layer")
            self.outlet_cells = []
            self.current_assignments = {}
            self.selected_highlight_cells = set()
            self.selected_catchment_groups = {}
            self.selection_polygon_geom = None
            self._clear_selection_polygon_overlay()
            self._clear_outlet_line_overlay()

            mask_layer = self.mask_layer if self._mask_layer_is_available() else None
            self.engine = D8HydrologyEngine(
                dem_layer,
                self._progress,
                self._abort_has_been_requested,
                mask_layer=mask_layer,
            )
            self.engine.build()

            valid_count = len(self.engine.valid_ids)
            if valid_count > 750000 and min_acc <= 1:
                response = QMessageBox.question(
                    self,
                    "Large flow-path layer",
                    f"This will create up to {valid_count:,} cell-size flow-path features.\n\n"
                    "QGIS can do it, but it may become sluggish. Continue anyway?",
                    enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                    enum_member(QMessageBox, "StandardButton", "No"),
                )
                if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                    self.status_label.setText(
                        "Graph built, but vector layer creation was cancelled. Increase the display accumulation threshold and build again."
                    )
                    return

            flow_layer = self.engine.create_flow_layer(min_accumulation=min_acc)
            QgsProject.instance().addMapLayer(flow_layer)
            mask_text = (
                f" Analysis mask active: {self.engine.mask_cell_count:,} DEM cells."
                if getattr(self.engine, "mask_cell_count", 0) else
                " Mask polygon: not used."
            )
            self.status_label.setText(
                f"Flow graph ready. Valid DEM cells: {valid_count:,}. "
                f"Displayed flow-path features: {flow_layer.featureCount():,}."
                f"{mask_text} "
                f"Cell size: {self.engine.cell_width:g} x {self.engine.cell_height:g} map units. "
                f"Minimum preview unit: {self.engine.cell_area:g} m² per DEM cell."
            )
            self.progress.setValue(100)
        except HydrologyCancelled:
            self._cleanup_after_abort(clear_engine=True)
            self.status_label.setText("Flow-path build aborted. Memoy released.")
        except HydrologyBuildError as exc:
            QMessageBox.critical(self, "DDM HydroLogic", str(exc))
            self.status_label.setText("DEM processing failed.")
        except Exception as exc:  # pragma: no cover - QGIS runtime safety net
            QMessageBox.critical(self, "DDM HydroLogic", f"Unexpected error while building flow paths:\n\n{exc}")
            self.status_label.setText("DEM processing failed.")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def _progress(self, pct, message):
        self.progress.setValue(max(0, min(100, int(pct))))
        self.status_label.setText(message)
        QApplication.processEvents()

    def _set_busy(self, busy):
        for widget in (
            self.refresh_btn,
            self.mask_btn,
            self.build_btn,
            self.click_btn,
            self.clear_btn,
            self.draw_btn,
            self.clear_outlet_btn,
            self.process_subcatchments_btn,
            self.clear_subcatchments_btn,
            self.export_btn,
            self.rorb_export_btn,
            self.wbnm_export_btn,
            self.xprafts_export_btn,
            self.tuflow_export_btn,
        ):
            widget.setEnabled(not busy)
        self.abort_btn.setEnabled(bool(busy))
        if busy:
            QApplication.setOverrideCursor(qt_enum(Qt, "CursorShape", "WaitCursor"))
        else:
            QApplication.restoreOverrideCursor()

    def request_abort(self):
        self.abort_requested = True
        self.status_label.setText("Abort requested. The current operation will stop at the next safe checkpoint and release the memory.")
        QApplication.processEvents()

    def _abort_has_been_requested(self):
        return bool(self.abort_requested)

    # ------------------------------------------------------------------
    # Interactive map tools
    # ------------------------------------------------------------------
    def activate_click_tool(self):
        if not self._require_engine_and_layer():
            return
        self.click_tool = QgsMapToolEmitPoint(self.canvas)
        self.click_tool.canvasClicked.connect(self._handle_flow_path_click)
        self.canvas.setMapTool(self.click_tool)
        self.status_label.setText("Click a flow-path segment to highlight upstream paths in yellow and reveal contributing upstream catchment (light-green overlay). CTRL+left-click to de-select a sub catchment.")

    def _handle_flow_path_click(self, point, button):
        if not self._require_engine_and_layer():
            return

        ctrl_left = self._is_ctrl_left_click(button)
        if ctrl_left:
            catchment_key = self._selected_catchment_key_at_point(point)
            if catchment_key is None:
                self.status_label.setText("No sub catchments to de-select.")
                return
            cell_id = int(catchment_key)
        else:
            cell_id = self.engine.nearest_flow_cell(point, self.canvas.mapSettings().destinationCrs())
            if cell_id is None:
                self.status_label.setText("No flow path found near the click location.")
                return

        self.abort_requested = False
        self.active_operation = "selection"
        self._set_busy(True)
        try:
            if ctrl_left:
                removed_cells = self._deselect_catchment_group(cell_id)
                action_text = (
                    f"Deselected highlighted catchment {cell_id}. Removed {removed_cells:,} DEM cells "
                    "and their displayed upstream flow-path highlights."
                )
            else:
                upstream_cells = set(int(c) for c in self.engine.collect_upstream(cell_id))
                self.selected_catchment_groups[int(cell_id)] = upstream_cells
                before_groups = len(self.selected_catchment_groups)
                self.selected_catchment_groups = self.engine.normalize_overlapping_cell_groups(self.selected_catchment_groups)
                after_groups = len(self.selected_catchment_groups)
                merged_text = ""
                if after_groups < before_groups:
                    merged_text = " Overlapping child catchment(s) were merged into their downstream parent catchment."
                action_text = f"Selected cell {cell_id}. This click added/updated {len(upstream_cells):,} upstream DEM cells.{merged_text}"

            highlight = self._refresh_selection_outputs()
            displayed_highlight_count = highlight.featureCount() if highlight is not None else 0
            group_count = len(self.selected_catchment_groups)
            self.status_label.setText(
                f"{action_text} "
                f"Visible highlighted flow paths: {displayed_highlight_count:,}, limited to the displayed accumulation threshold. "
                f"Highlighted catchment group(s): {group_count:,}. "
                f"Persistent contributing cells: {len(self.selected_highlight_cells):,}. "
                f"Approx highlighted area: {len(self.selected_highlight_cells) * self.engine.cell_area:,.2f} m² "
                f"({len(self.selected_highlight_cells) * self.engine.cell_area / 10000.0:,.3f} ha). "
                "Catchment polygons are shown as separate, non-overlapping in-memory light-green overlays, not project layers."
            )
        except HydrologyCancelled:
            self._cleanup_after_abort(clear_engine=False)
            self.status_label.setText("Flow-path selection aborted. Temporary layers and memory released.")
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not select upstream flow paths:\n\n{exc}")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def _is_ctrl_left_click(self, button):
        """Returns True when a click is CTRL + left mouse button across PyQt builds."""
        try:
            ctrl_modifier = qt_enum(Qt, "KeyboardModifier", "ControlModifier")
            left_button = qt_enum(Qt, "MouseButton", "LeftButton")
            return bool(QApplication.keyboardModifiers() & ctrl_modifier) and button == left_button
        except Exception:
            try:
                return bool(QApplication.keyboardModifiers() & qt_enum(Qt, "KeyboardModifier", "ControlModifier"))
            except Exception:
                return False

    def _selected_catchment_key_at_point(self, point):
        """Returns the highlighted catchment group containing a CTRL-click point."""
        if self.engine is None or not self.selected_catchment_groups:
            return None
        cell_id = self.engine.point_to_cell_id(point, self.canvas.mapSettings().destinationCrs())
        if cell_id is None:
            return None

        containing = []
        for outlet_id, cells in self.selected_catchment_groups.items():
            if int(cell_id) in cells:
                containing.append((int(outlet_id), len(cells)))
        if not containing:
            return None

        # Groups are normalised to be non-overlapping, but choose the smallest
        # containing group defensively if a stale state is loaded. The click is
        # meant to act on the catchment the user visibly clicked, not a nearby
        # flow-path segment.
        containing.sort(key=lambda item: (item[1], item[0]))
        return containing[0][0]

    def _deselect_catchment_group(self, outlet_id):
        """Removes one highlighted catchment overlay and it's flow-path highlights."""
        if not self.selected_catchment_groups:
            return 0
        cells = self.selected_catchment_groups.pop(int(outlet_id), set())
        return len(cells or [])

    def _refresh_selection_outputs(self):
        """Refreshes yellow path highlights and light-green catchment overlays.

        Callers normalise ``selected_catchment_groups`` before calling this, so
        normalisation is not repeated.
        """
        self.selected_highlight_cells = set()
        for cells in self.selected_catchment_groups.values():
            self.selected_highlight_cells.update(int(c) for c in cells)

        self._remove_layer_if_present("highlight_layer")
        if not self.selected_highlight_cells:
            self._clear_selection_polygon_overlay()
            return None

        highlight = self.engine.create_highlight_layer(sorted(self.selected_highlight_cells))
        QgsProject.instance().addMapLayer(highlight)
        self._update_selection_polygon_overlays(self.selected_catchment_groups)
        return highlight

    def reset_flow_path_selection(self):
        if self.engine is None:
            return
        self.selected_highlight_cells = set()
        self.selected_catchment_groups = {}
        self.selection_polygon_geom = None
        self._catchment_geom_cache = {}
        self._remove_layer_if_present("highlight_layer")
        self._clear_selection_polygon_overlay()
        self.status_label.setText("Flow path selection reset. Yellow upstream highlights and light-green catchment overlays cleared.")

    def activate_draw_tool(self):
        if not self._require_engine_and_layer():
            return
        self.draw_tool = DrawOutletLineTool(self.canvas)
        self.draw_tool.lineFinished.connect(self._handle_drawn_line)
        self.draw_tool.cancelled.connect(lambda: self.status_label.setText("Outlet line cancelled."))
        self.canvas.setMapTool(self.draw_tool)
        self.status_label.setText("Draw a line crossing flow-path cells. The outlet/crossing line is shown as a thick red canvas overlay. Right-click to finish.")

    def _handle_drawn_line(self, points):
        if not self._require_engine_and_layer() or len(points) < 2:
            return
        self._update_outlet_line_overlay(points)
        layer_points = self.engine.transform_polyline(
            points,
            self.canvas.mapSettings().destinationCrs(),
            self.engine.flow_layer.crs(),
        )
        geom = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in layer_points])
        self.outlet_line_layer_geom = QgsGeometry(geom)
        self.outlet_cells = self._flow_cells_crossed_by_outlet_geometry(geom)
        self.current_assignments = {}
        self._remove_layer_if_present("subcatchment_layer")
        if not self.outlet_cells:
            self.status_label.setText("The drawn line did not cross any displayed flow-path cells.")
            return
        self.status_label.setText(
            f"Outlet line captured. It crossed {len(self.outlet_cells):,} displayed flow-path cells. "
            "Press Process subcatchments to generate the preview/output polygons."
        )

    def _subcatchment_parameters_changed(self):
        if self.engine is None:
            return
        if self._engine_layer_is_available("subcatchment_layer"):
            self._remove_layer_if_present("subcatchment_layer")
            self.current_assignments = {}
            if self.outlet_cells:
                self.status_label.setText(
                    "Minimum subcatchment size changed. Press Process subcatchments to regenerate polygons."
                )
        else:
            # A user may have manually deleted the temporary preview layer. Clear
            # the stale Python reference so later actions do not touch a deleted
            # wrapped C++ object.
            setattr(self.engine, "subcatchment_layer", None)

    def process_subcatchments(self):
        if self.engine is None:
            QMessageBox.warning(self, "DDM HydroLogic", "Build the temporary flow paths first.")
            return

        min_area_m2 = float(self.min_subcatchment_spin.value())
        if min_area_m2 <= 0.0:
            QMessageBox.warning(
                self,
                "Minimum subcatchment size required",
                "Please input a Minimum subcatchment size greater than 0 m² before processing subcatchments.",
            )
            self.status_label.setText("Minimum subcatchment size must be greater than 0 m².")
            return

        if not self.outlet_cells:
            response = QMessageBox.question(
                self,
                "No outlet line drawn",
                "An outlet line was not drawn. This will process the whole DEM. Do you wish to continue?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                self.status_label.setText(
                    "Subcatchment processing cancelled. Draw an outlet/crossing line in section 3, then press Process subcatchments again."
                )
                return

        min_cells = self.engine.cells_for_area_m2(min_area_m2)
        self._remove_all_plugin_temporary_layers(include_flow=False, include_highlight=False, include_subcatchments=True)
        self._remove_layer_if_present("subcatchment_layer")
        self.current_assignments = {}
        self.abort_requested = False
        self.active_operation = "subcatchments"
        self._set_busy(True)
        try:
            assignments = self.engine.build_area_threshold_subcatchments(
                min_cells=min_cells,
                boundary_outlet_cells=self.outlet_cells or None,
                include_residual=True,
            )
            assignments = self.engine._normalise_assignments_no_overlap(assignments)
            self.current_assignments = assignments
            if not assignments:
                scope = "upstream of the drawn outlet/crossing line" if self.outlet_cells else "the DEM"
                self.status_label.setText(
                    f"No subcatchments could be created for {scope}. Lower the minimum subcatchment size."
                )
                return
            layer = self.engine.create_subcatchment_layer(assignments, min_cells=min_cells)
            QgsProject.instance().addMapLayer(layer)
            self.progress.setValue(100)
            self._recalculate_subcatchment_area_fields(layer)
            total_area_ha = self._layer_total_area_ha(layer)
            total_cells = sum(len(cells) for cells in assignments.values())
            theoretical_count = int(total_cells // max(1, min_cells))
            scope = "upstream of the drawn outlet/crossing line" if self.outlet_cells else "the whole DEM flow graph"
            if self.outlet_cells:
                self._restrict_flow_layer_to_assignment_domain(assignments)
            self._remove_layer_if_present("highlight_layer")
            self.selected_highlight_cells = set()
            self.selected_catchment_groups = {}
            self.selection_polygon_geom = None
            self._clear_selection_polygon_overlay()
            text = (
                f"Subcatchments processed successfully for {scope}: {len(assignments):,} dissolved outline polygon(s). "
                f"Total area: {total_area_ha:,.2f} ha. "
                f"Minimum size: {min_area_m2:,.0f} m² (~{min_cells:,} DEM cells). "
                f"Theoretical maximum by area alone: {theoretical_count:,}; actual count is constrained by D8 connectivity and confluences."
            )
            self.status_label.setText(text)
            QMessageBox.information(
                self,
                "DDM HydroLogic",
                f"Subcatchment processing successful.\n\nSubcatchments: {len(assignments):,}\nTotal area: {total_area_ha:,.2f} ha",
            )
        except HydrologyCancelled:
            self._cleanup_after_abort(clear_engine=False)
            self.status_label.setText("Subcatchment processing aborted. Temporary preview memory was released; existing flow paths remain available.")
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not process subcatchments:\n\n{exc}")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def _update_outlet_line_overlay(self, points):
        """Shows the outlet/crossing line as a thick red canvas overlay, not as a project layer."""
        self._clear_outlet_line_overlay()
        if not points or len(points) < 2:
            return
        self.outlet_line_points = [QgsPointXY(p) for p in points]
        band = QgsRubberBand(
            self.canvas,
            enum_member(QgsWkbTypes, "GeometryType", "LineGeometry"),
        )
        if hasattr(band, "setStrokeColor"):
            band.setStrokeColor(QColor(220, 0, 0, 235))
        else:
            band.setColor(QColor(220, 0, 0, 235))
        band.setWidth(5)
        geom = QgsGeometry.fromPolylineXY(self.outlet_line_points)
        band.setToGeometry(geom, None)
        band.show()
        self.outlet_line_band = band

    def _clear_outlet_line_overlay(self):
        """Removes and sanitises all red outlet/crossing canvas state."""
        for band in (getattr(self, "outlet_line_band", None), getattr(getattr(self, "draw_tool", None), "rubber_band", None)):
            if band is not None:
                try:
                    band.reset(enum_member(QgsWkbTypes, "GeometryType", "LineGeometry"))
                    band.hide()
                    try:
                        self.canvas.scene().removeItem(band)
                    except Exception:
                        pass
                except Exception:
                    pass
        try:
            if self.draw_tool is not None:
                self.draw_tool.points = []
        except Exception:
            pass
        self.outlet_line_band = None
        self.outlet_line_points = []
        self.outlet_line_layer_geom = None
        try:
            self.canvas.refresh()
        except Exception:
            pass

    def _update_selection_polygon_overlays(self, selection_groups):
        """Draws one non-overlapping light-green dissolved catchment overlay per selection group.

        Dissolved catchment geometries are cached per outlet id and reused across
        clicks; only groups whose cell set changed are re-dissolved, and the
        dissolve uses the fast GDAL polygonisation path.
        """
        self._clear_selection_polygon_overlay()
        if self.engine is None or not selection_groups:
            self._catchment_geom_cache = {}
            return

        source_crs = self.engine.flow_layer.crs() if self.engine.flow_layer is not None else self.engine.dem_layer.crs()
        dest_crs = self.canvas.mapSettings().destinationCrs()
        transform = None
        if source_crs != dest_crs:
            transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())

        old_cache = getattr(self, "_catchment_geom_cache", {})
        new_cache = {}
        combined_geoms = []

        for outlet_id, selected_cells in list(selection_groups.items()):
            self._check_abort_from_dock()
            if not selected_cells:
                continue
            cells = set(int(c) for c in selected_cells)

            # Reuse the dissolved geometry if this catchment's cells are unchanged.
            cached = old_cache.get(int(outlet_id))
            if cached is not None and cached[0] == cells:
                geom_layer = cached[1]
            else:
                geom_layer = self.engine.dissolve_cells_fast(cells)
            if geom_layer is None or geom_layer.isNull() or geom_layer.isEmpty():
                continue
            new_cache[int(outlet_id)] = (cells, geom_layer)

            # Cache stores the layer-CRS geometry; transform a copy for display.
            if transform is not None:
                geom = QgsGeometry(geom_layer)
                geom.transform(transform)
            else:
                geom = geom_layer

            band = QgsRubberBand(
                self.canvas,
                enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"),
            )
            if hasattr(band, "setStrokeColor"):
                band.setStrokeColor(QColor(70, 170, 70, 235))
            else:
                band.setColor(QColor(70, 170, 70, 235))
            if hasattr(band, "setFillColor"):
                band.setFillColor(QColor(144, 238, 144, 85))
            band.setWidth(2)
            band.setToGeometry(geom, None)
            band.show()
            self.selection_polygon_bands.append(band)
            combined_geoms.append(geom)

        self._catchment_geom_cache = new_cache
        if combined_geoms:
            self.selection_polygon_geom = QgsGeometry.unaryUnion(combined_geoms) if len(combined_geoms) > 1 else combined_geoms[0]

    def _clear_selection_polygon_overlay(self):
        if self.selection_polygon_band is not None:
            try:
                self.selection_polygon_band.reset(enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
                self.selection_polygon_band.hide()
            except Exception:
                pass
        self.selection_polygon_band = None

        for band in list(getattr(self, "selection_polygon_bands", [])):
            try:
                band.reset(enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
                band.hide()
                try:
                    self.canvas.scene().removeItem(band)
                except Exception:
                    pass
            except Exception:
                pass
        self.selection_polygon_bands = []
        self.selection_polygon_geom = None

    # ------------------------------------------------------------------
    # Export and cleanup
    # ------------------------------------------------------------------
    def export_outputs(self):
        if not self._require_engine_and_layer():
            return
        if not self._engine_layer_is_available("subcatchment_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting.")
            return
        if self.engine.subcatchment_layer.featureCount() == 0:
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting.")
            return

        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export DDM HydroLogic outputs",
            os.path.expanduser("~/DDM_HydroLogic_outputs_version_.gpkg"),
            "GeoPackage (*.gpkg)",
        )
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"

        try:
            self.abort_requested = False
            self.active_operation = "export"
            self._set_busy(True)
            self._progress(10, "Dissolving final flow paths into connected Strahler reaches")
            export_flow_layer = self.engine.create_reach_flow_layer()
            self._progress(35, "Exporting dissolved flow paths to GeoPackage")
            self._write_layer_to_gpkg(export_flow_layer, path, "flow_paths", overwrite_file=True)
            self._save_layer_style_to_gpkg(export_flow_layer, path, "flow_paths", "DDM Strahler order")
            self._progress(60, "Recomputing subcatchment areas from final geometry")
            self._recalculate_subcatchment_area_fields(self.engine.subcatchment_layer)
            self._progress(70, "Exporting subcatchments to GeoPackage")
            self._write_layer_to_gpkg(self.engine.subcatchment_layer, path, "subcatchments", overwrite_file=False)
            self._progress(80, "Recomputing exported GeoPackage subcatchment areas from $area")
            exported_subcatchments = QgsVectorLayer(f"{path}|layername=subcatchments", "subcatchments", "ogr")
            if exported_subcatchments.isValid():
                self._recalculate_subcatchment_area_fields(exported_subcatchments)
            self._progress(88, "Loading exported GeoPackage layers into QGIS")
            loaded_names = self._load_exported_gpkg_layers(path)
            self.progress.setValue(100)
            loaded_text = f" Loaded layers: {', '.join(loaded_names)}." if loaded_names else ""
            self.status_label.setText(f"Exported flow paths and subcatchments to: {path}.{loaded_text}")
            QMessageBox.information(self, "DDM HydroLogic", f"Export complete and loaded into QGIS:\n\n{path}")
        except HydrologyCancelled:
            self.status_label.setText("Export aborted. Existing in-memory layers were left available; temporary plugin memory was released where safe.")
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "DDM HydroLogic", f"Export failed:\n\n{exc}")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def _flow_cells_crossed_by_outlet_geometry(self, geom):
        """Returns displayed flow-path cell ids crossed by an outlet geometry.

        Uses the engine spatial index first, then falls back to a full layer scan.
        The fallback protects RORB export after in-session feature deletion or
        layer filtering, where the spatial index can be stale.
        """
        if self.engine is None or geom is None or geom.isNull() or geom.isEmpty():
            return []
        cells = []
        try:
            cells = list(self.engine.line_crossing_cells(geom) or [])
        except Exception:
            cells = []
        if not cells:
            try:
                layer = self.engine.flow_layer
                if layer is not None and layer.isValid():
                    seen = set()
                    request = QgsFeatureRequest().setFilterRect(geom.boundingBox())
                    for feat in layer.getFeatures(request):
                        try:
                            fgeom = feat.geometry()
                            if fgeom is None or fgeom.isNull() or fgeom.isEmpty():
                                continue
                            if not fgeom.intersects(geom):
                                continue
                            cid = int(feat["cell_id"])
                            if cid not in seen:
                                seen.add(cid)
                                cells.append(cid)
                        except Exception:
                            continue
            except Exception:
                cells = []
        try:
            cells = sorted(set(int(c) for c in cells), key=lambda c: int(self.engine.accumulation[int(c)]), reverse=True)
        except Exception:
            cells = [int(c) for c in cells]
        return cells

    def _refresh_outlet_cells_from_red_line(self):
        """Recomputes outlet cells from the stored red outlet line if needed."""
        if self.engine is None:
            return []
        if self.outlet_cells:
            return list(self.outlet_cells)
        geom = getattr(self, "outlet_line_layer_geom", None)
        if geom is None or geom.isNull() or geom.isEmpty():
            try:
                if self.outlet_line_points and len(self.outlet_line_points) >= 2:
                    flow_crs = self.engine.flow_layer.crs() if self._engine_layer_is_available("flow_layer") else self.engine.dem_layer.crs()
                    layer_points = self.engine.transform_polyline(
                        self.outlet_line_points,
                        self.canvas.mapSettings().destinationCrs(),
                        flow_crs,
                    )
                    geom = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in layer_points])
                    self.outlet_line_layer_geom = QgsGeometry(geom)
            except Exception:
                geom = None
        if geom is None or geom.isNull() or geom.isEmpty():
            return []
        cells = self._flow_cells_crossed_by_outlet_geometry(geom)
        if cells:
            self.outlet_cells = list(cells)
        return list(self.outlet_cells or [])

    def _current_rorb_outlet_cell(self):
        """Returns the outlet DEM cell derived from the drawn red outlet line.

        The highest-accumulation crossed flow-path cell is treated as the model
        outlet cell. This keeps the exported RORB network to one connected
        drainage tree instead of exporting disconnected branches.
        """
        try:
            if self.engine is None:
                return None
            cells = self._refresh_outlet_cells_from_red_line()
            if not cells:
                return None
            raw_valid_ids = getattr(self.engine, "valid_ids", None)
            valid_ids = set()
            if raw_valid_ids is not None:
                try:
                    valid_ids = set(int(c) for c in list(raw_valid_ids))
                except Exception:
                    try:
                        valid_ids = set(int(c) for c in raw_valid_ids)
                    except Exception:
                        valid_ids = set()
            # Do not use `raw_valid_ids or []` here. In QGIS 4/Python 3.12 the
            # engine may store valid_ids as a NumPy array, whose truth value is
            # deliberately ambiguous. That was causing a swallowed exception and
            # the false "No outlet line has been drawn" warning.
            valid_cells = []
            for c in cells:
                try:
                    cid = int(c)
                    if not valid_ids or cid in valid_ids:
                        valid_cells.append(cid)
                except Exception:
                    continue
            if not valid_cells:
                return None
            return int(max(valid_cells, key=lambda cid: (int(self.engine.accumulation[int(cid)]), int(cid))))
        except Exception:
            return None


    def _current_rorb_outlet_point(self):
        """Returns an explicit RORB outlet coordinate from the drawn QGIS outlet line.

        The exporter works in the DEM/flow-layer CRS. When a drawn outlet line
        crosses one or more displayed flow-path cells, use the crossed cell with
        the highest accumulation as the model outlet. This generally corresponds
        to the downstream-most outlet on the drawn line. If no crossed cells are
        available, fall back to the midpoint of the drawn red line transformed
        into the flow-layer CRS.
        """
        if self.engine is None:
            return None
        try:
            outlet_cell = self._current_rorb_outlet_cell()
            if outlet_cell is not None:
                point = self.engine.cell_center(int(outlet_cell))
                return (float(point.x()), float(point.y()))
        except Exception:
            pass
        try:
            if self.outlet_line_points:
                pts = list(self.outlet_line_points)
                if len(pts) >= 2:
                    mid_index = len(pts) // 2
                    if len(pts) % 2 == 1:
                        mid_pt = QgsPointXY(pts[mid_index])
                    else:
                        a = QgsPointXY(pts[mid_index - 1])
                        b = QgsPointXY(pts[mid_index])
                        mid_pt = QgsPointXY((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
                    flow_crs = self.engine.flow_layer.crs() if self._engine_layer_is_available("flow_layer") else self.canvas.mapSettings().destinationCrs()
                    transformed = self.engine.transform_polyline(
                        [mid_pt],
                        self.canvas.mapSettings().destinationCrs(),
                        flow_crs,
                    )
                    if transformed:
                        p = QgsPointXY(transformed[0])
                        return (float(p.x()), float(p.y()))
        except Exception:
            pass
        return None

    def _show_dependency_or_runtime_error(self, title, exc):
        """Shows a more useful message when a module/file dependency is missing."""
        if isinstance(exc, (ModuleNotFoundError, ImportError, FileNotFoundError)):
            name = getattr(exc, "name", None) or getattr(exc, "filename", None) or str(exc)
            QMessageBox.critical(
                self,
                "DDM HydroLogic dependency/file missing",
                f"{title} failed because a required Python module or plugin file is missing.\n\n"
                f"Reported issue: {name}\n\n"
                "How to fix it:\n"
                "1. Delete the existing DDM_HydroLogic plugin folder from your QGIS profile.\n"
                "2. Reinstall the latest DDM HydroLogic ZIP using Plugins > Manage and Install Plugins > Install from ZIP.\n"
                "3. If the missing item is a third-party Python package such as numpy, install or repair the QGIS/OSGeo4W Python package that provides it.\n"
                "4. Restart QGIS.\n\n"
                f"Technical detail: {exc}",
            )
        else:
            QMessageBox.critical(self, "DDM HydroLogic", f"{title} failed:\n\n{exc}")

    def export_rorb_catg(self):
        """Exports the current plugin outputs to a RORBwin/RORB GE .catg file."""
        if not self._require_engine_and_layer():
            return
        if not self._engine_layer_is_available("subcatchment_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting a RORB GE .catg file.")
            return
        if not self.current_assignments:
            QMessageBox.warning(self, "DDM HydroLogic", "Current subcatchment assignments are not available. Press Process subcatchments before exporting a RORB GE .catg file.")
            return

        try:
            subcatchment_count = int(self.engine.subcatchment_layer.featureCount())
        except Exception:
            subcatchment_count = len(self.current_assignments)
        if subcatchment_count <= 15:
            response = QMessageBox.question(
                self,
                "RORB subcatchment count warning",
                "Its recommended to breakdown the catchment into more than 15 subcatchments. Do you still wish to proceed?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                self.status_label.setText("RORB GE .catg export cancelled. Create more than 15 subcatchments before exporting if practical.")
                return

        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export to RORB (.catg)",
            os.path.expanduser("~/DDM_HydroLogic_outputs_version_.catg"),
            "RORB catchment (*.catg)",
        )
        if not path:
            return
        if not path.lower().endswith(".catg"):
            path += ".catg"

        try:
            self.abort_requested = False
            self.active_operation = "rorb_export"
            self._set_busy(True)
            self._progress(10, "Preparing RORB GE .catg export")
            outlet_cell = self._current_rorb_outlet_cell()
            outlet_point = self._current_rorb_outlet_point()
            if outlet_cell is None:
                # Final defensive refresh in case the line was drawn before the flow layer was filtered/rebuilt.
                self._refresh_outlet_cells_from_red_line()
                outlet_cell = self._current_rorb_outlet_cell()
                outlet_point = self._current_rorb_outlet_point()
            if outlet_cell is None:
                response = QMessageBox.question(
                    self,
                    "No RORB outlet line",
                    "No outlet line has been drawn. RORB requires a connected model outlet. Do you still wish to proceed using an inferred outlet?",
                    enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                    enum_member(QMessageBox, "StandardButton", "No"),
                )
                if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                    self.status_label.setText("RORB GE .catg export cancelled. Draw an outlet line before exporting to RORB.")
                    return
            output_path, basin_count, reach_count = write_rorb_catg_from_engine(
                self.engine,
                self.current_assignments,
                path,
                rorb_version="6.52",
                fraction_impervious=0.0,
                impervious_field=None,
                outlet_point=outlet_point,
                outlet_name="outlet",
                model_outlet_cell=outlet_cell,
            )
            self._progress(78, "Loading generated RORB temporary layers into QGIS")
            loaded_rorb_layers = self._load_rorb_layers_group_from_catg(output_path)
            self.progress.setValue(100)
            self.status_label.setText(
                f"Exported RORB .catg file: {output_path}. "
                f"Basins/subareas: {basin_count:,}; reaches: {reach_count:,}. "
                f"Loaded temporary RORB layers: {', '.join(loaded_rorb_layers) if loaded_rorb_layers else 'none'}. " +
                ("The drawn outlet line was used as the RORB outlet." if outlet_point is not None else "No drawn outlet line was available, so the outlet was inferred from the terminal drainage point.")
            )
            QMessageBox.information(
                self,
                "DDM HydroLogic",
                "RORB .catg export complete.\n\n"
                f"{output_path}\n\n"
                f"Subareas: {basin_count:,}\n"
                f"Reaches: {reach_count:,}\n\n"
                "Review the generated catchment in RORB GE before running hydrology. Subcatchment areas are written to basin/node attributes and fraction impervious defaults to 0.00. " +
                ("The drawn outlet line was used as the explicit RORB outlet." if outlet_point is not None else "No drawn outlet line was available, so the outlet was inferred from the terminal drainage point."),
            )
        except HydrologyCancelled:
            self.status_label.setText("RORB GE .catg export aborted.")
        except RorbCatgExportError as exc:
            QMessageBox.warning(self, "DDM HydroLogic", str(exc))
            self.status_label.setText("RORB GE .catg export was not completed.")
        except Exception as exc:  # pragma: no cover
            self._show_dependency_or_runtime_error("RORB GE .catg export", exc)
            self.status_label.setText("RORB GE .catg export failed.")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def export_wbnm_2025(self):
        """Exports the current plugin outputs to a first-pass WBNM 2025 .wbn runfile."""
        if not self._require_engine_and_layer():
            return
        if not self._engine_layer_is_available("subcatchment_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting a WBNM 2025 .wbn file.")
            return
        if not self.current_assignments:
            QMessageBox.warning(self, "DDM HydroLogic", "Current subcatchment assignments are not available. Press Process subcatchments before exporting a WBNM 2025 .wbn file.")
            return

        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export to WBNM 2025 (.wbn)",
            os.path.expanduser("~/DDM_HydroLogic_outputs_version_.wbn"),
            "WBNM runfile (*.wbn)",
        )
        if not path:
            return
        if not path.lower().endswith(".wbn"):
            path += ".wbn"

        try:
            self.abort_requested = False
            self.active_operation = "wbnm_export"
            self._set_busy(True)
            self._progress(10, "Preparing WBNM 2025 runfile export")
            output_path, sub_count, total_area_ha, flowpath_count = write_wbnm_2025_from_engine(
                self.engine,
                self.current_assignments,
                path,
                model_name="DDM_HydroLogic",
            )
            self.progress.setValue(100)
            self.status_label.setText(
                f"Exported WBNM 2025 .wbn runfile: {output_path}. "
                f"Subareas: {sub_count:,}; total area: {total_area_ha:,.2f} ha; routed flowpaths: {flowpath_count:,}. "
                "Rainfall, losses and structures are placeholders and must be reviewed/edited before modelling."
            )
            QMessageBox.information(
                self,
                "DDM HydroLogic",
                "WBNM 2025 .wbn export complete.\n\n"
                f"{output_path}\n\n"
                f"Subareas: {sub_count:,}\n"
                f"Total area: {total_area_ha:,.2f} ha\n"
                f"Routed flowpaths: {flowpath_count:,}\n\n"
                "This is a first-pass scaffold. Review and replace dummy rainfall, losses, imperviousness, structures and routing assumptions before running WBNM."
            )
        except HydrologyCancelled:
            self.status_label.setText("WBNM 2025 .wbn export aborted.")
        except Wbnm2025ExportError as exc:
            QMessageBox.warning(self, "DDM HydroLogic", str(exc))
            self.status_label.setText("WBNM 2025 .wbn export was not completed.")
        except Exception as exc:  # pragma: no cover
            self._show_dependency_or_runtime_error("WBNM 2025 .wbn export", exc)
            self.status_label.setText("WBNM 2025 .wbn export failed.")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def export_xprafts(self):
        """Exports the current plugin outputs to a first-pass XP-RAFTS .xpx file."""
        if not self._require_engine_and_layer():
            return
        if not self._engine_layer_is_available("subcatchment_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting an XP-RAFTS .xpx file.")
            return
        if not self.current_assignments:
            QMessageBox.warning(self, "DDM HydroLogic", "Current subcatchment assignments are not available. Press Process subcatchments before exporting an XP-RAFTS .xpx file.")
            return

        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export to XP-RAFTS (.xpx)",
            os.path.expanduser("~/DDM_HydroLogic_outputs_version_.xpx"),
            "XP-RAFTS exchange file (*.xpx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xpx"):
            path += ".xpx"

        try:
            self.abort_requested = False
            self.active_operation = "xprafts_export"
            self._set_busy(True)
            self._progress(10, "Preparing XP-RAFTS .xpx export")
            output_path, node_count, link_count, total_area_ha = write_xprafts_from_engine(
                self.engine,
                self.current_assignments,
                path,
                model_name="DDM_HydroLogic",
            )
            self.progress.setValue(100)
            self.status_label.setText(
                f"Exported XP-RAFTS .xpx file: {output_path}. "
                f"Nodes: {node_count:,}; links: {link_count:,}; total area: {total_area_ha:,.2f} ha. "
                "Import it in XP-RAFTS (File > Import > XPX). Roughness, slope, routing and storms are defaults to review."
            )
            QMessageBox.information(
                self,
                "DDM HydroLogic",
                "XP-RAFTS .xpx export complete.\n\n"
                f"{output_path}\n\n"
                f"Nodes: {node_count:,}\n"
                f"Links: {link_count:,}\n"
                f"Total area: {total_area_ha:,.2f} ha\n\n"
                "Import the file into XP-RAFTS with File > Import > XPX. This is a first-pass scaffold: "
                "sub-area areas come from QGIS, while Manning's n, slope, channel routing, losses and storms are defaults to review."
            )
        except HydrologyCancelled:
            self.status_label.setText("XP-RAFTS .xpx export aborted.")
        except XpRaftsExportError as exc:
            QMessageBox.warning(self, "DDM HydroLogic", str(exc))
            self.status_label.setText("XP-RAFTS .xpx export was not completed.")
        except Exception as exc:  # pragma: no cover
            self._show_dependency_or_runtime_error("XP-RAFTS .xpx export", exc)
            self.status_label.setText("XP-RAFTS .xpx export failed.")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def export_tuflow(self):
        """Exports the merged catchment boundary as TUFLOW region shps."""
        if not self._require_engine_and_layer():
            return
        if not self._engine_layer_is_available("subcatchment_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Press Process subcatchments before exporting TUFLOW shapefiles.")
            return
        if not self.current_assignments:
            QMessageBox.warning(self, "DDM HydroLogic", "Current subcatchment assignments are not available. Press Process subcatchments before exporting TUFLOW shapefiles.")
            return

        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder for TUFLOW shapefiles",
            os.path.expanduser("~"),
        )
        if not folder:
            return

        existing = [
            base_name + ".shp"
            for base_name, _fields, _values in TUFLOW_LAYERS
            if os.path.exists(os.path.join(folder, base_name + ".shp"))
        ]
        if existing:
            response = QMessageBox.question(
                self,
                "TUFLOW files already exist",
                "This folder already contains "
                f"{len(existing)} of the TUFLOW shapefiles (e.g. {existing[0]}).\n\n"
                "Overwrite them?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                return

        try:
            self.abort_requested = False
            self.active_operation = "tuflow_export"
            self._set_busy(True)
            self._progress(10, "Preparing TUFLOW shapefile export")
            output_dir, written, total_area_ha = write_tuflow_from_engine(
                self.engine,
                self.current_assignments,
                folder,
            )
            self.progress.setValue(100)
            file_names = "\n".join(os.path.basename(path) for path in written)
            self.status_label.setText(
                f"Exported {len(written)} TUFLOW region shapefiles to {output_dir}. "
                f"Catchment boundary area: {total_area_ha:,.2f} ha. "
                "Materials, soils, codes and plot outputs carry default or blank values. Please review."
            )
            QMessageBox.information(
                self,
                "DDM HydroLogic",
                "TUFLOW shapefile export complete.\n\n"
                f"{output_dir}\n\n"
                f"{file_names}\n\n"
                f"Catchment boundary area: {total_area_ha:,.2f} ha\n\n"
                "Each file holds the subcatchments merged into one boundary polygon, in the DEM CRS. "
                "Review the blank Material, SoilID, Nest_Level and plot-output attributes before running."
            )
        except HydrologyCancelled:
            self.status_label.setText("TUFLOW shapefile export aborted.")
        except TuflowExportError as exc:
            QMessageBox.warning(self, "DDM HydroLogic", str(exc))
            self.status_label.setText("TUFLOW shapefile export was not completed.")
        except Exception as exc:  # pragma: no cover
            self._show_dependency_or_runtime_error("TUFLOW shapefile export", exc)
            self.status_label.setText("TUFLOW shapefile export failed.")
        finally:
            self.active_operation = None
            self._set_busy(False)

    def _layer_total_area_ha(self, layer):
        """Returns total polygon area in hectares from actual feature geometries."""
        total_m2 = 0.0
        if layer is None or not layer.isValid():
            return 0.0
        for feat in layer.getFeatures():
            self._check_abort_from_dock()
            geom = feat.geometry()
            if geom is not None and not geom.isNull() and not geom.isEmpty():
                total_m2 += float(geom.area())
        return round(total_m2 / 10000.0, 2)

    def _restrict_flow_layer_to_assignment_domain(self, assignments):
        """Keeps only blue Strahler flow paths contributing to the processed outlet domain."""
        if self.engine is None or not self._engine_layer_is_available("flow_layer"):
            return
        domain_cells = set()
        for cells in (assignments or {}).values():
            domain_cells.update(int(c) for c in cells or [])
        if not domain_cells:
            return
        layer = self.engine.flow_layer
        delete_ids = []
        for feat in layer.getFeatures():
            try:
                cell_id = int(feat["cell_id"])
                if cell_id not in domain_cells:
                    delete_ids.append(int(feat.id()))
            except Exception:
                continue
        if delete_ids:
            try:
                layer.dataProvider().deleteFeatures(delete_ids)
                layer.updateExtents()
                layer.triggerRepaint()
            except Exception:
                pass
        try:
            self.engine._style_flow_layer(layer)
            self.engine._rebuild_spatial_index()
        except Exception:
            pass
        self.canvas.refresh()

    def _load_rorb_layers_group_from_catg(self, path):
        """Loads generated RORB temporary layers under a top-of-panel RORB group."""
        loaded = []
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        try:
            old_group = root.findGroup("RORB")
            if old_group is not None:
                root.removeChildNode(old_group)
        except Exception:
            pass
        group = root.insertGroup(0, "RORB")

        def add_to_group(layer):
            if layer is None or not layer.isValid():
                return
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            loaded.append(layer.name())

        try:
            nodes_layer, links_layer = load_rorb_catg_layers(path, crs_uri=self._canvas_crs_uri())
            add_to_group(links_layer)
            add_to_group(nodes_layer)
        except Exception:
            pass

        self.canvas.refresh()
        return loaded

    def _load_exported_gpkg_layers(self, path):
        """Loads the saved GeoPackage outputs back into the current QGIS project."""
        self._check_abort_from_dock()
        loaded_names = []

        flow_layer = QgsVectorLayer(f"{path}|layername=flow_paths", "flow_paths", "ogr")
        if flow_layer.isValid():
            try:
                # Re-apply the in-session Strahler renderer so the loaded output
                # displays as blue graduated lines immediately, even on builds
                # that ignore a newly stored GeoPackage default style.
                if self.engine is not None:
                    self.engine._style_flow_layer(flow_layer)
                self._save_layer_style_to_gpkg(flow_layer, path, "flow_paths", "DDM Strahler order")
            except Exception:
                pass
            QgsProject.instance().addMapLayer(flow_layer)
            loaded_names.append(flow_layer.name())

        sub_layer = QgsVectorLayer(f"{path}|layername=subcatchments", "subcatchments", "ogr")
        if sub_layer.isValid():
            try:
                if self.engine is not None and self.engine.subcatchment_layer is not None:
                    renderer = self.engine.subcatchment_layer.renderer()
                    if renderer is not None:
                        sub_layer.setRenderer(renderer.clone())
            except Exception:
                pass
            QgsProject.instance().addMapLayer(sub_layer)
            loaded_names.append(sub_layer.name())

        self.canvas.refresh()
        return loaded_names

    def _recalculate_subcatchment_area_fields(self, layer):
        """Refreshes area_m2 and area_ha from each output polygon geometry.

        This mirrors QGIS functions $area and $area / 10000 so the
        exported attributes agree with the actual dissolved polygons rather
        than the original DEM-cell counts.
        """
        self._check_abort_from_dock()
        if layer is None or not layer.isValid():
            return
        idx_m2 = layer.fields().indexFromName("area_m2")
        idx_ha = layer.fields().indexFromName("area_ha")
        if idx_m2 < 0 and idx_ha < 0:
            return

        provider = layer.dataProvider()
        expr_area = QgsExpression("$area")
        expr_ha = QgsExpression("$area / 10000")
        context = QgsExpressionContext()
        try:
            context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        except Exception:
            pass
        changes = {}
        for feat in layer.getFeatures():
            self._check_abort_from_dock()
            geom = feat.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            context.setFeature(feat)
            try:
                evaluated_m2 = expr_area.evaluate(context)
                area_m2 = round(float(evaluated_m2), 2)
            except Exception:
                area_m2 = round(float(geom.area()), 2)
            try:
                evaluated_ha = expr_ha.evaluate(context)
                area_ha = round(float(evaluated_ha), 2)
            except Exception:
                area_ha = round(float(area_m2 / 10000.0), 2)
            attrs = {}
            if idx_m2 >= 0:
                attrs[idx_m2] = area_m2
            if idx_ha >= 0:
                attrs[idx_ha] = area_ha
            if attrs:
                changes[int(feat.id())] = attrs
        if changes:
            provider.changeAttributeValues(changes)
            layer.updateFields()
            layer.triggerRepaint()

    def _write_layer_to_gpkg(self, layer, path, layer_name, overwrite_file=False):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer_name
        if hasattr(QgsVectorFileWriter, "CreateOrOverwriteFile"):
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile if overwrite_file else QgsVectorFileWriter.CreateOrOverwriteLayer
            )
        else:
            action_enum = getattr(QgsVectorFileWriter, "ActionOnExistingFile", None)
            if action_enum is not None:
                options.actionOnExistingFile = (
                    action_enum.CreateOrOverwriteFile if overwrite_file else action_enum.CreateOrOverwriteLayer
                )
        if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                path,
                QgsProject.instance().transformContext(),
                options,
            )
        else:  # Compatibility fallback for older QGIS writer signatures.
            result = QgsVectorFileWriter.writeAsVectorFormat(
                layer,
                path,
                "UTF-8",
                layer.crs(),
                "GPKG",
                onlySelected=False,
            )
        error_code = result[0] if isinstance(result, tuple) else result
        if error_code != _writer_no_error_code():
            raise RuntimeError(f"Could not write layer '{layer_name}' to GeoPackage. Writer result: {result}")

    def _save_layer_style_to_gpkg(self, source_layer, path, layer_name, style_name):
        """Persists the source layer renderer as the default style on a GeoPackage layer."""
        self._check_abort_from_dock()
        try:
            gpkg_layer = QgsVectorLayer(f"{path}|layername={layer_name}", layer_name, "ogr")
            if not gpkg_layer.isValid():
                return False
            renderer = source_layer.renderer()
            if renderer is not None:
                gpkg_layer.setRenderer(renderer.clone())

            # GeoPackage-backed layers support storing QGIS styles in the GPKG
            # database. saveStyleToDatabase signatures vary between QGIS builds,
            # hence the two-call fallback below.
            try:
                gpkg_layer.saveStyleToDatabase(style_name, "DDM Strahler order blue renderer", True, "")
            except TypeError:
                gpkg_layer.saveStyleToDatabase(style_name, "DDM Strahler order blue renderer", True, "", None)
            return True
        except Exception:
            # Style persistence is useful, but an export should not fail just
            # because a QGIS build changes the style-save API. The data remains
            # exported and the in-session layer keeps the renderer.
            return False

    def _check_abort_from_dock(self):
        if self.abort_requested:
            raise HydrologyCancelled("Processing aborted by user.")

    def clear_temporary_layers(self):
        """Clears only flow-path selection highlights/overlays.

        Outlet lines and subcatchment outputs have their own dedicated clear
        buttons, so this button now just restarts the interactive flow-path
        selection state.
        """
        self._remove_all_plugin_temporary_layers(include_flow=False, include_highlight=True, include_subcatchments=False)
        self._remove_layer_if_present("highlight_layer")
        self.selected_highlight_cells = set()
        self.selected_catchment_groups = {}
        self.selection_polygon_geom = None
        self._clear_selection_polygon_overlay()
        gc.collect()
        self.progress.setValue(100)
        self.status_label.setText("Temporary flow-path highlights and light-green catchment overlays cleared. Use 5. Click on flow paths to re-start the selection process.")

    def clear_outlet_line(self):
        """Clears only the red outlet/crossing line and its captured outlet cells."""
        self.outlet_cells = []
        self._clear_outlet_line_overlay()
        try:
            if self.draw_tool is not None:
                self.draw_tool.reset()
        except Exception:
            pass
        gc.collect()
        self.progress.setValue(100)
        self.status_label.setText("Outlet line cleared. Draw a new outlet line before processing subcatchments, or process the whole DEM when prompted.")

    def clear_subcatchments(self):
        """Clears only the generated subcatchment layer/assignments."""
        self._remove_all_plugin_temporary_layers(include_flow=False, include_highlight=False, include_subcatchments=True)
        self._remove_layer_if_present("subcatchment_layer")
        self.current_assignments = {}
        gc.collect()
        self.progress.setValue(100)
        self.status_label.setText("Subcatchments cleared. Adjust 7. Minimum subcatchment size and press 8. Process subcatchments to recompute them.")

    def _cleanup_after_abort(self, clear_engine=False):
        """Releases unnecessary temporary layers, selections and large Python objects after abort."""
        self.abort_requested = False
        try:
            self._remove_all_plugin_temporary_layers(include_flow=False, include_highlight=True, include_subcatchments=True)
            self._remove_layer_if_present("highlight_layer")
            self._remove_layer_if_present("subcatchment_layer")
        except Exception:
            pass
        self.current_assignments = {}
        self.outlet_cells = []
        self.selected_highlight_cells = set()
        self.selected_catchment_groups = {}
        self.selection_polygon_geom = None
        self._clear_selection_polygon_overlay()
        self._clear_outlet_line_overlay()

        if clear_engine:
            try:
                self._remove_layer_if_present("flow_layer")
            except Exception:
                pass
            self.engine = None

        gc.collect()

    def _qgis_object_is_deleted(self, obj):
        """Returns True when a PyQGIS wrapper points to a deleted C++ object."""
        if obj is None:
            return True
        try:
            return bool(sip.isdeleted(obj))
        except Exception:
            # Some bindings do not expose sip.isdeleted consistently. Probe a
            # harmless method; deleted wrappers raise RuntimeError here.
            try:
                obj.id()
                return False
            except RuntimeError:
                return True
            except Exception:
                return False

    def _layer_id_safe(self, layer):
        if layer is None or self._qgis_object_is_deleted(layer):
            return None
        try:
            return layer.id()
        except RuntimeError:
            return None

    def _engine_layer_is_available(self, attr_name):
        """Checks that an engine layer still exists in the project and is valid."""
        if self.engine is None:
            return False
        layer = getattr(self.engine, attr_name, None)
        layer_id = self._layer_id_safe(layer)
        if not layer_id:
            setattr(self.engine, attr_name, None)
            return False
        if QgsProject.instance().mapLayer(layer_id) is None:
            setattr(self.engine, attr_name, None)
            return False
        try:
            if not layer.isValid():
                setattr(self.engine, attr_name, None)
                return False
        except RuntimeError:
            setattr(self.engine, attr_name, None)
            return False
        return True

    def _remove_layer_if_present(self, attr_name):
        if self.engine is None:
            return
        layer = getattr(self.engine, attr_name, None)
        layer_id = self._layer_id_safe(layer)
        if layer_id and QgsProject.instance().mapLayer(layer_id) is not None:
            try:
                QgsProject.instance().removeMapLayer(layer_id)
            except RuntimeError:
                # The layer was removed between the project lookup and removal.
                pass
        setattr(self.engine, attr_name, None)
        if attr_name == "flow_layer" and self.engine is not None:
            self.engine.spatial_index = None
            self.engine.feature_to_cell = {}
            self.engine.cell_to_feature = {}

    def _handle_project_layers_will_be_removed(self, layer_ids):
        """Clears stale layer references when the user manually deletes temp layers."""
        if isinstance(layer_ids, str):
            layer_ids = [layer_ids]
        try:
            removed = set(layer_ids)
        except TypeError:
            removed = set()

        mask_id = self._layer_id_safe(getattr(self, "mask_layer", None))
        if mask_id and mask_id in removed:
            self.mask_layer = None

        if self.engine is None:
            return
        for attr_name in ("flow_layer", "highlight_layer", "subcatchment_layer"):
            layer = getattr(self.engine, attr_name, None)
            layer_id = self._layer_id_safe(layer)
            if layer_id and layer_id in removed:
                setattr(self.engine, attr_name, None)
                if attr_name == "flow_layer":
                    self.engine.spatial_index = None
                    self.engine.feature_to_cell = {}
                    self.engine.cell_to_feature = {}
                    self.outlet_cells = []
                    self.current_assignments = {}
                    self.selected_highlight_cells = set()
                    self.selected_catchment_groups = {}
                    self._clear_selection_polygon_overlay()
                    self._clear_outlet_line_overlay()
                    QTimer.singleShot(0, lambda: self._remove_all_plugin_temporary_layers(include_flow=False, include_highlight=True, include_subcatchments=True))

    def _remove_all_plugin_temporary_layers(self, include_flow=False, include_highlight=True, include_subcatchments=True):
        """Removes stale DDM temporary layers by name, including stale ghost outputs."""
        prefixes = []
        if include_flow:
            prefixes.append("DDM HydroLogic flow paths")
        if include_highlight:
            prefixes.append("DDM HydroLogic upstream highlight")
            prefixes.append("NoData - problematic areas")
            prefixes.append("DDM HydroLogic mask NoData warning")
        if include_subcatchments:
            prefixes.append("DDM HydroLogic subcatchments")

        if not prefixes:
            return

        project = QgsProject.instance()
        to_remove = []
        for layer in list(project.mapLayers().values()):
            try:
                name = layer.name()
                if any(name.startswith(prefix) for prefix in prefixes):
                    to_remove.append(layer.id())
            except RuntimeError:
                continue
            except Exception:
                continue

        for layer_id in to_remove:
            try:
                if project.mapLayer(layer_id) is not None:
                    project.removeMapLayer(layer_id)
            except RuntimeError:
                pass
            except Exception:
                pass

        if self.engine is not None:
            if include_flow:
                self.engine.flow_layer = None
                self.engine.spatial_index = None
                self.engine.feature_to_cell = {}
                self.engine.cell_to_feature = {}
            if include_highlight:
                self.engine.highlight_layer = None
            if include_subcatchments:
                self.engine.subcatchment_layer = None

    def _require_engine_and_layer(self):
        if self.engine is None or not self._engine_layer_is_available("flow_layer"):
            QMessageBox.warning(self, "DDM HydroLogic", "Build the temporary flow paths first.")
            return False
        return True
