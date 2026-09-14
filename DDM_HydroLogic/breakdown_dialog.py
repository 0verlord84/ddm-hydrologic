# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Subcatchments breakdown window.

This is where subcatchments are made. The window splits the catchment by number
of subcatchments, by minimum subcatchment size or by Strahler order, and lists
every subcatchment with its areas, the area reporting to it from upstream and
its main-stream slope.

There is only ever one subcatchment layer. Each Process overwrites it in place.
Close asks before putting back whatever was there when the window opened, and
Confirm keeps the latest result.

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
from qgis.core import QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from .catchment_geometry import channel_slopes, downstream_outlet_map, upstream_area_ha
from .compat import enum_member, log_ignored, qt_enum
from .hydrology_engine import HydrologyCancelled

LAYER_NAME = "DDM HydroLogic subcatchments - dissolved outlines temporary"
DEFAULT_MIN_AREA_M2 = 100000.0
DEFAULT_COUNT = 10

# Label, and the square metres one unit covers. A pixel is one DEM cell, so its
# size comes from the raster rather than a fixed factor.
AREA_UNITS = (
    ("pixel", None),
    ("m²", 1.0),
    ("km²", 1_000_000.0),
    ("ha", 10_000.0),
)
M2_UNIT = 1

COLUMNS = ("ID", "Area (m²)", "km²", "ha", "Label", "Upstream area (ha)", "Slope (%)")
LABEL_COLUMN = 4

MODE_COUNT = "count"
MODE_AREA = "area"
MODE_STRAHLER = "strahler"


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


def _yes_no(parent, title, text):
    """Yes/No question that defaults to No, so a stray Enter changes nothing."""
    yes = enum_member(QMessageBox, "StandardButton", "Yes")
    no = enum_member(QMessageBox, "StandardButton", "No")
    return QMessageBox.question(parent, title, text, yes | no, no) == yes


