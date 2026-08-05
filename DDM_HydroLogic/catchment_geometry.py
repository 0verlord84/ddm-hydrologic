# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Per-subarea geometry shared by the model exporters.

The exporters all need the same handful of measurements off the D8 graph: where
a subarea's main stream runs, where rainfall-excess should enter it, how long
each channel is and how steep it is. Keeping them here means RORB, URBS and the
GIS outputs describe the same catchment rather than each deriving its own.

The sub-area entry point follows the RORB convention: a node sits on the
sub-area's main stream at the point adjacent to the sub-area centroid, which is
where the sub-area's rainfall-excess enters the channel network.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

# Slopes are floored so routing always has a positive value to work with.
SLOPE_FLOOR = 0.0005


def accumulation_of(engine, cell_id: int) -> int:
    """Flow accumulation for a cell, whether the engine stores a dict or array."""
    acc = getattr(engine, "accumulation", None)
    if acc is None:
        return 0
    try:
        if hasattr(acc, "get"):
            return int(acc.get(int(cell_id), 0))
        return int(acc[int(cell_id)])
    except Exception:
        return 0


def elevation_of(engine, cell_id: int) -> float:
    """Cell elevation, preferring the pit-filled surface so profiles run downhill."""
    arr = getattr(engine, "filled_dem", None)
    if arr is None:
        arr = getattr(engine, "dem", None)
    if arr is None:
        return 0.0
    try:
        cols = int(engine.cols)
        return float(arr[int(cell_id) // cols, int(cell_id) % cols])
    except Exception:
        return 0.0


def cell_distance(engine, cell_a: int, cell_b: int) -> float:
    """Plan distance in metres between two cell centres."""
    pa = engine.cell_center(int(cell_a))
    pb = engine.cell_center(int(cell_b))
    return math.hypot(float(pa.x()) - float(pb.x()), float(pa.y()) - float(pb.y()))


def centroid_of(feat) -> Tuple[float, float, bool]:
    """Subarea centroid as ``(x, y, inside)``.

    ``inside`` is False when the true centroid falls outside its own polygon,
    which happens on horseshoe or ribbon shaped subareas. Callers that need a
    point guaranteed to sit on the polygon get the point-on-surface instead.
    """
    geom = feat.geometry()
    if geom is None or geom.isNull() or geom.isEmpty():
        return (0.0, 0.0, False)
    inside = True
    point = None
    try:
        centroid = geom.centroid()
        if centroid is not None and not centroid.isNull() and not centroid.isEmpty():
            point = centroid.asPoint()
            try:
                inside = bool(geom.contains(centroid))
            except Exception:
                inside = True
    except Exception:
        point = None
    if point is None or not inside:
        try:
            surface = geom.pointOnSurface()
            if surface is not None and not surface.isNull() and not surface.isEmpty():
                point = surface.asPoint()
        except Exception:
            pass
    if point is None:
        bbox = geom.boundingBox()
        return ((bbox.xMinimum() + bbox.xMaximum()) / 2.0,
                (bbox.yMinimum() + bbox.yMaximum()) / 2.0,
                False)
    return (float(point.x()), float(point.y()), bool(inside))


def main_stem(engine, outlet_cell: int, member: Set[int]) -> List[int]:
    """The subarea's main stream, ordered outlet -> head.

    Traced from the subarea outlet upstream, always taking the branch carrying
    the most flow and never leaving the subarea.
    """
    outlet_cell = int(outlet_cell)
    path = [outlet_cell]
    seen = {outlet_cell}
    current = outlet_cell
    upstream = getattr(engine, "upstream", {})
    while True:
        candidates = [int(u) for u in upstream.get(current, []) if int(u) in member and int(u) not in seen]
        if not candidates:
            break
        nxt = max(candidates, key=lambda c: accumulation_of(engine, c))
        path.append(nxt)
        seen.add(nxt)
        current = nxt
    return path


def entry_cell(engine, stem: List[int], centroid_xy: Tuple[float, float]) -> int:
    """The cell on the main stream adjacent to the subarea centroid.

    This is the RORB sub-area entry point: the place on the modelled stream
    where the subarea's rainfall-excess joins the channel network.
    """
    if not stem:
        return -1
    cx, cy = float(centroid_xy[0]), float(centroid_xy[1])

    def offset(cell_id: int) -> float:
        pt = engine.cell_center(int(cell_id))
        return math.hypot(float(pt.x()) - cx, float(pt.y()) - cy)

    return int(min(stem, key=offset))


def path_length_m(engine, path: List[int]) -> float:
    """Length in metres along an ordered run of cells."""
    total = 0.0
    for i in range(1, len(path)):
        total += cell_distance(engine, path[i - 1], path[i])
    return total


def downstream_path(engine, from_cell: int, to_cell: Optional[int] = None, limit: int = 1_000_000) -> List[int]:
    """Cells followed downstream from ``from_cell``.

    Stops on ``to_cell`` when given, otherwise runs to the end of the graph.
    Returns an empty list if ``to_cell`` is never reached.
    """
    from_cell = int(from_cell)
    path = [from_cell]
    if to_cell is not None and int(to_cell) == from_cell:
        return path
    downstream = getattr(engine, "downstream", None)
    seen = {from_cell}
    current = from_cell
    for _ in range(int(limit)):
        try:
            nxt = int(downstream[int(current)])
        except Exception:
            nxt = -1
        if nxt < 0 or nxt in seen:
            break
        path.append(nxt)
        seen.add(nxt)
        if to_cell is not None and nxt == int(to_cell):
            return path
        current = nxt
    return [] if to_cell is not None else path


def equal_area_slope(engine, path: List[int]) -> float:
    """Equal-area slope of a profile given as cells ordered outlet -> head.

    The equal-area slope is the gradient of the line drawn from the outlet that
    leaves the same area under it as the real long section, ``Se = 2A/L^2``.
    """
    if len(path) < 2:
        return SLOPE_FLOOR
    outlet_z = elevation_of(engine, path[0])
    distances = [0.0]
    heights = [0.0]
    running = 0.0
    for i in range(1, len(path)):
        running += cell_distance(engine, path[i - 1], path[i])
        distances.append(running)
        heights.append(max(0.0, elevation_of(engine, path[i]) - outlet_z))
    length = distances[-1]
    if length <= 0:
        return SLOPE_FLOOR
    area = 0.0
    for i in range(1, len(distances)):
        area += 0.5 * (heights[i] + heights[i - 1]) * (distances[i] - distances[i - 1])
    return max(2.0 * area / (length * length), SLOPE_FLOOR)


def catchment_profile(engine, outlet_cell: int, member: Set[int]) -> List[int]:
    """Profile for the catchment slope: subarea high point down to its outlet.

    Returned outlet -> high point so it shares the equal-area routine with the
    channel profile.
    """
    if not member:
        return [int(outlet_cell)]
    high_cell = max(member, key=lambda c: elevation_of(engine, c))
    path = downstream_path(engine, int(high_cell), int(outlet_cell))
    if not path:
        return [int(outlet_cell)]
    path.reverse()
    return path


def dissolved_reaches(engine, cells) -> List[Tuple[List[int], int, float]]:
    """Merge a subarea's displayed flow-path cells into Strahler reaches.

    Consecutive cells are joined while they share the same Strahler order, the
    same way the exported flow-path layer is built. A reach is cut where the
    order changes, where the run leaves the subarea, or where two same-order
    branches meet and the join would be ambiguous.

    Returns ``(cells, strahler, length_m)`` per reach, ordered from the lowest
    order upwards so the layer draws small streams first.
    """
    displayed = getattr(engine, "cell_to_feature", {}) or {}
    order_by = getattr(engine, "display_strahler_by_cell", {}) or {}
    upstream = getattr(engine, "upstream", {})
    downstream = getattr(engine, "downstream", None)

    pool = {int(c) for c in cells if int(c) in displayed}
    if not pool:
        return []

    def order_of(cell_id: int) -> int:
        try:
            return int(order_by.get(int(cell_id), 1) or 1)
        except Exception:
            return 1

    def same_order_parents(cell_id: int) -> List[int]:
        return [int(u) for u in upstream.get(int(cell_id), [])
                if int(u) in pool and order_of(u) == order_of(cell_id)]

    def can_merge(cell_id: int, next_cell: int) -> bool:
        if int(next_cell) not in pool:
            return False
        if order_of(cell_id) != order_of(next_cell):
            return False
        parents = same_order_parents(int(next_cell))
        return len(parents) == 1 and int(parents[0]) == int(cell_id)

    def has_merge_parent(cell_id: int) -> bool:
        parents = same_order_parents(int(cell_id))
        if len(parents) != 1:
            return False
        return can_merge(int(parents[0]), int(cell_id))

    def sort_key(cell_id: int):
        return (order_of(cell_id), accumulation_of(engine, cell_id), int(cell_id))

    visited: Set[int] = set()
    runs: List[List[int]] = []

    def build(start_cell: int) -> List[int]:
        run: List[int] = []
        current = int(start_cell)
        while current in pool and current not in visited:
            run.append(current)
            visited.add(current)
            try:
                nxt = int(downstream[current])
            except Exception:
                break
            if nxt < 0 or nxt in visited or not can_merge(current, nxt):
                break
            current = nxt
        return run

    for start in sorted([c for c in pool if not has_merge_parent(c)], key=sort_key):
        if start in visited:
            continue
        run = build(start)
        if run:
            runs.append(run)
    # Anything left over is its own reach rather than being dropped.
    for cell_id in sorted(pool, key=sort_key):
        if cell_id in visited:
            continue
        run = build(cell_id)
        if run:
            runs.append(run)

    reaches: List[Tuple[List[int], int, float]] = []
    for run in runs:
        path = list(run)
        try:
            last_down = int(downstream[int(run[-1])])
        except Exception:
            last_down = -1
        # Carry the line to the next cell centre so reaches meet on the map.
        if last_down >= 0:
            path.append(last_down)
        if len(path) < 2:
            continue
        reaches.append((path, order_of(run[-1]), path_length_m(engine, path)))
    reaches.sort(key=lambda item: (item[1], -item[2]))
    return reaches


def downstream_outlet_map(engine, assignments, selected) -> Dict[int, Optional[int]]:
    """Map each selected subarea outlet to the next selected outlet downstream.

    A value of None means the subarea drains out of the model, so it sits at a
    model outlet.
    """
    cell_to_outlet: Dict[int, int] = {}
    domain: Set[int] = set()
    for outlet_id, cells in assignments.items():
        outlet_id = int(outlet_id)
        if outlet_id not in selected:
            continue
        for cell in cells or []:
            cell_to_outlet[int(cell)] = outlet_id
            domain.add(int(cell))

    ds_map: Dict[int, Optional[int]] = {}
    downstream = getattr(engine, "downstream", None)
    for outlet_id in selected:
        outlet_id = int(outlet_id)
        current = outlet_id
        seen = {outlet_id}
        result = None
        while True:
            try:
                nxt = int(downstream[int(current)])
            except Exception:
                nxt = -1
            if nxt < 0 or nxt in seen:
                break
            seen.add(nxt)
            other = cell_to_outlet.get(nxt)
            if other is not None and int(other) != outlet_id:
                result = int(other)
                break
            if nxt not in domain:
                break
            current = nxt
        ds_map[outlet_id] = result
    return ds_map


def subarea_metrics(engine, features: Dict[int, object], assignments: Dict[int, object],
                    outlets: List[int]) -> Dict[int, dict]:
    """Measure every selected subarea once, keyed by its outlet cell id.

    Each entry carries the centroid, the sub-area entry point, the main stream
    and its length/slope, and the equal-area catchment slope.
    """
    metrics: Dict[int, dict] = {}
    for outlet in outlets:
        outlet = int(outlet)
        member = {int(c) for c in (assignments.get(outlet) or [])}
        feat = features[outlet]
        cx, cy, inside = centroid_of(feat)
        stem = main_stem(engine, outlet, member)
        entry = entry_cell(engine, stem, (cx, cy))
        stem_length_m = path_length_m(engine, stem)
        metrics[outlet] = {
            "centroid_x": cx,
            "centroid_y": cy,
            "centroid_inside": inside,
            "stem": stem,
            "entry_cell": entry,
            "length_km": stem_length_m / 1000.0,
            "channel_slope": equal_area_slope(engine, stem),
            "catchment_slope": equal_area_slope(engine, catchment_profile(engine, outlet, member)),
        }
    return metrics
