# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Interactive DEM flow-path engine for DDM HydroLogic.

This module intentionally keeps the hydrology logic independent from the dock UI so
it can be tested more easily inside QGIS. It builds a D8 graph directly from a DEM,
then exposes helpers for upstream tracing, outlet-line selection and vector export.
"""

from collections import defaultdict
import heapq
import math

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsFeatureRequest,
    QgsGraduatedSymbolRenderer,
    QgsLineSymbol,
    QgsPointXY,
    QgsProject,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsFillSymbol,
)


class HydrologyBuildError(Exception):
    """Raised when the DEM cannot be converted into a usable flow graph."""


class HydrologyCancelled(Exception):
    """Raised when the user aborts a running hydrology operation."""


class D8HydrologyEngine:
    """Build and query a D8 flow graph from a north-up raster DEM."""

    def __init__(self, dem_layer, progress_callback=None, cancel_callback=None, mask_layer=None):
        self.dem_layer = dem_layer
        self.progress_callback = progress_callback or (lambda _pct, _msg: None)
        self.cancel_callback = cancel_callback or (lambda: False)
        # Boundary-aware hydrological connectivity is always used, with any optional
        # mask polygon rasterised into the valid-cell domain before routing.
        self.mask_layer = mask_layer
        self.mask_cell_count = 0
        self.mask_raster_cell_count = 0
        self.mask_nodata_cell_count = 0
        self.mask_nodata_cell_ids = []

        self.dataset_path = None
        self.geotransform = None
        self.projection = None
        self.rows = 0
        self.cols = 0
        self.cell_width = 0.0
        self.cell_height = 0.0
        self.cell_area = 0.0
        self.nodata = None
        self.dem = None
        self.valid = None

        self.downstream = None
        self.accumulation = None
        self.strahler = None
        self.display_strahler_by_cell = {}
        self.upstream = defaultdict(list)
        self.valid_ids = None
        self.filled_dem = None
        self.connectivity_outlet_count = 0

        self.flow_layer = None
        self.highlight_layer = None
        self.subcatchment_layer = None
        self.spatial_index = None
        self.feature_to_cell = {}
        self.cell_to_feature = {}

    # ------------------------------------------------------------------
    # DEM reading and graph construction
    # ------------------------------------------------------------------
    def build(self):
        """Read DEM data and build downstream, accumulation and Strahler arrays."""
        self._read_dem()
        self._build_downstream_graph()
        self._build_upstream_accumulation_and_strahler()
        return self

    def _emit(self, pct, msg):
        self._check_cancelled()
        self.progress_callback(int(pct), msg)
        self._check_cancelled()

    def _check_cancelled(self):
        try:
            if self.cancel_callback():
                raise HydrologyCancelled("Processing aborted by user.")
        except HydrologyCancelled:
            raise
        except Exception:
            # Cancellation checks must never become the reason a hydrology build fails.
            pass

    def _read_dem(self):
        self._emit(2, "Reading DEM")
        uri = self.dem_layer.dataProvider().dataSourceUri()
        self.dataset_path = uri.split("|", 1)[0]

        ds = gdal.Open(self.dataset_path)
        if ds is None:
            raise HydrologyBuildError(f"GDAL could not open DEM source: {self.dataset_path}")

        gt = ds.GetGeoTransform()
        if gt is None:
            raise HydrologyBuildError("The DEM does not expose a GDAL geotransform.")
        if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
            raise HydrologyBuildError(
                "Rotated/skewed rasters are not supported by the interactive graph builder. "
                "Reproject/resample the DEM to a north-up grid first."
            )

        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        if arr is None:
            raise HydrologyBuildError("Could not read band 1 from the DEM.")

        self.geotransform = gt
        self.projection = ds.GetProjection()
        self.rows, self.cols = arr.shape
        self.cell_width = abs(float(gt[1]))
        self.cell_height = abs(float(gt[5]))
        self.cell_area = self.cell_width * self.cell_height
        self.nodata = band.GetNoDataValue()

        dem = arr.astype("float64", copy=False)
        valid = np.isfinite(dem)
        if self.nodata is not None and np.isfinite(self.nodata):
            valid &= dem != float(self.nodata)

        self.dem = dem
        self.valid = valid

        if self.mask_layer is not None:
            self._emit(8, "Applying analysis mask polygon to DEM")
            valid = self._apply_analysis_mask_to_valid(valid)

        if not np.any(valid):
            raise HydrologyBuildError("The DEM contains no valid cells after NoData and analysis-mask filtering.")

        self.valid = valid
        self.valid_ids = np.flatnonzero(valid.reshape(-1))
        suffix = f"; analysis mask cells: {self.mask_cell_count:,}" if self.mask_layer is not None else ""
        self._emit(12, f"DEM read: {self.cols} x {self.rows} cells{suffix}")

    def _combined_mask_geometry_in_dem_crs(self):
        """Return the optional analysis mask geometry transformed to the DEM CRS."""
        layer = self.mask_layer
        if layer is None:
            return None
        try:
            if not layer.isValid():
                return None
        except Exception:
            return None

        geoms = []
        try:
            for feat in layer.getFeatures():
                self._check_cancelled()
                geom = feat.geometry()
                if geom is not None and not geom.isNull() and not geom.isEmpty():
                    geoms.append(QgsGeometry(geom))
        except Exception as exc:
            raise HydrologyBuildError(f"Could not read the analysis mask polygon: {exc}") from exc

        if not geoms:
            return None
        mask_geom = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
        if mask_geom.isNull() or mask_geom.isEmpty():
            return None
        try:
            if not mask_geom.isGeosValid():
                fixed = mask_geom.buffer(0, 1)
                if not fixed.isNull() and not fixed.isEmpty():
                    mask_geom = fixed
        except Exception:
            pass

        try:
            source_crs = layer.crs()
            dest_crs = self.dem_layer.crs()
            if source_crs != dest_crs:
                transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
                mask_geom.transform(transform)
        except Exception as exc:
            raise HydrologyBuildError(f"Could not transform the analysis mask polygon to the DEM CRS: {exc}") from exc
        return mask_geom

    def _rasterize_mask_geometry(self, mask_geom):
        """Rasterise a QGIS mask geometry onto the DEM grid using GDAL MEM."""
        try:
            mask_ds = gdal.GetDriverByName("MEM").Create("", int(self.cols), int(self.rows), 1, gdal.GDT_Byte)
            mask_ds.SetGeoTransform(self.geotransform)
            if self.projection:
                mask_ds.SetProjection(self.projection)
            mask_band = mask_ds.GetRasterBand(1)
            mask_band.Fill(0)

            ogr_driver = ogr.GetDriverByName("Memory")
            vector_ds = ogr_driver.CreateDataSource("ddm_mask")
            srs = None
            if self.projection:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(self.projection)
            vector_layer = vector_ds.CreateLayer("mask", srs=srs, geom_type=ogr.wkbMultiPolygon)
            feature_defn = vector_layer.GetLayerDefn()
            feature = ogr.Feature(feature_defn)
            ogr_geom = ogr.CreateGeometryFromWkt(mask_geom.asWkt())
            if ogr_geom is None:
                raise HydrologyBuildError("Could not convert the analysis mask geometry to OGR WKT.")
            if ogr_geom.GetGeometryType() == ogr.wkbPolygon:
                multi = ogr.Geometry(ogr.wkbMultiPolygon)
                multi.AddGeometry(ogr_geom)
                ogr_geom = multi
            feature.SetGeometry(ogr_geom)
            vector_layer.CreateFeature(feature)
            feature = None

            err = gdal.RasterizeLayer(mask_ds, [1], vector_layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
            if err != 0:
                raise HydrologyBuildError(f"GDAL RasterizeLayer failed with code {err}.")
            arr = mask_band.ReadAsArray()
            return arr.astype(bool)
        except HydrologyBuildError:
            raise
        except Exception as exc:
            raise HydrologyBuildError(f"Could not rasterise the analysis mask polygon: {exc}") from exc

    def _apply_analysis_mask_to_valid(self, valid):
        """Restrict valid DEM cells to the optional user digitised mask polygon."""
        mask_geom = self._combined_mask_geometry_in_dem_crs()
        if mask_geom is None:
            self.mask_layer = None
            self.mask_cell_count = 0
            self.mask_raster_cell_count = 0
            self.mask_nodata_cell_count = 0
            self.mask_nodata_cell_ids = []
            return valid

        mask_valid = self._rasterize_mask_geometry(mask_geom)
        filtered = valid & mask_valid
        nodata_mask = mask_valid & (~valid)
        self.mask_raster_cell_count = int(np.count_nonzero(mask_valid))
        self.mask_cell_count = int(np.count_nonzero(filtered))
        self.mask_nodata_cell_count = int(np.count_nonzero(nodata_mask))
        if self.mask_nodata_cell_count:
            self.mask_nodata_cell_ids = [int(i) for i in np.flatnonzero(nodata_mask.reshape(-1))]
        else:
            self.mask_nodata_cell_ids = []
        if self.mask_cell_count <= 0:
            raise HydrologyBuildError("The analysis mask polygon does not overlap any valid DEM cells.")
        return filtered

    def inspect_analysis_mask_quality(self):
        """Return a preflight summary of how the optional mask intersects the DEM.

        The dock uses this before full graph construction so the user can decide
        whether to continue when the digitised mask touches NoData/outside cells.
        """
        if self.mask_layer is None:
            return {
                "has_mask": False,
                "mask_cells": 0,
                "valid_cells": 0,
                "nodata_cells": 0,
                "nodata_cell_ids": [],
            }
        self._read_dem()
        return {
            "has_mask": True,
            "mask_cells": int(self.mask_raster_cell_count),
            "valid_cells": int(self.mask_cell_count),
            "nodata_cells": int(self.mask_nodata_cell_count),
            "nodata_cell_ids": list(self.mask_nodata_cell_ids),
        }

    def _build_downstream_graph(self):
        """Build the D8 receiver graph using boundary-aware connectivity."""
        self._build_downstream_graph_fill_connectivity()

    def _valid_boundary_mask(self):
        """Return valid DEM cells that can behave as outlets.

        A clipped DEM often has a NoData frame around the real terrain. Boundary-aware
        routing treats valid cells beside NoData as possible outlets rather than
        forcing the graph toward a single interior low point.

        Boundary cells are therefore any valid cells on the raster edge OR any
        valid cells touching NoData in the D8 neighbourhood.
        """
        valid = self.valid
        rows, cols = self.rows, self.cols
        boundary = np.zeros((rows, cols), dtype=bool)
        if rows <= 0 or cols <= 0:
            return boundary

        boundary[0, :] |= valid[0, :]
        boundary[rows - 1, :] |= valid[rows - 1, :]
        boundary[:, 0] |= valid[:, 0]
        boundary[:, cols - 1] |= valid[:, cols - 1]

        invalid = ~valid
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                src_r = slice(max(0, -dr), min(rows, rows - dr))
                src_c = slice(max(0, -dc), min(cols, cols - dc))
                nbr_r = slice(max(0, dr), min(rows, rows + dr))
                nbr_c = slice(max(0, dc), min(cols, cols + dc))
                view = boundary[src_r, src_c]
                view |= valid[src_r, src_c] & invalid[nbr_r, nbr_c]
                boundary[src_r, src_c] = view
        return boundary

    def _priority_flood_connectivity_graph(self, progress_start=18, progress_span=34, message="Connecting D8 flow paths from low cells upstream"):
        """Build a boundary-aware priority-flood receiver graph.

        This helper is used by the full fill/connectivity mode and as a safety
        fallback for the pre-conditioned mode. It starts from every valid
        raster/mask boundary cell, not merely from the outside row/column of the
        raster. That distinction matters for clipped DEMs with a NoData collar.
        """
        rows, cols = self.rows, self.cols
        downstream = np.full(rows * cols, -1, dtype=np.int64)
        filled = np.full((rows, cols), np.nan, dtype="float64")
        visited = np.zeros((rows, cols), dtype=bool)
        visit_order = np.full(rows * cols, -1, dtype=np.int64)

        directions = (
            (-1, -1), (-1, 0), (-1, 1),
            (0, -1), (0, 1),
            (1, -1), (1, 0), (1, 1),
        )

        heap = []
        counter = 0
        outlet_count = 0
        popped = 0

        def add_seed(row, col):
            nonlocal counter, outlet_count
            if not self.valid[row, col] or visited[row, col]:
                return
            visited[row, col] = True
            elev = float(self.dem[row, col])
            filled[row, col] = elev
            heapq.heappush(heap, (elev, counter, int(row), int(col)))
            counter += 1
            outlet_count += 1

        boundary = self._valid_boundary_mask()
        seed_ids = np.flatnonzero(boundary.reshape(-1))
        for seed_id in seed_ids:
            self._check_cancelled()
            add_seed(int(seed_id) // cols, int(seed_id) % cols)

        if not heap:
            # Extremely defensive fallback. A valid DEM with no boundary should
            # not exist after the boundary mask above, but GIS formats are an
            # endless source of character-building disappointment.
            seed_id = min(self.valid_ids, key=lambda cid: float(self.dem[int(cid) // cols, int(cid) % cols]))
            add_seed(int(seed_id) // cols, int(seed_id) % cols)

        total_valid = max(1, len(self.valid_ids))
        processed = 0

        while True:
            while heap:
                self._check_cancelled()
                elev, _seq, row, col = heapq.heappop(heap)
                cell_id = row * cols + col
                if visit_order[cell_id] < 0:
                    visit_order[cell_id] = popped
                    popped += 1
                processed += 1

                for dr, dc in directions:
                    nr = row + dr
                    nc = col + dc
                    if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                        continue
                    if visited[nr, nc] or not self.valid[nr, nc]:
                        continue

                    nbr_id = nr * cols + nc
                    visited[nr, nc] = True
                    downstream[nbr_id] = cell_id

                    nbr_elev = float(self.dem[nr, nc])
                    spill_elev = max(nbr_elev, float(elev))
                    filled[nr, nc] = spill_elev
                    heapq.heappush(heap, (spill_elev, counter, int(nr), int(nc)))
                    counter += 1

                if processed % 50000 == 0:
                    self._emit(progress_start + progress_span * processed / total_valid, message)

            self._check_cancelled()
            remaining = self.valid & ~visited
            if not np.any(remaining):
                break
            remaining_ids = np.flatnonzero(remaining.reshape(-1))
            seed_id = min(remaining_ids, key=lambda cid: float(self.dem[int(cid) // cols, int(cid) % cols]))
            add_seed(int(seed_id) // cols, int(seed_id) % cols)

        return downstream, filled, outlet_count, visit_order

    def _build_downstream_graph_fill_connectivity(self):
        """Build a hydrologically connected D8 receiver graph.

        Uses a boundary-aware priority-flood pass. Valid cells adjacent to NoData
        are seeded as outlets as well as valid cells on the outer raster edge.
        This avoids the old behaviour where a clipped DEM with a NoData border
        could be forced to drain everything to one global-low cell.
        """
        self._emit(18, "Building hydrologically connected D8 flow graph")
        downstream, filled, outlet_count, _visit_order = self._priority_flood_connectivity_graph(
            progress_start=18,
            progress_span=34,
            message="Connecting D8 flow paths from low boundary cells upstream",
        )
        self.downstream = downstream
        self.filled_dem = filled
        self.connectivity_outlet_count = outlet_count
        self._emit(52, f"D8 connectivity complete. Boundary outlet cells: {outlet_count:,}")

    def _build_upstream_accumulation_and_strahler(self):
        """Compute accumulation and Strahler order over the connected D8 graph.

        The traversal is rooted at the lowest/outlet cells generated by the
        connectivity pass, then expands upstream through the receiver tree. Cell
        totals are finalised in post-order so each downstream cell receives the
        complete contribution from all of its upstream tributaries. That gives
        proper hydrological connectivity without double-counting cells.
        """
        self._emit(56, "Computing accumulation and Strahler order from connected outlets")

        n = self.rows * self.cols
        valid_flat = self.valid.reshape(-1)
        self.upstream = defaultdict(list)

        for src in self.valid_ids:
            dst = int(self.downstream[int(src)])
            if dst >= 0 and valid_flat[dst]:
                self.upstream[dst].append(int(src))

        # Stable outlet order: lowest filled/elevation outlets first. This does
        # not change the maths, but it keeps processing and layer creation
        # deterministic, a rare kindness in GIS software.
        outlet_ids = [int(i) for i in self.valid_ids if int(self.downstream[int(i)]) < 0]
        outlet_ids.sort(key=lambda cid: (float(self.filled_dem[cid // self.cols, cid % self.cols]), cid))

        accumulation = np.zeros(n, dtype=np.int64)
        # Broad DEM-cell Strahler-style ordering is kept as a fallback/internal
        # diagnostic. Display and export Strahler order is recalculated later on
        # the extracted stream network only, because official Strahler stream
        # order applies to stream/channel links, not every DEM cell.
        strahler = np.full(n, -1, dtype=np.int16)
        processed = 0
        total_valid = max(1, len(self.valid_ids))

        seen_roots = set()
        for outlet in outlet_ids:
            self._check_cancelled()
            if outlet in seen_roots:
                continue
            stack = [(outlet, False)]
            while stack:
                self._check_cancelled()
                cell_id, expanded = stack.pop()
                if not expanded:
                    if accumulation[cell_id] > 0:
                        continue
                    stack.append((cell_id, True))
                    for upstream_id in self.upstream.get(cell_id, []):
                        if accumulation[upstream_id] == 0:
                            stack.append((upstream_id, False))
                else:
                    children = self.upstream.get(cell_id, [])
                    if not children:
                        accumulation[cell_id] = 1
                        strahler[cell_id] = 1
                    else:
                        accumulation[cell_id] = 1 + sum(int(accumulation[ch]) for ch in children)
                        child_orders = [int(strahler[ch]) for ch in children if int(strahler[ch]) >= 1]
                        if not child_orders:
                            strahler[cell_id] = 1
                        else:
                            max_order = max(child_orders)
                            strahler[cell_id] = max_order + (1 if child_orders.count(max_order) >= 2 else 0)
                    seen_roots.add(cell_id)
                    processed += 1
                    if processed % 50000 == 0:
                        self._emit(56 + 24 * processed / total_valid, "Accumulation and Strahler order")

        # Safety fallback for any valid cell not reached because of an unexpected
        # malformed graph. This should not happen after the connectivity pass, but
        # defensive code is cheaper than explaining a crash to QGIS users.
        missing = [int(i) for i in self.valid_ids if accumulation[int(i)] == 0]
        for cell_id in missing:
            accumulation[cell_id] = 1
            strahler[cell_id] = 1

        self.accumulation = accumulation
        self.strahler = strahler
        self._emit(82, "Accumulation and Strahler order complete")

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def cell_center(self, cell_id):
        row = int(cell_id) // self.cols
        col = int(cell_id) % self.cols
        gt = self.geotransform
        x = gt[0] + (col + 0.5) * gt[1]
        y = gt[3] + (row + 0.5) * gt[5]
        return QgsPointXY(float(x), float(y))

    def cell_polygon(self, cell_id):
        row = int(cell_id) // self.cols
        col = int(cell_id) % self.cols
        gt = self.geotransform
        x0 = gt[0] + col * gt[1]
        x1 = gt[0] + (col + 1) * gt[1]
        y0 = gt[3] + row * gt[5]
        y1 = gt[3] + (row + 1) * gt[5]
        xmin, xmax = sorted((float(x0), float(x1)))
        ymin, ymax = sorted((float(y0), float(y1)))
        ring = [
            QgsPointXY(xmin, ymin),
            QgsPointXY(xmax, ymin),
            QgsPointXY(xmax, ymax),
            QgsPointXY(xmin, ymax),
            QgsPointXY(xmin, ymin),
        ]
        return QgsGeometry.fromPolygonXY([ring])

    def point_to_cell_id(self, map_point, source_crs=None):
        """Return the valid DEM cell id containing a map/canvas point.

        This is deliberately different from nearest_flow_cell(): CTRL-click
        deselection acts on the highlighted catchment polygon area, not the
        closest visible flow-path line. The point is transformed into the DEM
        CRS, converted through the north-up raster geotransform, then checked
        against the valid DEM mask.
        """
        if self.geotransform is None or self.valid is None:
            return None
        point = QgsPointXY(map_point)
        if source_crs is not None and self.dem_layer is not None:
            point = self._transform_point(point, source_crs, self.dem_layer.crs())

        gt = self.geotransform
        try:
            col = int(math.floor((float(point.x()) - float(gt[0])) / float(gt[1])))
            row = int(math.floor((float(point.y()) - float(gt[3])) / float(gt[5])))
        except Exception:
            return None

        if row < 0 or row >= self.rows or col < 0 or col >= self.cols:
            return None
        if not bool(self.valid[row, col]):
            return None
        return int(row * self.cols + col)

    def _layer_crs_uri(self):
        crs = self.dem_layer.crs()
        authid = crs.authid()
        if authid:
            return authid
        # Fallback. Most project DEMs should have an authid, but QGIS memory layers
        # can still accept a WKT-style CRS URI in current versions.
        return crs.toWkt()

    # ------------------------------------------------------------------
    # Vector layer creation and styling
    # ------------------------------------------------------------------
    def _compute_stream_strahler_orders(self, displayed_cells):
        """Compute classic 1-based Strahler order on the extracted stream network.

        The DEM graph contains every valid raster cell. Strahler stream order,
        however, is defined on the extracted stream/channel network. Therefore
        this method works only on cells that will be displayed/exported as flow
        path features after the user's accumulation threshold is applied.

        Rules implemented:
        - channel/source links with no upstream channel link are Strahler 1;
        - if two or more upstream channel links of the maximum order meet, the
          downstream channel order increases by one;
        - if a lower-order link joins a higher-order link, the downstream order
          stays at the higher order.
        """
        displayed = {int(c) for c in displayed_cells}
        if not displayed:
            self.display_strahler_by_cell = {}
            return {}

        stream_upstream = {
            c: [int(u) for u in self.upstream.get(int(c), []) if int(u) in displayed]
            for c in displayed
        }

        # Accumulation increases monotonically downstream in the D8 tree, so this
        # is an upstream-to-downstream traversal of the extracted stream network.
        ordered_cells = sorted(displayed, key=lambda c: (int(self.accumulation[int(c)]), int(c)))
        stream_order = {}
        for cell_id in ordered_cells:
            child_orders = [int(stream_order[u]) for u in stream_upstream.get(int(cell_id), []) if int(u) in stream_order]
            if not child_orders:
                stream_order[int(cell_id)] = 1
                continue
            max_order = max(child_orders)
            stream_order[int(cell_id)] = int(max_order + 1 if child_orders.count(max_order) >= 2 else max_order)

        # Defensive fallback in case a malformed graph left a stream cell without
        # processed upstream records. It should not occur, but QGIS users already
        # suffer enough without mysterious missing attribute values.
        for cell_id in displayed:
            stream_order.setdefault(int(cell_id), 1)

        self.display_strahler_by_cell = stream_order
        return stream_order

    def create_flow_layer(self, min_accumulation=1):
        """Create a temporary line layer containing cell-to-cell flow segments."""
        self._emit(84, "Creating temporary flow-path line layer")

        layer = QgsVectorLayer(
            f"LineString?crs={self._layer_crs_uri()}",
            "DDM HydroLogic flow paths - temporary",
            "memory",
        )
        provider = layer.dataProvider()
        fields = QgsFields()
        for name, variant in (
            ("cell_id", QVariant.LongLong),
            ("row", QVariant.Int),
            ("col", QVariant.Int),
            ("down_id", QVariant.LongLong),
            ("acc_cells", QVariant.LongLong),
            ("strahler", QVariant.Int),
            ("length_m", QVariant.Double),
        ):
            field = QgsField(name, variant)
            if name == "length_m":
                field.setLength(20)
                field.setPrecision(2)
            fields.append(field)
        provider.addAttributes(fields)
        layer.updateFields()

        candidate_ids = []
        for cell_id in self.valid_ids:
            down_id = int(self.downstream[cell_id])
            if down_id < 0:
                continue
            if int(self.accumulation[cell_id]) < int(min_accumulation):
                continue
            candidate_ids.append(int(cell_id))

        stream_strahler = self._compute_stream_strahler_orders(candidate_ids)
        candidate_ids.sort(key=lambda c: (int(stream_strahler.get(int(c), 1)), int(self.accumulation[c]), c))
        fields = layer.fields()
        features = []
        chunk_size = 25000
        total = max(1, len(candidate_ids))

        for idx, cell_id in enumerate(candidate_ids):
            self._check_cancelled()
            down_id = int(self.downstream[cell_id])
            p0 = self.cell_center(cell_id)
            p1 = self.cell_center(down_id) if down_id >= 0 else p0
            geom = QgsGeometry.fromPolylineXY([p0, p1])
            row = cell_id // self.cols
            col = cell_id % self.cols
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes([
                int(cell_id),
                int(row),
                int(col),
                int(down_id),
                int(self.accumulation[cell_id]),
                int(stream_strahler.get(int(cell_id), 1)),
                round(float(geom.length()), 2),
            ])
            features.append(feat)

            if len(features) >= chunk_size:
                provider.addFeatures(features)
                features = []
                self._emit(84 + 10 * idx / total, "Adding flow-path features")

        if features:
            provider.addFeatures(features)

        layer.updateExtents()
        self._style_flow_layer(layer)
        self.flow_layer = layer
        self._rebuild_spatial_index()
        self._emit(95, "Flow-path layer ready")
        return layer

    def _style_flow_layer(self, layer):
        """Style flow paths in blues, with higher Strahler order darker/thicker."""
        orders = set()
        try:
            for feat in layer.getFeatures():
                try:
                    order = int(feat["strahler"])
                    if order >= 1:
                        orders.add(order)
                except Exception:
                    continue
        except Exception:
            # Fallback for partially initialised layers. Cartography should not be
            # the reason processing fails, despite QGIS occasionally trying.
            orders = set(int(v) for v in getattr(self, "display_strahler_by_cell", {}).values() if int(v) >= 1)

        orders = sorted(orders)
        if not orders:
            symbol = QgsLineSymbol.createSimple({"color": "120,170,210", "width": "0.20"})
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            return

        min_order = min(orders)
        max_order = max(orders)
        light = (190, 222, 246)
        dark = (0, 32, 95)
        ranges = []
        for order in orders:
            t = 0.0 if max_order == min_order else (order - min_order) / float(max_order - min_order)
            rgb = tuple(int(light[i] + (dark[i] - light[i]) * t) for i in range(3))
            width = 0.25 + min(order, 12) * 0.12
            symbol = QgsLineSymbol.createSimple({"color": f"{rgb[0]},{rgb[1]},{rgb[2]}", "width": f"{width:.2f}"})
            ranges.append(QgsRendererRange(order - 0.5, order + 0.5, symbol, f"Strahler {order}"))
        renderer = QgsGraduatedSymbolRenderer("strahler", ranges)
        layer.setRenderer(renderer)

    def create_reach_flow_layer(self):
        """Create an export-only flow layer merged into same-order Strahler reaches.

        The temporary display layer keeps one feature per DEM-cell segment for
        precise clicking. The final GeoPackage is cleaner: consecutive displayed
        segments are merged while they share the same Strahler order. Cells with
        Strahler order less than 1 are excluded. A lower-order tributary joining a
        higher-order stem no longer fragments the higher-order stem, because the
        Strahler hierarchy has not changed. Reaches are split where the same-order
        sequence stops, where the downstream order changes, or where same-order
        topology would be ambiguous.
        """
        self._check_cancelled()
        if self.flow_layer is None:
            raise HydrologyBuildError("No temporary flow-path layer is available for export.")

        displayed_cells = set()
        stream_order = {}
        for feat in self.flow_layer.getFeatures():
            self._check_cancelled()
            try:
                cell_id = int(feat["cell_id"])
                displayed_cells.add(cell_id)
                stream_order[cell_id] = max(1, int(feat["strahler"]))
            except Exception:
                continue
        if not displayed_cells:
            raise HydrologyBuildError("The temporary flow-path layer contains no displayed flow paths to export.")

        # Recalculate from the displayed network as the source of truth in case a
        # stale temporary layer was created before the corrected Strahler logic.
        stream_order = self._compute_stream_strahler_orders(displayed_cells)

        # Keep only valid Strahler streams. Proper Strahler order is 1-based; any
        # zero/negative/blank order is either stale data or a malformed temporary
        # feature and must not leak into the final GeoPackage.
        displayed_cells = {
            int(c) for c in displayed_cells
            if int(stream_order.get(int(c), 0)) >= 1
        }
        if not displayed_cells:
            raise HydrologyBuildError("The temporary flow-path layer contains no Strahler order 1 or higher paths to export.")

        layer = QgsVectorLayer(
            f"LineString?crs={self._layer_crs_uri()}",
            "DDM HydroLogic flow paths - dissolved Strahler reaches export",
            "memory",
        )
        provider = layer.dataProvider()
        fields = QgsFields()
        for name, variant in (
            ("reach_id", QVariant.LongLong),
            ("start_cell", QVariant.LongLong),
            ("end_cell", QVariant.LongLong),
            ("cell_count", QVariant.LongLong),
            ("from_acc", QVariant.LongLong),
            ("to_acc", QVariant.LongLong),
            ("strahler", QVariant.Int),
            ("length_m", QVariant.Double),
        ):
            field = QgsField(name, variant)
            if name == "length_m":
                field.setLength(20)
                field.setPrecision(2)
            fields.append(field)
        provider.addAttributes(fields)
        layer.updateFields()

        displayed_upstream = defaultdict(list)
        for cell_id in displayed_cells:
            down = int(self.downstream[int(cell_id)])
            if down in displayed_cells:
                displayed_upstream[int(down)].append(int(cell_id))

        def same_order_upstream(cell_id):
            """Upstream displayed cells that have the same Strahler order."""
            order = int(stream_order.get(int(cell_id), 0))
            return [
                int(u) for u in displayed_upstream.get(int(cell_id), [])
                if int(stream_order.get(int(u), 0)) == order
            ]

        def can_merge_downstream(cell_id, next_cell):
            """Return True if adjacent segments belong to the same Strahler reach.

            This intentionally uses same-order topology, not total tributary
            count. A lower-order tributary entering a higher-order stem should not
            fragment the higher-order reach, because the Strahler order field is
            unchanged. The merge stops when the downstream cell is not displayed,
            the order changes, or more than one same-order candidate could feed
            the downstream segment.
            """
            cell_id = int(cell_id)
            next_cell = int(next_cell)
            if next_cell not in displayed_cells:
                return False
            order = int(stream_order.get(cell_id, 0))
            next_order = int(stream_order.get(next_cell, 0))
            if order < 1 or next_order < 1 or order != next_order:
                return False
            same_order_parents = same_order_upstream(next_cell)
            return len(same_order_parents) == 1 and int(same_order_parents[0]) == cell_id

        def has_merge_parent(cell_id):
            same_order_parents = same_order_upstream(int(cell_id))
            if len(same_order_parents) != 1:
                return False
            return can_merge_downstream(int(same_order_parents[0]), int(cell_id))

        starts = [int(c) for c in displayed_cells if not has_merge_parent(int(c))]
        starts.sort(key=lambda c: (int(stream_order.get(int(c), 1)), int(self.accumulation[int(c)]), int(c)))

        visited = set()
        features = []

        def build_reach(start_cell):
            cells = []
            cur = int(start_cell)
            while cur in displayed_cells and cur not in visited:
                self._check_cancelled()
                cells.append(cur)
                visited.add(cur)
                nxt = int(self.downstream[int(cur)])
                if not can_merge_downstream(cur, nxt) or nxt in visited:
                    break
                cur = int(nxt)
            return cells

        def add_reach(cells, reach_id):
            if not cells:
                return False
            points = [self.cell_center(c) for c in cells]
            last_down = int(self.downstream[int(cells[-1])])
            if last_down >= 0:
                points.append(self.cell_center(last_down))
            if len(points) < 2:
                return False
            geom = QgsGeometry.fromPolylineXY(points)
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                int(reach_id),
                int(cells[0]),
                int(cells[-1]),
                int(len(cells)),
                int(self.accumulation[int(cells[0])]),
                int(self.accumulation[int(cells[-1])]),
                int(stream_order.get(int(cells[-1]), 1)),
                round(float(geom.length()), 2),
            ])
            features.append(feat)
            return True

        reach_id = 1
        for start in starts:
            if int(start) in visited:
                continue
            if add_reach(build_reach(start), reach_id):
                reach_id += 1

        # Defensive fallback: malformed graph or edited temporary layer. Export any
        # unvisited segment as its own reach rather than silently dropping it.
        for cell_id in sorted(displayed_cells, key=lambda c: (int(stream_order.get(int(c), 1)), int(self.accumulation[int(c)]), int(c))):
            if int(cell_id) in visited:
                continue
            if add_reach(build_reach(cell_id), reach_id):
                reach_id += 1

        provider.addFeatures(features)
        layer.updateExtents()
        self._style_flow_layer(layer)
        return layer

    def _rebuild_spatial_index(self):
        self.spatial_index = QgsSpatialIndex()
        self.feature_to_cell = {}
        self.cell_to_feature = {}
        if self.flow_layer is None:
            return
        for feat in self.flow_layer.getFeatures():
            self.spatial_index.addFeature(feat)
            cell_id = int(feat["cell_id"])
            self.feature_to_cell[int(feat.id())] = cell_id
            self.cell_to_feature[cell_id] = int(feat.id())

    def nearest_flow_cell(self, map_point, canvas_crs, max_distance_map_units=None):
        """Return the nearest flow-cell id to a clicked canvas point."""
        if self.flow_layer is None or self.spatial_index is None:
            return None

        point = self._transform_point(map_point, canvas_crs, self.flow_layer.crs())
        candidate_fids = self.spatial_index.nearestNeighbor(point, 12)
        if not candidate_fids:
            return None

        point_geom = QgsGeometry.fromPointXY(point)
        best = None
        best_dist = None
        for fid in candidate_fids:
            feat = next(self.flow_layer.getFeatures(QgsFeatureRequest().setFilterFid(int(fid))), None)
            if feat is None:
                continue
            dist = feat.geometry().distance(point_geom)
            if best is None or dist < best_dist:
                best = int(fid)
                best_dist = dist

        if best is None:
            return None
        if max_distance_map_units is not None and best_dist is not None and best_dist > max_distance_map_units:
            return None
        return self.feature_to_cell.get(best)

    @staticmethod
    def _transform_point(point, source_crs, dest_crs):
        if source_crs == dest_crs:
            return QgsPointXY(point)
        transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
        return QgsPointXY(transform.transform(point))

    def transform_polyline(self, points, source_crs, dest_crs):
        if source_crs == dest_crs:
            return [QgsPointXY(p) for p in points]
        transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
        return [QgsPointXY(transform.transform(p)) for p in points]

    # ------------------------------------------------------------------
    # Upstream tracing and catchment construction
    # ------------------------------------------------------------------
    def collect_upstream(self, outlet_cell_id, limit=None):
        """Return all upstream cells contributing to a nominated outlet cell."""
        outlet_cell_id = int(outlet_cell_id)
        result = []
        stack = [outlet_cell_id]
        seen = set()
        while stack:
            self._check_cancelled()
            cell_id = int(stack.pop())
            if cell_id in seen:
                continue
            seen.add(cell_id)
            result.append(cell_id)
            if limit is not None and len(result) > limit:
                break
            stack.extend(self.upstream.get(cell_id, []))
        return result

    def dissolve_cells_to_geometry(self, cell_ids, chunk_size=10000):
        """Return one dissolved polygon geometry for a collection of DEM cells.

        The returned geometry is kept as a single QgsGeometry object for canvas
        overlays. It is deliberately not written to a QgsVectorLayer, so the
        interactive selection stage does not clutter the Layers panel with
        temporary polygon layers.
        """
        unique_ids = sorted({int(c) for c in cell_ids})
        if not unique_ids:
            return QgsGeometry()

        partials = []
        for start in range(0, len(unique_ids), int(chunk_size)):
            self._check_cancelled()
            geoms = [self.cell_polygon(c) for c in unique_ids[start:start + int(chunk_size)]]
            if not geoms:
                continue
            if len(geoms) == 1:
                partials.append(geoms[0])
            else:
                partials.append(QgsGeometry.unaryUnion(geoms))

        if not partials:
            return QgsGeometry()
        if len(partials) == 1:
            return partials[0]
        return QgsGeometry.unaryUnion(partials)

    def dissolve_cells_fast(self, cell_ids):
        """Catchment-boundary polygon for the interactive overlay, the quick way.

        Unioning one square polygon per cell is slow for large catchments. This
        instead rasterises the selected cells into a small mask over just their
        bounding window and vectorises it with GDAL, which is typically one to two
        orders of magnitude faster. The boundary follows the same cell edges as
        the union, so the result is equivalent. If GDAL polygonisation is
        unavailable or fails for any reason, it falls back to the exact per-cell
        union in dissolve_cells_to_geometry.
        """
        try:
            ids = np.fromiter((int(c) for c in cell_ids), dtype=np.int64)
            if ids.size == 0:
                return QgsGeometry()
            if self.geotransform is None or not self.cols:
                return self.dissolve_cells_to_geometry(cell_ids)

            cols = int(self.cols)
            cell_rows = ids // cols
            cell_cols = ids % cols
            r0, r1 = int(cell_rows.min()), int(cell_rows.max())
            c0, c1 = int(cell_cols.min()), int(cell_cols.max())
            win_rows = r1 - r0 + 1
            win_cols = c1 - c0 + 1

            mask = np.zeros((win_rows, win_cols), dtype=np.uint8)
            mask[cell_rows - r0, cell_cols - c0] = 1

            gt = self.geotransform
            win_gt = (
                gt[0] + c0 * gt[1] + r0 * gt[2],
                gt[1], gt[2],
                gt[3] + c0 * gt[4] + r0 * gt[5],
                gt[4], gt[5],
            )

            mem_ds = gdal.GetDriverByName("MEM").Create("", win_cols, win_rows, 1, gdal.GDT_Byte)
            mem_ds.SetGeoTransform(win_gt)
            if self.projection:
                mem_ds.SetProjection(self.projection)
            band = mem_ds.GetRasterBand(1)
            band.WriteArray(mask)

            ogr_ds = ogr.GetDriverByName("Memory").CreateDataSource("ddm_dissolve")
            srs = None
            if self.projection:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(self.projection)
            ogr_layer = ogr_ds.CreateLayer("catchment", srs=srs, geom_type=ogr.wkbPolygon)

            # Using the band as its own mask means only value==1 pixels are
            # collected, so no background polygon is produced.
            gdal.Polygonize(band, band, ogr_layer, -1, [])

            geoms = []
            for feature in ogr_layer:
                ogr_geom = feature.GetGeometryRef()
                if ogr_geom is None:
                    continue
                qgis_geom = QgsGeometry.fromWkt(ogr_geom.ExportToWkt())
                if qgis_geom is not None and not qgis_geom.isNull() and not qgis_geom.isEmpty():
                    geoms.append(qgis_geom)

            if not geoms:
                return self.dissolve_cells_to_geometry(cell_ids)
            if len(geoms) == 1:
                return geoms[0]
            return QgsGeometry.unaryUnion(geoms)
        except Exception:
            # Any GDAL/binding issue degrades gracefully to the exact union.
            return self.dissolve_cells_to_geometry(cell_ids)

    def create_highlight_layer(self, upstream_cells):
        """Create or refresh a yellow line layer showing displayed upstream flow paths.

        The DEM graph may contain many more upstream cells than the temporary
        flow-path layer displays when **Display paths from accumulation** is
        greater than 1. Highlight only cells that actually exist as output
        flow-path features, otherwise the click tool draws hidden low-
        accumulation lines back into existence. Computers love undoing UI
        choices unless watched.
        """
        layer = QgsVectorLayer(
            f"LineString?crs={self._layer_crs_uri()}",
            "DDM HydroLogic upstream highlight - temporary",
            "memory",
        )
        provider = layer.dataProvider()
        fields = QgsFields()
        fields.append(QgsField("cell_id", QVariant.LongLong))
        fields.append(QgsField("acc_cells", QVariant.LongLong))
        fields.append(QgsField("strahler", QVariant.Int))
        provider.addAttributes(fields)
        layer.updateFields()

        features = []
        for cell_id in upstream_cells:
            self._check_cancelled()
            if int(cell_id) not in self.cell_to_feature:
                continue
            down_id = int(self.downstream[cell_id])
            if down_id < 0:
                continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY([self.cell_center(cell_id), self.cell_center(down_id)]))
            feat.setAttributes([
                int(cell_id),
                int(self.accumulation[cell_id]),
                int(getattr(self, "display_strahler_by_cell", {}).get(int(cell_id), 1)),
            ])
            features.append(feat)
        provider.addFeatures(features)
        layer.updateExtents()

        symbol = QgsLineSymbol.createSimple({"color": "255,230,0", "width": "0.75"})
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        self.highlight_layer = layer
        return layer

    def line_crossing_cells(self, line_geometry):
        """Return flow-cell ids whose vector segment intersects the supplied line."""
        if self.flow_layer is None or self.spatial_index is None:
            return []
        candidate_fids = self.spatial_index.intersects(line_geometry.boundingBox())
        outlet_cells = []
        seen = set()
        for fid in candidate_fids:
            feat = next(self.flow_layer.getFeatures(QgsFeatureRequest().setFilterFid(int(fid))), None)
            if feat is None:
                continue
            if not feat.geometry().intersects(line_geometry):
                continue
            cell_id = int(feat["cell_id"])
            if cell_id not in seen:
                seen.add(cell_id)
                outlet_cells.append(cell_id)
        outlet_cells.sort(key=lambda c: int(self.accumulation[c]), reverse=True)
        return outlet_cells

    def cells_for_area_m2(self, area_m2):
        """Convert a minimum area in square metres to a minimum DEM-cell count."""
        try:
            area_m2 = float(area_m2)
        except (TypeError, ValueError):
            area_m2 = 0.0
        if area_m2 <= 0 or self.cell_area <= 0:
            return 1
        return max(1, int(math.ceil(area_m2 / self.cell_area)))

    def build_subcatchment_assignments(self, outlet_cells, min_cells=1):
        """Build subcatchment assignments for the current outlet boundary."""
        return self.build_area_threshold_subcatchments(
            min_cells=min_cells,
            boundary_outlet_cells=outlet_cells,
            include_residual=True,
        )

    def _domain_cells_for_subcatchments(self, boundary_outlet_cells=None):
        """Return the DEM cells to subdivide into subcatchments.

        If the user drew an outlet/crossing line, only cells upstream of those
        crossings are processed. If no line was drawn, the whole valid DEM graph
        is processed. This keeps the drawn line useful without making it a
        compulsory hoop, because software already has enough hoops.
        """
        if boundary_outlet_cells:
            domain = set()
            for outlet in boundary_outlet_cells:
                self._check_cancelled()
                domain.update(self.collect_upstream(int(outlet)))
            return domain
        return set(int(c) for c in self.valid_ids)

    def normalize_overlapping_cell_groups(self, groups):
        """Merge nested/overlapping catchment cell groups into parent groups.

        Upstream catchments from a D8 tree should be either disjoint or nested.
        If a user selects both a child and its downstream parent, drawing both
        creates overlapping catchment polygons. This normalisation keeps the
        larger/downstream parent group and merges any overlapping smaller child
        group into it. The result is a dictionary whose cell sets do not overlap.
        """
        if not groups:
            return {}

        prepared = []
        for key, cells in groups.items():
            self._check_cancelled()
            cell_set = {int(c) for c in cells or []}
            if not cell_set:
                continue
            key = int(key)
            prepared.append((key, cell_set))

        # Largest catchments are most likely to be downstream parents. When sizes
        # tie, prefer the higher accumulation outlet as the parent-ish feature.
        prepared.sort(
            key=lambda item: (
                len(item[1]),
                int(self.accumulation[item[0]]) if self.accumulation is not None and item[0] >= 0 else 0,
                item[0],
            ),
            reverse=True,
        )

        kept = []
        for key, cell_set in prepared:
            self._check_cancelled()
            merged = False
            for idx, (parent_key, parent_cells) in enumerate(kept):
                if not cell_set.isdisjoint(parent_cells):
                    # Merge the smaller/child catchment into the parent group.
                    parent_cells.update(cell_set)
                    # Keep the parent key stable, unless the incoming group is
                    # actually larger because of an odd stale input ordering.
                    kept[idx] = (parent_key, parent_cells)
                    merged = True
                    break
            if not merged:
                kept.append((key, set(cell_set)))

        # A defensive second pass handles rare partial-overlap chains after the
        # first merge pass. Hydrology graphs should not need it, but computers
        # love creating edge cases as a lifestyle choice.
        changed = True
        while changed:
            changed = False
            for idx in range(len(kept)):
                if changed:
                    break
                key_i, cells_i = kept[idx]
                for jdx in range(idx + 1, len(kept)):
                    key_j, cells_j = kept[jdx]
                    if cells_i.isdisjoint(cells_j):
                        continue
                    if len(cells_j) > len(cells_i):
                        cells_j.update(cells_i)
                        kept[jdx] = (key_j, cells_j)
                        kept.pop(idx)
                    else:
                        cells_i.update(cells_j)
                        kept[idx] = (key_i, cells_i)
                        kept.pop(jdx)
                    changed = True
                    break

        return {int(key): set(cells) for key, cells in kept}

    def _normalise_assignments_no_overlap(self, assignments):
        """British-spelled internal alias retained for readability in calls."""
        return {key: sorted(cells) for key, cells in self.normalize_overlapping_cell_groups(assignments).items()}

    def build_area_threshold_subcatchments(self, min_cells=1, boundary_outlet_cells=None, include_residual=True):
        """Subdivide the selected DEM area into all target-size subcatchments.

        The method walks cells from upstream to downstream. Unassigned residual
        area is accumulated along the D8 graph; once the residual area reaches
        ``min_cells`` the current cell becomes a subcatchment outlet and that
        residual contribution is closed off. Cells are then assigned to the
        first selected outlet they encounter downstream, yielding non-overlapping
        subcatchments rather than repeated full upstream basins.
        """
        min_cells = max(1, int(min_cells))
        domain_set = self._domain_cells_for_subcatchments(boundary_outlet_cells)
        if not domain_set:
            return {}

        # Upstream cells always have lower accumulation than their downstream
        # receiver in this D8 tree, so this is a deterministic upstream-to-
        # downstream traversal without building a separate topological index.
        domain_ids = list(domain_set)
        domain_ids.sort(key=lambda cid: (int(self.accumulation[int(cid)]), int(cid)))

        pending_counts = defaultdict(int)
        selected_outlets = []
        selected_set = set()

        total = max(1, len(domain_ids))
        for idx, cell_id in enumerate(domain_ids):
            self._check_cancelled()
            cell_id = int(cell_id)
            count = int(pending_counts.pop(cell_id, 0)) + 1
            down = int(self.downstream[cell_id]) if cell_id >= 0 else -1
            has_downstream_in_domain = down >= 0 and down in domain_set

            if count >= min_cells or (include_residual and not has_downstream_in_domain and count > 0):
                selected_outlets.append(cell_id)
                selected_set.add(cell_id)
            elif has_downstream_in_domain:
                pending_counts[down] += count

            if idx and idx % 50000 == 0:
                self._emit(60 + 10 * idx / total, "Choosing minimum-size subcatchment outlets")

        if not selected_outlets:
            return {}

        assignments = {int(outlet): [] for outlet in selected_outlets}
        cache = {}

        def assign_cell_to_selected_outlet(start_cell):
            start_cell = int(start_cell)
            if start_cell in cache:
                return cache[start_cell]
            path = []
            cur = start_cell
            while True:
                self._check_cancelled()
                if cur in cache:
                    outlet = cache[cur]
                    break
                if cur in selected_set:
                    outlet = cur
                    break
                path.append(cur)
                down = int(self.downstream[cur]) if cur >= 0 else -1
                if down < 0 or down not in domain_set:
                    outlet = None
                    break
                cur = down
            for item in path:
                cache[item] = outlet
            cache[start_cell] = outlet
            return outlet

        for idx, cell_id in enumerate(domain_ids):
            self._check_cancelled()
            outlet = assign_cell_to_selected_outlet(int(cell_id))
            if outlet is not None:
                assignments[int(outlet)].append(int(cell_id))
            if idx and idx % 50000 == 0:
                self._emit(70 + 10 * idx / total, "Assigning DEM cells to subcatchment outlets")

        # Drop any empty entries left by edge cases, then defensively normalise
        # overlaps. The area assignment algorithm is designed to be cell-
        # disjoint already; this extra pass protects stale/outlet-edge cases and
        # keeps final polygons from overlapping.
        assignments = {outlet: cells for outlet, cells in assignments.items() if cells}
        return self._normalise_assignments_no_overlap(assignments)

    def create_subcatchment_layer(self, assignments, min_cells=1, dissolve_limit=None):
        """Create a temporary polygon layer with one dissolved outline per subcatchment."""
        layer = QgsVectorLayer(
            f"MultiPolygon?crs={self._layer_crs_uri()}",
            "DDM HydroLogic subcatchments - dissolved outlines temporary",
            "memory",
        )
        provider = layer.dataProvider()
        fields = QgsFields()
        for name, variant in (
            ("outlet_id", QVariant.LongLong),
            ("cell_count", QVariant.LongLong),
            ("area_m2", QVariant.Double),
            ("area_ha", QVariant.Double),
            ("strahler", QVariant.Int),
        ):
            field = QgsField(name, variant)
            if name in ("area_m2", "area_ha"):
                field.setLength(20)
                field.setPrecision(2)
            fields.append(field)
        provider.addAttributes(fields)
        layer.updateFields()

        features = []
        for idx, (outlet_id, cells) in enumerate(assignments.items()):
            self._check_cancelled()
            if not cells:
                continue

            # Always dissolve DEM-cell polygons. Older builds fell back to
            # collectGeometry for large catchments, which produced the cell-grid
            # confetti the user quite reasonably objected to.
            geom = self.dissolve_cells_to_geometry(cells)
            if geom.isNull() or geom.isEmpty():
                continue
            if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
                # Buffer by zero is a common GEOS nudge for polygonal geometry
                # collections. If it still fails, skip the broken feature rather
                # than showing thousands of individual cells.
                fixed = geom.buffer(0, 1)
                if not fixed.isNull() and not fixed.isEmpty() and QgsWkbTypes.geometryType(fixed.wkbType()) == QgsWkbTypes.PolygonGeometry:
                    geom = fixed
                else:
                    continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            # Recompute from the dissolved output geometry, equivalent to
            # QGIS expression fields using $area and $area / 10000. This avoids
            # disagreements between DEM-cell-count area and the final polygon
            # geometry stored in the GeoPackage.
            area_m2 = round(float(geom.area()), 2)
            feat.setAttributes([
                int(outlet_id),
                int(len(cells)),
                area_m2,
                round(float(area_m2 / 10000.0), 2),
                int(getattr(self, "display_strahler_by_cell", {}).get(int(outlet_id), self.strahler[outlet_id])),
            ])
            features.append(feat)
            if idx and idx % 25 == 0:
                self._emit(80, "Dissolving subcatchments into outline polygons")

        provider.addFeatures(features)
        layer.updateExtents()
        symbol = QgsFillSymbol.createSimple({
            "color": "255,255,0,0",
            "outline_color": "255,230,0",
            "outline_width": "0.45",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        self.subcatchment_layer = layer
        return layer
