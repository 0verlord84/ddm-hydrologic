# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Write the GIS companion shapefiles that go with a model export.

Every model export can drop five shapefiles beside it so the model can be read
back on the map: the sub-area polygons, their centroids, the sub-area entry
points where rainfall-excess joins the channel network, the nodal links carrying
the routing topology, and the stream network.

Each layer carries Model_ID, holding the sub-area label exactly as it appears in
the model file, so a node in the .catg, .vec, .csv, .wbn or .xpx can be found on
the map and the other way round.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .catchment_geometry import accumulation_of, cell_distance, subarea_metrics

CENTROIDS = "Centroids"
ENTRY_POINTS = "EntryPoints"
NODAL_LINKS = "NodalLinks"
SUBAREAS = "Subareas"
STREAMS = "Streams"

# QVariant type plus the memory-provider type name for each field kind.
_FIELD_TYPES = {
    "string": (QVariant.String, "string"),
    "integer": (QVariant.Int, "integer"),
    "double": (QVariant.Double, "double"),
}

# (name, kind, length, precision)
_SUBAREA_FIELDS = [
    ("ID", "integer", 9, 0),
    ("Model_ID", "string", 20, 0),
    ("Area_km2", "double", 15, 5),
    ("CatSlope", "double", 15, 5),
    ("Length_km", "double", 15, 5),
    ("Slope_m_m", "double", 15, 5),
    ("FracImp", "double", 15, 5),
    ("FracUrban", "double", 15, 5),
    ("FracForest", "double", 15, 5),
    ("Downstream", "integer", 9, 0),
]

_LINK_FIELDS = [
    ("From_ID", "integer", 9, 0),
    ("To_ID", "integer", 9, 0),
    ("ID", "integer", 9, 0),
    ("Model_ID", "string", 20, 0),
]

_STREAM_FIELDS = [
    ("ID", "integer", 9, 0),
    ("Model_ID", "string", 20, 0),
    ("Strahler", "integer", 9, 0),
    ("Length_km", "double", 15, 5),
]


def _make_field(name: str, kind: str, width: int, precision: int) -> "QgsField":
    qvariant_type, type_name = _FIELD_TYPES[kind]
    return QgsField(name, qvariant_type, type_name, width, precision)


def _numeric_id(label, fallback: int) -> int:
    """Numeric id taken from a model label, so ID lines up with Model_ID.

    URBS numbers its sub-areas directly, WBNM and XP-RAFTS use names like S001;
    both reduce to the same integer. Anything without digits keeps its position.
    """
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    try:
        return int(digits) if digits else int(fallback)
    except Exception:
        return int(fallback)


class GisOutputError(Exception):
    """Raised when the companion shapefiles cannot be written."""


def _writer_no_error_code():
    if hasattr(QgsVectorFileWriter, "NoError"):
        return QgsVectorFileWriter.NoError
    writer_error = getattr(QgsVectorFileWriter, "WriterError", None)
    if writer_error is not None and hasattr(writer_error, "NoError"):
        return writer_error.NoError
    return 0


def _fraction(feat, field_name: str, present: set) -> float:
    if field_name not in present:
        return 0.0
    try:
        value = float(feat[field_name])
        return value if math.isfinite(value) else 0.0
    except Exception:
        return 0.0


def _field_names(feat) -> set:
    try:
        return {f.name() for f in feat.fields()}
    except Exception:
        return set()


def _area_km2(feat) -> float:
    try:
        geom = feat.geometry()
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            area = float(geom.area())
            if math.isfinite(area) and area > 0:
                return area / 1_000_000.0
    except Exception:
        pass
    return 0.0


def _write_layer(path: str, geometry_type: str, fields_spec, rows, crs) -> None:
    """Build an in-memory layer from ``rows`` of (geometry, attributes) and save it."""
    layer = QgsVectorLayer(geometry_type, os.path.basename(path), "memory")
    if not layer.isValid():
        raise GisOutputError("QGIS could not create an in-memory layer for the GIS outputs.")
    if crs is not None:
        layer.setCrs(crs)
    provider = layer.dataProvider()
    provider.addAttributes([_make_field(n, kind, ln, pr) for (n, kind, ln, pr) in fields_spec])
    layer.updateFields()

    features = []
    for geom, attrs in rows:
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttributes(list(attrs))
        features.append(feat)
    if features:
        provider.addFeatures(features)
    layer.updateExtents()

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile"
    options.fileEncoding = "UTF-8"
    if hasattr(QgsVectorFileWriter, "CreateOrOverwriteFile"):
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    else:
        action_enum = getattr(QgsVectorFileWriter, "ActionOnExistingFile", None)
        if action_enum is not None:
            options.actionOnExistingFile = action_enum.CreateOrOverwriteFile

    if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsProject.instance().transformContext(), options
        )
    else:  # Compatibility fallback for older QGIS writer signatures.
        result = QgsVectorFileWriter.writeAsVectorFormat(
            layer, path, "UTF-8", layer.crs(), "ESRI Shapefile", onlySelected=False
        )
    error_code = result[0] if isinstance(result, tuple) else result
    if error_code != _writer_no_error_code():
        raise GisOutputError(f"Could not write {os.path.basename(path)}. Writer result: {result}")


