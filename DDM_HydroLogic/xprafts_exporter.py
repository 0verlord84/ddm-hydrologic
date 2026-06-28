# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Write an XP-RAFTS exchange file (.xpx) from the processed subcatchments.

XP-RAFTS stores a model in a pseudo-binary ``.xp`` project, but it imports and
exports an ASCII *XP eXchange* file (``.xpx``) for moving model data in and out.
That exchange file is the one external tools produce, so this exporter writes an
``.xpx`` that XP-RAFTS can import (File > Import > XPX) to build the catchment.

What QGIS can supply: one RAFTS node per subcatchment
(named, with easting/northing), one link per drainage connection, and the
sub-area area in hectares. Everything else (roughness, slope, baseflow, outlet
structures, storms) is written as a clearly-defined default for the user to
calibrate in XP-RAFTS.

The XPX grammar (see the XP-RAFTS reference manual, "XPX Command Reference"):

    NODE  node_type  "name"  x  y
    LINK  link_type  "name"  "from"  "to"
    DATA  field      "object"  instance  count  value...
    GLDBITEM  "db_type"  "record"
    GLDBDATA  field  "db_type"  "record"  count  value...

node_type 134 is a circle; link_type 136 is a single-conduit line. Each RAFTS
node carries five sub-area slots (instances 0-4); slot 0 holds the real
sub-area and slots 1-4 are inert placeholders, exactly as a native export does.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import QgsProject

NODE_CIRCLE = 134
LINK_LINE = 136
SUBAREA_SLOTS = 5  # RAFTS allows up to five sub-areas per node.

# Default Initial/Continuing loss record referenced by every node.
LOSS_DB = "Init./Cont. Losses"
LOSS_NAME = "ARR IL=10,CL=2.5"
LOSS_IL = 10.0
LOSS_CL = 2.5

# Per-node fields that exist once for each of the five sub-area slots, written
# as (field, value_for_slot_0, value_for_slots_1_to_4). CA and SC are filled in
# per node from the GIS geometry; the values here are their slot defaults.
NODE_SUBAREA_FIELDS = [
    ("ASUBCTL", "user", "user"),    # sub-area control: user-defined hydrograph
    ("BCTL", "calc", "0"),          # basin routing: calculated for the active slot
    ("BFCTL", "no", "0"),           # baseflow control off
    ("BFLOW", "1", "1"),            # baseflow multiplier slot
    ("BFMULT", "1", "1"),
    ("CA", "0.001", "0.001"),       # catchment area (ha) - overwritten per node
    ("FACTOR", "1", "1"),           # vectoring factor
    ("LST", "0", "0"),
    ("NCC", "-0.285", "-0.285"),    # RAFTS storage-discharge exponent
    ("NCTL", "no", "0"),
    ("PERN", "0.035", "0.025"),     # pervious Manning's n
    ("QN", "-0.285000", "-0.285000"),
    ("SC", "0.70", "0.001"),        # average sub-area slope (%) - overwritten
    ("STCTL", "1", "1"),
]

# Per-node fields that exist once (instance 0). PIMP is filled in per node.
# Result fields (peak flows etc.) are deliberately left out of a fresh model.
NODE_SINGLE_FIELDS = [
    ("PIMP", "0.00"),               # percent impervious - overwritten per node
    ("OSDR_FLAG", "0"),             # no on-site detention
    ("IOPT", "1"), ("IOPTC", "0"), ("IOUT", "3"),
    ("IHYD", "0"), ("INFNC", "0"), ("ISDO", "0"), ("ISWO", "0"),
    ("LTYP", "1"), ("NPPS", "1"), ("MULT", "1"),
    ("DIA", "0.5"), ("HEIGHT", "0.5"), ("OWIDTH", "0.2"), ("PSTW", "0.2"),
    ("CWEIR", "1.7"), ("FCWEIR", "1.700000"),
    ("OKE", "0.5"), ("OMANN", "0.011"), ("ORIFICE", "0"),
    ("PL", "20"), ("CLOG", "0"), ("CULVMETH", "0"),
    ("BPFLAG", "0"), ("BPFSUB", "0"),
    ("FFL", "1"), ("FFT", "1"), ("FUSE", "0"),
    ("HCON", "0"), ("IBFL", "0"), ("IFLAP", "1"), ("IGHD", "0"),
    ("IMPD", "0"), ("IMPD2", "0"),
    ("QIN1", "0"), ("SAVREV", "0"), ("SHYD", "0"),
    ("SPILL", "0"), ("SPILL_MF", "1"), ("STVOL", "1"),
    ("TFC1X", "1"), ("TWATER", "0"), ("VOLRT", "1"),
    ("WHT", "0"), ("WTCTL", "0"), ("WTDEP", "0"), ("RDIA", "0"),
]

