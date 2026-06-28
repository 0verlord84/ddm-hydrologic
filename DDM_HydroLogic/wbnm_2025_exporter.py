# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Write a WBNM runfile (.wbn) from the plugin's processed subcatchments.

WBNM reads its runfiles by fixed columns and block markers, so this module
only fills in only the spatial information the QGIS layers can supply: the subarea topology,
the surface areas, the catchment/outlet coordinates and which subareas need a
natural stream segment. Rainfall, losses and structures are written as a
clearly-labelled placeholder storm and empty structure blocks, leaving a
complete but dummy runfile that the modeller finishes in WBNM.

The runfile layout follows the WBNM2023 "Runfile Structure" document:
  - 8 lines in the preamble,
  - two blank lines between blocks and none inside them,
  - 12-character fixed fields, with the downstream subarea name in the
    topology block starting in column 62.
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsGeometry, QgsProject

# Two blank lines separate every block; field width is 12 characters.
FIELD = 12
BLOCK_GAP = ("", "")


class Wbnm2025ExportError(Exception):
    """Raised when the current plugin outputs cannot be written as a runfile."""


def _num(value: float, decimals: int = 2) -> str:
    """Formats one numeric field, right-justified in a 12-character column."""
    if not math.isfinite(value):
        value = 0.0
    return f"{value:{FIELD}.{decimals}f}"


def _int(value: int) -> str:
    """Formats one integer field, right-justified in a 12-character column."""
    return f"{int(value):{FIELD}d}"


def _name(text: str) -> str:
    """Formats a subarea/gauge name, left-justified and clipped to 12 chars."""
    return f"{str(text)[:FIELD]:<{FIELD}s}"


def _fields(*values: float, decimals: int = 2) -> str:
    """A row of right-justified numeric fields with no leading name."""
    return "".join(_num(v, decimals) for v in values)


def _row(name: str, *values: float, decimals: int = 2) -> str:
    """A named data row: 12-char name followed by right-justified numbers."""
    return _name(name) + _fields(*values, decimals=decimals)


def _topology_row(name: str, cg_e, cg_n, out_e, out_n, downstream: str) -> str:
    """Topology row. The four coordinates fill columns 13-60 so the downstream
    name lands in column 62, as WBNM requires for a reliable read."""
    return _row(name, cg_e, cg_n, out_e, out_n) + " " + downstream


def _block(start: str, body: Iterable[str], end: str) -> List[str]:
    return [start, *body, end, *BLOCK_GAP]


# --- reading values back out of the engine --------------------------------

def _subcatchment_features_by_outlet(engine) -> Dict[int, object]:
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise Wbnm2025ExportError(
            "No valid subcatchment layer is available. Press Process subcatchments first."
        )
    features: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            features[int(feat["outlet_id"])] = feat
        except Exception:
            continue
    if not features:
        raise Wbnm2025ExportError(
            "The subcatchment layer contains no outlet_id features to convert."
        )
    return features


def _feature_area_m2(feat) -> float:
    """Subarea area in m², preferring live geometry over the stored field."""
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


def _feature_centroid_xy(feat) -> Tuple[float, float]:
    """A representative point inside the subarea, used as its centre of gravity."""
    geom = feat.geometry()
    if geom is None or geom.isNull() or geom.isEmpty():
        return (0.0, 0.0)
    try:
        # pointOnSurface stays inside concave shapes where a centroid would not.
        point = geom.pointOnSurface()
        if point is not None and not point.isNull() and not point.isEmpty():
            p = point.asPoint()
            return (float(p.x()), float(p.y()))
    except Exception:
        pass
    try:
        p = geom.centroid().asPoint()
        return (float(p.x()), float(p.y()))
    except Exception:
        bbox = geom.boundingBox()
        return ((bbox.xMinimum() + bbox.xMaximum()) / 2.0,
                (bbox.yMinimum() + bbox.yMaximum()) / 2.0)


