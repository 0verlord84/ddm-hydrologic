# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Subcatchment breakdown window.

Lists every processed subcatchment with its areas, the area reporting to it from
upstream and its main-stream slope, and rebuilds the set by target total, by
area or by Strahler order without leaving the window.

A re-processed set is written to its own temporary layer so the original one
stays on the map. Close throws the re-processing away and puts the original set
back; Confirm keeps the new set and asks what to do with the original.

The ID column counts outwards from the model outlet, so 1 is an outlet sub-area.
It is a QGIS-side handle only: the hydrologic model files keep their own
numbering, which for RORB is fixed by the order its control vector visits
sub-areas and cannot be changed.
"""

from __future__ import annotations

import csv
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from .catchment_geometry import channel_slopes, downstream_outlet_map, upstream_area_ha
from .compat import enum_member, log_ignored, qt_enum
from .hydrology_engine import HydrologyCancelled

PREVIEW_LAYER_NAME = "DDM HydroLogic subcatchments - breakdown preview temporary"
FINAL_LAYER_NAME = "DDM HydroLogic subcatchments - dissolved outlines temporary"

# Label, and the square metres one unit covers. A pixel is one DEM cell, so its
# size comes from the raster rather than a fixed factor.
AREA_UNITS = (
    ("pixel", None),
    ("m²", 1.0),
    ("km²", 1_000_000.0),
    ("ha", 10_000.0),
)

COLUMNS = ("ID", "Area (m²)", "km²", "ha", "Label", "Upstream area (ha)", "Slope (%)")
LABEL_COLUMN = 4


class _NumberItem(QTableWidgetItem):
    """Table cell that sorts on its value rather than its formatted text."""

    def __init__(self, value, text):
        super().__init__(text)
        self.value = float(value)

    def __lt__(self, other):
        try:
            return self.value < float(other.value)
        except (AttributeError, TypeError, ValueError):
            return super().__lt__(other)


class BreakdownDialog(QDialog):
    """Summary of the processed subcatchments, with the three rebuild modes."""

    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.engine = dock.engine
        self.setWindowTitle("DDM HydroLogic - subcatchment breakdown")
        self.setModal(False)
        self.resize(980, 640)

        # What Close has to be able to put back.
        self.original_assignments = {int(k): list(v) for k, v in (dock.current_assignments or {}).items()}
        self.original_layer = getattr(self.engine, "subcatchment_layer", None)
        self.original_area_m2 = float(dock.min_subcatchment_spin.value())

        self.preview_assignments = None
        self.preview_layer = None
        self.preview_cells = None
        self.notes = {}
        self.highlight_band = None
        self._loading = False
        self._committing = False

        self._build_ui()
        self.reload_table()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        process_group = QGroupBox("Processing")
        process_layout = QVBoxLayout(process_group)

        self.target_radio = QRadioButton("Set target total")
        self.target_spin = QSpinBox()
        self.target_spin.setRange(1, 1000000)
        self.target_spin.setValue(max(1, len(self.original_assignments)))
        self.target_spin.setToolTip(
            "Number of subcatchments wanted. Confluences force cuts of their own, so the "
            "count that comes out is the closest the flow graph allows."
        )
        process_layout.addLayout(self._mode_row(self.target_radio, [self.target_spin]))

        self.area_radio = QRadioButton("Set by area")
        self.area_spin = QDoubleSpinBox()
        self.area_spin.setRange(0.000001, 1000000000000.0)
        self.area_spin.setDecimals(6)
        self.area_spin.setValue(self.original_area_m2 if self.original_area_m2 > 0 else 100000.0)
        self.unit_combo = QComboBox()
        for label, _factor in AREA_UNITS:
            self.unit_combo.addItem(label)
        self.unit_combo.setCurrentIndex(1)
        self.area_spin.setToolTip("Each subcatchment comes out at this size or larger, as closely as the flow graph allows.")
        process_layout.addLayout(self._mode_row(self.area_radio, [self.area_spin, self.unit_combo]))

        self.strahler_radio = QRadioButton("Set by Strahler order")
        self.strahler_spin = QSpinBox()
        self.orders = self._available_orders()
        top_order = max(self.orders) if self.orders else 1
        self.strahler_spin.setRange(1, max(1, top_order))
        self.strahler_spin.setValue(max(1, min(2, top_order)))
        self.order_hint = QLabel(
            f"highest order on the displayed flow paths: {top_order}" if self.orders
            else "no Strahler orders on the displayed flow paths"
        )
        self.strahler_spin.setToolTip(
            "Subcatchments are cut at stream confluences of this order or above. Strahler order "
            "only exists on the flow paths the accumulation threshold displays, so a coarse "
            "threshold leaves fewer orders to cut on."
        )
        process_layout.addLayout(self._mode_row(self.strahler_radio, [self.strahler_spin, self.order_hint]))

        self.reprocess_btn = QPushButton("Re-process subcatchments")
        reprocess_row = QHBoxLayout()
        reprocess_row.addStretch(1)
        reprocess_row.addWidget(self.reprocess_btn)
        process_layout.addLayout(reprocess_row)
        layout.addWidget(process_group)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(enum_member(QAbstractItemView, "SelectionBehavior", "SelectRows"))
        self.table.setSelectionMode(enum_member(QAbstractItemView, "SelectionMode", "SingleSelection"))
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(enum_member(QHeaderView, "ResizeMode", "Stretch"))
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.csv_btn = QPushButton("Export as CSV")
        self.close_btn = QPushButton("Close")
        self.confirm_btn = QPushButton("Confirm new subcatchments")
        button_row = QHBoxLayout()
        button_row.addWidget(self.csv_btn)
        button_row.addStretch(1)
        button_row.addWidget(self.close_btn)
        button_row.addWidget(self.confirm_btn)
        layout.addLayout(button_row)

        self.area_radio.setChecked(True)
        if not self.orders:
            self.strahler_radio.setEnabled(False)
            self.strahler_radio.setToolTip(
                "No Strahler orders are available. Lower the display flow-path accumulation "
                "threshold in step 2 and compute the flow paths again."
            )

        for radio in (self.target_radio, self.area_radio, self.strahler_radio):
            radio.toggled.connect(self._sync_mode)
        self.reprocess_btn.clicked.connect(self.reprocess)
        self.csv_btn.clicked.connect(self.export_csv)
        self.close_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self.confirm)
        self.table.itemSelectionChanged.connect(self._highlight_selected_row)
        self.table.itemChanged.connect(self._note_edited)
        self._sync_mode()

    @staticmethod
    def _mode_row(radio, widgets):
        row = QHBoxLayout()
        row.addWidget(radio)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        return row

    def _available_orders(self):
        try:
            return self.engine.strahler_orders_available(self.dock.outlet_cells or None)
        except Exception:
            log_ignored("breakdown_dialog._available_orders")
            return []

    def _sync_mode(self):
        """Only the active mode accepts input; the other two grey out."""
        self.target_spin.setEnabled(self.target_radio.isChecked())
        self.area_spin.setEnabled(self.area_radio.isChecked())
        self.unit_combo.setEnabled(self.area_radio.isChecked())
        self.strahler_spin.setEnabled(self.strahler_radio.isChecked())

    def _set_controls_enabled(self, enabled):
        for widget in (self.target_radio, self.area_radio, self.strahler_radio, self.reprocess_btn,
                       self.csv_btn, self.close_btn, self.confirm_btn, self.table):
            widget.setEnabled(bool(enabled))
        if enabled:
            self._sync_mode()
            self.strahler_radio.setEnabled(bool(self.orders))
        else:
            for widget in (self.target_spin, self.area_spin, self.unit_combo, self.strahler_spin):
                widget.setEnabled(False)

    # ------------------------------------------------------------------
    # the table
    # ------------------------------------------------------------------
    def active_assignments(self):
        return self.preview_assignments if self.preview_assignments is not None else self.original_assignments

    def active_layer(self):
        return self.preview_layer if self.preview_layer is not None else self.original_layer

    def reload_table(self):
        """Rebuilds every row from the assignments currently in play."""
        layer = self.active_layer()
        assignments = self.active_assignments()
        if layer is None or not assignments:
            self.table.setRowCount(0)
            self.summary_label.setText("No subcatchments are available.")
            return

        QApplication.setOverrideCursor(qt_enum(Qt, "CursorShape", "WaitCursor"))
        try:
            features = {}
            for feat in layer.getFeatures():
                try:
                    features[int(feat["outlet_id"])] = feat
                except Exception:
                    log_ignored("breakdown_dialog.reload_table")
                    continue

            outlets = [int(o) for o in assignments if int(o) in features]
            id_of = self.engine.apply_breakdown_ids(layer, assignments, self.dock.outlet_cells, self.notes)
            ds_map = downstream_outlet_map(self.engine, assignments, set(outlets))
            areas_m2 = {o: max(0.0, float(features[o].geometry().area())) for o in outlets}
            areas_ha = {o: areas_m2[o] / 10000.0 for o in outlets}
            upstream = upstream_area_ha(areas_ha, ds_map)
            slopes = channel_slopes(self.engine, assignments, outlets)

            outlets.sort(key=lambda o: (int(id_of.get(o, 0)), o))
            self._loading = True
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(outlets))
            editable = enum_member(Qt, "ItemFlag", "ItemIsEditable")
            for row, outlet in enumerate(outlets):
                cells = [
                    _NumberItem(id_of.get(outlet, 0), str(int(id_of.get(outlet, 0)))),
                    _NumberItem(areas_m2[outlet], f"{areas_m2[outlet]:,.1f}"),
                    _NumberItem(areas_m2[outlet] / 1_000_000.0, f"{areas_m2[outlet] / 1_000_000.0:,.4f}"),
                    _NumberItem(areas_ha[outlet], f"{areas_ha[outlet]:,.2f}"),
                    QTableWidgetItem(str(self.notes.get(outlet, ""))),
                    _NumberItem(upstream.get(outlet, 0.0), f"{upstream.get(outlet, 0.0):,.2f}"),
                    _NumberItem(slopes.get(outlet, 0.0) * 100.0, f"{slopes.get(outlet, 0.0) * 100.0:,.3f}"),
                ]
                cells[0].outlet = int(outlet)
                cells[0].feature_id = int(features[outlet].id())
                for column, item in enumerate(cells):
                    if column != LABEL_COLUMN:
                        item.setFlags(item.flags() & ~editable)
                    self.table.setItem(row, column, item)
            self.table.setSortingEnabled(True)
            self._loading = False

            total_ha = sum(areas_ha.values())
            source = "re-processed" if self.preview_assignments is not None else "current"
            self.summary_label.setText(
                f"{len(outlets):,} {source} subcatchments, {total_ha:,.2f} ha in total. "
                "Slope is the equal-area slope of each subcatchment main flowpath."
            )
        finally:
            QApplication.restoreOverrideCursor()

    def _note_edited(self, item):
        if self._loading or item.column() != LABEL_COLUMN:
            return
        id_item = self.table.item(item.row(), 0)
        outlet = getattr(id_item, "outlet", None)
        if outlet is None:
            return
        text = str(item.text()).strip()
        if text:
            self.notes[int(outlet)] = text
        else:
            self.notes.pop(int(outlet), None)

    # ------------------------------------------------------------------
    # canvas highlight
    # ------------------------------------------------------------------
    def _highlight_selected_row(self):
        self._clear_highlight()
        items = self.table.selectedItems()
        if not items:
            return
        id_item = self.table.item(items[0].row(), 0)
        outlet = getattr(id_item, "outlet", None)
        layer = self.active_layer()
        if outlet is None or layer is None:
            return
        try:
            feature_id = getattr(id_item, "feature_id", None)
            if feature_id is None:
                return
            geom = QgsGeometry(layer.getFeature(int(feature_id)).geometry())
            if geom.isNull() or geom.isEmpty():
                return
            dest_crs = self.dock.canvas.mapSettings().destinationCrs()
            if layer.crs() != dest_crs:
                geom.transform(QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance()))
            band = QgsRubberBand(self.dock.canvas, enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
            if hasattr(band, "setStrokeColor"):
                band.setStrokeColor(QColor(0, 110, 255, 255))
            else:
                band.setColor(QColor(0, 110, 255, 255))
            if hasattr(band, "setFillColor"):
                band.setFillColor(QColor(0, 110, 255, 70))
            band.setWidth(3)
            band.setToGeometry(geom, None)
            band.show()
            self.highlight_band = band
        except Exception:
            log_ignored("breakdown_dialog._highlight_selected_row")

    def _clear_highlight(self):
        if self.highlight_band is None:
            return
        try:
            self.highlight_band.reset(enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry"))
            self.dock.canvas.scene().removeItem(self.highlight_band)
        except Exception:
            log_ignored("breakdown_dialog._clear_highlight")
        self.highlight_band = None

    # ------------------------------------------------------------------
    # re-processing
    # ------------------------------------------------------------------
    def _requested_cells(self):
        """Area threshold in DEM cells for the Set by area mode."""
        label, factor = AREA_UNITS[max(0, self.unit_combo.currentIndex())]
        value = float(self.area_spin.value())
        if factor is None:
            return max(1, int(round(value))), f"{value:,.0f} {label}"
        area_m2 = value * float(factor)
        return self.engine.cells_for_area_m2(area_m2), f"{value:,.4g} {label}"

    def reprocess(self):
        if self.notes:
            response = QMessageBox.question(
                self,
                "Labels will be discarded",
                "Re-processing replaces every subcatchment, so the labels you have typed cannot "
                "follow them and will be cleared. Do you want to continue?",
                enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
                enum_member(QMessageBox, "StandardButton", "No"),
            )
            if response != enum_member(QMessageBox, "StandardButton", "Yes"):
                return

        boundary = self.dock.outlet_cells or None
        self.dock.abort_requested = False
        self.dock.active_operation = "breakdown"
        self.dock._set_busy(True)
        self._set_controls_enabled(False)
        warning = ""
        try:
            if self.target_radio.isChecked():
                target = int(self.target_spin.value())
                assignments, achieved, cells = self.engine.build_target_count_subcatchments(target, boundary)
                if achieved != target:
                    warning = (
                        f"{achieved:,} subcatchments were created against the {target:,} requested. "
                        "The count is set by where the flow graph allows a cut: confluences force their own "
                        "boundaries and the area threshold moves the count in steps, so an exact total is "
                        "usually out of reach. The nearest achievable breakdown is shown."
                    )
                description = f"target total {target:,}"
            elif self.area_radio.isChecked():
                cells, described = self._requested_cells()
                assignments = self.engine.build_area_threshold_subcatchments(
                    min_cells=cells, boundary_outlet_cells=boundary, include_residual=True)
                description = f"minimum size {described}"
            else:
                order = int(self.strahler_spin.value())
                assignments = self.engine.build_strahler_confluence_subcatchments(order, boundary)
                cells = None
                description = f"confluences of Strahler order {order} and above"

            if not assignments:
                QMessageBox.warning(
                    self,
                    "DDM HydroLogic",
                    "No subcatchments could be created with those settings. Try a smaller minimum size, "
                    "a larger target total, or a lower Strahler order.",
                )
                return

            self._replace_preview_layer(assignments, cells)
            self.notes = {}
            self.reload_table()
            self.dock.status_label.setText(
                f"Breakdown re-processed by {description}: {len(assignments):,} subcatchment(s) "
                "in the breakdown preview layer. Confirm or close the Breakdown window."
            )
            if warning:
                QMessageBox.information(self, "Nearest achievable breakdown", warning)
        except HydrologyCancelled:
            self.dock.status_label.setText("Breakdown re-processing aborted. The previous subcatchments are unchanged.")
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not re-process the subcatchments:\n\n{exc}")
        finally:
            self.dock.active_operation = None
            self.dock._set_busy(False)
            self._set_controls_enabled(True)

    def _replace_preview_layer(self, assignments, cells):
        """Puts the re-processed set on its own layer, leaving the original alone."""
        self._clear_highlight()
        self._remove_layer(self.preview_layer)
        layer = self.engine.create_subcatchment_layer(assignments, min_cells=max(1, int(cells or 1)))
        layer.setName(PREVIEW_LAYER_NAME)
        QgsProject.instance().addMapLayer(layer)
        self.dock._recalculate_subcatchment_area_fields(layer)
        self.preview_layer = layer
        self.preview_assignments = {int(k): list(v) for k, v in assignments.items()}
        self.preview_cells = int(cells) if cells else None
        self.dock.current_assignments = self.preview_assignments

    @staticmethod
    def _remove_layer(layer):
        if layer is None:
            return
        try:
            layer_id = layer.id()
        except Exception:
            log_ignored("breakdown_dialog._remove_layer")
            return
        try:
            if QgsProject.instance().mapLayer(layer_id) is not None:
                QgsProject.instance().removeMapLayer(layer_id)
        except Exception:
            log_ignored("breakdown_dialog._remove_layer")

    # ------------------------------------------------------------------
    # leaving the window
    # ------------------------------------------------------------------
    def export_csv(self):
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "DDM HydroLogic", "There is nothing to export yet.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export the subcatchment breakdown",
            os.path.join(os.path.expanduser("~"), "DDM_HydroLogic_subcatchments.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(COLUMNS)
                for row in range(self.table.rowCount()):
                    values = []
                    for column in range(self.table.columnCount()):
                        item = self.table.item(row, column)
                        if item is None:
                            values.append("")
                        elif isinstance(item, _NumberItem):
                            values.append(item.value)
                        else:
                            values.append(item.text())
                    writer.writerow(values)
        except OSError as exc:
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not write the CSV file:\n\n{exc}")
            return
        self.dock.status_label.setText(f"Subcatchment breakdown exported: {path}")

    def confirm(self):
        """Keeps the re-processed set and asks what to do with the original layer."""
        if self.preview_assignments is None:
            self.reject()
            return

        response = QMessageBox.question(
            self,
            "Keep the original subcatchments?",
            "The re-processed subcatchments will be used from here on. Do you want to keep the "
            "original subcatchment layer on the map as well?",
            enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
            enum_member(QMessageBox, "StandardButton", "No"),
        )
        keep_original = response == enum_member(QMessageBox, "StandardButton", "Yes")

        if keep_original:
            try:
                if self.original_layer is not None:
                    self.original_layer.setName(FINAL_LAYER_NAME + " (superseded)")
            except Exception:
                log_ignored("breakdown_dialog.confirm")
        else:
            self._remove_layer(self.original_layer)

        try:
            self.preview_layer.setName(FINAL_LAYER_NAME)
        except Exception:
            log_ignored("breakdown_dialog.confirm")

        self.engine.subcatchment_layer = self.preview_layer
        self.dock.current_assignments = self.preview_assignments
        if self.preview_cells:
            effective_m2 = float(self.preview_cells) * float(getattr(self.engine, "cell_area", 0.0) or 0.0)
            if effective_m2 > 0:
                self.dock.min_subcatchment_spin.blockSignals(True)
                self.dock.min_subcatchment_spin.setValue(effective_m2)
                self.dock.min_subcatchment_spin.blockSignals(False)
        if self.dock.outlet_cells:
            try:
                self.dock._restrict_flow_layer_to_assignment_domain(self.preview_assignments)
            except Exception:
                log_ignored("breakdown_dialog.confirm")

        count = len(self.preview_assignments)
        note = "" if self.preview_cells else " The minimum subcatchment size was left as it was, because this mode does not set one."
        self.dock.status_label.setText(
            f"Breakdown confirmed: {count:,} subcatchment(s) are now the current output.{note}"
        )
        self._committing = True
        self.preview_layer = None
        self.accept()

    def _discard(self):
        """Throws the re-processing away and puts the original set back."""
        self._clear_highlight()
        if self.preview_layer is not None:
            self._remove_layer(self.preview_layer)
            self.preview_layer = None
            self.preview_assignments = None
            self.engine.subcatchment_layer = self.original_layer
            self.dock.current_assignments = self.original_assignments
            self.dock.status_label.setText(
                "Breakdown closed. The re-processing was discarded and the original subcatchments are back in use."
            )

    def reject(self):
        self._discard()
        super().reject()

    def closeEvent(self, event):
        if not self._committing:
            self._discard()
        self._clear_highlight()
        self.dock.breakdown_window = None
        try:
            self.dock._refresh_step_gating(force=True)
        except Exception:
            log_ignored("breakdown_dialog.closeEvent")
        super().closeEvent(event)

    def done(self, result):
        self.dock.breakdown_window = None
        try:
            self.dock._refresh_step_gating(force=True)
        except Exception:
            log_ignored("breakdown_dialog.done")
        super().done(result)
