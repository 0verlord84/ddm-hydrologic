# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Write URBS input files (a .vec routing vector file and a .csv catchment data
file) from the processed subcatchments.

URBS reads a catchment as two files: a routing vector file that describes the
tree of RAIN / ADD RAIN / ROUTE THRU commands with STORE. / GET. branch markers,
and a catchment data file listing each subarea's area, land-use fractions and
catchment slope. This module builds both from the plugin's subcatchment topology.

Lengths (L) and channel slopes (Sc) come from each subarea's main channel; the
catchment slope (CS) is the equal-area slope from the subarea's high point down to
its outlet. Land-use fractions are read from the subcatchment layer if the fields
exist, otherwise written as zero. Model parameters and losses are written as
defaults for the modeller to complete in URBS.
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Tuple

# Channel and catchment slopes are floored to this value, as URBS routing needs a
# positive slope and the sample files use the same floor.
SLOPE_FLOOR = 0.0005

# Written verbatim as the routing-file header; these are model defaults the
# modeller reviews in URBS.
MODEL_LINE = "Model: SPLIT"
USES_LINE = "USES: L , CS , Sc , U , I , F*0.5 "
DEFAULT_PARAMETERS_LINE = (
    "DEFAULT PARAMETERS: alpha = 0.005 m = 0.8 beta = 2.5 n = 1 x = 0 IL = 0 CL = 0.0"
)

SUBCAT_FILE = "URBS_SubcatFile.csv"
ROUTING_FILE = "URBS_RoutingFile.vec"

LANDUSE_FIELDS = {"U": "FracUrban", "UF": "FracForest", "I": "FracImp"}


class UrbsExportError(Exception):
    """Raised when the current plugin outputs cannot be written as URBS files."""


# --- reading values back out of the engine --------------------------------

def _subcatchment_features_by_outlet(engine) -> Dict[int, object]:
    layer = getattr(engine, "subcatchment_layer", None)
    if layer is None or not layer.isValid():
        raise UrbsExportError(
            "No valid subcatchment layer is available. Press Process subcatchments first."
        )
    features: Dict[int, object] = {}
    for feat in layer.getFeatures():
        try:
            features[int(feat["outlet_id"])] = feat
        except Exception:
            continue
    if not features:
        raise UrbsExportError(
            "The subcatchment layer contains no outlet_id features to convert."
        )
    return features


def _feature_area_km2(feat) -> float:
    try:
        geom = feat.geometry()
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            area = float(geom.area())
            if math.isfinite(area) and area > 0:
                return area / 1_000_000.0
    except Exception:
        pass
    try:
        area = float(feat["area_m2"])
        if math.isfinite(area) and area > 0:
            return area / 1_000_000.0
    except Exception:
        pass
    return 0.0


def _feature_field_names(feat) -> set:
    try:
        return {f.name() for f in feat.fields()}
    except Exception:
        return set()


def _feature_fraction(feat, field_name: str, present_names: set) -> float:
    if field_name not in present_names:
        return 0.0
    try:
        value = float(feat[field_name])
        return value if math.isfinite(value) else 0.0
    except Exception:
        return 0.0


def _accumulation(engine, cell_id: int) -> int:
    acc = getattr(engine, "accumulation", None)
    if acc is None:
        return 0
    try:
        if hasattr(acc, "get"):
            return int(acc.get(int(cell_id), 0))
        return int(acc[int(cell_id)])
    except Exception:
        return 0