def _catchment_centroid(features: Iterable[object]) -> Tuple[float, float]:
    """Centroid of the whole catchment, used in the display block."""
    geoms = []
    for feat in features:
        try:
            geom = feat.geometry()
            if geom is not None and not geom.isNull() and not geom.isEmpty():
                geoms.append(QgsGeometry(geom))
        except Exception:
            continue
    if not geoms:
        return (0.0, 0.0)
    try:
        union = QgsGeometry.unaryUnion(geoms)
        if union is not None and not union.isNull() and not union.isEmpty():
            p = union.centroid().asPoint()
            return (float(p.x()), float(p.y()))
    except Exception:
        pass
    # Fall back to the mean of the per-subarea bounding-box centres.
    centres = []
    for geom in geoms:
        try:
            b = geom.boundingBox()
            centres.append(((b.xMinimum() + b.xMaximum()) / 2.0,
                            (b.yMinimum() + b.yMaximum()) / 2.0))
        except Exception:
            continue
    if not centres:
        return (0.0, 0.0)
    return (sum(x for x, _ in centres) / len(centres),
            sum(y for _, y in centres) / len(centres))


def _epsg_code(engine) -> int:
    try:
        authid = engine.dem_layer.crs().authid() or ""
        if authid.upper().startswith("EPSG:"):
            return int(authid.split(":", 1)[1])
    except Exception:
        pass
    return 0


def _project_path() -> str:
    try:
        return QgsProject.instance().fileName() or "No QGIS project file saved yet"
    except Exception:
        return "No QGIS project file saved yet"


# --- working out the subarea network --------------------------------------

def _downstream_map(engine, assignments, selected) -> Dict[int, Optional[int]]:
    """Maps each subarea outlet to the next downstream selected outlet, or None
    when it drains out of the model (the SINK)."""
    cell_to_outlet: Dict[int, int] = {}
    domain: set = set()
    for outlet_id, cells in assignments.items():
        outlet_id = int(outlet_id)
        if outlet_id not in selected:
            continue
        for cell in cells or []:
            cell_to_outlet[int(cell)] = outlet_id
            domain.add(int(cell))

    ds_map: Dict[int, Optional[int]] = {}
    for outlet_id in selected:
        outlet_id = int(outlet_id)
        current = outlet_id
        seen = {outlet_id}
        downstream = None
        while True:
            try:
                nxt = int(engine.downstream[int(current)])
            except Exception:
                nxt = -1
            if nxt < 0 or nxt in seen:
                break
            seen.add(nxt)
            other = cell_to_outlet.get(nxt)
            if other is not None and int(other) != outlet_id:
                downstream = int(other)
                break
            if nxt not in domain:
                # The flow path has left the processed area: this is a terminal.
                break
            current = nxt
        ds_map[outlet_id] = downstream
    return ds_map


def _topological_order(ds_map, engine) -> List[int]:
    """Orders subareas upstream-to-downstream, as the topology block expects."""
    depth_cache: Dict[int, int] = {}

    def depth(outlet_id: int) -> int:
        outlet_id = int(outlet_id)
        if outlet_id in depth_cache:
            return depth_cache[outlet_id]
        seen: set = set()
        cursor, d = outlet_id, 0
        while True:
            nxt = ds_map.get(int(cursor))
            if nxt is None or int(nxt) in seen:
                break
            seen.add(int(cursor))
            d += 1
            cursor = int(nxt)
        depth_cache[outlet_id] = d
        return d

    accumulation = getattr(engine, "accumulation", None)

    def accum(outlet_id: int) -> int:
        try:
            if accumulation is not None and hasattr(accumulation, "get"):
                return int(accumulation.get(int(outlet_id), 0))
            return int(accumulation[int(outlet_id)])
        except Exception:
            return 0

    # Deepest (furthest upstream) first, breaking ties by flow accumulation.
    return sorted(ds_map, key=lambda o: (-depth(o), accum(o), int(o)))


def _terminal_outlet(ds_map) -> int:
    terminals = [int(k) for k, v in ds_map.items() if v is None]
    if len(terminals) == 1:
        return terminals[0]
    if not terminals:
        raise Wbnm2025ExportError(
            "No terminal subarea could be identified. Reprocess subcatchments "
            "and redraw the outlet line so the network drains to one point."
        )
    raise Wbnm2025ExportError(
        "The processed subcatchments drain to more than one outlet. Draw an "
        "outlet line, process subcatchments again, then export so the topology "
        "discharges to a single SINK."
    )


