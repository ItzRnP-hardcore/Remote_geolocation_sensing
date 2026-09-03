"""Along-road tracking on a topology rebuilt from Mapsforge geometry.

The accuracy in pranjali2105/SIH_2026's map matching comes from one idea:
"position is a scalar along a road polyline instead of an integrated heading."
Once the vehicle is confidently on a road, heading is not estimated at all —
the predicted displacement is walked along the road, and the road supplies the
direction. That removes the error term that dominates dead reckoning.

Doing it needs connectivity, which is why hers uses OSRM. Mapsforge carries no
node identity, but it stores coordinates in microdegrees (~0.11 m), so segments
that shared an OSM node still land on identical coordinates. Snapping endpoints
onto a 0.5 m grid rebuilds the graph: measured on 25,383 segments around IIT
Kharagpur, that gives 100% of segments at least one link, 98.5% in a single
connected component, and an average node degree of 2.73.

So the server is not actually required — only the topology was, and it can be
recovered on-device from the map already installed.
"""

from __future__ import annotations

import collections
import math

M_PER_DEG_LAT = 111_132.0
SNAP_TOL_M = 0.5

# Keeping several route hypotheses through junctions; a fork cannot be resolved
# at the moment it is reached, only in hindsight once the vehicle commits.
BEAM = 8

# Weight on turning sharply at a junction. Vehicles mostly go straight on, so a
# hard turn needs evidence rather than being free.
TURN_PENALTY_PER_DEG = 0.02


def mlon(lat):
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def seg_len_m(s):
    ml = mlon((s[0] + s[2]) / 2)
    return math.hypot((s[3] - s[1]) * ml, (s[2] - s[0]) * M_PER_DEG_LAT)


class RoadGraph:
    """Segments plus the adjacency recovered by snapping their endpoints."""

    def candidates(self, lat, lon):
        """Segment indices whose bounding box touches the 3x3 cell block at a point."""
        ci, cj = int(lat / CELL), int(lon / CELL)
        out = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out |= self._grid.get((ci + di, cj + dj), _EMPTY)
        return out

    def _build_index(self):
        """Grid over spanned cells.

        Spanned, not endpoint: a segment longer than the search block can pass beside
        the query point with both ends outside it. Only 0.2% of segments are that
        long, but they are motorway links and dual carriageways - exactly the roads a
        vehicle is on at speed.
        """
        self._grid = {}
        for i, s in enumerate(self.segs):
            i0, i1 = sorted((int(s[0] / CELL), int(s[2] / CELL)))
            j0, j1 = sorted((int(s[1] / CELL), int(s[3] / CELL)))
            for ci in range(i0, i1 + 1):
                for cj in range(j0, j1 + 1):
                    self._grid.setdefault((ci, cj), set()).add(i)

    def __init__(self, segs, tol_m=SNAP_TOL_M):
        # segs: list of (alat, alon, blat, blon, bearing)
        self.segs = segs
        self.lens = [seg_len_m(s) for s in segs]
        q = tol_m / M_PER_DEG_LAT
        self._q = q

        self.node_of = []           # per segment: (start_node, end_node)
        nodes = {}

        def node_id(lat, lon):
            k = (round(lat / q), round(lon / q))
            if k not in nodes:
                nodes[k] = len(nodes)
            return nodes[k]

        self.at_node = collections.defaultdict(list)   # node -> [(seg, end)]
        for i, s in enumerate(segs):
            a = node_id(s[0], s[1])
            b = node_id(s[2], s[3])
            self.node_of.append((a, b))
            self.at_node[a].append((i, 0))
            self.at_node[b].append((i, 1))
        self.n_nodes = len(nodes)
        self._build_index()

    def point_at(self, seg_i, offset_m, direction):
        """Coordinates `offset_m` along a segment, travelling in `direction`."""
        s = self.segs[seg_i]
        L = self.lens[seg_i] or 1e-9
        t = max(0.0, min(1.0, offset_m / L))
        if direction < 0:
            t = 1.0 - t
        return (s[0] + (s[2] - s[0]) * t, s[1] + (s[3] - s[1]) * t)

    def heading_of(self, seg_i, direction):
        b = self.segs[seg_i][4]
        return b if direction > 0 else (b + 180.0) % 360.0

    def successors(self, seg_i, direction):
        """Segments continuing past the far end, as (seg, direction, turn_deg)."""
        exit_node = self.node_of[seg_i][1 if direction > 0 else 0]
        incoming = self.heading_of(seg_i, direction)
        out = []
        for j, end in self.at_node[exit_node]:
            if j == seg_i:
                continue
            # Leaving the shared node means travelling away from it.
            d = 1 if end == 0 else -1
            turn = abs(((self.heading_of(j, d) - incoming + 180) % 360) - 180)
            out.append((j, d, turn))
        return out


