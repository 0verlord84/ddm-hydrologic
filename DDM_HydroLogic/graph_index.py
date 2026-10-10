# -*- coding: utf-8 -*-
# DDM HydroLogic: catchment delineation and hydrology/hydraulic model export for QGIS.
# Copyright (C) 2026 Davide Di Mauro
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY. See the GNU General Public
# License (the LICENSE file) for more details.
"""Array index over the D8 receiver forest.

Built once when the flow paths are computed. Accumulation, Strahler order,
upstream walks and the subcatchment passes run as whole-array numpy operations
instead of per-cell Python loops. Every array is 32-bit or narrower, and any pass
that would need a full-length temporary works through the cells in blocks, so the
index costs about 10 bytes per cell of the grid plus 8 per valid cell, on top of
the receiver array it is built from.

Two orderings carry everything:

- waves: cells grouped by how far they sit from their furthest source cell, so
  every cell in a wave has all its upstream cells in earlier waves. Walking the
  waves forward is an upstream-to-downstream pass, walking them backward the
  reverse. Within a wave, cells draining to the same receiver sit together.
- pre-order: the order a depth-first walk upstream visits cells, taking upstream
  cells highest cell id first. Every cell's upstream area is then one contiguous
  slice of that order.

Receivers must be D8 neighbours on a grid ``cols`` cells wide, which is what the
priority flood produces, so a receiver is stored as one of eight directions.
"""

import time

import numpy as np

# Cells per block for passes that would otherwise allocate full-length temporaries.
BLOCK = 1 << 22


class FlowGraphError(Exception):
    """Raised when a receiver array is not a D8 forest the index can be built on."""


def nonzero32(mask):
    """Positions of the True values of a flat mask, as int32, ascending."""
    parts = []
    for start in range(0, mask.size, BLOCK):
        found = np.flatnonzero(mask[start:start + BLOCK])
        if found.size:
            parts.append((found + start).astype(np.int32))
    if not parts:
        return np.empty(0, dtype=np.int32)
    return parts[0] if len(parts) == 1 else np.concatenate(parts)


class Domain:
    """Cells upstream of a set of boundary cells, grouped by wave.

    ``receiver_at`` is filled on first use: the position in ``cells`` of every
    cell's receiver, -1 where the receiver is outside the domain.
    """

    __slots__ = ("mask", "cells", "starts", "receiver_at")

    def __init__(self, mask, cells, starts):
        self.mask = mask
        self.cells = cells
        self.starts = starts
        self.receiver_at = None

    @property
    def size(self):
        return int(self.cells.size)