# --- the runfile itself ---------------------------------------------------

def write_wbnm_2025_from_engine(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_path: str,
    model_name: str = "DDM_HydroLogic",
    lag_parameter: float = 1.60,
    nonlinearity_exponent: float = 0.77,
    impervious_lag_factor: float = 0.10,
    impervious_percent: float = 0.0,
    global_eia_factor: float = 1.0,
    stream_lag_factor: float = 1.0,
) -> Tuple[str, int, float, int]:
    """Writes a scaffolding WBNM 2025 ``.wbn`` runfile from the processed model.

    Returns ``(output_path, subarea_count, total_area_ha, flowpath_count)``.
    """
    if engine is None:
        raise Wbnm2025ExportError("No DEM flow graph is available. Press Compute first.")
    if not assignments:
        raise Wbnm2025ExportError(
            "No subcatchment assignments are available. Press Process subcatchments first."
        )

    features = _subcatchment_features_by_outlet(engine)
    selected = {int(o) for o, cells in assignments.items() if cells and int(o) in features}
    if not selected:
        raise Wbnm2025ExportError(
            "No processed subcatchments with matching outlet_id values are available to export."
        )

    ds_map = _downstream_map(engine, assignments, selected)
    terminal = _terminal_outlet(ds_map)
    ordered = _topological_order(ds_map, engine)
    name_of = {int(o): f"S{i:03d}" for i, o in enumerate(ordered, start=1)}

    # Only subareas that receive upstream inflow need a stream segment; head
    # subareas route nothing and can be left out of the flowpaths block.
    receivers = {int(v) for v in ds_map.values() if v is not None}
    flowpath_outlets = [o for o in ordered if o in receivers]

    catch_e, catch_n = _catchment_centroid(features[o] for o in ordered)
    outlet_pt = engine.cell_center(int(terminal))
    areas_ha = {o: max(0.0, _feature_area_m2(features[o]) / 10_000.0) for o in ordered}
    total_area_ha = sum(areas_ha.values())

    now = datetime.now()
    epsg = _epsg_code(engine)
    lines: List[str] = []

    # 1. Preamble - 8 lines of free text.
    lines += _block(
        "#####START_PREAMBLE_BLOCK##########|###########|###########|###########|",
        [
            "First-pass WBNM 2025 runfile exported by the DDM HydroLogic QGIS plugin.",
            "Topology, subarea areas and stream segments are derived from the DEM.",
            "Catchment and outlet coordinates come from the processed geometry.",
            "Subareas are listed upstream to downstream; the last one drains to SINK.",
            "Rainfall is a single placeholder storm with zero depth - user to replace it.",
            "Losses, imperviousness and structures are defaults - user to review them.",
            "Stream routing uses the natural lag factor 1.00 on every segment.",
            "Check the model in WBNM (WBNMCHCK/WBNMSORT) before using any results.",
        ],
        "#####END_PREAMBLE_BLOCK############|###########|###########|###########|",
    )

    # 2. Status - path plus QA metadata. The "!" here is a field separator.
    lines += _block(
        "#####START_STATUS_BLOCK############|###########|###########|###########|",
        [
            output_path[:1024],
            f"Last Edit    ! {now:%d/%m/%Y}",
            "By           ! DDM HydroLogic QGIS plugin",
            "2025_001     ! First-pass QGIS export - check before modelling",
        ],
        "#####END_STATUS_BLOCK##############|###########|###########|###########|",
    )

    # 3. Display - catchment CG, outlet and EPSG, then GIS file and notes.
    lines += _block(
        "#####START_DISPLAY_BLOCK###########|###########|###########|###########|",
        [
            _fields(catch_e, catch_n, outlet_pt.x(), outlet_pt.y()) + _int(epsg),
            _project_path()[:1024],
            "Note         ! First-pass scaffold from DDM HydroLogic - edit before use",
        ],
        "#####END_DISPLAY_BLOCK#############|###########|###########|###########|",
    )

    # 4. Topology - one row per subarea, downstream name in column 62.
    topo = [_int(len(ordered)) + f"      {model_name}"]
    for outlet in ordered:
        cg_e, cg_n = _feature_centroid_xy(features[outlet])
        out_pt = engine.cell_center(int(outlet))
        ds = ds_map.get(int(outlet))
        downstream = "SINK" if ds is None else name_of[int(ds)]
        topo.append(_topology_row(name_of[outlet], cg_e, cg_n, out_pt.x(), out_pt.y(), downstream))
    lines += _block(
        "#####START_TOPOLOGY_BLOCK##########|###########|###########|###########|",
        topo,
        "#####END_TOPOLOGY_BLOCK############|###########|###########|###########|",
    )

    # 5. Surfaces - global parameters, the linear-switch threshold, then areas.
    surfaces = [
        _fields(nonlinearity_exponent, lag_parameter, impervious_lag_factor, global_eia_factor),
        _num(-99.90),
    ]
    for outlet in ordered:
        surfaces.append(_row(name_of[outlet], areas_ha[outlet], impervious_percent))
    lines += _block(
        "#####START_SURFACES_BLOCK##########|###########|###########|###########|",
        surfaces,
        "#####END_SURFACES_BLOCK############|###########|###########|###########|",
    )

    # 6. Flowpaths - natural-channel routing for every subarea with inflow.
    flowpaths: List[str] = [_int(len(flowpath_outlets))]
    for outlet in flowpath_outlets:
        flowpaths.append(name_of[outlet])
        flowpaths.append("#####ROUTING")
        flowpaths.append(_num(stream_lag_factor))
    lines += _block(
        "#####START_FLOWPATHS_BLOCK#########|###########|###########|###########|",
        flowpaths,
        "#####END_FLOWPATHS_BLOCK###########|###########|###########|###########|",
    )

    # 7-8. No structures are derived from GIS; both blocks are empty.
    lines += _block(
        "#####START_LOCAL_STRUCTURES_BLOCK##|###########|###########|###########|",
        [_int(0)],
        "#####END_LOCAL_STRUCTURES_BLOCK####|###########|###########|###########|",
    )
    lines += _block(
        "#####START_OUTLET_STRUCTURES_BLOCK#|###########|###########|###########|",
        [_int(0)],
        "#####END_OUTLET_STRUCTURES_BLOCK###|###########|###########|###########|",
    )

    # 9. Storm - one placeholder recorded event of zero rainfall. This keeps the
    # runfile complete without inventing rainfall the GIS layer cannot provide.
    storm = [
        _int(1),
        "#####START_STORM#1",
        "Placeholder storm - replace with a recorded or ARR design event.",
        _num(5.0),                       # calculation timestep (min)
        _num(5.0),                       # output timestep (min)
        "#####START_RECORDED_RAIN",
        "01/01/2000",                    # event date
        "00:00",                         # event time
        _int(6) + _num(5.0),             # 6 rain periods of 5 minutes
        "MM/HOUR",
        _int(1),                         # one gauge
        "GAUGE_1",
        _fields(catch_e, catch_n),
        *[_num(0.0) for _ in range(6)],  # zero-depth hyetograph
        "#####END_RECORDED_RAIN",
        "#####START_CALC_RAINGAUGE_WEIGHTS",
        "#####END_CALC_RAINGAUGE_WEIGHTS",
        "#####START_LOSS_RATES",
        _row("GLOBAL", 0.0, 0.0),        # initial loss, continuing loss
        "#####END_LOSS_RATES",
        "#####START_RECORDED_HYDROGRAPHS",
        _int(0),
        "#####END_RECORDED_HYDROGRAPHS",
        "#####START_IMPORTED_HYDROGRAPHS",
        _int(0),
        "#####END_IMPORTED_HYDROGRAPHS",
        "#####END_STORM#1",
    ]
    lines += _block(
        "#####START_STORM_BLOCK#############|###########|###########|###########|",
        storm,
        "#####END_STORM_BLOCK###############|###########|###########|###########|",
    )

    if not output_path.lower().endswith(".wbn"):
        output_path += ".wbn"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")

    return output_path, len(ordered), round(total_area_ha, 3), len(flowpath_outlets)
