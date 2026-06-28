# -*- coding: utf-8 -*-
# DDM_HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Import RORB GE/RORBwin .catg graphical node/link blocks as temporary QGIS layers.

The importer is intentionally conservative. RORB GE .catg files contain a
textual hydrology/control block and a graphical block. For GIS display, the
safe pieces to reconstruct are the graphical #NODES and #REACHES sections. Basin/subcatchment areas are stored as node attributes where the .catg file includes them; polygon boundaries are not generally recoverable from these graphical blocks.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)


class RorbCatgImportError(Exception):
    """Raised when a .catg file cannot be parsed into temporary GIS layers."""


def _strip_comment_prefix(line: str) -> str:
    text = (line or "").rstrip("\n\r")
    if text.startswith("C"):
        return text[1:].strip()
    return text.strip()


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def _clean_node_ref(value: str) -> str:
    return str(value or "").strip().strip("<>")


def _extract_section(lines: List[str], section_header: str) -> List[str]:
    start = None
    header_upper = section_header.upper()
    for i, line in enumerate(lines):
        if header_upper in line.upper():
            start = i + 1
            break
    if start is None:
        return []

    result = []
    for line in lines[start:]:
        upper = line.upper()
        if line.startswith("C #") and header_upper not in upper:
            break
        if upper.startswith("C END RORB_GE"):
            break
        result.append(line)
    return result


def _parse_nodes(lines: List[str]) -> Dict[str, dict]:
    nodes: Dict[str, dict] = {}
    for raw in lines:
        stripped = _strip_comment_prefix(raw)
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 8:
            continue
        if not parts[0].lstrip("+-").isdigit() or not _looks_numeric(parts[1]) or not _looks_numeric(parts[2]):
            continue
        try:
            node_id = _clean_node_ref(parts[0])
            x = float(parts[1])
            y = float(parts[2])
            icon = float(parts[3]) if len(parts) > 3 and _looks_numeric(parts[3]) else 0.0
            is_basin = int(float(parts[4])) if len(parts) > 4 and _looks_numeric(parts[4]) else 0
            is_outlet = int(float(parts[5])) if len(parts) > 5 and _looks_numeric(parts[5]) else 0
            downstream = _clean_node_ref(parts[6]) if len(parts) > 6 else ""
            name = parts[7] if len(parts) > 7 else node_id
            area_km2 = float(parts[8]) if len(parts) > 8 and _looks_numeric(parts[8]) else 0.0
            fi = float(parts[9]) if len(parts) > 9 and _looks_numeric(parts[9]) else 0.0
            print_code = int(float(parts[10])) if len(parts) > 10 and _looks_numeric(parts[10]) else 0
            excess = int(float(parts[11])) if len(parts) > 11 and _looks_numeric(parts[11]) else 0
            comment = int(float(parts[12])) if len(parts) > 12 and _looks_numeric(parts[12]) else 0
        except Exception:
            continue
        nodes[node_id] = {
            "node_id": node_id,
            "name": name,
            "x": x,
            "y": y,
            "icon": icon,
            "is_basin": is_basin,
            "is_outlet": is_outlet,
            "downstream": downstream,
            "area_km2": area_km2,
            "imperv_frac": fi,
            "print_code": print_code,
            "excess": excess,
            "comment": comment,
        }
    return nodes


def _parse_reaches(lines: List[str]) -> List[dict]:
    reaches: List[dict] = []
    i = 0
    while i < len(lines):
        stripped = _strip_comment_prefix(lines[i])
        parts = stripped.split()
        if len(parts) < 10 or not parts[0].lstrip("+-").isdigit():
            i += 1
            continue
        try:
            reach_id = _clean_node_ref(parts[0])

            # RORB GE reach records can either include a reach-name field or
            # leave the fixed-width name field blank.  When the name is blank,
            # ``split()`` collapses that empty field, so the record has this
            # layout:
            #   id us_node ds_node translation reach_type print length slope npts comment
            # The plugin writer now deliberately uses blank reach names because
            # RORBwin accepted that format.  The importer therefore needs to
            # support both forms, otherwise it interprets us/ds nodes one column
            # too far to the right and no QGIS link features are created.
            if len(parts) >= 11 and (not _looks_numeric(parts[1])):
                name = parts[1]
                us_node = _clean_node_ref(parts[2])
                ds_node = _clean_node_ref(parts[3])
                offset = 4
            else:
                name = ""
                us_node = _clean_node_ref(parts[1])
                ds_node = _clean_node_ref(parts[2])
                offset = 3

            translation = int(float(parts[offset])) if len(parts) > offset and _looks_numeric(parts[offset]) else 0
            reach_type = int(float(parts[offset + 1])) if len(parts) > offset + 1 and _looks_numeric(parts[offset + 1]) else 0
            print_code = int(float(parts[offset + 2])) if len(parts) > offset + 2 and _looks_numeric(parts[offset + 2]) else 0
            length_km = float(parts[offset + 3]) if len(parts) > offset + 3 and _looks_numeric(parts[offset + 3]) else 0.0
            slope = float(parts[offset + 4]) if len(parts) > offset + 4 and _looks_numeric(parts[offset + 4]) else 0.0
            npoints = int(float(parts[offset + 5])) if len(parts) > offset + 5 and _looks_numeric(parts[offset + 5]) else 0
            comment = int(float(parts[offset + 6])) if len(parts) > offset + 6 and _looks_numeric(parts[offset + 6]) else 0
        except Exception:
            i += 1
            continue

        mid_x = None
        mid_y = None
        # RORB GE-style files write graphical reach x and y on the following two C-lines.
        if i + 1 < len(lines):
            p1 = _strip_comment_prefix(lines[i + 1]).split()
            if p1 and _looks_numeric(p1[0]):
                mid_x = float(p1[0])
                i += 1
        if i + 1 < len(lines):
            p2 = _strip_comment_prefix(lines[i + 1]).split()
            if p2 and _looks_numeric(p2[0]):
                mid_y = float(p2[0])
                i += 1

        reaches.append({
            "reach_id": reach_id,
            "name": name,
            "us_node": us_node,
            "ds_node": ds_node,
            "translation": translation,
            "reach_type": reach_type,
            "print_code": print_code,
            "length_km": length_km,
            "slope": slope,
            "npoints": npoints,
            "comment": comment,
            "mid_x": mid_x,
            "mid_y": mid_y,
        })
        i += 1
    return reaches


