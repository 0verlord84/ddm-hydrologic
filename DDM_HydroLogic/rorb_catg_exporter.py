# -*- coding: utf-8 -*-
# DDM_HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""RORBwin GE .catg exporter for DDM HydroLogic.

This module converts the plugin's generated subcatchments and D8 flow graph into
an initial RORB GE/RORBwin graphical catchment file. The writer is deliberately
self-contained so the plugin controls the node-link topology written to RORB directly.
"""

from __future__ import annotations

import math
import os
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from qgis.core import QgsPointXY


class RorbCatgExportError(Exception):
    """Raised when the current plugin outputs cannot be converted to .catg."""


class ReachType(Enum):
    NATURAL = 1
    UNLINED = 2
    LINED = 3
    DROWNED = 4


def _point_tuple(point: QgsPointXY) -> Tuple[float, float]:
    return (float(point.x()), float(point.y()))


def _same_xy(a: Tuple[float, float], b: Tuple[float, float], tol: float = 1e-9) -> bool:
    return abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= tol


def _safe_layer_field(feat, name: str, default=None):
    try:
        return feat[name]
    except Exception:
        return default


def _feature_area_m2(feat) -> float:
    """Returns final polygon area in m² from geometry, falling back to attributes."""
    try:
        geom = feat.geometry()
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            area = float(geom.area())
            if math.isfinite(area) and area > 0:
                return area
    except Exception:
        pass
    try:
        area = float(_safe_layer_field(feat, "area_m2", 0.0))
        if math.isfinite(area) and area > 0:
            return area
    except Exception:
        pass
    return 0.0


def _feature_impervious_fraction(feat, field_name: str | None = None, default: float = 0.0) -> float:
    """Returns a safe RORB impervious fraction from feature attributes or the default."""
    if not field_name:
        return max(0.0, min(1.0, float(default or 0.0)))
    value = default
    try:
        raw = _safe_layer_field(feat, field_name, None)
        if raw not in (None, ""):
            value = float(raw)
    except Exception:
        value = default
    try:
        if not math.isfinite(float(value)):
            return 0.0
        value = float(value)
    except Exception:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value > 1.0:
        value = 1.0
    return float(value)


def _subcatchment_features_by_outlet(engine) -> Dict[int, object]:
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise RorbCatgExportError("No valid subcatchment layer is available. Press Process subcatchments first.")
    result: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            outlet_id = int(feat["outlet_id"])
        except Exception:
            continue
        result[outlet_id] = feat
    if not result:
        raise RorbCatgExportError("The subcatchment layer contains no outlet_id features to convert.")
    return result


def _distance(points: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for a, b in zip(points[:-1], points[1:]):
        total += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    return float(total)


def _dedupe_consecutive_points(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    cleaned: List[Tuple[float, float]] = []
    for point in points:
        p = (float(point[0]), float(point[1]))
        if not cleaned or not _same_xy(cleaned[-1], p):
            cleaned.append(p)
    return cleaned


def _tiny_offset_point(engine, point: Tuple[float, float]) -> Tuple[float, float]:
    dx = float(getattr(engine, "cell_width", 1.0) or 1.0)
    dy = float(getattr(engine, "cell_height", 1.0) or 1.0)
    offset = max(abs(dx), abs(dy), 1.0) * 0.25
    return (float(point[0]) + offset, float(point[1]) - offset)


def _midpoint(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    if not points:
        return (0.0, 0.0)
    if len(points) == 1:
        return (float(points[0][0]), float(points[0][1]))
    half = _distance(points) / 2.0
    if half <= 0:
        return (float(points[0][0]), float(points[0][1]))
    walked = 0.0
    for a, b in zip(points[:-1], points[1:]):
        seg = math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        if seg <= 0:
            continue
        if walked + seg >= half:
            t = (half - walked) / seg
            return (float(a[0]) + (float(b[0]) - float(a[0])) * t, float(a[1]) + (float(b[1]) - float(a[1])) * t)
        walked += seg
    return (float(points[-1][0]), float(points[-1][1]))


def _walk_downstream_path(engine, start_cell: int, selected_outlets: set[int], domain_cells: set[int], model_outlet_cell: Optional[int] = None):
    """Walks from a subcatchment outlet to the next selected outlet or model outlet.

    Returns ``(path_points, downstream_selected_outlet, terminal_point)``.
    If ``downstream_selected_outlet`` is ``None``, the reach is terminal and is
    explicitly connected to the single RORB outlet node.
    """
    start_cell = int(start_cell)
    points = [_point_tuple(engine.cell_center(start_cell))]

    if model_outlet_cell is not None and int(start_cell) == int(model_outlet_cell):
        return points, None, points[-1]

    current = start_cell
    seen = {start_cell}
    downstream_selected = None
    terminal_point = points[-1]

    while True:
        engine._check_cancelled()
        try:
            down = int(engine.downstream[int(current)])
        except Exception:
            down = -1
        if down < 0 or down in seen:
            terminal_point = points[-1]
            break

        down_point = _point_tuple(engine.cell_center(down))
        if not _same_xy(points[-1], down_point):
            points.append(down_point)
        terminal_point = down_point

        if model_outlet_cell is not None and int(down) == int(model_outlet_cell):
            break

        if down in selected_outlets and down != start_cell:
            downstream_selected = int(down)
            break

        if down not in domain_cells:
            break

        seen.add(down)
        current = down

    return points, downstream_selected, terminal_point


def _normalise_graphical_coordinates(nodes: Dict[int, dict], reaches: List[dict]) -> Tuple[Dict[int, Tuple[float, float]], Dict[int, Tuple[float, float]]]:
    """Scales actual CRS coordinates into the 0-100-ish RORB GE display window."""
    xs: List[float] = []
    ys: List[float] = []
    for node in nodes.values():
        xs.append(float(node["x"]))
        ys.append(float(node["y"]))
    for reach in reaches:
        mx, my = _midpoint(reach["points"])
        reach["mid_actual"] = (mx, my)
        xs.append(float(mx))
        ys.append(float(my))
    if not xs or not ys:
        return {}, {}
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    sx = max(max_x - min_x, 1.0)
    sy = max(max_y - min_y, 1.0)

    def tr(pt: Tuple[float, float]) -> Tuple[float, float]:
        return ((float(pt[0]) - min_x) / sx * 90.0 + 2.5, (float(pt[1]) - min_y) / sy * 90.0 + 2.5)

    node_xy = {int(nid): tr((node["x"], node["y"])) for nid, node in nodes.items()}
    reach_xy = {int(reach["id"]): tr(reach.get("mid_actual", _midpoint(reach["points"]))) for reach in reaches}
    return node_xy, reach_xy


def _format_table(values: Sequence[float], decimals: int = 3) -> str:
    if not values:
        return " -99"
    rows = []
    for i in range(0, len(values), 5):
        chunk = values[i:i + 5]
        rows.append("".join(f"{float(v):8.{decimals}f}," for v in chunk))
    rows.append(" -99")
    return "\n".join(rows)


def _route_vector_lines(upstream: Dict[int, List[int]], downstream: Dict[int, int], reach_by_us: Dict[int, dict], outlet_node_id: int):
    """Builds RORB calculation/control lines and basin area order.

    This mirrors the simple tree order used by RORB GE examples: upstream leaf
    basins use code 1, downstream basins that receive a running hydrograph use
    code 2, code 3 stores the running hydrograph at a confluence, and code 4
    adds the next branch back to the stored hydrograph.
    """
    lines: List[str] = ["1"]  # Reach types are taken from the graphical reach block.
    basin_order: List[int] = []

    def emit_basin(node_id: int):
        children = sorted(upstream.get(int(node_id), []), key=lambda n: (len(upstream.get(int(n), [])), int(n)), reverse=True)
        if not children:
            code = 1
        else:
            for i, child in enumerate(children):
                emit_basin(int(child))
                if len(children) > 1:
                    lines.append("3" if i == 0 else "4")
            code = 2
        reach = reach_by_us.get(int(node_id))
        if reach is None:
            raise RorbCatgExportError("A RORB basin node is missing its downstream reach. Recompute subcatchments and export again.")
        length_km = max(0.0, float(reach["length_m"]) / 1000.0)
        lines.append(f"{code}, {length_km:8.3f},  -99")
        basin_order.append(int(node_id))

    outlet_children = sorted(upstream.get(int(outlet_node_id), []), key=lambda n: (len(upstream.get(int(n), [])), int(n)), reverse=True)
    if not outlet_children:
        raise RorbCatgExportError("The RORB outlet has no upstream subcatchment nodes. Draw an outlet line, process subcatchments, then export again.")
    for i, child in enumerate(outlet_children):
        emit_basin(int(child))
        if len(outlet_children) > 1:
            lines.append("3" if i == 0 else "4")
    lines.append("7.2                                              ,                                  PRINT")
    lines.append("outlet")
    lines.append("0")
    return lines, basin_order


def _validate_connected_to_outlet(downstream: Dict[int, int], outlet_node_id: int):
    for node_id in list(downstream):
        seen = set()
        cursor = int(node_id)
        while cursor != int(outlet_node_id):
            if cursor in seen:
                raise RorbCatgExportError("A loop was detected in the RORB export network. Recompute subcatchments and redraw the outlet line.")
            seen.add(cursor)
            if cursor not in downstream:
                raise RorbCatgExportError(
                    f"Subcatchment node {cursor} does not connect to the model outlet. "
                    "Redraw the outlet line, process subcatchments, then export to RORB again."
                )
            cursor = int(downstream[cursor])


def _write_manual_catg(output_path: str, version: str, nodes: Dict[int, dict], reaches: List[dict], outlet_node_id: int, basin_order: List[int], vector_lines: List[str]):
    node_xy, reach_xy = _normalise_graphical_coordinates(nodes, reaches)
    # RORB GE expects basin node labels in the graphical #NODES block to be
    # numeric subarea identifiers, not arbitrary strings such as S001.  Using
    # alphanumeric labels in this field makes RORBwin reject the file while
    # reading the node block. Keep the name field at the narrow fixed width
    # used by RORB GE example files; wider fields shift the area/impervious
    # columns and RORB rejects the first C node record.  The mapping below mirrors the order used in the
    # Sub Area Data table and the vector/calculation block. Outlet nodes retain
    # the literal name "outlet".
    basin_label_by_node = {int(node_id): str(idx) for idx, node_id in enumerate(basin_order, start=1)}
    lines: List[str] = []
    lines.append("DDM HydroLogic RORB export")
    lines.append(f"C RORB_GE {version}")
    lines.append("C WARNING - DO NOT EDIT THIS FILE OUTSIDE RORB TO ENSURE BOTH GRAPHICAL AND CATCHMENT DATA ARE COMPATIBLE WITH EACH OTHER")
    lines.append(f"C THIS FILE CANNOT BE OPENED IN EARLIER VERSIONS OF RORB GE - CURRENT VERSION IS v{version}")
    lines.append("C")
    lines.append("C DDM HydroLogic RORB export")
    lines.append("C")
    for title in ("#FILE COMMENTS", "#SUB-AREA AREA COMMENTS", "#IMPERVIOUS FRACTION COMMENTS"):
        lines.append(f"C {title}")
        lines.append("C   0")
        lines.append("C")
    lines.append("C #BACKGROUND IMAGE")
    lines.append("C  T  F")
    lines.append("C")
    lines.append("C #NODES")
    lines.append(f"C {len(nodes):6d}")
    for node_id in sorted(nodes):
        node = nodes[int(node_id)]
        gx, gy = node_xy[int(node_id)]
        ds = int(node.get("downstream", 0) or 0)
        is_basin = int(node.get("is_basin", 0))
        is_outlet = int(node.get("is_outlet", 0))
        if is_outlet:
            name = str(node.get("name", "outlet"))[:20]
            label_line = name
        elif is_basin:
            name = basin_label_by_node.get(int(node_id), str(node_id))[:20]
            label_line = ""
        else:
            name = ""
            label_line = ""
        area = float(node.get("area_km2", 0.0) or 0.0)
        fi = float(node.get("fi", 0.0) or 0.0)
        prn = 72 if is_outlet else 0
        lines.append(
            f"C {node_id:6d}{gx:15.3f}{gy:15.3f}{1.0:15.3f} {is_basin:d} {is_outlet:d}{ds:6d} {name:<20s}{area:15.3f}{fi:15.3f}{prn:3d}  0  0"
        )
        lines.append(f"C {label_line:<50s}")
    lines.append("C")
    lines.append("C #REACHES")
    lines.append(f"C {len(reaches):6d}")
    for reach in reaches:
        rid = int(reach["id"])
        gx, gy = reach_xy[int(rid)]
        # RORB GE example files leave the graphical reach name field blank.
        # Alphanumeric reach labels are unnecessary and can make older parsers
        # brittle, so keep the field empty and identify reaches by number.
        name = ""
        us = int(reach["us_node"])
        ds = int(reach["ds_node"])
        rtype = int(reach.get("reach_type", 1) or 1)
        length_km = max(0.0, float(reach.get("length_m", 0.0)) / 1000.0)
        slope = float(reach.get("slope", 0.0) or 0.0)
        lines.append(f"C {rid:6d} {name:<20s}{us:6d}{ds:6d}{0:15d}{rtype:2d} {0:d}{length_km:15.3f}{slope:15.3f}{1:6d}  0")
        lines.append(f"C {gx:15.3f}")
        lines.append(f"C {gy:15.3f}")
    lines.append("C")
    lines.append("C #STORAGES")
    lines.append("C      0")
    lines.append("C")
    lines.append("C #INFLOW/OUTFLOW")
    lines.append("C      0")
    lines.append("C")
    lines.append("C END RORB_GE")
    lines.append("C")
    lines.extend(vector_lines)
    lines.append("C Sub Area Data")
    lines.append("C Areas, km**2, of subareas A,B...")
    lines.append(_format_table([float(nodes[int(n)]["area_km2"]) for n in basin_order], decimals=3))
    lines.append("C Impervious Fraction Data")
    lines.append(" 1 ,")
    lines.append(_format_table([float(nodes[int(n)].get("fi", 0.0) or 0.0) for n in basin_order], decimals=3))
    lines.append(" ")

    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def write_rorb_catg_from_engine(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_path: str,
    rorb_version: str = "6.52",
    fraction_impervious: float = 0.0,
    reach_type: ReachType = ReachType.NATURAL,
    impervious_field: str | None = None,
    outlet_point: Optional[Tuple[float, float]] = None,
    outlet_name: str = "outlet",
    model_outlet_cell: Optional[int] = None,
) -> Tuple[str, int, int]:
    """Writes a RORBwin/RORB GE .catg file from plugin outputs.

    The exported graph is explicitly validated so every basin node has one
    downstream chain ending at the single model outlet node. If an outlet line
    was drawn in QGIS, the export is clipped to subcatchments upstream of that
    outlet cell.
    """
    if engine is None:
        raise RorbCatgExportError("No DEM flow graph is available. Press Compute first.")
    if not assignments:
        raise RorbCatgExportError("No current subcatchment assignments are available. Press Process subcatchments first.")

    sub_features = _subcatchment_features_by_outlet(engine)
    selected_outlets = {int(k) for k, cells in assignments.items() if cells and int(k) in sub_features}
    if not selected_outlets:
        raise RorbCatgExportError("No processed subcatchments with matching outlet_id values are available for RORB export.")

    model_outlet_cell = int(model_outlet_cell) if model_outlet_cell is not None else None
    upstream_to_model_outlet = None
    if model_outlet_cell is not None:
        try:
            upstream_to_model_outlet = set(int(c) for c in engine.collect_upstream(int(model_outlet_cell)))
            upstream_to_model_outlet.add(int(model_outlet_cell))
        except Exception:
            upstream_to_model_outlet = None

    if upstream_to_model_outlet is not None:
        selected_outlets = {int(o) for o in selected_outlets if int(o) in upstream_to_model_outlet}
        if not selected_outlets:
            raise RorbCatgExportError(
                "No processed subcatchments drain to the drawn outlet line. "
                "Clear/re-draw the outlet line, process subcatchments again, then export to RORB."
            )

    domain_cells = set()
    for outlet_id, cells in assignments.items():
        if int(outlet_id) not in selected_outlets:
            continue
        for cell in cells or []:
            cell = int(cell)
            if upstream_to_model_outlet is None or cell in upstream_to_model_outlet:
                domain_cells.add(cell)
    if model_outlet_cell is not None:
        domain_cells.add(int(model_outlet_cell))

    ordered_outlets = sorted(selected_outlets, key=lambda cid: (int(engine.accumulation[int(cid)]), int(cid)))
    outlet_node_id = len(ordered_outlets) + 1

    # Determine explicit outlet coordinate. When the drawn outlet is exactly on a
    # basin node, offset the graphical outlet slightly so RORB GE does not confuse
    # the basin and outlet nodes as the same object.
    if outlet_point is not None:
        try:
            out_x = float(outlet_point[0])
            out_y = float(outlet_point[1])
        except Exception:
            outlet_point = None
    if outlet_point is None:
        if model_outlet_cell is not None:
            out_x, out_y = _point_tuple(engine.cell_center(int(model_outlet_cell)))
        else:
            downstream_most = max(ordered_outlets, key=lambda cid: (int(engine.accumulation[int(cid)]), int(cid)))
            out_x, out_y = _point_tuple(engine.cell_center(int(downstream_most)))
            out_x, out_y = _tiny_offset_point(engine, (out_x, out_y))

    outlet_id_to_node: Dict[int, int] = {}
    nodes: Dict[int, dict] = {}
    for idx, outlet_id in enumerate(ordered_outlets, start=1):
        pt = engine.cell_center(int(outlet_id))
        if _same_xy((float(pt.x()), float(pt.y())), (float(out_x), float(out_y)), tol=1e-6):
            out_x, out_y = _tiny_offset_point(engine, (float(out_x), float(out_y)))
            break

    for idx, outlet_id in enumerate(ordered_outlets, start=1):
        pt = engine.cell_center(int(outlet_id))
        feat = sub_features[int(outlet_id)]
        area_km2 = max(0.0, float(_feature_area_m2(feat)) / 1_000_000.0)
        fi = _feature_impervious_fraction(feat, field_name=impervious_field, default=float(fraction_impervious))
        outlet_id_to_node[int(outlet_id)] = int(idx)
        nodes[int(idx)] = {
            "name": f"S{idx:03d}",
            "x": float(pt.x()),
            "y": float(pt.y()),
            "is_basin": 1,
            "is_outlet": 0,
            "downstream": 0,  # populated below
            "area_km2": area_km2,
            "fi": fi,
        }
    nodes[int(outlet_node_id)] = {
        "name": "outlet",
        "x": float(out_x),
        "y": float(out_y),
        "is_basin": 0,
        "is_outlet": 1,
        "downstream": 0,
        "area_km2": 0.0,
        "fi": 0.0,
    }

    raw_reach_records = []
    for outlet_id in ordered_outlets:
        engine._check_cancelled()
        points, ds_outlet, terminal_point = _walk_downstream_path(
            engine, int(outlet_id), selected_outlets, domain_cells, model_outlet_cell=model_outlet_cell
        )
        raw_reach_records.append((int(outlet_id), ds_outlet, points, terminal_point))

    downstream: Dict[int, int] = {}
    reaches: List[dict] = []
    reach_type_value = int(getattr(reach_type, "value", reach_type or 1) or 1)
    for rid, (outlet_id, ds_outlet, points, _terminal_point) in enumerate(raw_reach_records, start=1):
        us_node = outlet_id_to_node[int(outlet_id)]
        if ds_outlet is not None and int(ds_outlet) in outlet_id_to_node:
            ds_node = outlet_id_to_node[int(ds_outlet)]
            ds_pt = (float(nodes[int(ds_node)]["x"]), float(nodes[int(ds_node)]["y"]))
        else:
            ds_node = int(outlet_node_id)
            ds_pt = (float(out_x), float(out_y))
        nodes[int(us_node)]["downstream"] = int(ds_node)
        downstream[int(us_node)] = int(ds_node)

        if not points:
            points = [_point_tuple(engine.cell_center(int(outlet_id)))]
        points = list(points)
        if not _same_xy(points[-1], ds_pt):
            points.append(ds_pt)
        points = _dedupe_consecutive_points(points)
        if len(points) < 2:
            points.append(_tiny_offset_point(engine, points[0]))
        reaches.append({
            "id": int(rid),
            "name": f"R{rid:03d}",
            "us_node": int(us_node),
            "ds_node": int(ds_node),
            "reach_type": reach_type_value,
            "length_m": max(0.0, _distance(points)),
            "slope": 0.0,
            "points": points,
        })

    if not reaches:
        raise RorbCatgExportError("No reaches could be built from the processed subcatchments.")

    _validate_connected_to_outlet(downstream, int(outlet_node_id))
    upstream: Dict[int, List[int]] = {int(n): [] for n in nodes}
    for us, ds in downstream.items():
        upstream.setdefault(int(ds), []).append(int(us))
    reach_by_us = {int(r["us_node"]): r for r in reaches}
    vector_lines, basin_order = _route_vector_lines(upstream, downstream, reach_by_us, int(outlet_node_id))
    if len(basin_order) != len(ordered_outlets):
        raise RorbCatgExportError("The RORB calculation order did not include every subcatchment. Recompute subcatchments and export again.")

    if not output_path.lower().endswith(".catg"):
        output_path += ".catg"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    _write_manual_catg(output_path, str(rorb_version or "6.52"), nodes, reaches, int(outlet_node_id), basin_order, vector_lines)
    return output_path, len(ordered_outlets), len(reaches)