class BreakdownDialog(QDialog):
    """Makes the subcatchments and summarises them in a table."""

    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.engine = dock.engine
        self.setWindowTitle("DDM HydroLogic - subcatchments breakdown")
        self.setModal(False)
        self.resize(980, 640)

        # Whatever was there when the window opened, so Close can put it back.
        live = self._live_layer()
        self.opened_with_layer = live is not None
        self.opening_assignments = {int(k): list(v) for k, v in (dock.current_assignments or {}).items()}
        self.opening_rows = self._snapshot(live)

        self.dirty = False
        self.pending_settings = None
        self.whole_dem_accepted = False
        self.notes = self._notes_from_layer(live)
        self.highlight_band = None
        self._loading = False
        self._finished = False
        self._cleaned_up = False
        self._unit_index = M2_UNIT

        self._build_ui()
        self._apply_settings(getattr(dock, "breakdown_settings", None))
        self.reload_table()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        process_group = QGroupBox("Processing")
        process_layout = QVBoxLayout(process_group)

        self.count_radio = QRadioButton("Set n. of subcatchments")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 1000000)
        self.count_spin.setToolTip(
            "Number of subcatchments wanted. The count moves in steps as the size threshold "
            "changes, so the closest reachable count is used."
        )
        process_layout.addLayout(self._mode_row(self.count_radio, [self.count_spin]))

        self.area_radio = QRadioButton("Set by minimum subcatchment size")
        self.area_spin = QDoubleSpinBox()
        self.area_spin.setDecimals(2)
        self.area_spin.setRange(0.01, 1000000000000.0)
        if hasattr(self.area_spin, "setGroupSeparatorShown"):
            self.area_spin.setGroupSeparatorShown(True)
        self.unit_combo = QComboBox()
        for label, _factor in AREA_UNITS:
            self.unit_combo.addItem(label)
        self.unit_combo.setCurrentIndex(M2_UNIT)
        self.area_spin.setToolTip(
            "Smallest subcatchment size. Subcatchments at a catchment outlet take the area left over "
            "and can be smaller."
        )
        process_layout.addLayout(self._mode_row(self.area_radio, [self.area_spin, self.unit_combo]))

        self.strahler_radio = QRadioButton("Set by Strahler order")
        self.strahler_spin = QSpinBox()
        self.orders = self._available_orders()
        top_order = max(self.orders) if self.orders else 1
        self.strahler_spin.setRange(1, max(1, top_order))
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

        self.process_btn = QPushButton("Process subcatchments")
        process_row = QHBoxLayout()
        process_row.addStretch(1)
        process_row.addWidget(self.process_btn)
        process_layout.addLayout(process_row)
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

        if not self.orders:
            self.strahler_radio.setEnabled(False)
            self.strahler_radio.setToolTip(
                "No Strahler orders are available. Lower the display flow-path accumulation "
                "threshold in step 2 and compute the flow paths again."
            )

        for radio in (self.count_radio, self.area_radio, self.strahler_radio):
            radio.toggled.connect(self._sync_mode)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        self.process_btn.clicked.connect(self.process)
        self.csv_btn.clicked.connect(self.export_csv)
        self.close_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self.confirm)
        self.table.itemSelectionChanged.connect(self._highlight_selected_row)
        self.table.itemChanged.connect(self._note_edited)

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

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def _apply_settings(self, settings):
        """Opens on the settings that made the current subcatchments this session."""
        settings = settings or {}
        self.count_spin.setValue(int(settings.get("count", len(self.opening_assignments) or DEFAULT_COUNT)))

        unit = int(settings.get("unit", M2_UNIT))
        unit = unit if 0 <= unit < len(AREA_UNITS) else M2_UNIT
        self.unit_combo.blockSignals(True)
        self.unit_combo.setCurrentIndex(unit)
        self.unit_combo.blockSignals(False)
        self._unit_index = unit
        self.area_spin.setValue(float(settings.get("area", DEFAULT_MIN_AREA_M2)))

        top_order = self.strahler_spin.maximum()
        self.strahler_spin.setValue(max(1, min(int(settings.get("order", min(2, top_order))), top_order)))

        mode = settings.get("mode", MODE_AREA)
        if mode == MODE_STRAHLER and not self.orders:
            mode = MODE_AREA
        {MODE_COUNT: self.count_radio, MODE_STRAHLER: self.strahler_radio}.get(mode, self.area_radio).setChecked(True)
        self._sync_mode()

    def _current_settings(self):
        if self.count_radio.isChecked():
            mode = MODE_COUNT
        elif self.strahler_radio.isChecked():
            mode = MODE_STRAHLER
        else:
            mode = MODE_AREA
        return {
            "mode": mode,
            "count": int(self.count_spin.value()),
            "area": float(self.area_spin.value()),
            "unit": int(self.unit_combo.currentIndex()),
            "order": int(self.strahler_spin.value()),
        }

    def _sync_mode(self):
        """Only the active mode accepts input; the other two grey out."""
        self.count_spin.setEnabled(self.count_radio.isChecked())
        self.area_spin.setEnabled(self.area_radio.isChecked())
        self.unit_combo.setEnabled(self.area_radio.isChecked())
        self.strahler_spin.setEnabled(self.strahler_radio.isChecked())

    def _square_metres_per_unit(self, index):
        _label, factor = AREA_UNITS[index]
        if factor is None:
            return float(getattr(self.engine, "cell_area", 0.0) or 0.0)
        return float(factor)

    def _unit_changed(self, index):
        """Keeps the same area when the unit changes, rather than the same number."""
        old = self._square_metres_per_unit(self._unit_index)
        new = self._square_metres_per_unit(index)
        self._unit_index = int(index)
        if old > 0 and new > 0:
            self.area_spin.setValue(float(self.area_spin.value()) * old / new)

    def _set_controls_enabled(self, enabled):
        for widget in (self.count_radio, self.area_radio, self.strahler_radio, self.process_btn,
                       self.csv_btn, self.close_btn, self.table):
            widget.setEnabled(bool(enabled))
        if enabled:
            self._sync_mode()
            self.strahler_radio.setEnabled(bool(self.orders))
        else:
            for widget in (self.count_spin, self.area_spin, self.unit_combo, self.strahler_spin):
                widget.setEnabled(False)
        # Nothing to confirm until something has been processed in this visit.
        self.confirm_btn.setEnabled(bool(enabled) and self.dirty)

    # ------------------------------------------------------------------
    # the layer
    # ------------------------------------------------------------------
    def _live_layer(self):
        if self.dock._engine_layer_is_available("subcatchment_layer"):
            return self.engine.subcatchment_layer
        return None

    @staticmethod
    def _snapshot(layer):
        """Geometry and attributes of every feature, enough to rebuild the layer."""
        if layer is None:
            return []
        rows = []
        for feat in layer.getFeatures():
            rows.append((QgsGeometry(feat.geometry()), list(feat.attributes())))
        return rows

    @staticmethod
    def _notes_from_layer(layer):
        notes = {}
        if layer is None or layer.fields().indexOf("label") < 0:
            return notes
        for feat in layer.getFeatures():
            try:
                text = feat["label"]
                if text:
                    notes[int(feat["outlet_id"])] = str(text)
            except Exception:
                log_ignored("breakdown_dialog._notes_from_layer")
        return notes

    @staticmethod
    def _replace_features(layer, rows):
        """Swaps every feature in a layer, so the same layer stays on the map."""
        provider = layer.dataProvider()
        old_ids = [int(feat.id()) for feat in layer.getFeatures()]
        if old_ids:
            provider.deleteFeatures(old_ids)
        features = []
        for geom, attrs in rows:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry(geom))
            feat.setAttributes(list(attrs))
            features.append(feat)
        if features:
            provider.addFeatures(features)
        layer.updateExtents()
        layer.triggerRepaint()

    def _install(self, assignments, cells):
        """Writes a freshly processed set into the one subcatchment layer."""
        self._clear_highlight()
        live = self._live_layer()
        fresh = self.engine.create_subcatchment_layer(assignments, min_cells=max(1, int(cells or 1)))
        if live is None:
            fresh.setName(LAYER_NAME)
            QgsProject.instance().addMapLayer(fresh)
            live = fresh
        else:
            self._replace_features(live, self._snapshot(fresh))
            self.engine.subcatchment_layer = live
        self.dock._recalculate_subcatchment_area_fields(live)
        self.engine.apply_breakdown_ids(live, assignments, self.dock.outlet_cells)
        self.dock.current_assignments = {int(k): list(v) for k, v in assignments.items()}

    def _revert(self):
        """Puts back whatever existed when the window opened."""
        self._clear_highlight()
        live = self._live_layer()
        if not self.opened_with_layer:
            if live is not None:
                try:
                    QgsProject.instance().removeMapLayer(live.id())
                except Exception:
                    log_ignored("breakdown_dialog._revert")
            self.engine.subcatchment_layer = None
            self.dock.current_assignments = {}
        else:
            if live is not None:
                self._replace_features(live, self.opening_rows)
            self.dock.current_assignments = {int(k): list(v) for k, v in self.opening_assignments.items()}
        self.dirty = False

    # ------------------------------------------------------------------
    # the table
    # ------------------------------------------------------------------
    def reload_table(self):
        """Rebuilds every row from the subcatchments currently on the map."""
        layer = self._live_layer()
        assignments = self.dock.current_assignments or {}
        if layer is None or not assignments:
            self.table.setRowCount(0)
            self.summary_label.setText(
                "No subcatchments yet. Choose how to split the catchment and press Process subcatchments."
            )
            self._set_controls_enabled(True)
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
            state = "processed, not yet confirmed" if self.dirty else "current"
            self.summary_label.setText(
                f"{len(outlets):,} subcatchments ({state}), {total_ha:,.2f} ha in total. "
                "Slope is the equal-area slope of each subcatchment main flowpath."
            )
        finally:
            self._loading = False
            QApplication.restoreOverrideCursor()
            self._set_controls_enabled(True)

    def _note_edited(self, item):
        """Keeps a typed label and writes it straight onto the layer."""
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

        layer = self._live_layer()
        feature_id = getattr(id_item, "feature_id", None)
        if layer is None or feature_id is None:
            return
        label_idx = layer.fields().indexOf("label")
        if label_idx < 0:
            return
        try:
            layer.dataProvider().changeAttributeValues({int(feature_id): {label_idx: text}})
        except Exception:
            log_ignored("breakdown_dialog._note_edited")

    # ------------------------------------------------------------------
    # canvas highlight
    # ------------------------------------------------------------------
    def _highlight_selected_row(self):
        self._clear_highlight()
        items = self.table.selectedItems()
        if not items:
            return
        id_item = self.table.item(items[0].row(), 0)
        layer = self._live_layer()
        feature_id = getattr(id_item, "feature_id", None)
        if layer is None or feature_id is None:
            return
        try:
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
    # processing
    # ------------------------------------------------------------------
    def _requested_cells(self):
        """Minimum subcatchment size in DEM cells, and how to describe it."""
        index = max(0, self.unit_combo.currentIndex())
        label, factor = AREA_UNITS[index]
        value = float(self.area_spin.value())
        if factor is None:
            return max(1, int(round(value))), f"{value:,.0f} {label}"
        return self.engine.cells_for_area_m2(value * float(factor)), f"{value:,.2f} {label}"

    def process(self):
        if self.notes and not _yes_no(
            self,
            "Labels will be discarded",
            "Processing replaces every subcatchment, so the labels you have typed cannot "
            "follow them and will be cleared. Do you want to continue?",
        ):
            return

        boundary = self.dock.outlet_cells or None
        if boundary is None and not self.whole_dem_accepted:
            if not _yes_no(
                self,
                "No outlet line drawn",
                "An outlet line was not drawn. This will process the whole DEM. Do you wish to continue?",
            ):
                self.dock.status_label.setText(
                    "Subcatchment processing cancelled. Draw an outlet line in 6. Draw outlet line(s), "
                    "then press Process subcatchments again."
                )
                return
            self.whole_dem_accepted = True

        settings = self._current_settings()
        self.dock.abort_requested = False
        self.dock.active_operation = "breakdown"
        self.dock._set_busy(True)
        self._set_controls_enabled(False)
        finished = False
        warning = ""
        try:
            if settings["mode"] == MODE_COUNT:
                target = settings["count"]
                assignments, achieved, cells = self.engine.build_target_count_subcatchments(target, boundary)
                if achieved != target:
                    floor = self.engine.terminal_outlet_count(boundary)
                    if target < floor:
                        warning = (
                            f"{achieved:,} subcatchments were created against the {target:,} requested. "
                            f"The catchment drains out at {floor:,} separate points, and each of those has to be "
                            f"a subcatchment, so {floor:,} is the fewest possible. An outlet line makes one of "
                            "these for every flow path it crosses; draw it across a single flow path to allow fewer."
                        )
                    else:
                        warning = (
                            f"{achieved:,} subcatchments were created against the {target:,} requested. "
                            "The count moves in steps as the size threshold changes, so an exact number is "
                            "usually out of reach. The nearest achievable breakdown is shown."
                        )
                description = f"number of subcatchments {target:,}"
            elif settings["mode"] == MODE_AREA:
                cells, described = self._requested_cells()
                assignments = self.engine.build_area_threshold_subcatchments(
                    min_cells=cells, boundary_outlet_cells=boundary, include_residual=True)
                description = f"minimum subcatchment size {described}"
            else:
                order = settings["order"]
                assignments = self.engine.build_strahler_confluence_subcatchments(order, boundary)
                cells = None
                description = f"confluences of Strahler order {order} and above"

            if not assignments:
                QMessageBox.warning(
                    self,
                    "DDM HydroLogic",
                    "No subcatchments could be created with those settings. Try a smaller minimum "
                    "subcatchment size, a larger number of subcatchments, or a lower Strahler order.",
                )
                return

            self._install(assignments, cells)
            if boundary:
                self.dock._restrict_flow_layer_to_assignment_domain(assignments)
            self.dock._clear_flow_selection()
            self.notes = {}
            self.dirty = True
            self.pending_settings = settings
            self.reload_table()
            finished = True
            self.dock.status_label.setText(
                f"Subcatchments processed by {description}: {len(assignments):,} subcatchment(s). "
                "Confirm or close the Subcatchments breakdown window."
            )
            if warning:
                QMessageBox.information(self, "Nearest achievable breakdown", warning)
        except HydrologyCancelled:
            self.dock.status_label.setText("Subcatchment processing aborted. The subcatchments on the map are unchanged.")
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "DDM HydroLogic", f"Could not process the subcatchments:\n\n{exc}")
        finally:
            # Every dock operation ends by setting the bar; without this it froze
            # wherever the engine last reported, usually in the high seventies.
            self.dock.progress.setValue(100 if finished else 0)
            self.dock.active_operation = None
            self.dock._set_busy(False)
            self._set_controls_enabled(True)

    # ------------------------------------------------------------------
    # leaving the window
    # ------------------------------------------------------------------
    def export_csv(self):
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "DDM HydroLogic", "There is nothing to export yet.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export the subcatchments breakdown",
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
        self.dock.status_label.setText(f"Subcatchments breakdown exported: {path}")

    def confirm(self):
        """Keeps the latest processed subcatchments."""
        if not self.dirty:
            return
        live = self._live_layer()
        if live is not None:
            self.engine.apply_breakdown_ids(live, self.dock.current_assignments, self.dock.outlet_cells, self.notes)
        if self.pending_settings:
            self.dock.breakdown_settings = dict(self.pending_settings)
        self.dirty = False
        self.dock.status_label.setText(
            f"Subcatchments confirmed: {len(self.dock.current_assignments):,} subcatchment(s) are now the current output."
        )
        self._finished = True
        self.accept()

    def _may_leave(self):
        """Asks before discarding unconfirmed processing; False keeps the window open."""
        if self._finished:
            return True
        if self.dirty:
            if not _yes_no(
                self,
                "Discard the processed subcatchments?",
                "The subcatchments processed in this window have not been confirmed. Discard them "
                "and go back to what was there when the window opened?",
            ):
                return False
            self._revert()
            self.dock.status_label.setText(
                "Subcatchments breakdown closed. The processing was discarded and the previous state is back."
            )
        self._finished = True
        return True

    def reject(self):
        if not self._may_leave():
            return
        super().reject()

    def closeEvent(self, event):
        if not self._may_leave():
            event.ignore()
            return
        self._cleanup()
        super().closeEvent(event)

    def done(self, result):
        self._cleanup()
        super().done(result)

    def _cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._clear_highlight()
        if getattr(self.dock, "breakdown_window", None) is self:
            self.dock.breakdown_window = None
        try:
            self.dock._refresh_step_gating(force=True)
        except Exception:
            log_ignored("breakdown_dialog._cleanup")
        self.deleteLater()