def load_rorb_catg_layers(path: str, crs_uri: str = "") -> Tuple[QgsVectorLayer, QgsVectorLayer]:
    """Loads RORB GE .catg graphical nodes/reaches into temporary memory layers."""
    if not path or not os.path.exists(path):
        raise RorbCatgImportError("The selected .catg file does not exist.")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception as exc:
        raise RorbCatgImportError(f"Could not read the selected .catg file: {exc}") from exc

    node_section = _extract_section(lines, "#NODES")
    reach_section = _extract_section(lines, "#REACHES")
    nodes = _parse_nodes(node_section)
    reaches = _parse_reaches(reach_section)
    if not nodes:
        raise RorbCatgImportError("No RORB GE #NODES block could be parsed from the selected .catg file.")
    if not reaches:
        raise RorbCatgImportError("No RORB GE #REACHES block could be parsed from the selected .catg file.")

    crs = f"?crs={crs_uri}" if crs_uri else ""
    nodes_layer = QgsVectorLayer(f"Point{crs}", "RORB nodes", "memory")
    nodes_provider = nodes_layer.dataProvider()
    nodes_provider.addAttributes([
        QgsField("node_id", QVariant.String),
        QgsField("name", QVariant.String),
        QgsField("downstream", QVariant.String),
        QgsField("is_basin", QVariant.Int),
        QgsField("is_outlet", QVariant.Int),
        QgsField("area_km2", QVariant.Double, "double", 20, 6),
        QgsField("imperv_frac", QVariant.Double, "double", 12, 6),
        QgsField("print_code", QVariant.Int),
    ])
    nodes_layer.updateFields()

    node_features = []
    for node in nodes.values():
        feat = QgsFeature(nodes_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(node["x"]), float(node["y"]))))
        feat.setAttributes([
            node["node_id"], node["name"], node["downstream"], int(node["is_basin"]), int(node["is_outlet"]),
            float(node["area_km2"]), float(node["imperv_frac"]), int(node["print_code"]),
        ])
        node_features.append(feat)
    nodes_provider.addFeatures(node_features)
    nodes_layer.updateExtents()

    links_layer = QgsVectorLayer(f"LineString{crs}", "RORB links", "memory")
    links_provider = links_layer.dataProvider()
    links_provider.addAttributes([
        QgsField("reach_id", QVariant.String),
        QgsField("name", QVariant.String),
        QgsField("us_node", QVariant.String),
        QgsField("ds_node", QVariant.String),
        QgsField("reach_type", QVariant.Int),
        QgsField("length_km", QVariant.Double, "double", 20, 6),
        QgsField("slope", QVariant.Double, "double", 20, 6),
        QgsField("print_code", QVariant.Int),
    ])
    links_layer.updateFields()

    link_features = []
    for reach in reaches:
        us = nodes.get(reach["us_node"])
        ds = nodes.get(reach["ds_node"])
        if not us or not ds:
            continue
        pts = [QgsPointXY(float(us["x"]), float(us["y"]))]
        if reach.get("mid_x") is not None and reach.get("mid_y") is not None:
            mid = QgsPointXY(float(reach["mid_x"]), float(reach["mid_y"]))
            # Only include a midpoint where it actually changes the display geometry.
            if mid != pts[-1]:
                pts.append(mid)
        ds_pt = QgsPointXY(float(ds["x"]), float(ds["y"]))
        if ds_pt != pts[-1]:
            pts.append(ds_pt)
        if len(pts) < 2:
            continue
        feat = QgsFeature(links_layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
        feat.setAttributes([
            reach["reach_id"], reach["name"], reach["us_node"], reach["ds_node"], int(reach["reach_type"]),
            float(reach["length_km"]), float(reach["slope"]), int(reach["print_code"]),
        ])
        link_features.append(feat)
    links_provider.addFeatures(link_features)
    links_layer.updateExtents()

    node_symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,165,0", "outline_color": "90,90,90", "size": "2.8"})
    nodes_layer.setRenderer(QgsSingleSymbolRenderer(node_symbol))
    link_symbol = QgsLineSymbol.createSimple({"line_color": "180,80,0", "line_width": "0.55"})
    links_layer.setRenderer(QgsSingleSymbolRenderer(link_symbol))

    return nodes_layer, links_layer
