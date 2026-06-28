# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Write the standard TUFLOW region shapefiles from the processed subcatchments.

A TUFLOW 2D model reads most of its spatial inputs from GIS layers whose names
follow the ``2d_<type>_<scenario>_R`` convention (the ``_R`` marks a region/
polygon layer). This exporter dissolves all processed subcatchments into one
topologically valid catchment boundary polygon and writes that single feature
into some of the most popular region layers a model setup usually starts from:

    2d_code   active-area code polygon (Code = 1, the cells TUFLOW computes)
    2d_loc    model location/orientation region
    2d_mat    materials region (roughness n coeff) - Values left blank for user to fill in
    2d_soil   soils region - SoilID left blank for user to fill in
    2d_rf     direct-rainfall region (Name plus the f1/f2 multipliers)
    2d_po     plot-output region - Type/Label/Comment left blank
    2d_qnl    quadtree nesting-level region - Nest_Level left blank

Every shapefile is written in the CRS of the source DEM. Field names, types,
widths and precisions follow the TUFLOW data formats, so the files load into a
TUFLOW model without editing the schema; only the blank attribute values are
left for the modeller to complete.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

# QVariant type plus the memory-provider type name for each field kind.
_FIELD_TYPES = {
    "string": (QVariant.String, "string"),
    "integer": (QVariant.Int, "integer"),
    "double": (QVariant.Double, "double"),
}

# The region layers, written exactly to the TUFLOW data formats:
# (base filename, [(field, kind, width, precision)], [attribute values]).
# A None value writes NULL, which a shapefile shows as a blank cell.
# Code is an integer field, so "001" is stored as the integer 1 - DBF integer
# columns cannot keep leading zeros and TUFLOW reads the value, not the text.
TUFLOW_LAYERS = [
    ("2d_rf__s1_s2_e1_e2_e3_EXG_001_R",
     [("Name", "string", 100, 0), ("f1", "double", 15, 5), ("f2", "double", 15, 5)],
     ["Rain_001", 1.0, 1.0]),
    ("2d_qnl__s1_s2_e1_e2_e3_EXG_001_R",
     [("Nest_Level", "integer", 8, 0)],
     [None]),
    ("2d_po__s1_s2_e1_e2_e3_EXG_001_R",
     [("Type", "string", 20, 0), ("Label", "string", 30, 0), ("Comment", "string", 250, 0)],
     [None, None, None]),
    ("2d_mat__s1_s2_e1_e2_e3_EXG_001_R",
     [("Material", "integer", 8, 0)],
     [None]),
    ("2d_loc__s1_s2_e1_e2_e3_EXG_001_R",
     [("Comment", "string", 250, 0)],
     ["Region_001"]),
    ("2d_code__s1_s2_e1_e2_e3_EXG_001_R",
     [("Code", "integer", 8, 0)],
     [1]),
    ("2d_soil__s1_s2_e1_e2_e3_EXG_001_R",
     [("SoilID", "integer", 8, 0)],
     [None]),
]


class TuflowExportError(Exception):
    """Raised when the current plugin outputs cannot be written as TUFLOW layers."""


def _writer_no_error_code():
    """Returns the vector-writer success code for QGIS 3 or QGIS 4."""
    if hasattr(QgsVectorFileWriter, "NoError"):
        return QgsVectorFileWriter.NoError
    writer_error = getattr(QgsVectorFileWriter, "WriterError", None)
    if writer_error is not None and hasattr(writer_error, "NoError"):
        return writer_error.NoError
    return 0


def _make_field(name: str, kind: str, width: int, precision: int) -> "QgsField":
    qvariant_type, type_name = _FIELD_TYPES[kind]
    return QgsField(name, qvariant_type, type_name, width, precision)


# --- reading values back out of the engine --------------------------------

def _subcatchment_features_by_outlet(engine) -> Dict[int, object]:
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise TuflowExportError(
            "No valid subcatchment layer is available. Press Process subcatchments first."
        )
    features: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            features[int(feat["outlet_id"])] = feat
        except Exception:
            continue
    if not features:
        raise TuflowExportError(
            "The subcatchment layer contains no outlet_id features to convert."
        )
    return features


def _feature_area_m2(feat) -> float:
    try:
        geom = feat.geometry()
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            area = float(geom.area())
            if math.isfinite(area) and area > 0:
                return area
    except Exception:
        pass
    try:
        area = float(feat["area_m2"])
        if math.isfinite(area) and area > 0:
            return area
    except Exception:
        pass
    return 0.0


def _dem_crs(engine):
    try:
        crs = engine.dem_layer.crs()
        if crs is not None and crs.isValid():
            return crs
    except Exception:
        pass
    return None