class FlowIndex:
    """Waves, subtree sizes, Strahler order and pre-order of a D8 forest."""

    def __init__(self, downstream, valid, cols, progress=None):
        """``progress`` is called with the fraction done while the index is built,
        and with None now and then during the passes; it may raise to cancel."""
        self._progress = progress or (lambda _fraction: None)
        self._last_report = 0.0
        self.cols = int(cols)
        if self.cols < 3:
            raise FlowGraphError("the grid must be at least three cells wide")
        self.valid = np.asarray(valid, dtype=bool).reshape(-1)
        self.n = int(self.valid.size)
        cols = self.cols
        self.offsets = (-cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1)
        self._key_type = np.int32 if 8 * self.n < 2 ** 31 else np.int64
        self._step_of = np.array(self.offsets, dtype=self._key_type)
        self.valid_count = int(np.count_nonzero(self.valid))

        self.parent = self._receivers(downstream)
        self._report(0.02)
        self.code = self._directions()
        self._code_view = memoryview(self.code)
        self._report(0.05)
        self._build_waves()
        self.is_forest = int(self.wave_starts[-1]) == self.valid_count
        if not self.is_forest:
            return
        self._build_preorder()
        self._domain_cache = {}

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _report(self, fraction):
        """Progress at most a few times a second; None only checks for cancellation."""
        now = time.monotonic()
        if now - self._last_report >= 0.25:
            self._last_report = now
            self._progress(fraction)

    def _receivers(self, downstream):
        """The receiver of every valid cell, -1 where it has none or it is not valid.

        The receiver array from the priority flood already reads that way and is
        used as it is; anything else gets a cleaned copy.
        """
        down = np.asarray(downstream).reshape(-1)
        if down.size != self.n:
            raise FlowGraphError("the receiver array and the valid mask differ in size")
        if down.size and (int(down.max()) >= self.n or int(down.min()) < -1):
            raise FlowGraphError("a receiver points outside the grid")
        parent = down.astype(np.int32, copy=False)
        self._orphans = np.empty(0, dtype=np.int32)
        clean = True
        for start in range(0, self.n, BLOCK):
            block = parent[start:start + BLOCK]
            linked = block >= 0
            if (linked & ~self.valid[start:start + BLOCK]).any() or not self.valid[block[linked]].all():
                clean = False
                break
        if clean:
            return parent
        parent = parent.copy()
        orphans = []
        for start in range(0, self.n, BLOCK):
            block = parent[start:start + BLOCK]
            here = self.valid[start:start + BLOCK]
            linked = (block >= 0) & here
            keep = linked.copy()
            keep[keep] = self.valid[block[keep]]
            orphans.append(np.flatnonzero(linked & ~keep).astype(np.int32) + start)
            block[~keep] = -1
        self._orphans = np.concatenate(orphans)
        return parent

    def _directions(self):
        """Direction 0-7 from every cell to its receiver, -1 for none."""
        code = np.full(self.n, -1, dtype=np.int8)
        for start in range(0, self.n, BLOCK):
            stop = min(self.n, start + BLOCK)
            receivers = self.parent[start:stop].astype(np.int64)
            linked = receivers >= 0
            step = receivers - np.arange(start, stop, dtype=np.int64)
            block = code[start:stop]
            for k, offset in enumerate(self.offsets):
                block[linked & (step == offset)] = k
            if (block[linked] < 0).any():
                raise FlowGraphError("a receiver is not a D8 neighbour")
        return code

    def _by_receiver(self, cells):
        """The cells grouped by receiver: cells with none first, then by receiver id."""
        if cells.size < 2:
            return cells
        receivers = self.parent[cells]
        roots = cells[receivers < 0]
        linked = cells[receivers >= 0]
        if linked.size > 1:
            keys = self.parent[linked].astype(self._key_type) * 8 + self.code[linked]
            keys.sort()
            linked = (keys >> 3) - self._step_of[keys & 7]
            linked = linked.astype(np.int32, copy=False)
        if not roots.size:
            return linked
        return np.concatenate((roots, linked))

    def _linked_blocks(self, cells):
        """Blocks of the cells that have a receiver, with their receivers.

        ``cells`` must be grouped by receiver, as every wave is. A block can split
        one receiver's group, but never holds two separate runs of it.
        """
        receivers = self.parent[cells]
        first = int(np.searchsorted(receivers, 0))
        for start in range(first, cells.size, BLOCK):
            yield cells[start:start + BLOCK], receivers[start:start + BLOCK]

    @staticmethod
    def _groups(receivers):
        """Start of every run of equal receivers in a grouped block."""
        starts = np.flatnonzero(receivers[1:] != receivers[:-1]) + 1
        return np.concatenate(([0], starts))

    def _build_waves(self):
        n, code, valid = self.n, self.code, self.valid
        indegree = np.zeros(n, dtype=np.int8)
        for j, offset in enumerate(self.offsets):
            lo, hi = max(0, -offset), min(n, n - offset)
            indegree[lo:hi] += code[lo + offset:hi + offset] == 7 - j
        size = valid.astype(np.int32)
        order = np.zeros(n, dtype=np.int8)
        best = np.zeros(n, dtype=np.int8)
        ties = np.zeros(n, dtype=np.int8)
        level_order = np.empty(self.valid_count, dtype=np.int32)
        starts = [0]

        wave = self._by_receiver(nonzero32(valid & (indegree == 0)))
        filled = 0
        while wave.size:
            if filled + wave.size > level_order.size:
                break
            level_order[filled:filled + wave.size] = wave
            filled += wave.size
            starts.append(filled)
            self._report(0.05 + 0.55 * filled / max(1, self.valid_count))
            # Strahler: a source is 1; two or more upstream cells sharing the top
            # order raise it by one.
            top = best[wave]
            order[wave] = np.where(top == 0, 1, top + (ties[wave] >= 2))

            touched = []
            for cells, receivers in self._linked_blocks(wave):
                heads = self._groups(receivers)
                up = receivers[heads]
                counts = np.diff(np.append(heads, receivers.size))
                size[up] += np.add.reduceat(size[cells], heads).astype(np.int32)
                child_order = order[cells]
                peak = np.maximum.reduceat(child_order, heads)
                peak_ties = np.add.reduceat(child_order == np.repeat(peak, counts), heads)
                old_best, old_ties = best[up], ties[up]
                merged = np.maximum(old_best, peak)
                best[up] = merged
                ties[up] = (np.where(old_best == merged, old_ties, 0)
                            + np.where(peak == merged, peak_ties, 0)).astype(np.int8)
                indegree[up] -= counts.astype(np.int8)
                touched.append(up)
            if not touched:
                break
            up = touched[0] if len(touched) == 1 else np.unique(np.concatenate(touched))
            wave = self._by_receiver(up[indegree[up] == 0])

        np.putmask(order, ~valid, -1)
        self.size = size
        self.order = order
        self.level_order = level_order[:filled] if filled < level_order.size else level_order
        self.wave_starts = np.asarray(starts, dtype=np.int64)

    def _build_preorder(self):
        """Position of every cell in a depth-first walk upstream, siblings highest id first."""
        n, parent, code, size = self.n, self.parent, self.code, self.size
        pre = np.zeros(n, dtype=np.int32)
        # A cell sits one after its receiver, plus the upstream areas of the
        # siblings walked before it, which are those with higher ids. A higher id
        # is a later direction, since the eight offsets are in ascending order.
        for start in range(0, n, BLOCK):
            own = code[start:start + BLOCK]
            linked = own >= 0
            if not linked.any():
                continue
            cells = np.flatnonzero(linked) + start
            receivers = parent[cells].astype(np.int64)
            side = 7 - own[linked].astype(np.int64)
            ahead = np.ones(cells.size, dtype=np.int64)
            for j, offset in enumerate(self.offsets):
                later = np.flatnonzero(side < j)
                if not later.size:
                    continue
                sibling = receivers[later] + offset
                real = code[sibling] == 7 - j
                ahead[later[real]] += size[sibling[real]]
            pre[cells] = ahead
        self._report(0.75)

        # The walk visits the trees one after another, in ascending root id.
        roots = nonzero32(self.valid & (code < 0))
        root_sizes = size[roots].astype(np.int64)
        pre[roots] = np.cumsum(root_sizes) - root_sizes

        starts = self.wave_starts
        for w in range(len(starts) - 2, -1, -1):
            if not w & 255:
                self._report(0.75 + 0.2 * (1.0 - w / max(1, len(starts) - 1)))
            for cells, receivers in self._linked_blocks(self.level_order[starts[w]:starts[w + 1]]):
                pre[cells] += pre[receivers]

        preorder = np.full(self.valid_count, -1, dtype=np.int32)
        for start in range(0, self.valid_count, BLOCK):
            cells = self.level_order[start:start + BLOCK]
            spots = pre[cells]
            if int(spots.min()) < 0 or int(spots.max()) >= self.valid_count:
                raise FlowGraphError("the upstream walk order ran outside the valid cells")
            preorder[spots] = cells
        if (preorder < 0).any():
            raise FlowGraphError("the upstream walk order is not a permutation of the valid cells")
        self.pre = pre
        self.preorder = preorder
        self._report(1.0)

    # ------------------------------------------------------------------
    # accumulation, Strahler order and upstream walks
    # ------------------------------------------------------------------
    def accumulation(self):
        """Cell counts upstream, 0 off the valid cells.

        A tree that ends at a receiver which is not a valid cell gets no
        accumulation from the outlet walk, so its cells count 1. The priority flood
        never builds such a tree, so the subtree sizes are normally used as they are.
        """
        if not self._orphans.size:
            return self.size
        acc = self.size.copy()
        for root in self._orphans.tolist():
            acc[self.upstream_of(root)] = 1
        return acc

    def strahler(self):
        """Cell-level Strahler order, -1 off the valid cells, 1 on trees the outlet walk never reached."""
        if not self._orphans.size:
            return self.order
        order = self.order.copy()
        for root in self._orphans.tolist():
            order[self.upstream_of(root)] = 1
        return order

    def upstream_of(self, cell, limit=None):
        """Same cells, in the same order, as the depth-first upstream walk."""
        start = int(self.pre[cell])
        count = int(self.size[cell])
        if limit is not None and count > limit:
            count = int(limit) + 1
        return self.preorder[start:start + count]

    def drains_through(self, cells, outlet):
        """True where a cell drains through ``outlet``, the outlet itself included."""
        start = int(self.pre[outlet])
        stop = start + int(self.size[outlet])
        cells = np.asarray(cells)
        spots = self.pre[cells]
        return self.valid[cells] & (spots >= start) & (spots < stop)

    def upstream_cells(self, cell):
        """Valid cells whose receiver is ``cell``, lowest id first."""
        code = self._code_view
        found = []
        for j, offset in enumerate(self.offsets):
            other = cell + offset
            if 0 <= other < self.n and code[other] == 7 - j:
                found.append(other)
        return found

    def stream_order(self, shown):
        """Strahler order over the cells of a mask, as if only those cells existed.

        Returns an int8 array, 0 off the mask.
        """
        order = np.zeros(self.n, dtype=np.int8)
        best = np.zeros(self.n, dtype=np.int8)
        ties = np.zeros(self.n, dtype=np.int8)
        domain = self._filter(shown)
        for w in range(len(domain.starts) - 1):
            wave = domain.cells[domain.starts[w]:domain.starts[w + 1]]
            top = best[wave]
            order[wave] = np.where(top == 0, 1, top + (ties[wave] >= 2))
            for cells, receivers in self._linked_blocks(wave):
                keep = shown[receivers]
                if not keep.any():
                    continue
                cells, receivers = cells[keep], receivers[keep]
                heads = self._groups(receivers)
                up = receivers[heads]
                counts = np.diff(np.append(heads, receivers.size))
                child_order = order[cells]
                peak = np.maximum.reduceat(child_order, heads)
                peak_ties = np.add.reduceat(child_order == np.repeat(peak, counts), heads)
                old_best, old_ties = best[up], ties[up]
                merged = np.maximum(old_best, peak)
                best[up] = merged
                ties[up] = (np.where(old_best == merged, old_ties, 0)
                            + np.where(peak == merged, peak_ties, 0)).astype(np.int8)
        return order

    # ------------------------------------------------------------------
    # subcatchment passes
    # ------------------------------------------------------------------
    def _filter(self, mask):
        """The cells of a mask in wave order, with the start of every wave."""
        parts, sizes = [], [0]
        starts = self.wave_starts
        for w in range(len(starts) - 1):
            if not w & 1023:
                self._report(None)
            wave = self.level_order[starts[w]:starts[w + 1]]
            keep = wave[mask[wave]]
            if keep.size:
                parts.append(keep)
                sizes.append(keep.size)
        cells = np.concatenate(parts) if parts else np.empty(0, dtype=np.int32)
        return Domain(mask, cells, np.cumsum(sizes, dtype=np.int64))

    def domain(self, boundary_cells):
        """Every cell upstream of the boundary cells, grouped by wave."""
        key = tuple(sorted({int(c) for c in boundary_cells if 0 <= int(c) < self.n and self.valid[int(c)]}))
        cached = self._domain_cache.get(key)
        if cached is not None:
            return cached
        mask = np.zeros(self.n, dtype=bool)
        for cell in key:
            mask[self.upstream_of(cell)] = True
        domain = self._filter(mask)
        if len(self._domain_cache) >= 2:
            self._domain_cache.clear()
        self._domain_cache[key] = domain
        return domain

    def all_valid_domain(self):
        """The whole valid raster as one domain."""
        return Domain(self.valid, self.level_order, self.wave_starts)

    def blocks(self, domain, reverse=False):
        """``(cells, receivers)`` blocks, wave by wave; a block never spans two waves."""
        starts = domain.starts
        waves = range(len(starts) - 1)
        for step, w in enumerate(reversed(waves) if reverse else waves):
            if not step & 255:
                self._report(None)
            end = int(starts[w + 1])
            for start in range(int(starts[w]), end, BLOCK):
                cells = domain.cells[start:min(end, start + BLOCK)]
                yield cells, self.parent[cells]

    @staticmethod
    def stays(domain, receivers):
        """Where a cell's receiver is inside the domain."""
        return (receivers >= 0) & domain.mask[receivers]

    def _receiver_positions(self, domain):
        """Fills and returns ``domain.receiver_at``."""
        if domain.receiver_at is None:
            position = np.full(self.n, -1, dtype=np.int32)
            position[domain.cells] = np.arange(domain.size, dtype=np.int32)
            receiver_at = np.empty(domain.size, dtype=np.int32)
            for start in range(0, domain.size, BLOCK):
                receivers = self.parent[domain.cells[start:start + BLOCK]]
                receiver_at[start:start + BLOCK] = np.where(receivers >= 0, position[np.maximum(receivers, 0)], -1)
            domain.receiver_at = receiver_at
        return domain.receiver_at

    def _spans(self, domain, reverse=False):
        """``(start, stop)`` positions in ``domain.cells``, wave by wave; a span never covers two waves."""
        starts = domain.starts
        waves = range(len(starts) - 1)
        for step, w in enumerate(reversed(waves) if reverse else waves):
            if not step & 255:
                self._report(None)
            end = int(starts[w + 1])
            for start in range(int(starts[w]), end, BLOCK):
                yield start, min(end, start + BLOCK)

    def select_outlets(self, domain, min_cells, include_residual=True, forced=None, protected=None):
        """Chooses outlets by accumulated residual area, upstream to downstream.

        A cell becomes an outlet when the residual area reaching it hits
        ``min_cells``; its area then stops travelling downstream. Forced cells are
        always outlets. Protected cells are never picked by size; forced cells, and
        with ``include_residual`` every cell that leaves the domain, are picked
        regardless. Returns a boolean mask of the chosen outlets.

        The running areas are held in the order of ``domain.cells``, so a wave
        reads them as one slice.
        """
        receiver_at = self._receiver_positions(domain)
        cells = domain.cells
        count = np.ones(domain.size, dtype=np.int32)
        picked = np.zeros(domain.size, dtype=bool)
        forced_here = forced[cells] if forced is not None else None
        protected_here = protected[cells] if protected is not None else None
        min_cells = max(1, int(min_cells))
        for start, stop in self._spans(domain):
            here = count[start:stop]
            dest = receiver_at[start:stop]
            stays = dest >= 0
            pick = here >= min_cells
            if protected_here is not None:
                pick &= ~protected_here[start:stop]
            if include_residual:
                pick |= (~stays) & (here > 0)
            if forced_here is not None:
                pick |= forced_here[start:stop]
            picked[start:stop] = pick
            move = stays & ~pick
            if move.any():
                dest = dest[move]
                heads = self._groups(dest)
                count[dest[heads]] += np.add.reduceat(here[move], heads).astype(np.int32)
        chosen = np.zeros(self.n, dtype=bool)
        chosen[cells[picked]] = True
        return chosen

    def assign(self, domain, chosen):
        """Owner outlet of every domain cell: the first chosen cell downstream."""
        receiver_at = self._receiver_positions(domain)
        cells = domain.cells
        picked = chosen[cells]
        owner_at = np.empty(domain.size, dtype=np.int32)
        for start, stop in self._spans(domain, reverse=True):
            dest = receiver_at[start:stop]
            follow = np.where(dest >= 0, owner_at[np.maximum(dest, 0)], -1)
            owner_at[start:stop] = np.where(picked[start:stop], np.arange(start, stop, dtype=np.int32), follow)
        owner = np.full(self.n, -1, dtype=np.int32)
        owned = owner_at >= 0
        owner[cells[owned]] = cells[owner_at[owned]]
        return owner

    def terminal_count(self, domain):
        """Domain cells whose receiver is outside the domain, or which have none."""
        return int(np.count_nonzero(self._receiver_positions(domain) < 0))

    def _owner_groups(self, domain, owner):
        """Owner outlets in ascending order, and every owned cell's position among them."""
        cells = domain.cells
        outlets = []
        for start in range(0, cells.size, BLOCK):
            block = cells[start:start + BLOCK]
            outlets.append(block[owner[block] == block])
        outlets = np.sort(np.concatenate(outlets)) if outlets else np.empty(0, dtype=np.int32)
        return outlets

    def owner_sizes(self, domain, owner):
        """Number of domain cells owned by each outlet, as ``{outlet: cells}``."""
        outlets = self._owner_groups(domain, owner)
        counts = np.zeros(outlets.size, dtype=np.int64)
        for start in range(0, domain.cells.size, BLOCK):
            values = owner[domain.cells[start:start + BLOCK]]
            values = values[values >= 0]
            if values.size:
                counts += np.bincount(np.searchsorted(outlets, values), minlength=outlets.size)
        return dict(zip(outlets.tolist(), counts.tolist()))

    def group(self, domain, owner, accumulation):
        """Owner array to the engine's assignment dict: largest first, cells ascending.

        All the cells go into one int32 array, one slice per subcatchment.
        """
        outlets = self._owner_groups(domain, owner)
        if not outlets.size:
            return {}
        narrow = np.int16 if outlets.size < 2 ** 15 else np.int32
        counts = np.zeros(outlets.size, dtype=np.int64)
        for start in range(0, self.n, BLOCK):
            values = owner[start:start + BLOCK]
            values = values[(values >= 0) & domain.mask[start:start + BLOCK]]
            if values.size:
                counts += np.bincount(np.searchsorted(outlets, values), minlength=outlets.size)
        bounds = np.concatenate(([0], np.cumsum(counts)))
        fill = bounds[:-1].copy()
        packed = np.empty(int(bounds[-1]), dtype=np.int32)
        for start in range(0, self.n, BLOCK):
            values = owner[start:start + BLOCK]
            here = (values >= 0) & domain.mask[start:start + BLOCK]
            if not here.any():
                continue
            cells = np.flatnonzero(here) + start
            slot = np.searchsorted(outlets, values[here]).astype(narrow)
            order = np.argsort(slot, kind="stable")
            slot, cells = slot[order], cells[order]
            heads = self._groups(slot)
            lengths = np.diff(np.append(heads, slot.size))
            rank = np.arange(slot.size) - np.repeat(heads, lengths)
            packed[fill[slot] + rank] = cells
            fill[slot[heads]] += lengths
        groups = [(int(outlets[i]), packed[bounds[i]:bounds[i + 1]]) for i in range(outlets.size)]
        groups.sort(key=lambda item: (len(item[1]), int(accumulation[item[0]]), item[0]), reverse=True)
        return {key: cells for key, cells in groups}
