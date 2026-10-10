# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrologic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Interactive DEM-to-flow paths engine for DDM HydroLogic.

This module intentionally keeps the hydrology logic independent from the dock UI so
it can be tested easily inside QGIS. It builds a D8 graph directly from a DEM,
then exposes helpers for upstream tracing, outlet-line selection and vector export.
"""

from array import array
from collections import defaultdict
import heapq
from itertools import repeat
import math
import weakref

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

from .catchment_geometry import breakdown_order
from .compat import enum_member, log_ignored
from .graph_index import BLOCK, FlowGraphError, FlowIndex, nonzero32

# Band types a 32-bit float holds exactly; anything else is read as 64-bit.
_FLOAT32_TYPES = {gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16, gdal.GDT_Float32}
if hasattr(gdal, "GDT_Int8"):
    _FLOAT32_TYPES.add(gdal.GDT_Int8)

# Cells the priority flood takes off its queue between progress updates.
_FLOOD_STEP = 65536

# Widest NoData gap, in cells, that Fill narrow NoData gaps fills. Where two
# seams cross, the crossing only closes once the seams either side are filled,
# so the search runs again with filled cells counting as data, up to this many
# times. The window round a mask polygon is wide enough for every search.
GAP_WIDTH = 5
_GAP_PASSES = 3
_WINDOW_MARGIN = 2 + GAP_WIDTH * _GAP_PASSES

# Peak memory of Compute and the processing after it, measured on a 1 m DEM: a
# fixed part, bytes per cell of the processing window on top of the elevation
# itself, and bytes per valid cell.
_BASE_BYTES = 256 * 2 ** 20
_BYTES_PER_CELL = 32
_BYTES_PER_VALID_CELL = 8


class HydrologyBuildError(Exception):
    """Raised when the DEM cannot be converted into a usble flow graph."""


class HydrologyCancelled(Exception):
    """Raised when the user aborts a running hydrology operation."""


def _vector_memory_driver():
    """In-memory vector driver: MEM from GDAL 3.11 on, Memory before that."""
    return ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")


def _cell_array(cells):
    """Cell ids as a numpy array, from an array, a list or a set."""
    if isinstance(cells, np.ndarray):
        return cells
    return np.fromiter((int(c) for c in cells), dtype=np.int64)


def _elevation_keys(values):
    """Turns a float array into unsigned integers that sort the same way, in place.

    -0.0 is made 0.0 first, so the two stay equal as they are when compared as floats.
    """
    np.add(values, 0, out=values)
    if values.dtype == np.float32:
        bits, top = values.view(np.uint32), np.uint32(1 << 31)
    else:
        bits, top = values.view(np.uint64), np.uint64(1 << 63)
    for start in range(0, bits.size, BLOCK):
        block = bits[start:start + BLOCK]
        negative = (block & top) != 0
        np.invert(block, out=block, where=negative)
        np.bitwise_or(block, top, out=block, where=~negative)
    return bits


def _float_values(bits):
    """Undoes _elevation_keys in place and returns the floats."""
    if bits.dtype == np.uint32:
        top, kind = np.uint32(1 << 31), np.float32
    else:
        top, kind = np.uint64(1 << 63), np.float64
    for start in range(0, bits.size, BLOCK):
        block = bits[start:start + BLOCK]
        positive = (block & top) != 0
        np.bitwise_and(block, ~top, out=block, where=positive)
        np.invert(block, out=block, where=~positive)
    return bits.view(kind)


def _round_up(value, figures=2):
    """A positive number rounded up to a whole number with at most ``figures`` significant figures."""
    if value <= 0:
        return 0
    scale = 10 ** max(0, int(math.floor(math.log10(value))) - figures + 1)
    return int(math.ceil(value / scale - 1e-9) * scale)


def _gap_runs(known, axis):
    """Unknown cells in a run of at most GAP_WIDTH unknown cells along ``axis``, with known cells at both ends."""
    length = known.shape[axis]
    before = np.zeros(known.shape, dtype=np.int8)
    after = np.zeros(known.shape, dtype=np.int8)
    for step in range(1, min(GAP_WIDTH, length - 1) + 1):
        head = (slice(None),) * axis + (slice(step, None),)
        tail = (slice(None),) * axis + (slice(None, length - step),)
        np.copyto(before[head], step, where=(before[head] == 0) & known[tail])
        np.copyto(after[tail], step, where=(after[tail] == 0) & known[head])
    return (before > 0) & (after > 0) & (before + after <= GAP_WIDTH + 1) & ~known


def _run_ends(known, rows, cols, d_row, d_col):
    """Steps from each cell to the nearest known cell in one direction, up to GAP_WIDTH; 0 if there is none."""
    found = np.zeros(rows.size, dtype=np.int8)
    height, width = known.shape
    for step in range(1, GAP_WIDTH + 1):
        r, c = rows + d_row * step, cols + d_col * step
        look = (found == 0) & (r >= 0) & (r < height) & (c >= 0) & (c < width)
        hit = np.zeros(rows.size, dtype=bool)
        hit[look] = known[r[look], c[look]]
        found[hit] = step
    return found


class _UpstreamCells:
    """``get(cell)`` gives the valid cells draining straight into a cell, lowest id first.

    Read from the flow index when asked, so no per-cell lists are kept.
    """

    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index

    def get(self, cell, default=None):
        if self.index is None:
            return [] if default is None else default
        return self.index.upstream_cells(int(cell))

    def __getitem__(self, cell):
        return self.get(cell)


class D8HydrologyEngine:
    """Build and query a D8 flow graph from a north-up raster DEM."""

    def __init__(self, dem_layer, progress_callback=None, cancel_callback=None, mask_layer=None, fill_gaps=False):
        self.dem_layer = dem_layer
        self.progress_callback = progress_callback or (lambda _pct, _msg: None)
        self.cancel_callback = cancel_callback or (lambda: False)
        # Boundary-aware hydrological connectivity is always used, with any optional
        # mask polygon rasterised into the valid-cell domain before routing.
        self.mask_layer = mask_layer
        self.mask_cell_count = 0
        self.mask_raster_cell_count = 0
        self.mask_nodata_cell_count = 0
        # NoData gaps up to GAP_WIDTH cells wide are filled before routing when set.
        self.fill_gaps = bool(fill_gaps)
        self.filled_cell_count = 0
        # The NoData the preflight checks report: 1 in a narrow gap, 2 elsewhere.
        self.nodata_kinds = None

        self.dataset_path = None
        self.geotransform = None
        self.projection = None
        self.raster_rows = 0
        self.raster_cols = 0
        # The grid is the processing window with a frame of invalid cells one cell
        # wide all round. Grid row 0 is raster row ``row_offset``, and a cell id
        # is ``row * cols + col`` on the grid. The layers carry cell numbers on the
        # whole raster instead, which stay the same whatever the window.
        self.rows = 0
        self.cols = 0
        self.row_offset = 0
        self.col_offset = 0
        self.cell_width = 0.0
        self.cell_height = 0.0
        self.cell_area = 0.0
        self.nodata = None
        self.elevation_bytes = 4
        self.dem = None
        self.valid = None
        self.valid_count = 0

        self.downstream = None
        self.accumulation = None
        self.strahler = None
        self.display_order = None
        self.filled_dem = None
        self.index = None
        self.metrics_cache = {}

        self.flow_layer = None
        self.highlight_layer = None
        self.subcatchment_layer = None
        self.spatial_index = None
        self.feature_to_cell = {}
        self.cell_to_feature = {}
        self.shown_cells = None

    @property
    def upstream(self):
        """Upstream neighbours of a cell on request: ``engine.upstream.get(cell, [])``."""
        return _UpstreamCells(self.index)

    # ------------------------------------------------------------------
    # DEM reading and graph construction
    # ------------------------------------------------------------------
    def build(self):
        """Read DEM data and build downstream, accumulation, and Strahler arrays."""
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
                raise HydrologyCancelled("Process aborted by user.")
        except HydrologyCancelled:
            raise
        except Exception:
            # Cancelation checks must never become the reason a hydrology build fails.
            log_ignored("hydrology_engine._check_cancelled")

    def _index_progress(self, fraction):
        """Progress while the flow index is built. Its later passes report None,
        which only checks for an abort: they also run outside any operation."""
        if fraction is None:
            self._check_cancelled()
        else:
            self._emit(56 + 26 * fraction, "Computing accumulation and Strahler order from connected outlets")

    def _read_dem(self, values=True, keep_nodata=False):
        """Reads the DEM over the processing window into the framed grid.

        With a mask polygon only the raster rows and columns around it are read,
        otherwise the whole raster. ``values`` False keeps the valid-cell mask
        only, which is all the preflight checks use, and ``keep_nodata`` marks the
        NoData cells they report. With ``fill_gaps`` set, narrow NoData gaps are
        filled before the mask polygon is applied.
        """
        self._emit(2, "Reading DEM")
        uri = self.dem_layer.dataProvider().dataSourceUri()
        self.dataset_path = uri.split("|", 1)[0]

        ds = gdal.Open(self.dataset_path)
        if ds is None:
            raise HydrologyBuildError(f"GDAL could not open DEM source: {self.dataset_path}")

        gt = ds.GetGeoTransform()
        if gt is None:
            raise HydrologyBuildError("The DEM does not exposes a GDAL geotransform.")
        if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
            raise HydrologyBuildError(
                "Rotated/skewed rasters are not supported by the interactive graph builder. "
                "Reproject/resample the DEM to a north-up grid first."
            )

        band = ds.GetRasterBand(1)
        if band is None:
            raise HydrologyBuildError("Could not read band 1 from the DEM.")

        self.geotransform = gt
        self.projection = ds.GetProjection()
        self.raster_rows, self.raster_cols = int(ds.RasterYSize), int(ds.RasterXSize)
        self.cell_width = abs(float(gt[1]))
        self.cell_height = abs(float(gt[5]))
        self.cell_area = self.cell_width * self.cell_height
        self.nodata = band.GetNoDataValue()

        mask_geom = None
        if self.mask_layer is not None:
            self._emit(4, "Applying analysis mask polygon to DEM")
            mask_geom = self._combined_mask_geometry_in_dem_crs()
            if mask_geom is None:
                self.mask_layer = None
        burned = self._set_window(ds.RasterYSize, ds.RasterXSize, mask_geom)

        kind = np.float32 if band.DataType in _FLOAT32_TYPES else np.float64
        self.elevation_bytes = np.dtype(kind).itemsize
        nodata = self._nodata_as(kind)
        rows, cols = self.rows - 2, self.cols - 2
        row0, col0 = self.row_offset + 1, self.col_offset + 1
        valid = np.zeros((self.rows, self.cols), dtype=bool)
        dem = np.zeros((self.rows, self.cols), dtype=kind) if values else None
        step = max(1, BLOCK // max(1, cols))
        for start in range(0, rows, step):
            self._emit(4 + 8 * start / rows, "Reading DEM")
            height = min(step, rows - start)
            arr = band.ReadAsArray(col0, row0 + start, cols, height)
            if arr is None:
                raise HydrologyBuildError("Could not read band 1 from the DEM.")
            block = arr.astype(kind, copy=False)
            ok = np.isfinite(block)
            if nodata is not None:
                ok &= block != nodata
            valid[1 + start:1 + start + height, 1:-1] = ok
            if dem is not None:
                dem[1 + start:1 + start + height, 1:-1] = block
        ds = None

        filling = self.fill_gaps and dem is not None
        if keep_nodata or filling:
            self._emit(12, "Looking for narrow NoData gaps")
            passes = self._narrow_gaps(valid)
            if keep_nodata:
                self.nodata_kinds = self._nodata_kinds(valid, passes, burned)
            if filling and passes.any():
                self._fill_gaps(dem, valid, passes)
                filled = passes > 0
                if burned is not None:
                    filled &= burned
                self.filled_cell_count = int(np.count_nonzero(filled))
            del passes

        if burned is not None:
            self.mask_raster_cell_count = int(np.count_nonzero(burned))
            self.mask_nodata_cell_count = int(np.count_nonzero(burned & ~valid))
            valid &= burned
            self.mask_cell_count = int(np.count_nonzero(valid))
        if burned is not None and self.mask_cell_count <= 0:
            raise HydrologyBuildError("The mask polygon does not overlaps with any valid DEM cells.")

        if not np.any(valid):
            raise HydrologyBuildError("The DEM contains no valid cells!")

        self.valid = valid
        self.valid_count = int(np.count_nonzero(valid))
        self.dem = dem
        suffix = f"; analysis mask cells: {self.mask_cell_count:,}" if self.mask_layer is not None else ""
        self._emit(12, f"DEM read: {cols} x {rows} cells{suffix}")

    def _nodata_as(self, kind):
        """The NoData value in the precision the DEM is held in, or None when no cell can equal it."""
        if self.nodata is None:
            return None
        try:
            value = float(self.nodata)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        if kind == np.float32 and abs(value) > float(np.finfo(np.float32).max):
            return None
        return kind(value)

    def _set_window(self, raster_rows, raster_cols, mask_geom):
        """Sets the grid to the raster cells the mask polygon touches, or to the whole raster.

        Returns the rasterised mask over the grid, or None without a mask.
        """
        full = (0, raster_rows, 0, raster_cols)
        window = full
        if mask_geom is not None:
            window = self._mask_window(raster_rows, raster_cols, mask_geom)
            if window is None:
                raise HydrologyBuildError("The mask polygon does not overlaps with any valid DEM cells.")
        while True:
            row0, row1, col0, col1 = window
            self.rows, self.cols = row1 - row0 + 2, col1 - col0 + 2
            self.row_offset, self.col_offset = row0 - 1, col0 - 1
            if self.rows * self.cols >= 2 ** 31:
                raise HydrologyBuildError(
                    f"The DEM area to process has {self.rows * self.cols:,} cells, more than the 2,147,483,647 "
                    "this plugin can index. Draw a mask polygon around the catchment to process a smaller area."
                )
            if mask_geom is None:
                return None
            burned = self._rasterize_mask_geometry(mask_geom)
            # The window is meant to hold every cell the polygon touches. Should one
            # sit on its edge where the raster carries on, read the whole raster.
            spills = ((row0 > 0 and burned[1].any()) or (row1 < raster_rows and burned[-2].any())
                      or (col0 > 0 and burned[:, 1].any()) or (col1 < raster_cols and burned[:, -2].any()))
            if not spills or window == full:
                return burned
            window = full

    def _mask_window(self, raster_rows, raster_cols, mask_geom, margin=_WINDOW_MARGIN):
        """Raster rows and columns ``(row0, row1, col0, col1)`` round the mask polygon,
        or None when it misses the raster.

        The margin leaves room for the narrow-gap search on either side of the
        polygon, so a gap inside it is found the same way the whole DEM would find it.
        """
        gt = self.geotransform
        box = mask_geom.boundingBox()
        cols = sorted(((box.xMinimum() - gt[0]) / gt[1], (box.xMaximum() - gt[0]) / gt[1]))
        rows = sorted(((box.yMinimum() - gt[3]) / gt[5], (box.yMaximum() - gt[3]) / gt[5]))
        if not all(math.isfinite(v) for v in cols + rows):
            return 0, raster_rows, 0, raster_cols
        col0 = max(0, int(math.floor(cols[0])) - margin)
        col1 = min(raster_cols, int(math.ceil(cols[1])) + margin)
        row0 = max(0, int(math.floor(rows[0])) - margin)
        row1 = min(raster_rows, int(math.ceil(rows[1])) + margin)
        if col1 <= col0 or row1 <= row0:
            return None
        return row0, row1, col0, col1

    def raster_cells(self, cells):
        """Cell numbers on the whole DEM, counted row by row from its top-left cell, for grid cell ids."""
        cells = np.asarray(cells, dtype=np.int64)
        return (cells // self.cols + self.row_offset) * self.raster_cols + cells % self.cols + self.col_offset

    def raster_cell(self, cell):
        return int(self.raster_cells(int(cell)))

    def grid_cells(self, numbers):
        """Grid cell ids for cell numbers on the whole DEM; -1 for any outside the processing window."""
        numbers = np.asarray(numbers, dtype=np.int64)
        row = numbers // max(1, self.raster_cols) - self.row_offset
        col = numbers % max(1, self.raster_cols) - self.col_offset
        inside = (numbers >= 0) & (row >= 0) & (row < self.rows) & (col >= 0) & (col < self.cols)
        return np.where(inside, row * self.cols + col, -1)

    def grid_cell(self, number):
        """Grid cell id for a cell number on the whole DEM, as the layers carry it; -1 outside the window."""
        return int(self.grid_cells(int(number)))

    def _grid_transform(self, row, col):
        """Geotransform of a block of the grid whose top-left cell is grid (row, col)."""
        gt = self.geotransform
        r, c = row + self.row_offset, col + self.col_offset
        return (gt[0] + c * gt[1] + r * gt[2], gt[1], gt[2], gt[3] + c * gt[4] + r * gt[5], gt[4], gt[5])

    def _combined_mask_geometry_in_dem_crs(self):
        """Returns the optional analysis mask poly inheriting the DEM CRS."""
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
            raise HydrologyBuildError(f"Could not read the mask polygon: {exc}") from exc

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
            log_ignored("hydrology_engine._combined_mask_geometry_in_dem_crs")

        try:
            source_crs = layer.crs()
            dest_crs = self.dem_layer.crs()
            if source_crs != dest_crs:
                transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
                mask_geom.transform(transform)
        except Exception as exc:
            raise HydrologyBuildError(f"Could not reproject the mask polygon to the DEM CRS: {exc}") from exc
        return mask_geom

    def _rasterize_mask_geometry(self, mask_geom):
        """Rasterises the mask poly onto the grid using GDAL MEM; the frame stays clear."""
        try:
            mask_ds = gdal.GetDriverByName("MEM").Create("", int(self.cols), int(self.rows), 1, gdal.GDT_Byte)
            mask_ds.SetGeoTransform(self._grid_transform(0, 0))
            if self.projection:
                mask_ds.SetProjection(self.projection)
            mask_band = mask_ds.GetRasterBand(1)
            mask_band.Fill(0)

            vector_ds = _vector_memory_driver().CreateDataSource("ddm_mask")
            srs = None
            if self.projection:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(self.projection)
            vector_layer = vector_ds.CreateLayer("mask", srs=srs, geom_type=ogr.wkbMultiPolygon)
            feature_defn = vector_layer.GetLayerDefn()
            feature = ogr.Feature(feature_defn)
            ogr_geom = ogr.CreateGeometryFromWkt(mask_geom.asWkt())
            if ogr_geom is None:
                raise HydrologyBuildError("Could not convert the mask polygon to OGR WKT.")
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
            burned = mask_band.ReadAsArray().view(np.bool_)
            burned[0, :] = burned[-1, :] = False
            burned[:, 0] = burned[:, -1] = False
            return burned
        except HydrologyBuildError:
            raise
        except MemoryError:
            raise
        except Exception as exc:
            raise HydrologyBuildError(f"Could not rasterise the mask polygon: {exc}") from exc

    def inspect_nodata(self):
        """Preflight summary of the NoData Compute will meet, before the graph is built.

        Gives the cells the mask polygon covers and how many of them hold data, and
        the NoData cells to report: with a mask polygon every one inside it,
        without one those inside the data, narrow gaps and cells with data on all
        four sides. ``gap_cells`` are the ones Fill narrow NoData gaps fills.
        """
        self._read_dem(values=False, keep_nodata=True)
        kinds = self.nodata_kinds
        gap_cells = int(np.count_nonzero(kinds == 1))
        has_mask = self.mask_layer is not None
        return {
            "has_mask": has_mask,
            "mask_cells": int(self.mask_raster_cell_count),
            "valid_cells": int(self.mask_cell_count if has_mask else self.valid_count),
            "gap_cells": gap_cells,
            "other_cells": int(np.count_nonzero(kinds)) - gap_cells,
        }

    def _narrow_gaps(self, valid):
        """The search pass in which each NoData cell in a narrow gap is found, 0 for every other cell.

        A narrow gap is a run of at most GAP_WIDTH NoData cells along a row or a
        column with data at both ends, such as a seam between DEM tiles. Where two
        seams cross, the crossing is only such a run once the seams either side
        are filled, so the search is repeated with the cells found counting as
        data. The NoData round the outside of the data never has data at both
        ends of a short run, so it is never a gap.
        """
        rows, cols = valid.shape
        passes = np.zeros((rows, cols), dtype=np.uint8)
        if valid[1:-1, 1:-1].all():
            return passes
        known = valid.copy()
        step = max(1, BLOCK // max(1, cols))
        for number in range(1, _GAP_PASSES + 1):
            found = False
            for start in range(1, rows - 1, step):
                self._check_cancelled()
                stop = min(rows - 1, start + step)
                if known[start:stop, 1:-1].all():
                    continue
                low, high = max(0, start - GAP_WIDTH), min(rows, stop + GAP_WIDTH)
                slab = known[low:high]
                core = slice(start - low, stop - low)
                closed = _gap_runs(slab[core], 1) | _gap_runs(slab, 0)[core]
                if closed.any():
                    passes[start:stop][closed] = number
                    found = True
            if not found:
                break
            known |= passes == number
        return passes

    def _nodata_kinds(self, valid, passes, burned):
        """Marks the NoData the preflight checks report: 1 in a narrow gap, 2 elsewhere.

        With a mask polygon that is every NoData cell inside it. Without one it is
        the narrow gaps and the cells with data somewhere to the left, right,
        above and below them, not the NoData round the outside of the data.
        """
        gaps = passes > 0
        if burned is not None:
            other = burned & ~valid & ~gaps
            gaps &= burned
        else:
            other = self._enclosed_nodata(valid)
            other &= ~gaps
        kinds = gaps.view(np.uint8)
        kinds[other] = 2
        return kinds

    def _enclosed_nodata(self, valid):
        """NoData cells with data somewhere to the left, right, above and below them in their row and column."""
        rows, cols = valid.shape
        step = max(1, BLOCK // max(1, cols))
        enclosed = np.empty((rows, cols), dtype=bool)
        seen = np.zeros(cols, dtype=bool)
        for start in range(0, rows, step):
            block = np.logical_or.accumulate(valid[start:start + step], axis=0)
            block |= seen
            enclosed[start:start + step] = block
            seen = block[-1].copy()
        seen[:] = False
        for start in reversed(range(0, rows, step)):
            self._check_cancelled()
            block = valid[start:start + step]
            below = np.logical_or.accumulate(block[::-1], axis=0)[::-1]
            below |= seen
            seen = below[0].copy()
            left = np.logical_or.accumulate(block, axis=1)
            right = np.logical_or.accumulate(block[:, ::-1], axis=1)[:, ::-1]
            enclosed[start:start + step] &= below & left & right & ~block
        return enclosed

    @staticmethod
    def _fill_gaps(dem, valid, passes):
        """Fills the narrow gaps in place, in the order the search found them, and marks them valid.

        Each cell takes the straight line between the data at either end of its
        run along the row, or the column, or the mean of the two where both runs
        are short enough.
        """
        for number in range(1, int(passes.max()) + 1):
            rows, cols = np.nonzero(passes == number)
            if not rows.size:
                continue
            total = np.zeros(rows.size)
            count = np.zeros(rows.size)
            for d_row, d_col in ((0, 1), (1, 0)):
                before = _run_ends(valid, rows, cols, -d_row, -d_col)
                after = _run_ends(valid, rows, cols, d_row, d_col)
                short = (before > 0) & (after > 0) & (before + after <= GAP_WIDTH + 1)
                if not short.any():
                    continue
                b = before[short].astype(np.int64)
                a = after[short].astype(np.int64)
                r, c = rows[short], cols[short]
                start = dem[r - b * d_row, c - b * d_col].astype(np.float64)
                end = dem[r + a * d_row, c + a * d_col].astype(np.float64)
                total[short] += start + (end - start) * b / (b + a)
                count[short] += 1
            done = count > 0
            dem[rows[done], cols[done]] = total[done] / count[done]
            valid[rows[done], cols[done]] = True

    def nodata_geometries(self):
        """Outlines of the NoData cells the last preflight read marked, as ``{kind: QgsGeometry}``."""
        kinds = self.nodata_kinds
        if kinds is None or not kinds.any():
            return {}
        rows = np.flatnonzero(kinds.any(axis=1))
        cols = np.flatnonzero(kinds.any(axis=0))
        r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
        parts = defaultdict(list)
        for value, geom in self._polygonize(np.ascontiguousarray(kinds[r0:r1, c0:c1]), r0, c0):
            parts[value].append(geom)
        return {value: geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
                for value, geoms in sorted(parts.items())}

    def memory_estimate(self):
        """Approximate peak bytes Compute and processing need, for the window and valid cells of the last read."""
        cells = self.rows * self.cols
        return (_BASE_BYTES + cells * (_BYTES_PER_CELL + self.elevation_bytes)
                + self.valid_count * _BYTES_PER_VALID_CELL)

    def _build_downstream_graph(self):
        """Builds a hydrologically connected D8 receiver graph.

        Uses a boundary-aware priority-flood pass. Valid cells adjacent to NoData
        are considered as outlets as well as valid cells on the outer raster edge.
        A clipped DEM with a NoData border therefore drains to its edges rather
        than to one global-low cell.
        """
        self._emit(18, "Building hydrologically connected D8 flow graph")
        outlet_count = self._priority_flood_connectivity_graph(
            progress_start=18,
            progress_span=34,
            message="Connecting D8 flow paths from low boundary cells upstream",
        )
        self._emit(52, f"D8 connectivity complete. Boundary outlet cells: {outlet_count:,}")

    def _valid_boundary_mask(self):
        """Returns valid DEM cells that can behave as outlets.

        Didnt realise that a clipped DEM often has a NoData frame around real terrain pixels. Boundary-aware
        routing treats valid cells beside NoData as possible outlets rather than
        forcing the graph toward a single interior low point.

        Core logic for outlets - Boundary cells are any valid cells on the raster edge OR any
        valid cells touching NoData in the D8 neighbourhood.
        """
        valid = self.valid
        rows, cols = self.rows, self.cols
        inner = valid[1:-1, 1:-1]
        # The invalid frame round the grid puts a cell on the raster edge beside an
        # invalid cell, so one test covers both cases.
        surrounded = inner.copy()
        for dr in (0, 1, 2):
            for dc in (0, 1, 2):
                if dr != 1 or dc != 1:
                    surrounded &= valid[dr:dr + rows - 2, dc:dc + cols - 2]
        boundary = np.zeros_like(valid)
        boundary[1:-1, 1:-1] = inner & ~surrounded
        return boundary

    def _priority_flood_connectivity_graph(self, progress_start=18, progress_span=34, message="Connecting D8 flow paths from low cells upstream"):
        """Builds a boundary-aware priority-flood receiver graph.

        Called by _build_downstream_graph. It starts from every valid
        raster/mask boundary cell, not merely from the outside row/column of the
        raster. That distinction matters for clipped DEM's with a NoData frame all around its bounds.

        Cells come off the queue lowest first, equal levels in the order they were
        queued, and each claims its unclaimed valid neighbours as upstream cells,
        raising any that sit lower to its own level. A level is held as an
        order-preserving integer so a heap entry is one integer, and a neighbour at
        or below the current level goes to a plain first-in first-out queue, which
        hands cells out in exactly the order the heap would. The DEM array becomes
        the pit-filled surface in place. Returns the number of outlet cells.
        """
        valid = self.valid.reshape(-1)
        cols = self.cols
        keys = _elevation_keys(self.dem.reshape(-1))
        downstream = np.full(valid.size, -1, dtype=np.int32)
        # 1 where a cell is not valid or has already been claimed.
        blocked = np.logical_not(valid).view(np.uint8)
        claimed, level_of, receiver = memoryview(blocked), memoryview(keys), memoryview(downstream)
        offsets = (-cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1)
        heappop, heappush = heapq.heappop, heapq.heappush
        low_bits, step = 0xFFFFFFFF, 1 << 32

        # A heap entry is level << 64 | queue order << 32 | cell, so entries sort
        # by level and then by the order they were queued.
        heap = []
        tag = 0
        for cell in nonzero32(self._valid_boundary_mask().reshape(-1)).tolist():
            claimed[cell] = 1
            heap.append((level_of[cell] << 64) | tag | cell)
            tag += step
        heapq.heapify(heap)
        outlet_count = len(heap)

        queue = array("i")
        pending = 0
        level = -1
        ceiling = 0
        done = 0
        total = max(1, self.valid_count)
        while True:
            exhausted = False
            for _ in repeat(None, _FLOOD_STEP):
                if heap and heap[0] < ceiling:
                    cell = heappop(heap) & low_bits
                elif pending < len(queue):
                    cell = queue[pending]
                    pending += 1
                elif heap:
                    entry = heappop(heap)
                    level = entry >> 64
                    ceiling = (level + 1) << 64
                    cell = entry & low_bits
                    if pending:
                        queue = array("i")
                        pending = 0
                else:
                    exhausted = True
                    break
                for offset in offsets:
                    other = cell + offset
                    if claimed[other]:
                        continue
                    claimed[other] = 1
                    receiver[other] = cell
                    key = level_of[other]
                    if key <= level:
                        level_of[other] = level
                        queue.append(other)
                    else:
                        heappush(heap, (key << 64) | tag | other)
                        tag += step
            if not exhausted:
                done += _FLOOD_STEP
                self._emit(progress_start + progress_span * min(1.0, done / total), message)
                continue
            left = np.flatnonzero(blocked == 0)
            if not left.size:
                break
            # Valid cells no boundary cell reaches, which the invalid frame round the
            # grid rules out: the lowest of them becomes an outlet, lowest id on ties.
            cell = int(left[np.argmin(keys[left])])
            claimed[cell] = 1
            heappush(heap, (level_of[cell] << 64) | tag | cell)
            tag += step
            outlet_count += 1
            level, ceiling = -1, 0

        filled = _float_values(keys)
        np.copyto(filled, np.nan, where=~valid)
        self.filled_dem = self.dem
        self.dem = None
        self.downstream = downstream
        return outlet_count

    def _build_upstream_accumulation_and_strahler(self):
        """Computes accumulation and Strahler order over the connected D8 graph.

        Accumulation is the number of cells draining through a cell, itself
        included, counted from the outlets the connectivity pass produced. The
        Strahler order here is per cell: a source cell is 1, and two or more
        upstream cells sharing the top order raise it by one. Both come from the
        array index built over the graph.
        """
        self._emit(56, "Computing accumulation and Strahler order from connected outlets")
        owner = weakref.ref(self)

        def progress(fraction):
            engine = owner()
            if engine is not None:
                engine._index_progress(fraction)

        try:
            index = FlowIndex(self.downstream, self.valid, self.cols, progress=progress)
        except FlowGraphError as exc:
            raise HydrologyBuildError(f"The D8 flow graph could not be indexed ({exc}). Press 4. Compute again.") from exc
        if not index.is_forest:
            raise HydrologyBuildError("The D8 flow graph contains a loop and cannot be used. Press 4. Compute again.")
        self.index = index
        self.accumulation = index.accumulation()
        self.strahler = index.strahler()
        self._emit(82, "Accumulation and Strahler order complete")

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def cell_center(self, cell_id):
        row = int(cell_id) // self.cols + self.row_offset
        col = int(cell_id) % self.cols + self.col_offset
        gt = self.geotransform
        x = gt[0] + (col + 0.5) * gt[1]
        y = gt[3] + (row + 0.5) * gt[5]
        return QgsPointXY(float(x), float(y))

    def cell_polygon(self, cell_id):
        row = int(cell_id) // self.cols + self.row_offset
        col = int(cell_id) % self.cols + self.col_offset
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
        """Returns the valid DEM cell id containing a map/canvas point.

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
            col = int(math.floor((float(point.x()) - float(gt[0])) / float(gt[1]))) - self.col_offset
            row = int(math.floor((float(point.y()) - float(gt[3])) / float(gt[5]))) - self.row_offset
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
        # Fallback. Most project DEM's should have an authid, but QGIS memory layers
        # can still accept a WKT-style CRS URI in current versions.
        return crs.toWkt()

    # ------------------------------------------------------------------
    # Vector layer creation and styling
    # ------------------------------------------------------------------
    def _compute_stream_strahler_orders(self, displayed_cells):
        """Computes classic 1-based Strahler order on the extracted stream network.

        The DEM graph contains every valid raster cell. Strahler stream order,
        however, is defined on the extracted stream/channel network. Therefore
        this method works only on cells that will be displayed/exported as flow
        path features after the user's accumulation threshold is applied.

        Disciplined:
        - channel/source links with no upstream channel link are Strahler 1;
        - if two or more upstream channel links of the maximum order meet, the
          downstream channel order increases by one;
        - if a lower-order link joins a higher-order link, the downstream order
          stays at the higher order.

        Returns the order of every grid cell, 0 off the network, and keeps it as
        ``display_order``.
        """
        shown = np.zeros(self.rows * self.cols, dtype=bool)
        cells = _cell_array(displayed_cells)
        if cells.size:
            shown[cells] = True
        self.display_order = self.index.stream_order(shown)
        return self.display_order

    def flow_cells(self, min_accumulation=1):
        """Cells drawn as flow-path segments at a threshold, ascending.

        A segment runs from a cell to its receiver, so a cell without one is not drawn.
        """
        valid = self.valid.reshape(-1)
        threshold = int(min_accumulation)
        parts = []
        for start in range(0, valid.size, BLOCK):
            stop = min(valid.size, start + BLOCK)
            keep = valid[start:stop] & (self.downstream[start:stop] >= 0) & (self.accumulation[start:stop] >= threshold)
            found = np.flatnonzero(keep)
            if found.size:
                parts.append((found + start).astype(np.int32))
        if not parts:
            return np.empty(0, dtype=np.int32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def flow_feature_count(self, min_accumulation=1):
        """Number of flow-path segments a threshold would draw."""
        valid = self.valid.reshape(-1)
        threshold = int(min_accumulation)
        count = 0
        for start in range(0, valid.size, BLOCK):
            stop = min(valid.size, start + BLOCK)
            keep = valid[start:stop] & (self.downstream[start:stop] >= 0) & (self.accumulation[start:stop] >= threshold)
            count += int(np.count_nonzero(keep))
        return count

    def area_for_segment_count(self, target):
        """Smallest display area, rounded up to two significant figures, that draws at most ``target`` segments.

        Returns the area in square metres and the number of segments it draws.
        """
        valid = self.valid.reshape(-1)
        keep = int(target) + 1
        top = np.empty(0, dtype=self.accumulation.dtype)
        floor = 0
        for start in range(0, valid.size, BLOCK):
            stop = min(valid.size, start + BLOCK)
            acc = self.accumulation[start:stop]
            values = acc[valid[start:stop] & (self.downstream[start:stop] >= 0) & (acc > floor)]
            if not values.size:
                continue
            top = np.concatenate((top, values))
            if top.size > keep:
                top = np.partition(top, top.size - keep)[top.size - keep:]
                floor = int(top.min())
        # A segment is drawn from an accumulation of min_cells up, so one more than
        # the accumulation of the (target + 1)th largest leaves target at most.
        min_cells = int(top.min()) + 1 if top.size >= keep else 1
        area = _round_up(min_cells * self.cell_area)
        return area, self.flow_feature_count(self.cells_for_area_m2(area))

    def create_flow_layer(self, min_accumulation=1):
        """Creates a temporary line layer containing cell-to-cell flow segments."""
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

        candidates = self.flow_cells(min_accumulation)
        stream_strahler = self._compute_stream_strahler_orders(candidates)
        accumulation = self.accumulation[candidates]
        orders = stream_strahler[candidates]
        sort = np.lexsort((candidates, accumulation, orders))
        candidates, accumulation, orders = candidates[sort], accumulation[sort], orders[sort]
        receivers = self.downstream[candidates]
        fields = layer.fields()
        features = []
        chunk_size = 25000
        total = max(1, candidates.size)
        raster_cols = self.raster_cols

        rows = zip(candidates.tolist(), receivers.tolist(), self.raster_cells(candidates).tolist(),
                   self.raster_cells(receivers).tolist(), accumulation.tolist(), orders.tolist())
        for idx, (cell_id, down_id, number, down_number, acc_cells, order) in enumerate(rows):
            if not idx & 4095:
                self._check_cancelled()
            geom = QgsGeometry.fromPolylineXY([self.cell_center(cell_id), self.cell_center(down_id)])
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes([
                number,
                number // raster_cols,
                number % raster_cols,
                down_number,
                acc_cells,
                order,
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
        """Styles flow paths in blues, with higher Strahler order darker/thicker."""
        orders = set()
        try:
            for feat in layer.getFeatures():
                try:
                    order = int(feat["strahler"])
                    if order >= 1:
                        orders.add(order)
                except Exception:
                    log_ignored("hydrology_engine._style_flow_layer")
                    continue
        except Exception:
            # Fallback for partially initialised layers.
            shown = self.display_order
            orders = set() if shown is None else {int(v) for v in np.unique(shown) if int(v) >= 1}

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
        """Creates an export-only flow layer merged into same-order Strahler reaches.

        The temporary display layer keeps one feature per DEM-cell segment for
        precise clicking. The final GeoPackage is cleaner: consecutive displayed
        segments are dissolved while they share the same Strahler order. Cells with
        Strahler order less than 1 are excluded to reduce noise and computational impacts.
        A lower-order tributary joining a higher-order stem does not split the
        higher-order stem, because the Strahler hierarchy has not changed.
        Reaches are split where the same-order sequence stops, where the downstream order changes,
        or where same-order topology would be ambiguous.
        """
        self._check_cancelled()
        if self.flow_layer is None:
            raise HydrologyBuildError("No temporary flow-path layer is available for export.")

        numbers = []
        for feat in self.flow_layer.getFeatures():
            self._check_cancelled()
            try:
                number = int(feat["cell_id"])
                # A feature whose order does not read as a number is left out.
                int(feat["strahler"])
            except Exception:
                log_ignored("hydrology_engine.create_reach_flow_layer")
                continue
            numbers.append(number)
        found = self.grid_cells(numbers)
        displayed_cells = set(found[found >= 0].tolist())
        if not displayed_cells:
            raise HydrologyBuildError("The temporary flow-path layer contains no displayed flow paths to export.")

        # Recalculates from the displayed network as the source of truth in case a
        # stale temporary layer was created before the corrected Strahler logic.
        stream_order = self._compute_stream_strahler_orders(displayed_cells)

        # Keeps only valid Strahler streams. Proper Strahler order is 1-based; any
        # zero/negative/blank order is either stale data or a malformed temporary
        # feature and must not leak into the final GeoPackage.
        displayed_cells = {c for c in displayed_cells if int(stream_order[c]) >= 1}
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
            down = int(self.downstream[cell_id])
            if down in displayed_cells:
                displayed_upstream[down].append(cell_id)

        def order_of(cell_id):
            return int(stream_order[cell_id])

        def same_order_upstream(cell_id):
            """Upstream displayed cells that have the same Strahler order."""
            order = order_of(cell_id)
            return [u for u in displayed_upstream.get(cell_id, []) if order_of(u) == order]

        def can_merge_downstream(cell_id, next_cell):
            """Returns True if adjacent segments belong to the same Strahler reach.

            This intentionally uses same-order topology, not total tributary
            count. A lower-order tributary entering a higher-order stem should not
            fragment the higher-order reach, because the Strahler order field is
            unchanged. The merge stops when the downstream cell is not displayed,
            the order changes, or more than one same-order candidate could feed
            the downstream segment.
            """
            if next_cell not in displayed_cells:
                return False
            order = order_of(cell_id)
            next_order = order_of(next_cell)
            if order < 1 or next_order < 1 or order != next_order:
                return False
            same_order_parents = same_order_upstream(next_cell)
            return len(same_order_parents) == 1 and same_order_parents[0] == cell_id

        def has_merge_parent(cell_id):
            same_order_parents = same_order_upstream(cell_id)
            if len(same_order_parents) != 1:
                return False
            return can_merge_downstream(same_order_parents[0], cell_id)

        def sort_key(cell_id):
            return (order_of(cell_id), int(self.accumulation[cell_id]), cell_id)

        starts = [c for c in displayed_cells if not has_merge_parent(c)]
        starts.sort(key=sort_key)

        visited = set()
        features = []

        def build_reach(start_cell):
            cells = []
            cur = start_cell
            while cur in displayed_cells and cur not in visited:
                self._check_cancelled()
                cells.append(cur)
                visited.add(cur)
                nxt = int(self.downstream[cur])
                if not can_merge_downstream(cur, nxt) or nxt in visited:
                    break
                cur = nxt
            return cells

        def add_reach(cells, reach_id):
            if not cells:
                return False
            points = [self.cell_center(c) for c in cells]
            last_down = int(self.downstream[cells[-1]])
            if last_down >= 0:
                points.append(self.cell_center(last_down))
            if len(points) < 2:
                return False
            geom = QgsGeometry.fromPolylineXY(points)
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                int(reach_id),
                self.raster_cell(cells[0]),
                self.raster_cell(cells[-1]),
                int(len(cells)),
                int(self.accumulation[cells[0]]),
                int(self.accumulation[cells[-1]]),
                order_of(cells[-1]),
                round(float(geom.length()), 2),
            ])
            features.append(feat)
            return True

        reach_id = 1
        for start in starts:
            if start in visited:
                continue
            if add_reach(build_reach(start), reach_id):
                reach_id += 1

        # Defensive fallback: malformed graph or edited temporary layer. Export any
        # unvisited segment as its own reach rather than silently dropping it.
        for cell_id in sorted(displayed_cells, key=sort_key):
            if cell_id in visited:
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
        self.shown_cells = None
        if self.flow_layer is None:
            return
        fids, numbers = [], []
        for feat in self.flow_layer.getFeatures():
            self.spatial_index.addFeature(feat)
            fids.append(int(feat.id()))
            numbers.append(int(feat["cell_id"]))
        cells = self.grid_cells(numbers)
        for fid, cell_id in zip(fids, cells.tolist()):
            if cell_id >= 0:
                self.feature_to_cell[fid] = cell_id
                self.cell_to_feature[cell_id] = fid
        shown = np.zeros(self.rows * self.cols, dtype=bool)
        shown[cells[cells >= 0]] = True
        self.shown_cells = shown

    def forget_flow_layer(self):
        """Drops the lookups that belong to the flow-path layer once it has gone."""
        self.flow_layer = None
        self.spatial_index = None
        self.feature_to_cell = {}
        self.cell_to_feature = {}
        self.shown_cells = None

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
        """Returns all upstream cells contributing to a nominated outlet cell, the outlet first."""
        cell = int(outlet_cell_id)
        index = self.index
        if index is None or not 0 <= cell < index.n or not index.valid[cell]:
            return np.array([cell], dtype=np.int64)
        return index.upstream_of(cell, limit)

    def drains_through(self, cells, outlet):
        """True where a cell drains through ``outlet``; the outlet counts as draining through itself."""
        cells = np.asarray(cells, dtype=np.int64)
        outlet = int(outlet)
        index = self.index
        if index is None or not 0 <= outlet < index.n or not index.valid[outlet]:
            return cells == outlet
        inside = (cells >= 0) & (cells < index.n)
        result = np.zeros(cells.shape, dtype=bool)
        result[inside] = index.drains_through(cells[inside], outlet)
        return result

    def outermost_outlets(self, outlets):
        """The outlets that do not drain through another one of them, largest first.

        Upstream areas on a D8 graph are either nested or apart, so of a selected
        catchment and one inside it only the outer one is kept. Equal sizes go to
        the higher accumulation, then the higher cell id.
        """
        def rank(outlet):
            size = 1
            if self.index is not None and 0 <= outlet < self.index.n and self.index.valid[outlet]:
                size = int(self.index.size[outlet])
            acc = int(self.accumulation[outlet]) if self.accumulation is not None and outlet >= 0 else 0
            return (size, acc, outlet)

        kept = []
        for outlet in sorted({int(o) for o in outlets}, key=rank, reverse=True):
            if not any(bool(self.drains_through(outlet, other)) for other in kept):
                kept.append(outlet)
        return kept

    def shown_among(self, cell_groups):
        """The cells of the given groups that have a flow-path segment, ascending, each once."""
        parts = []
        for cells in cell_groups:
            cells = _cell_array(cells)
            if not cells.size:
                continue
            if self.shown_cells is not None:
                parts.append(cells[self.shown_cells[cells]])
            else:
                parts.append(np.array([c for c in cells.tolist() if c in self.cell_to_feature], dtype=np.int64))
        if not parts:
            return np.empty(0, dtype=np.int64)
        return np.unique(np.concatenate(parts))

    def dissolve_cells_to_geometry(self, cell_ids, chunk_size=10000):
        """Returns one dissolved polygon geometry for a collection of DEM cells.

        The returned geometry is kept as a single QgsGeometry object for canvas
        overlays. It is deliberately not written to a QgsVectorLayer, so the
        interactive selection stage does not clutter the Layers panel with
        temporary polygon layers.
        """
        unique_ids = np.unique(_cell_array(cell_ids)).tolist()
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

    def _polygonize(self, raster, row, col):
        """GDAL outlines of the non-zero values of a block of the grid.

        ``raster`` covers the grid from (``row``, ``col``). Returns a
        ``(value, QgsGeometry)`` pair per polygon traced.
        """
        rows, cols = raster.shape
        gdal_type = gdal.GDT_Int32 if raster.dtype == np.int32 else gdal.GDT_Byte
        mem_ds = gdal.GetDriverByName("MEM").Create("", int(cols), int(rows), 1, gdal_type)
        mem_ds.SetGeoTransform(self._grid_transform(int(row), int(col)))
        if self.projection:
            mem_ds.SetProjection(self.projection)
        band = mem_ds.GetRasterBand(1)
        band.WriteArray(raster)

        ogr_ds = _vector_memory_driver().CreateDataSource("ddm_outlines")
        ogr_layer = ogr_ds.CreateLayer("outlines", geom_type=ogr.wkbPolygon)
        ogr_layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
        # The band masks itself, so 0 (no cell) is skipped.
        gdal.Polygonize(band, band, ogr_layer, 0, [])

        found = []
        for feature in ogr_layer:
            ogr_geom = feature.GetGeometryRef()
            if ogr_geom is None:
                continue
            geom = QgsGeometry.fromWkt(ogr_geom.ExportToWkt())
            if geom is not None and not geom.isNull() and not geom.isEmpty():
                found.append((int(feature.GetField(0)), geom))
        return found

    def dissolve_cells_fast(self, cell_ids):
        """Catchment-boundary polygon for the interactive overlay.

        Unioning one square polygon per cell is slow for large catchments. This
        instead marks the selected cells on the grid and vectorises just their
        bounding window with GDAL, which is typically one to two orders of
        magnitude faster. The boundary follows the same cell edges as the union,
        so the result is equivalent. If GDAL polygonisation is unavailable or
        fails for any reason, it falls back to the exact per-cell union in
        dissolve_cells_to_geometry.
        """
        try:
            ids = _cell_array(cell_ids)
            if ids.size == 0:
                return QgsGeometry()
            if self.geotransform is None or not self.cols:
                return self.dissolve_cells_to_geometry(ids)

            grid = np.zeros(self.rows * self.cols, dtype=np.uint8)
            grid[ids] = 1
            grid = grid.reshape(self.rows, self.cols)
            rows = np.flatnonzero(grid.any(axis=1))
            cols = np.flatnonzero(grid.any(axis=0))
            r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
            geoms = [geom for _value, geom in self._polygonize(np.ascontiguousarray(grid[r0:r1, c0:c1]), r0, c0)]
            del grid

            if not geoms:
                return self.dissolve_cells_to_geometry(ids)
            if len(geoms) == 1:
                return geoms[0]
            return QgsGeometry.unaryUnion(geoms)
        except MemoryError:
            raise
        except Exception:
            # On any GDAL or binding error, fall back to the per-cell union.
            return self.dissolve_cells_to_geometry(cell_ids)

    def create_highlight_layer(self, upstream_cells):
        """Creates or refreshes a yellow line layer showing displayed upstream flow paths.

        The DEM graph may contain many more upstream cells than the temporary
        flow-path layer displays when 2. Display flow paths from area (m2) is more
        than one cell. Highlights only cells that actually exist as output
        flow-path features, otherwise the click tool draws hidden low-
        accumulation lines back into existence.
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

        cells = _cell_array(upstream_cells)
        if self.shown_cells is not None and cells.size:
            cells = cells[self.shown_cells[cells]]
        orders = self.display_order
        features = []
        for position, cell_id in enumerate(cells.tolist()):
            if not position & 4095:
                self._check_cancelled()
            if cell_id not in self.cell_to_feature:
                continue
            down_id = int(self.downstream[cell_id])
            if down_id < 0:
                continue
            order = int(orders[cell_id]) if orders is not None else 0
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY([self.cell_center(cell_id), self.cell_center(down_id)]))
            feat.setAttributes([
                self.raster_cell(cell_id),
                int(self.accumulation[cell_id]),
                order if order >= 1 else 1,
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
            cell_id = self.grid_cell(feat["cell_id"])
            if cell_id >= 0 and cell_id not in seen:
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

    def _domain(self, boundary_outlet_cells=None):
        """Returns the DEM cells to subdivide into subcatchments.

        If the user drew an outlet/crossing line, only cells upstream of those
        crossings are processed. If no line was drawn, the whole valid DEM graph
        is processed. This keeps the drawn line useful without making it a
        compulsory step.
        """
        if self.index is None:
            raise HydrologyBuildError("Press 4. Compute to create the flow paths first.")
        if boundary_outlet_cells:
            return self.index.domain(boundary_outlet_cells)
        return self.index.all_valid_domain()

    def _forced_mask(self, domain, forced_cells):
        """Boolean mask of the forced cut cells that fall inside the domain."""
        if not forced_cells:
            return None
        index = self.index
        cells = np.asarray([int(c) for c in forced_cells], dtype=np.int64)
        cells = cells[(cells >= 0) & (cells < index.n)]
        mask = np.zeros(index.n, dtype=bool)
        mask[cells] = True
        mask &= domain.mask
        return mask if mask.any() else None

    def forced_cells_in_domain(self, forced_cells, boundary_outlet_cells=None):
        """Splits forced cut cells into those inside the processing domain and the rest."""
        cells = [int(c) for c in (forced_cells or [])]
        mask = self._domain(boundary_outlet_cells).mask
        inside = [c for c in cells if 0 <= c < mask.size and mask[c]]
        return inside, len(cells) - len(inside)

    def _group_chosen(self, domain, chosen):
        owner = self.index.assign(domain, chosen)
        return self.index.group(domain, owner, self.accumulation)

    def build_area_threshold_subcatchments(self, min_cells=1, boundary_outlet_cells=None, include_residual=True,
                                           forced_cells=None):
        """Subdivides the selected DEM area into all target-size subcatchments.

        Outlets are chosen by accumulated residual area, then cells are assigned
        to the first selected outlet they meet downstream, yielding
        non-overlapping subcatchments rather than repeated full upstream basins.
        Forced cells, from breaklines, are always outlets.
        """
        domain = self._domain(boundary_outlet_cells)
        self._emit(60, "Chosing minimum-size subcatchment outlets")
        chosen = self.index.select_outlets(domain, min_cells, include_residual,
                                           forced=self._forced_mask(domain, forced_cells))
        if not chosen.any():
            return {}
        self._emit(70, "Assigning DEM cells to subcatchment outlets")
        return self._group_chosen(domain, chosen)

    def _bisect_threshold(self, size, target_count, count_for, max_trials):
        """Area threshold, in cells, whose outlet count comes closest to the target."""
        counts = {}

        def cached(min_cells):
            min_cells = max(1, min(int(min_cells), size))
            if min_cells not in counts:
                self._emit(55, "Searching for a threshold that gives %d subcatchments" % target_count)
                counts[min_cells] = count_for(min_cells)
            return counts[min_cells]

        # A larger threshold can only hold the number of outlets level or reduce
        # it, so the count is non-increasing and a bisection applies.
        low, high = 1, max(1, size)
        best = min(high, max(1, int(round(size / float(target_count)))))
        best_count = cached(best)
        for _ in range(int(max_trials)):
            self._check_cancelled()
            if best_count == target_count or low > high:
                break
            mid = (low + high) // 2
            mid_count = cached(mid)
            if abs(mid_count - target_count) < abs(best_count - target_count):
                best, best_count = mid, mid_count
            if mid_count > target_count:
                low = mid + 1
            else:
                high = mid - 1
        return best

    def build_target_count_subcatchments(self, target_count, boundary_outlet_cells=None, max_trials=40,
                                         forced_cells=None):
        """Gets as close as the D8 graph allows to a requested number of subcatchments.

        The count is a step function of the area threshold, and it can never drop
        below the number of cells that drain straight out of the domain, so an
        exact target is often out of reach. The search narrows the threshold using
        outlet selection alone, which is cheap, and pays for cell assignment once at
        the end. Returns the assignments, the count achieved and the threshold in
        cells that produced it, so the caller can report the difference.
        """
        target_count = max(1, int(target_count))
        domain = self._domain(boundary_outlet_cells)
        size = domain.size
        if not size:
            return {}, 0, 1
        forced = self._forced_mask(domain, forced_cells)

        def count_for(min_cells):
            return int(np.count_nonzero(self.index.select_outlets(domain, min_cells, True, forced=forced)))

        best = self._bisect_threshold(size, target_count, count_for, max_trials)
        assignments = self._group_chosen(domain, self.index.select_outlets(domain, best, True, forced=forced))
        return assignments, len(assignments), int(best)

    def terminal_outlet_count(self, boundary_outlet_cells=None):
        """Fewest subcatchments any breakdown can give.

        Every cell that drains straight out of the domain has to be an outlet, so
        the count can never drop below the number of those. An outlet line that
        crosses several flow paths makes one per path.
        """
        return self.index.terminal_count(self._domain(boundary_outlet_cells))

    def strahler_orders_available(self, boundary_outlet_cells=None):
        """Strahler orders present on the displayed stream network inside the domain."""
        domain = self._domain(boundary_outlet_cells)
        orders = self._domain_order_array(domain)
        counts = np.zeros(128, dtype=np.int64)
        for start in range(0, domain.size, BLOCK):
            values = orders[domain.cells[start:start + BLOCK]]
            counts += np.bincount(values[values > 0], minlength=128)[:128]
        return [int(v) for v in np.flatnonzero(counts) if v >= 1]

    def _domain_order_array(self, domain):
        """Strahler order per cell for a processing domain.

        Orders come from the displayed flow paths. The flow-path layer has no
        segment for a cell with no receiver, so an outlet on the raster edge or
        beside NoData has no order of its own. Such an outlet takes the order its
        displayed upstream cells make where they meet there.
        """
        index = self.index
        if self.display_order is None:
            stream_order = np.zeros(index.n, dtype=np.int8)
        else:
            stream_order = self.display_order.copy()
        bare, feeding = [], []
        for cells, receivers in index.blocks(domain):
            stays = index.stays(domain, receivers)
            own = stream_order[cells]
            bare.append(cells[~stays & (own == 0)])
            feeding.append(cells[stays & (own > 0)])
        bare = np.concatenate(bare) if bare else np.empty(0, dtype=np.int32)
        if not bare.size:
            return stream_order
        is_bare = np.zeros(index.n, dtype=bool)
        is_bare[bare] = True
        feeding = np.concatenate(feeding)
        receivers = index.parent[feeding]
        reach = is_bare[receivers]
        feeding, receivers = feeding[reach], receivers[reach]
        if receivers.size:
            orders = stream_order[feeding]
            fed, slot = np.unique(receivers, return_inverse=True)
            top = np.zeros(fed.size, dtype=np.int8)
            np.maximum.at(top, slot, orders)
            tied = np.zeros(fed.size, dtype=np.int32)
            np.add.at(tied, slot, orders == top[slot])
            stream_order[fed] = top + (tied >= 2)
        return stream_order

    def minimum_subcatchment_count(self, boundary_outlet_cells=None, forced_cells=None):
        """Fewest subcatchments any breakdown can give: the outlets plus the breakline cuts."""
        domain = self._domain(boundary_outlet_cells)
        forced = self._forced_mask(domain, forced_cells)
        return int(np.count_nonzero(self.index.select_outlets(domain, domain.size + 1, True, forced=forced)))

    def build_strahler_subcatchments(self, order, boundary_outlet_cells=None, forced_cells=None,
                                     sliver_fraction=0.5):
        """Subcatchments built around the Strahler stream network.

        Every stream of the chosen order becomes one subcatchment with everything
        that drains into it. The rest of the catchment, the area draining straight
        into higher-order streams, is cut into pieces of about the mean size of
        those subcatchments. A catchment whose own highest order is the chosen one
        or lower stays whole, so the highest order gives one subcatchment per
        outlet. Forced cells, from breaklines, are always cut.

        A leftover at an outlet or breakline smaller than ``sliver_fraction`` of
        that mean is absorbed by dropping the smallest cut draining straight into
        it, repeated until it is big enough; a leftover with no such cut above it
        stays. Strahler order only exists on the displayed flow paths, so the
        step-2 area threshold sets which orders there are.

        Returns the assignments and a summary: the size threshold in cells, the
        number of subcatchments built around streams of the chosen order, and the
        number of slivers absorbed.
        """
        domain = self._domain(boundary_outlet_cells)
        index = self.index
        k = max(1, int(order))
        stream_order = self._domain_order_array(domain)

        # A stream of order k ends where the next cell down is of a higher order,
        # or where it leaves the domain.
        ends, terminals = [], []
        for cells, receivers in index.blocks(domain):
            stays = index.stays(domain, receivers)
            own = stream_order[cells]
            below = np.where(stays, stream_order[np.maximum(receivers, 0)], 0)
            ends.append(cells[(own == k) & (~stays | (below > k))])
            terminals.append(cells[~stays])
        ends = np.sort(np.concatenate(ends)) if ends else np.empty(0, dtype=np.int32)
        terminals = np.sort(np.concatenate(terminals)) if terminals else np.empty(0, dtype=np.int32)

        protected = np.zeros(index.n, dtype=bool)
        for end in ends.tolist():
            protected[index.upstream_of(end)] = True
        for terminal in terminals.tolist():
            upstream = index.upstream_of(terminal)
            if int(stream_order[upstream].max()) <= k:
                protected[upstream] = True

        sizes = index.size[ends]
        threshold = max(1, int(round(float(sizes.mean())))) if sizes.size else max(1, domain.size)

        fixed = np.zeros(index.n, dtype=bool)
        fixed[terminals] = True
        breaks = self._forced_mask(domain, forced_cells)
        if breaks is not None:
            fixed |= breaks
        forced = fixed.copy()
        forced[ends] = True

        self._emit(60, "Cutting subcatchments around the Strahler stream network")
        chosen = index.select_outlets(domain, threshold, True, forced=forced, protected=protected)
        owner = index.assign(domain, chosen)
        absorbed = self._absorb_slivers(domain, chosen, owner, fixed, threshold * float(sliver_fraction))
        if absorbed:
            owner = index.assign(domain, chosen)

        self._emit(70, "Assigning DEM cells to subcatchment outlets")
        assignments = index.group(domain, owner, self.accumulation)
        return assignments, {"threshold_cells": threshold, "basins": int(ends.size), "absorbed": absorbed}

    def _absorb_slivers(self, domain, chosen, owner, fixed, limit):
        """Merges each leftover smaller than ``limit`` at an outlet or breakline into the piece above it.

        The smallest droppable cut draining straight into the leftover is dropped,
        and again while the leftover is still under the limit. Outlets and
        breaklines are never dropped. A cut drains into exactly one piece, so each
        leftover is settled on its own in one pass. ``chosen`` is updated in place;
        returns the number of cuts dropped.
        """
        index = self.index
        outlets = np.flatnonzero(chosen)
        size = index.owner_sizes(domain, owner)
        receivers = index.parent[outlets]
        receiver = np.where(index.stays(domain, receivers), owner[np.maximum(receivers, 0)], -1)
        feeders = {}
        for outlet, down, droppable in zip(outlets.tolist(), receiver.tolist(), (~fixed[outlets]).tolist()):
            if droppable and down >= 0:
                feeders.setdefault(down, []).append(outlet)
        dropped = 0
        for step, outlet in enumerate(outlets[fixed[outlets]].tolist()):
            if not step & 1023:
                self._check_cancelled()
            while size.get(outlet, 0) < limit and feeders.get(outlet):
                pick = min(feeders[outlet], key=lambda f: (size.get(f, 0), f))
                feeders[outlet].remove(pick)
                chosen[pick] = False
                size[outlet] = size.get(outlet, 0) + size.pop(pick, 0)
                feeders[outlet].extend(feeders.pop(pick, []))
                dropped += 1
        return dropped

    def dissolve_assignment_polygons(self, assignments):
        """One dissolved outline per subcatchment, from a single GDAL pass.

        Every assigned cell is written into a label raster over the combined
        extent, and GDAL traces all the outlines at once. The boundaries follow the
        same cell edges as a union of one square per cell, at a fraction of the
        time. Returns None if GDAL cannot do it, so the caller falls back for
        every subcatchment.
        """
        try:
            keys = [int(k) for k, cells in assignments.items() if len(cells)]
            if not keys or self.geotransform is None or not self.cols:
                return None
            cols = int(self.cols)
            r0 = c0 = None
            r1 = c1 = -1
            for key in keys:
                cells = np.asarray(assignments[key])
                rows = cells // cols
                columns = cells % cols
                r0 = int(rows.min()) if r0 is None else min(r0, int(rows.min()))
                c0 = int(columns.min()) if c0 is None else min(c0, int(columns.min()))
                r1, c1 = max(r1, int(rows.max())), max(c1, int(columns.max()))
            raster = np.zeros((r1 - r0 + 1, c1 - c0 + 1), dtype=np.int32)
            for position, key in enumerate(keys, start=1):
                cells = np.asarray(assignments[key])
                raster[cells // cols - r0, cells % cols - c0] = position

            parts = defaultdict(list)
            for value, geom in self._polygonize(raster, r0, c0):
                parts[value].append(geom)
            del raster

            outlines = {}
            for position, key in enumerate(keys, start=1):
                geoms = parts.get(position)
                if not geoms:
                    return None
                if len(geoms) == 1:
                    outlines[key] = self._on_cell_grid(geoms[0])
                else:
                    outlines[key] = QgsGeometry.unaryUnion([self._on_cell_grid(g) for g in geoms])
            return outlines
        except Exception:
            log_ignored("hydrology_engine.dissolve_assignment_polygons")
            return None

    def _on_cell_grid(self, geom):
        """Puts a vertex on every cell corner along an outline, as unioning cell squares does.

        GDAL traces an outline with vertices only where it turns; a union of cell
        squares keeps one at every cell corner. The exports place subareas with
        pointOnSurface (WBNM and XP-RAFTS always, the others through centroid_of for
        horseshoe shapes), and pointOnSurface depends on where the vertices are, so
        the outline is rebuilt on the grid to give the same points as the union.
        """
        gt = self.geotransform

        def grid(point):
            return (int(round((point.x() - gt[0]) / gt[1])), int(round((point.y() - gt[3]) / gt[5])))

        def corner(col, row):
            return QgsPointXY(gt[0] + col * gt[1], gt[3] + row * gt[5])

        multipart = geom.isMultipart()
        polygons = geom.asMultiPolygon() if multipart else [geom.asPolygon()]
        rebuilt = []
        for polygon in polygons:
            rings = []
            for ring in polygon:
                nodes = [grid(p) for p in ring]
                points = []
                for (c0, r0), (c1, r1) in zip(nodes, nodes[1:]):
                    steps = max(abs(c1 - c0), abs(r1 - r0))
                    for i in range(steps):
                        points.append(corner(c0 + (c1 - c0) * i // steps, r0 + (r1 - r0) * i // steps))
                if points:
                    points.append(QgsPointXY(points[0]))
                    rings.append(points)
            if rings:
                rebuilt.append(rings)
        if not rebuilt:
            return geom
        return QgsGeometry.fromMultiPolygonXY(rebuilt) if multipart else QgsGeometry.fromPolygonXY(rebuilt[0])

    def _outlet_order(self, outlet_id):
        """Strahler order of a subcatchment outlet: its displayed order, else its cell order."""
        if self.display_order is not None:
            shown = int(self.display_order[outlet_id])
            if shown > 0:
                return shown
        return int(self.strahler[outlet_id])

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
            ("ddm_id", QVariant.LongLong),
            ("label", QVariant.String),
        ):
            field = QgsField(name, variant)
            if name in ("area_m2", "area_ha"):
                field.setLength(20)
                field.setPrecision(2)
            fields.append(field)
        provider.addAttributes(fields)
        layer.updateFields()

        self.metrics_cache = {}
        outlines = self.dissolve_assignment_polygons(assignments) or {}
        features = []
        for idx, (outlet_id, cells) in enumerate(assignments.items()):
            self._check_cancelled()
            if not len(cells):
                continue

            # Always dissolve DEM-cell polygons: collectGeometry leaves a cell grid,
            # not an outline. The single GDAL pass covers every subcatchment; the
            # per-cell union is the fallback.
            geom = outlines.get(int(outlet_id))
            if geom is None:
                geom = self.dissolve_cells_to_geometry(cells)
            if geom.isNull() or geom.isEmpty():
                continue
            poly_geom = enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry")
            if QgsWkbTypes.geometryType(geom.wkbType()) != poly_geom:
                # Buffer by zero is a common GEOS nudge for polygonal geometry
                # collections. If it still fails, skip the broken feature rather
                # than showing thousands of individual cells.
                fixed = geom.buffer(0, 1)
                if not fixed.isNull() and not fixed.isEmpty() and QgsWkbTypes.geometryType(fixed.wkbType()) == poly_geom:
                    geom = fixed
                else:
                    continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            # Recompute from the dissolved output geometry, equivalent to
            # QGIS expression fields using $area and $area / 10000. This avoids
            # disagreements between DEM-cell-count area and the final polygon
            # geometry stored in the attribute table.
            area_m2 = round(float(geom.area()), 2)
            feat.setAttributes([
                self.raster_cell(outlet_id),
                int(len(cells)),
                area_m2,
                round(float(area_m2 / 10000.0), 2),
                self._outlet_order(int(outlet_id)),
                0,
                "",
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

    def apply_breakdown_ids(self, layer, assignments, outlet_cells=None, labels=None):
        """Writes ddm_id and label onto a subcatchment layer.

        The id counts outwards from the model outlet(s): 1 is an outlet sub-area
        and the number grows with distance upstream. It is a QGIS-side handle for
        finding a sub-area on the map and in the companion shapefiles. The model
        files keep their own numbering, because RORB fixes sub-area identity by
        the order its control vector visits them and cannot be renumbered.

        Returns the ``{outlet cell: id}`` map so a caller can label a table with
        the same numbers.
        """
        order = breakdown_order(self, assignments, outlet_cells)
        id_of = {int(outlet): idx for idx, outlet in enumerate(order, start=1)}
        if layer is None:
            return id_of

        labels = labels or {}
        fields = layer.fields()
        id_idx = fields.indexOf("ddm_id")
        label_idx = fields.indexOf("label")
        if id_idx < 0 and label_idx < 0:
            return id_of

        updates = {}
        for feat in layer.getFeatures():
            try:
                outlet = self.grid_cell(feat["outlet_id"])
            except Exception:
                log_ignored("hydrology_engine.apply_breakdown_ids")
                continue
            attrs = {}
            if id_idx >= 0:
                attrs[id_idx] = int(id_of.get(outlet, 0))
            if label_idx >= 0:
                attrs[label_idx] = str(labels.get(outlet, "") or "")
            if attrs:
                updates[int(feat.id())] = attrs

        if updates:
            try:
                layer.dataProvider().changeAttributeValues(updates)
                layer.triggerRepaint()
            except Exception:
                log_ignored("hydrology_engine.apply_breakdown_ids")
        return id_of
