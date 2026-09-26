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
instead of per-cell Python loops.

Two orderings carry everything:

- waves: cells grouped by how far they sit from their furthest source cell, so
  every cell in a wave has all its upstream cells in earlier waves. Walking the
  waves forward is an upstream-to-downstream pass, walking them backward the
  reverse.
- pre-order: the order a depth-first walk upstream visits cells, taking upstream
  cells highest cell id first, as collect_upstream does. Every cell's upstream
  area is then one contiguous slice of that order.

Results match the depth-first walks in hydrology_engine exactly. A graph that is
not a clean forest (a loop somewhere) sets is_forest False and the engine uses
those walks instead.
"""

import numpy as np


class FlowIndex:
    """Waves, subtree sizes, Strahler order and pre-order of a D8 forest."""

    def __init__(self, downstream, valid_flat, check=None):
        self.check = check or (lambda: None)
        down = np.asarray(downstream, dtype=np.int64)
        valid = np.asarray(valid_flat, dtype=bool)
        n = int(down.size)
        self.n = n
        self.valid = valid

        # A receiver only counts when it is itself a valid cell, exactly like the
        # upstream lists the engine builds.
        parent = np.full(n, -1, dtype=np.int64)
        cells = np.flatnonzero(valid)
        targets = down[cells]
        linked = targets >= 0
        linked[linked] = valid[targets[linked]]
        parent[cells[linked]] = targets[linked]
        self.parent = parent
        self.downstream = down

        self._build_waves(cells)
        self.is_forest = bool(np.all(self.level[cells] >= 0))
        if not self.is_forest:
            return
        self._build_preorder(cells)
        self._build_outlet_trees(cells)
        self._domain_cache = {}

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_waves(self, cells):
        n, parent = self.n, self.parent
        linked = parent[cells] >= 0
        indegree = np.bincount(parent[cells][linked], minlength=n)
        size = np.ones(n, dtype=np.int64)
        order = np.zeros(n, dtype=np.int16)
        best = np.zeros(n, dtype=np.int16)
        best_count = np.zeros(n, dtype=np.int32)
        wave_best = np.zeros(n, dtype=np.int16)
        wave_count = np.zeros(n, dtype=np.int32)
        level = np.full(n, -1, dtype=np.int32)

        waves = []
        wave = cells[indegree[cells] == 0]
        step = 0
        while wave.size:
            if not step & 63:
                self.check()
            level[wave] = step
            waves.append(wave)
            # Strahler: a source is 1; two or more children sharing the top
            # order raise it by one.
            order[wave] = np.where(best[wave] == 0, 1, best[wave] + (best_count[wave] >= 2))

            parents = parent[wave]
            has = parents >= 0
            p = parents[has]
            if not p.size:
                break
            children = wave[has]
            np.add.at(size, p, size[children])

            child_order = order[children]
            np.maximum.at(wave_best, p, child_order)
            np.add.at(wave_count, p, child_order == wave_best[p])
            unique_p, arrivals = np.unique(p, return_counts=True)
            old_best, old_count = best[unique_p], best_count[unique_p]
            new_best, new_count = wave_best[unique_p], wave_count[unique_p]
            merged = np.maximum(old_best, new_best)
            best[unique_p] = merged
            kept = np.where(old_best == merged, old_count, 0)
            best_count[unique_p] = kept + np.where(new_best == merged, new_count, 0)
            wave_best[unique_p] = 0
            wave_count[unique_p] = 0

            indegree[unique_p] -= arrivals
            wave = unique_p[indegree[unique_p] == 0]
            step += 1

        self.size = size
        self.order = order
        self.level = level
        self.waves = waves

    def _build_preorder(self, cells):
        n, parent, size = self.n, self.parent, self.size
        children = cells[parent[cells] >= 0]
        owners = parent[children]
        # The upstream walk takes a cell's upstream neighbours highest id first.
        sort = np.lexsort((-children, owners))
        children, owners = children[sort], owners[sort]
        sizes = size[children]
        running = np.cumsum(sizes)
        group_start = np.r_[0, np.flatnonzero(owners[1:] != owners[:-1]) + 1]
        before_group = np.repeat(np.r_[0, running[group_start[1:] - 1]], np.diff(np.r_[group_start, children.size]))
        offset = running - sizes - before_group

        value = np.zeros(n, dtype=np.int64)
        value[children] = 1 + offset
        roots = cells[parent[cells] < 0]
        value[roots] = np.cumsum(size[roots]) - size[roots]

        # Pointer jumping: after each pass a cell holds the sum over twice as much
        # of its path to the root, so the loop runs about log2(longest path) times.
        pointer = parent.copy()
        active = cells[pointer[cells] >= 0]
        while active.size:
            self.check()
            ahead = pointer[active]
            value[active] += value[ahead]
            pointer[active] = pointer[ahead]
            active = active[pointer[active] >= 0]

        self.pre = value
        self.preorder = np.empty(int(size[roots].sum()), dtype=np.int64)
        self.preorder[value[cells]] = cells

    def _build_outlet_trees(self, cells):
        """Marks cells whose tree ends at a boundary outlet (a cell with no receiver).

        Only those trees get an accumulation and Strahler order from the outlet
        walk; a tree ending at a receiver that is not a valid cell gets 1.
        """
        n = self.n
        root = np.full(n, -1, dtype=np.int64)
        # The pre-order is one block per root, laid out in root id order.
        roots = cells[self.parent[cells] < 0]
        root[self.preorder] = np.repeat(roots, self.size[roots])
        self.root = root
        self.outlet_tree = np.zeros(n, dtype=bool)
        self.outlet_tree[cells] = self.downstream[root[cells]] < 0

    # ------------------------------------------------------------------
    # accumulation, Strahler order and upstream walks
    # ------------------------------------------------------------------
    def accumulation(self):
        """Cell counts upstream, 1 for cells the outlet walk never reached."""
        acc = np.zeros(self.n, dtype=np.int64)
        cells = np.flatnonzero(self.valid)
        acc[cells] = np.where(self.outlet_tree[cells], self.size[cells], 1)
        return acc

    def strahler(self):
        """Cell-level Strahler order, 1 for cells the outlet walk never reached."""
        order = np.full(self.n, -1, dtype=np.int16)
        cells = np.flatnonzero(self.valid)
        order[cells] = np.where(self.outlet_tree[cells], self.order[cells], 1)
        return order

    def upstream_of(self, cell, limit=None):
        """Same cells, in the same order, as the depth-first upstream walk."""
        start = int(self.pre[cell])
        count = int(self.size[cell])
        if limit is not None and count > limit:
            count = int(limit) + 1
        return self.preorder[start:start + count]

    # ------------------------------------------------------------------
    # subcatchment passes
    # ------------------------------------------------------------------
    def domain(self, boundary_cells):
        """Mask of every cell upstream of the boundary cells, and its waves."""
        key = tuple(sorted(int(c) for c in boundary_cells))
        cached = self._domain_cache.get(key)
        if cached is not None:
            return cached
        mask = np.zeros(self.n, dtype=bool)
        for cell in key:
            mask[self.upstream_of(cell)] = True
        cells = np.flatnonzero(mask)
        levels = self.level[cells]
        sort = np.argsort(levels, kind="stable")
        cells, levels = cells[sort], levels[sort]
        cuts = np.flatnonzero(levels[1:] != levels[:-1]) + 1
        waves = np.split(cells, cuts) if cells.size else []
        down = self.parent.copy()
        inside = down >= 0
        inside[inside] = mask[down[inside]]
        cached = (mask, waves, inside)
        if len(self._domain_cache) >= 2:
            self._domain_cache.clear()
        self._domain_cache[key] = cached
        return cached

    def all_valid_domain(self):
        """The whole valid raster as one domain."""
        cells = np.flatnonzero(self.valid)
        return self.domain(cells[self.parent[cells] < 0].tolist())

    def select_outlets(self, domain, min_cells, include_residual=True, forced=None, protected=None):
        """Chooses outlets by accumulated residual area, upstream to downstream.

        A cell becomes an outlet when the residual area reaching it hits
        ``min_cells``; its area then stops travelling downstream. Forced cells are
        always outlets. Protected cells are never picked by size; forced cells, and
        with ``include_residual`` every cell that leaves the domain, are picked
        regardless. Returns a boolean mask of the chosen outlets.
        """
        mask, waves, inside = domain
        parent = self.parent
        count = np.zeros(self.n, dtype=np.int64)
        count[mask] = 1
        chosen = np.zeros(self.n, dtype=bool)
        min_cells = max(1, int(min_cells))
        for step, wave in enumerate(waves):
            if not step & 63:
                self.check()
            here = count[wave]
            stays = inside[wave]
            pick = here >= min_cells
            if protected is not None:
                pick &= ~protected[wave]
            if include_residual:
                pick |= (~stays) & (here > 0)
            if forced is not None:
                pick |= forced[wave]
            chosen[wave] = pick
            move = stays & ~pick
            if move.any():
                np.add.at(count, parent[wave[move]], here[move])
        return chosen

    def assign(self, domain, chosen):
        """Owner outlet of every domain cell: the first chosen cell downstream."""
        mask, waves, inside = domain
        parent = self.parent
        owner = np.full(self.n, -1, dtype=np.int64)
        for step, wave in enumerate(reversed(waves)):
            if not step & 63:
                self.check()
            down = parent[wave]
            follow = np.where(inside[wave], owner[np.maximum(down, 0)], -1)
            owner[wave] = np.where(chosen[wave], wave, follow)
        return owner

    def group(self, domain, owner, accumulation):
        """Owner array to the engine's assignment dict: largest first, cells sorted."""
        mask = domain[0]
        cells = np.flatnonzero(mask & (owner >= 0))
        owners = owner[cells]
        sort = np.lexsort((cells, owners))
        cells, owners = cells[sort], owners[sort]
        cuts = np.flatnonzero(owners[1:] != owners[:-1]) + 1
        keys = owners[np.r_[0, cuts]] if cells.size else np.empty(0, dtype=np.int64)
        parts = np.split(cells, cuts) if cells.size else []
        groups = [(int(k), p.tolist()) for k, p in zip(keys.tolist(), parts)]
        groups.sort(key=lambda item: (len(item[1]), int(accumulation[item[0]]) if item[0] >= 0 else 0, item[0]),
                    reverse=True)
        return {key: cells for key, cells in groups}