# --- building the boundary polygon -----------------------------------------

def _dissolved_boundary(features: Iterable[object]) -> "QgsGeometry":
    """Merges all subcatchment polygons into one valid multipolygon boundary."""
    geoms: List[QgsGeometry] = []
    for feat in features:
        try:
            geom = feat.geometry()
            if geom is not None and not geom.isNull() and not geom.isEmpty():
                geoms.append(QgsGeometry(geom))
        except Exception:
            continue
    if not geoms:
        raise TuflowExportError(
            "None of the processed subcatchments has a usable polygon geometry."
        )

    union: Optional[QgsGeometry] = None
    try:
        union = QgsGeometry.unaryUnion(geoms)
    except Exception:
        union = None
    if union is None or union.isNull() or union.isEmpty():
        # Pairwise fallback for QGIS builds where unaryUnion is unavailable.
        union = geoms[0]
        for geom in geoms[1:]:
            try:
                combined = union.combine(geom)
            except Exception:
                combined = None
            if combined is not None and not combined.isNull() and not combined.isEmpty():
                union = combined
    if union is None or union.isNull() or union.isEmpty():
        raise TuflowExportError(
            "The subcatchment polygons could not be merged into a catchment boundary."
        )

    # Snap out any self-intersections the union may carry, then normalise to
    # multipolygon so every output layer has a consistent geometry type.
    try:
        repaired = union.makeValid()
        if repaired is not None and not repaired.isNull() and not repaired.isEmpty():
            union = repaired
    except Exception:
        pass
    try:
        union.convertToMultiType()
    except Exception:
        pass
    return union


def _transform_to_dem_crs(geometry: "QgsGeometry", source_layer, dem_crs) -> "QgsGeometry":
    """Reprojects the boundary into the DEM CRS when the layers ever diverge."""
    if dem_crs is None:
        return geometry
    try:
        source_crs = source_layer.crs()
        if source_crs is None or not source_crs.isValid() or source_crs == dem_crs:
            return geometry
        transform = QgsCoordinateTransform(source_crs, dem_crs, QgsProject.instance())
        geometry.transform(transform)
    except Exception:
        # Subcatchments are derived from the DEM, so the CRSs match in
        # practice; a failed transform should not abort the export.
        pass
    return geometry


# --- writing one region shapefile -------------------------------------------

def _write_region_shapefile(path: str, fields_spec, values, geometry: "QgsGeometry", crs) -> None:
    layer = QgsVectorLayer("MultiPolygon", os.path.basename(path), "memory")
    if not layer.isValid():
        raise TuflowExportError("QGIS could not create an in-memory layer for the export.")
    if crs is not None:
        layer.setCrs(crs)

    provider = layer.dataProvider()
    provider.addAttributes([_make_field(*spec) for spec in fields_spec])
    layer.updateFields()

    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry(geometry))
    feature.setAttributes(list(values))
    provider.addFeatures([feature])
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
            "ESRI Shapefile",
            onlySelected=False,
        )
    error_code = result[0] if isinstance(result, tuple) else result
    if error_code != _writer_no_error_code():
        raise TuflowExportError(
            f"Could not write {os.path.basename(path)}. Writer result: {result}"
        )


# --- the export -------------------------------------------------------------

def write_tuflow_from_engine(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_dir: str,
) -> Tuple[str, List[str], float]:
    """Writes first-pass TUFLOW region shapefiles into ``output_dir``.

    Returns ``(output_dir, written_paths, total_area_ha)``.
    """
    if engine is None:
        raise TuflowExportError("No DEM flow graph is available. Press Compute first.")
    if not assignments:
        raise TuflowExportError(
            "No subcatchment assignments are available. Press Process subcatchments first."
        )

    features = _subcatchment_features_by_outlet(engine)
    selected = {int(o) for o, cells in assignments.items() if cells and int(o) in features}
    if not selected:
        raise TuflowExportError(
            "No processed subcatchments with matching outlet_id values are available to export."
        )

    chosen = [features[outlet] for outlet in sorted(selected)]
    total_area_ha = sum(_feature_area_m2(feat) for feat in chosen) / 10_000.0

    boundary = _dissolved_boundary(chosen)
    crs = _dem_crs(engine)
    boundary = _transform_to_dem_crs(boundary, engine.subcatchment_layer, crs)

    os.makedirs(os.path.abspath(output_dir), exist_ok=True)
    written: List[str] = []
    for base_name, fields_spec, values in TUFLOW_LAYERS:
        path = os.path.join(output_dir, base_name + ".shp")
        _write_region_shapefile(path, fields_spec, values, boundary, crs)
        written.append(path)

    return output_dir, written, round(total_area_ha, 3)