CELL = 0.002          # ~222 m of latitude; see RoadGraph._build_index
_EMPTY = frozenset()


def nearest_state(graph, lat, lon, course_deg, radius_m=80.0):
    """Best (segment, offset, direction) for a position and a course.

    Candidates come from a grid rather than a scan over every segment. On the
    Kharagpur network a scan cost about 0.02 s, which was tolerable; on the 155,050
    segment UK network it costs 0.125 s, which is the entire budget at 10 Hz for a
    single call.
    """
    ml = mlon(lat)
    best = None
    for i in graph.candidates(lat, lon):
        s = graph.segs[i]
        ax, ay = (s[1] - lon) * ml, (s[0] - lat) * M_PER_DEG_LAT
        bx, by = (s[3] - lon) * ml, (s[2] - lat) * M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 < 1e-9:
            continue
        t = max(0.0, min(1.0, ((-ax) * dx + (-ay) * dy) / l2))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(px, py)
        if d > radius_m:
            continue
        for direction in (1, -1):
            h = graph.heading_of(i, direction)
            turn = abs(((h - course_deg + 180) % 360) - 180) if course_deg is not None else 0.0
            cost = d + turn * 0.5
            if best is None or cost < best[0]:
                off = t * graph.lens[i] if direction > 0 else (1 - t) * graph.lens[i]
                best = (cost, i, off, direction)
    if best is None:
        return None
    return (best[1], best[2], best[3])


def advance(graph, hypotheses, distance_m):
    """Walk every hypothesis `distance_m` further along the network."""
    out = []
    for (seg, off, direction, cost) in hypotheses:
        stack = [(seg, off, direction, cost, distance_m)]
        while stack:
            s, o, d, c, remaining = stack.pop()
            room = graph.lens[s] - o
            if remaining <= room or not graph.successors(s, d):
                out.append((s, min(o + remaining, graph.lens[s]), d, c))
                continue
            spent = room
            for (j, jd, turn) in graph.successors(s, d):
                stack.append((j, 0.0, jd, c + turn * TURN_PENALTY_PER_DEG,
                              remaining - spent))
    out.sort(key=lambda h: h[3])
    # Collapse hypotheses that ended up on the same segment travelling the same
    # way; keeping both would spend the beam on duplicates.
    seen, uniq = set(), []
    for h in out:
        k = (h[0], h[2])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    return uniq[:BEAM]


def track(graph, start_lat, start_lon, start_course, displacements):
    """Positions after walking each per-second displacement along the network.

    Returns None when the start cannot be placed on a road, which is the honest
    answer off-network rather than snapping to something far away.
    """
    st = nearest_state(graph, start_lat, start_lon, start_course)
    if st is None:
        return None
    hyps = [(st[0], st[1], st[2], 0.0)]
    positions = []
    for d in displacements:
        hyps = advance(graph, hyps, float(d))
        if not hyps:
            return None
        best = hyps[0]
        positions.append(graph.point_at(best[0], best[1], best[2]))
    return positions