# Per-link fields (instance 0). RCQO (a result) is intentionally omitted.
LINK_FIELDS = [
    ("LFCTL", "0"),                 # link routing control
    ("LAG", "0.0"),                 # channel lag (min) - placeholder, calibrate
    ("NPIPES", "1"), ("OFRAC", "1.0"),
    ("CHNC", "0.04"), ("CHNL", "0.07"), ("CHNR", "0.07"),  # channel Manning's n
    ("CR_TYPE", "1"), ("CR_K", "0.0"), ("CR_X", "0.0"), ("CR_LSFLAG", "0"),
    ("PSLP", "0.001"), ("PCTL", "0"),
    ("LBANK", "0"), ("RBANK", "0"),
    ("H2ELEV", "0.000000"), ("H2STN", "0.000000"), ("HDIST", "1"),
    ("SECNO", "0"), ("XSECT", "0"), ("JXCTL", "0"),
    ("FPDIA", "0.3"), ("FPFSUB", "0"),
]

# Global model settings (object name ""). No storms are selected (SMODEL = 0),
# so the imported model has geometry but waits for the modeller to add rainfall.
GLOBAL_FIELDS = [
    ("IUNITS", 1, "0"),             # 0 = metric input
    ("OUNITS", 1, "0"),             # 0 = metric output
    ("IRAIN", 1, "1"),
    ("NVAL", 10, " ".join(["0"] * 10)),
    ("DT", 10, " ".join(["1.000000"] * 10)),
    ("SMODEL", 10, " ".join(["0"] * 10)),
    ("STDATE", 1, '"01/01/2000"'),
    ("STTIME", 1, '"00:00"'),
    ("APPLY_ARF", 1, "1"),
    ("MAXITR", 1, "10"),
    ("RELTOL", 1, "0.05"),
    ("KG", 1, "0.94"),
    ("BX", 1, "1"),
    ("SAVAREV", 1, "1"),
    ("DEBUG", 1, "2"),
    ("_HIDEBLNK", 1, "1"),
    ("_BXAUTO", 1, "1"),
    ("_BXSIZE", 1, "50.0"),
]


class XpRaftsExportError(Exception):
    """Raised when the current plugin outputs cannot be written as an XPX file."""


def _q(text: str) -> str:
    return f'"{text}"'


def _f(value: float, decimals: int = 3) -> str:
    if not math.isfinite(value):
        value = 0.0
    return f"{value:.{decimals}f}"


def _node(node_type: int, name: str, x: float, y: float) -> str:
    return f"NODE {node_type} {_q(name)} {_f(x)} {_f(y)}"


def _link(link_type: int, name: str, node_from: str, node_to: str) -> str:
    return f"LINK {link_type} {_q(name)} {_q(node_from)} {_q(node_to)}"


def _data(field: str, obj: str, instance: int, *values: str) -> str:
    payload = " ".join(values)
    return f"DATA {field} {_q(obj)} {instance} {len(values)} {payload}"


# --- reading values back out of the engine --------------------------------