def write_model_gis_outputs(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_dir: str,
    prefix: str,
    model_ids: Dict[int, str],
    downstream_map: Dict[int, Optional[int]],
) -> Tuple[str, List[str]]:
    """Write the five companion shapefiles for a model export.

    ``model_ids`` maps a subcatchment outlet cell to the label that identifies it
    in the model file, and ``downstream_map`` maps it to the outlet cell it drains
    to (or None at a model outlet).

    Returns ``(output_dir, written_paths)``.
    """
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise GisOutputError("No valid subcatchment layer is available for the GIS outputs.")

    features: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            features[int(feat["outlet_id"])] = feat
        except Exception:
            continue

    outlets = [int(o) for o in model_ids if int(o) in features]
    if not outlets:
        raise GisOutputError("No subcatchments matched the model outputs, so no GIS files were written.")
    outlets.sort(key=lambda o: _numeric_id(model_ids[int(o)], o))

    try:
        crs = engine.dem_layer.crs()
    except Exception:
        crs = None

    metrics = subarea_metrics(engine, features, assignments, outlets)
    # Numeric ids keep the shapefile tables joinable even where a model labels its
    # sub-areas with text; Model_ID carries the label itself.
    number_of = {int(o): _numeric_id(model_ids[int(o)], index)
                 for index, o in enumerate(outlets, start=1)}

    displayed = getattr(engine, "cell_to_feature", {}) or {}
    strahler_by_cell = getattr(engine, "display_strahler_by_cell", {}) or {}
    downstream_cells = getattr(engine, "downstream", None)

    subarea_rows = []
    centroid_rows = []
    entry_rows = []
    link_rows = []
    stream_rows = []

    for outlet in outlets:
        feat = features[int(outlet)]
        info = metrics[int(outlet)]
        present = _field_names(feat)
        ds_outlet = downstream_map.get(int(outlet))
        attrs = [
            int(number_of[int(outlet)]),
            str(model_ids[int(outlet)]),
            round(_area_km2(feat), 5),
            round(float(info["catchment_slope"]), 5),
            round(float(info["length_km"]), 5),
            round(float(info["channel_slope"]), 5),
            round(_fraction(feat, "FracImp", present), 5),
            round(_fraction(feat, "FracUrban", present), 5),
            round(_fraction(feat, "FracForest", present), 5),
            int(number_of[int(ds_outlet)]) if ds_outlet is not None and int(ds_outlet) in number_of else -1,
        ]

        geom = feat.geometry()
        subarea_rows.append((QgsGeometry(geom) if geom is not None else None, attrs))

        centroid = QgsGeometry.fromPointXY(
            QgsPointXY(float(info["centroid_x"]), float(info["centroid_y"]))
        )
        centroid_rows.append((centroid, attrs))

        entry = int(info.get("entry_cell", -1))
        entry_cell_id = entry if entry >= 0 else int(outlet)
        entry_pt = engine.cell_center(entry_cell_id)
        entry_rows.append((QgsGeometry.fromPointXY(QgsPointXY(entry_pt)), attrs))

        # Nodal link: a straight line from this sub-area's entry point to the
        # entry point of the sub-area it drains into.
        if ds_outlet is not None and int(ds_outlet) in metrics:
            ds_info = metrics[int(ds_outlet)]
            ds_entry = int(ds_info.get("entry_cell", -1))
            ds_cell = ds_entry if ds_entry >= 0 else int(ds_outlet)
            ds_pt = engine.cell_center(ds_cell)
            link_rows.append((
                QgsGeometry.fromPolylineXY([QgsPointXY(entry_pt), QgsPointXY(ds_pt)]),
                [
                    int(number_of[int(outlet)]),
                    int(number_of[int(ds_outlet)]),
                    int(number_of[int(outlet)]),
                    str(model_ids[int(outlet)]),
                ],
            ))

        # Streams: every displayed flow-path segment inside this sub-area.
        for cell in (assignments.get(outlet) or []):
            cell = int(cell)
            if cell not in displayed:
                continue
            try:
                down = int(downstream_cells[cell])
            except Exception:
                down = -1
            if down < 0:
                continue
            a = engine.cell_center(cell)
            b = engine.cell_center(down)
            stream_rows.append((
                QgsGeometry.fromPolylineXY([QgsPointXY(a), QgsPointXY(b)]),
                [
                    int(number_of[int(outlet)]),
                    str(model_ids[int(outlet)]),
                    int(strahler_by_cell.get(cell, 1) or 1),
                    round(cell_distance(engine, cell, down) / 1000.0, 5),
                ],
            ))

    os.makedirs(os.path.abspath(output_dir), exist_ok=True)
    written: List[str] = []
    plan = [
        (SUBAREAS, "MultiPolygon", _SUBAREA_FIELDS, subarea_rows),
        (CENTROIDS, "Point", _SUBAREA_FIELDS, centroid_rows),
        (ENTRY_POINTS, "Point", _SUBAREA_FIELDS, entry_rows),
        (NODAL_LINKS, "LineString", _LINK_FIELDS, link_rows),
        (STREAMS, "LineString", _STREAM_FIELDS, stream_rows),
    ]
    for name, geometry_type, fields_spec, rows in plan:
        path = os.path.join(output_dir, f"{prefix}_{name}.shp")
        _write_layer(path, geometry_type, fields_spec, rows, crs)
        written.append(path)

    return output_dir, written