def _elevation(engine, cell_id: int) -> float:
    arr = getattr(engine, "filled_dem", None)
    if arr is None:
        arr = getattr(engine, "dem", None)
    if arr is None:
        return 0.0
    cols = int(engine.cols)
    try:
        return float(arr[int(cell_id) // cols, int(cell_id) % cols])
    except Exception:
        return 0.0


def _cell_distance(engine, cell_a: int, cell_b: int) -> float:
    pa = engine.cell_center(int(cell_a))
    pb = engine.cell_center(int(cell_b))
    return math.hypot(float(pa.x()) - float(pb.x()), float(pa.y()) - float(pb.y()))


# --- per-subarea channel geometry -----------------------------------------

def _main_stem_path(engine, outlet_cell: int, member: set) -> List[int]:
    """Trace the main channel of a subarea: from its outlet cell upstream,
    following the highest-accumulation branch, staying inside the subarea.

    Returns the cell ids ordered outlet -> channel head.
    """
    path = [int(outlet_cell)]
    seen = {int(outlet_cell)}
    current = int(outlet_cell)
    upstream = getattr(engine, "upstream", {})
    while True:
        candidates = [
            int(u) for u in upstream.get(current, [])
            if int(u) in member and int(u) not in seen
        ]
        if not candidates:
            break
        nxt = max(candidates, key=lambda c: _accumulation(engine, c))
        path.append(nxt)
        seen.add(nxt)
        current = nxt
    return path


def _catchment_profile_path(engine, outlet_cell: int, member: set) -> List[int]:
    """Path for the catchment slope: from the subarea's highest cell down to the
    outlet, following the downstream receiver graph. Returned outlet -> high point
    so it shares the equal-area routine with the channel path.
    """
    if not member:
        return [int(outlet_cell)]
    high_cell = max(member, key=lambda c: _elevation(engine, c))
    downstream = getattr(engine, "downstream", None)
    path = [int(high_cell)]
    seen = {int(high_cell)}
    current = int(high_cell)
    while current != int(outlet_cell):
        try:
            nxt = int(downstream[current])
        except Exception:
            break
        if nxt < 0 or nxt in seen or nxt not in member:
            break
        path.append(nxt)
        seen.add(nxt)
        current = nxt
    path.reverse()  # outlet -> high point
    return path


def _reach_length_km(engine, path: List[int]) -> float:
    """Length in km of a cell path (outlet -> head)."""
    total = 0.0
    for i in range(1, len(path)):
        total += _cell_distance(engine, path[i - 1], path[i])
    return total / 1000.0


def _equal_area_slope(engine, path: List[int]) -> float:
    """Equal-area slope (m/m) of a longitudinal profile given as cells ordered
    outlet -> head. The equal-area slope is the slope of a line drawn from the
    outlet whose area equals the area under the actual elevation profile:
    Se = 2 * A / L**2, floored to SLOPE_FLOOR.
    """
    if len(path) < 2:
        return SLOPE_FLOOR
    outlet_z = _elevation(engine, path[0])
    distances = [0.0]
    heights = [0.0]
    running = 0.0
    for i in range(1, len(path)):
        running += _cell_distance(engine, path[i - 1], path[i])
        distances.append(running)
        heights.append(max(0.0, _elevation(engine, path[i]) - outlet_z))
    length = distances[-1]
    if length <= 0:
        return SLOPE_FLOOR
    area = 0.0
    for i in range(1, len(distances)):
        area += 0.5 * (heights[i] + heights[i - 1]) * (distances[i] - distances[i - 1])
    slope = 2.0 * area / (length * length)
    return max(slope, SLOPE_FLOOR)


# --- topology -------------------------------------------------------------

def _downstream_map(engine, assignments, selected) -> Dict[int, Optional[int]]:
    """Map each subarea outlet to the next downstream selected outlet, or None
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


def _all_upstream(subarea: str, upstream: Dict[str, List[str]]) -> List[str]:
    """Every subarea upstream of ``subarea``. Iterative depth-first with a
    cycle guard, so a malformed topology raises instead of looping forever."""
    if subarea not in upstream:
        return []
    out: List[str] = []
    seen: set = set()
    stack = [(subarea, iter(upstream.get(subarea, [])))]
    on_path = {subarea}
    while stack:
        node, children = stack[-1]
        advanced = False
        for child in children:
            if child in on_path:
                raise UrbsExportError(f"Cycle detected in subarea routing near '{child}'.")
            if child not in seen:
                seen.add(child)
                out.append(child)
                stack.append((child, iter(upstream.get(child, []))))
                on_path.add(child)
            advanced = True
            break
        if not advanced:
            stack.pop()
            on_path.discard(node)
    return out


# --- the routing tree -----------------------------------------------------

def _routing_tree_lines(
    ds_subcat: Dict[str, str],
    stream_lengths: Dict[str, float],
    stream_slopes: Dict[str, float],
) -> List[str]:
    """Build the ordered RAIN / ADD RAIN / ROUTE THRU / STORE. / GET. lines.

    A URBS routing file reads like a running hydrograph: STORE. sets the current
    hydrograph aside to start a new branch, GET. brings the last stored one back
    and adds it in. Headwater subareas start a hydrograph with RAIN; subareas with
    upstream inflow ROUTE THRU their reach then ADD RAIN the local runoff.
    """
    upstream: Dict[str, List[str]] = {sub: [] for sub in ds_subcat}
    original_upstream: Dict[str, List[str]] = {sub: [] for sub in ds_subcat}
    rooted: Dict[str, bool] = {sub: False for sub in ds_subcat}
    upstream_counts: Dict[str, int] = {sub: 0 for sub in ds_subcat}

    for sub, downstream in ds_subcat.items():
        if downstream in ("-1", "0"):
            continue
        if downstream not in upstream:
            raise UrbsExportError(
                f"Subarea {sub} drains to unknown subarea {downstream}."
            )
        upstream[downstream].append(sub)
        original_upstream[downstream].append(sub)

    for sub in ds_subcat:
        upstream_counts[sub] = len(_all_upstream(sub, upstream))

    def length(sub: str) -> float:
        return round(float(stream_lengths.get(sub, 0.0)), 5)

    def slope(sub: str) -> float:
        return round(max(float(stream_slopes.get(sub, 0.0)), SLOPE_FLOOR), 5)

    lines: List[str] = []
    indent = 0
    outlets = 0
    pending = True
    while pending:
        pending = False
        queue: List[str] = []
        best = -1
        for sub, done in rooted.items():
            if done:
                continue
            if upstream_counts[sub] > best:
                queue = [sub]
                best = upstream_counts[sub]

        while queue:
            sub = queue[0]
            if upstream[sub]:
                # Descend into the branch with the most upstream subareas first.
                nxt = max(upstream[sub], key=lambda u: upstream_counts[u])
                queue.append(nxt)
                queue.pop(0)
                continue

            pad = "\t" * indent
            rooted[sub] = True
            if upstream_counts[sub] == 0:
                lines.append(f"{pad}RAIN #{sub} L = {length(sub)} Sc = {slope(sub)} ")
            else:
                half = round(length(sub) / 2, 5)
                lines.append(f"{pad}ADD RAIN #{sub} L = {half} Sc = {slope(sub)} ")

            if ds_subcat[sub] not in ("-1", "0"):
                queue.append(ds_subcat[sub])
                try:
                    upstream[ds_subcat[sub]].remove(sub)
                except ValueError as exc:
                    raise UrbsExportError(
                        "Inconsistent upstream/downstream routing; recompute subcatchments."
                    ) from exc

            queue.pop(0)
            if queue:
                nxt = queue[0]
                if upstream[nxt]:
                    lines.append(f"{pad}STORE.")
                    indent += 1
                else:
                    for _ in range(1, len(original_upstream[nxt])):
                        indent -= 1
                        pad = "\t" * indent
                        lines.append(f"{pad}GET.")
                    half = round(length(nxt) / 2, 5)
                    lines.append(f"{pad}ROUTE THRU #{nxt} L = {half} Sc = {slope(nxt)} ")

        for sub, done in rooted.items():
            if not done:
                lines.append("STORE.")
                outlets += 1
                pending = True
                break

    for _ in range(outlets):
        lines.append("GET.")
    lines.append("END OF CATCHMENT DATA.")
    return lines


# --- the export -----------------------------------------------------------

def write_urbs_from_engine(
    engine,
    assignments: Dict[int, Iterable[int]],
    output_dir: str,
    model_name: str = "DDM_HydroLogic",
) -> Tuple[str, List[str], int, int, float]:
    """Write ``URBS_RoutingFile.vec`` and ``URBS_SubcatFile.csv`` into ``output_dir``.

    Returns ``(output_dir, written_paths, subarea_count, outlet_count, total_area_km2)``.
    """
    if engine is None:
        raise UrbsExportError("No DEM flow graph is available. Press Compute first.")
    if not assignments:
        raise UrbsExportError(
            "No subcatchment assignments are available. Press Process subcatchments first."
        )

    features = _subcatchment_features_by_outlet(engine)
    selected = {int(o) for o, cells in assignments.items() if cells and int(o) in features}
    if not selected:
        raise UrbsExportError(
            "No processed subcatchments with matching outlet_id values are available to export."
        )

    ds_map = _downstream_map(engine, assignments, selected)
    ordered = sorted(selected)
    id_of = {outlet: index for index, outlet in enumerate(ordered, start=1)}
    members = {int(o): {int(c) for c in (assignments.get(o) or [])} for o in ordered}

    ds_subcat: Dict[str, str] = {}
    stream_lengths: Dict[str, float] = {}
    stream_slopes: Dict[str, float] = {}
    csv_rows: List[str] = []
    total_area_km2 = 0.0

    for outlet in ordered:
        sub_id = str(id_of[outlet])
        member = members[int(outlet)]
        feat = features[int(outlet)]
        present = _feature_field_names(feat)

        area_km2 = _feature_area_km2(feat)
        total_area_km2 += area_km2
        frac_u = _feature_fraction(feat, LANDUSE_FIELDS["U"], present)
        frac_uf = _feature_fraction(feat, LANDUSE_FIELDS["UF"], present)
        frac_i = _feature_fraction(feat, LANDUSE_FIELDS["I"], present)

        stem = _main_stem_path(engine, int(outlet), member)
        stream_lengths[sub_id] = _reach_length_km(engine, stem)
        stream_slopes[sub_id] = _equal_area_slope(engine, stem)
        catch_slope = _equal_area_slope(engine, _catchment_profile_path(engine, int(outlet), member))

        downstream_outlet = ds_map.get(int(outlet))
        ds_subcat[sub_id] = "-1" if downstream_outlet is None else str(id_of[int(downstream_outlet)])

        csv_rows.append(
            f"{sub_id},{round(area_km2, 5)},{round(frac_u, 5)},"
            f"{round(frac_uf, 5)},{round(catch_slope, 5)},{round(frac_i, 5)}"
        )

    outlet_count = sum(1 for v in ds_map.values() if v is None)

    os.makedirs(os.path.abspath(output_dir), exist_ok=True)
    csv_path = os.path.join(output_dir, SUBCAT_FILE)
    vec_path = os.path.join(output_dir, ROUTING_FILE)

    with open(csv_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("Index,Area,U,UF,CS,I\n")
        handle.write("\n".join(csv_rows) + "\n")

    routing = [
        model_name,
        MODEL_LINE,
        USES_LINE,
        DEFAULT_PARAMETERS_LINE,
        f"CATCHMENT DATA FILE = {SUBCAT_FILE}",
    ]
    routing.extend(_routing_tree_lines(ds_subcat, stream_lengths, stream_slopes))
    with open(vec_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(routing) + "\n")

    return output_dir, [vec_path, csv_path], len(ordered), outlet_count, round(total_area_km2, 3)