def _subcatchment_features_by_outlet(engine) -> Dict[int, object]:
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise XpRaftsExportError(
            "No valid subcatchment layer is available. Press Process subcatchments first."
        )
    features: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            features[int(feat["outlet_id"])] = feat
        except Exception:
            continue
    if not features:
        raise XpRaftsExportError(
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


def _feature_centroid_xy(feat) -> Tuple[float, float]:
    geom = feat.geometry()
    if geom is None or geom.isNull() or geom.isEmpty():
        return (0.0, 0.0)
    try:
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


def _downstream_map(engine, assignments, selected) -> Dict[int, Optional[int]]:
    """Maps each subarea outlet to the next downstream selected outlet, or None
    when it drains out of the model."""
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
                break
            current = nxt
        ds_map[outlet_id] = downstream
    return ds_map


def _topological_order(ds_map, engine) -> List[int]:
    """Orders subareas upstream-to-downstream so nodes precede their links."""
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

    return sorted(ds_map, key=lambda o: (-depth(o), accum(o), int(o)))


def _terminal_outlet(ds_map) -> int:
    terminals = [int(k) for k, v in ds_map.items() if v is None]
    if len(terminals) == 1:
        return terminals[0]
    if not terminals:
        raise XpRaftsExportError(
            "No terminal subarea could be identified. Reprocess subcatchments "
            "and redraw the outlet line so the network drains to one point."
        )
    raise XpRaftsExportError(
        "The processed subcatchments drain to more than one outlet. Draw an "
        "outlet line, process subcatchments again, then export so the model "
        "discharges to a single downstream node."
    )


def _project_path() -> str:
    try:
        return QgsProject.instance().fileName() or ""
    except Exception:
        return ""


# --- the exchange file ----------------------------------------------------

def write_xprafts_from_engine(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_path: str,
    model_name: str = "DDM_HydroLogic",
    impervious_percent: float = 0.0,
    pervious_mannings_n: float = 0.035,
    subarea_slope_pct: float = 0.70,
) -> Tuple[str, int, int, float]:
    """Writes a scaffolding XP-RAFTS ``.xpx`` exchange file.

    Returns ``(output_path, node_count, link_count, total_area_ha)``.
    """
    if engine is None:
        raise XpRaftsExportError("No DEM flow graph is available. Press Compute first.")
    if not assignments:
        raise XpRaftsExportError(
            "No subcatchment assignments are available. Press Process subcatchments first."
        )

    features = _subcatchment_features_by_outlet(engine)
    selected = {int(o) for o, cells in assignments.items() if cells and int(o) in features}
    if not selected:
        raise XpRaftsExportError(
            "No processed subcatchments with matching outlet_id values are available to export."
        )

    ds_map = _downstream_map(engine, assignments, selected)
    _terminal_outlet(ds_map)  # validate a single outlet before writing
    ordered = _topological_order(ds_map, engine)
    name_of = {int(o): f"S{i:03d}" for i, o in enumerate(ordered, start=1)}

    areas_ha = {o: max(0.0, _feature_area_m2(features[o]) / 10_000.0) for o in ordered}
    coords = {o: _feature_centroid_xy(features[o]) for o in ordered}
    total_area_ha = sum(areas_ha.values())

    lines: List[str] = []
    lines.append(f"/* XP-RAFTS XPX exchange file - first-pass scaffold from DDM HydroLogic ({model_name}). */")
    lines.append("/* Nodes, links and sub-area areas come from QGIS; roughness, slope, routing and storms are defaults. */")
    project = _project_path()
    if project:
        lines.append(f"/* Source QGIS project: {project} */")
    lines.append(f'DATA TITLE "" 0 1 "DDM HydroLogic - {model_name}"')
    for field, count, value in GLOBAL_FIELDS:
        lines.append(f"DATA {field} \"\" 0 {count} {value}")

    # Default loss record that each node refers to.
    lines.append(f"GLDBITEM {_q(LOSS_DB)} {_q(LOSS_NAME)}")
    lines.append(f"GLDBDATA ILOSS {_q(LOSS_DB)} {_q(LOSS_NAME)} 1 {_f(LOSS_IL, 1)}")
    lines.append(f"GLDBDATA CLOSS {_q(LOSS_DB)} {_q(LOSS_NAME)} 1 {_f(LOSS_CL, 1)}")
    lines.append(f"GLDBDATA CLOSSCTL {_q(LOSS_DB)} {_q(LOSS_NAME)} 1 0")

    # Nodes first, then links (links may only reference nodes already defined).
    for outlet in ordered:
        x, y = coords[outlet]
        lines.append(_node(NODE_CIRCLE, name_of[outlet], x, y))

    links: List[Tuple[str, str, str]] = []
    for outlet in ordered:
        ds = ds_map.get(int(outlet))
        if ds is not None:
            links.append((f"Link {len(links) + 1}", name_of[outlet], name_of[int(ds)]))
    for link_name, node_from, node_to in links:
        lines.append(_link(LINK_LINE, link_name, node_from, node_to))

    # Per-node data: five sub-area slots for the indexed fields, then the
    # single-instance fields, then the loss reference.
    for outlet in ordered:
        name = name_of[outlet]
        active = {
            "CA": _f(areas_ha[outlet], 3),
            "SC": _f(subarea_slope_pct, 2),
            "PERN": _f(pervious_mannings_n, 3),
        }
        for field, slot0, slot_rest in NODE_SUBAREA_FIELDS:
            value0 = active.get(field, slot0)
            for slot in range(SUBAREA_SLOTS):
                lines.append(_data(field, name, slot, value0 if slot == 0 else slot_rest))
        single = dict(NODE_SINGLE_FIELDS)
        single["PIMP"] = _f(impervious_percent, 2)
        for field, _default in NODE_SINGLE_FIELDS:
            lines.append(_data(field, name, 0, single[field]))
        lines.append(_data("REFSTR_ICLOSS", name, 0, _q(LOSS_NAME)))

    # Per-link data.
    for link_name, _node_from, _node_to in links:
        for field, value in LINK_FIELDS:
            lines.append(_data(field, link_name, 0, value))

    if not output_path.lower().endswith(".xpx"):
        output_path += ".xpx"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines) + "\n")

    return output_path, len(ordered), len(links), round(total_area_ha, 3)
