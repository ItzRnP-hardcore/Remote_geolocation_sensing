"""Replay a recorded session through the map matcher, with and without road connectivity.

`MapMatcher.kt` scores the step between consecutive candidates by *straight-line* distance.
Its docstring justifies that by saying Mapsforge has no usable topology — which was true when
it was written and stopped being true when `RoadGraph.kt` landed, recovering the graph by
snapping coincident endpoints (25,383 segments, 100% linked, 98.5% one component).

Straight-line transitions cannot tell a road from the one running parallel to it twenty metres
away, because both are twenty metres from the same fix and both are reachable in a straight
line. Newson & Krumm's actual transition term — the disagreement between straight-line distance
and *route* distance — separates them immediately: reaching the parallel carriageway means
driving to a junction and back, so a 20 m sideways hop costs a 200 m detour it never made.

The second variable here is the correction budget. `SensorService` passes `DeadReckoner.driftMetres`
as the matcher's `uncertaintyM`, but that field is |position - anchor|: displacement since the
last GNSS fix, not error. Driving 200 m in a straight line with a perfect integrator sets it to
200, and the guard meant to stop a large sideways snap then permits one.

Both are measured against the same truth: how far the snapped point ends up from GNSS, versus
how far the unsnapped integrator was. A matcher that helps moves points toward the truth.

Run:  python -m eval.matcher_eval extracted_sessions/20260904_195146 --roads dataset/kgp_roads.csv
"""

from __future__ import annotations

import argparse
import csv
import heapq
import math
import os

import numpy as np

M_PER_DEG_LAT = 111_132.0

# --- ported verbatim from MapMatcher.kt so the two cannot drift apart silently ---
BASE_SEARCH_RADIUS_M = 60.0
MAX_SEARCH_RADIUS_M = 400.0
EMISSION_SIGMA_M = 30.0
TRANSITION_SIGMA_M = 25.0
HEADING_SIGMA_DEG = 35.0
BEAM = 12
MAX_CANDIDATES = 24
MIN_SPEED_FOR_HEADING = 1.5
RIVAL_BEARING_DEG = 30.0
MIN_CORRECTION_BUDGET_M = 12.0

# --- the proposed additions ---
# Slack on the route-distance comparison. The integrator's own step is itself uncertain, and
# a segment's discretisation means the route walk overshoots by up to a segment length.
ROUTE_SLACK_M = 30.0
# Log-probability charged to a candidate the graph cannot reach from a hypothesis. Large
# enough to lose to any reachable rival, finite so that a hole in the map cannot make every
# candidate impossible and strand the matcher.
UNREACHABLE_PENALTY = -12.0
# Ceiling on the correction budget regardless of how far the integrator has travelled. Roughly
# the local block size: beyond this a "correction" is a different road, not a lane offset.
MAX_CORRECTION_BUDGET_M = 35.0


def mlon(lat):
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def bearing_delta(a, b):
    return ((a - b + 180.0) % 360.0) - 180.0


def undirected_delta(a, b):
    d = abs(bearing_delta(a, b))
    return min(d, 180.0 - d)


class Roads:
    """Segments, a spatial index, and the endpoint-snapped topology."""

    CELL = 0.002
    SNAP_TOL_M = 0.5

    def __init__(self, path):
        self.segs, self.cls = [], []
        for r in csv.DictReader(open(path, encoding="utf-8")):
            a = (float(r["alat"]), float(r["alon"]))
            b = (float(r["blat"]), float(r["blon"]))
            if a == b:
                continue
            e = (b[1] - a[1]) * mlon((a[0] + b[0]) / 2)
            n = (b[0] - a[0]) * M_PER_DEG_LAT
            self.segs.append((a[0], a[1], b[0], b[1],
                              (math.degrees(math.atan2(e, n)) + 360) % 360))
            self.cls.append(r.get("cls", "road"))

        self.lens = [math.hypot((s[3] - s[1]) * mlon((s[0] + s[2]) / 2),
                                (s[2] - s[0]) * M_PER_DEG_LAT) for s in self.segs]

        # Index every cell a segment's bbox spans, not just its endpoints: a long segment can
        # pass beside the query point with both ends outside the search block.
        self.grid = {}
        for i, s in enumerate(self.segs):
            i0, i1 = sorted((int(s[0] / self.CELL), int(s[2] / self.CELL)))
            j0, j1 = sorted((int(s[1] / self.CELL), int(s[3] / self.CELL)))
            for ci in range(i0, i1 + 1):
                for cj in range(j0, j1 + 1):
                    self.grid.setdefault((ci, cj), set()).add(i)

        # Topology, by the RoadGraph.kt rule: coincident endpoints share an OSM node and come
        # back on identical microdegree coordinates, so a half-metre grid rebuilds the graph.
        q = self.SNAP_TOL_M / M_PER_DEG_LAT
        nodes = {}
        self.node_of = []
        self.at_node = {}
        for i, s in enumerate(self.segs):
            ends = []
            for lat, lon in ((s[0], s[1]), (s[2], s[3])):
                k = (round(lat / q), round(lon / q))
                if k not in nodes:
                    nodes[k] = len(nodes)
                ends.append(nodes[k])
            self.node_of.append(tuple(ends))
            self.at_node.setdefault(ends[0], []).append(i)
            self.at_node.setdefault(ends[1], []).append(i)
        self.n_nodes = len(nodes)

    def near(self, lat, lon, radius):
        """Projected candidates within `radius`, as (seg, lat, lon, dist, offset_from_a)."""
        ml = mlon(lat)
        ci, cj = int(lat / self.CELL), int(lon / self.CELL)
        seen = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                seen |= self.grid.get((ci + di, cj + dj), set())
        out = []
        for i in seen:
            alat, alon, blat, blon, _ = self.segs[i]
            ax, ay = (alon - lon) * ml, (alat - lat) * M_PER_DEG_LAT
            bx, by = (blon - lon) * ml, (blat - lat) * M_PER_DEG_LAT
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            if l2 < 1e-9:
                continue
            t = max(0.0, min(1.0, (-ax * dx - ay * dy) / l2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(px, py)
            if d <= radius:
                out.append((i, lat + py / M_PER_DEG_LAT, lon + px / ml, d, t * self.lens[i]))
        return out

    def node_distances(self, seg, off_from_a, limit_m):
        """Dijkstra from a point on `seg` to every node within `limit_m` of on-road travel.

        Bounded, so cost is set by the limit rather than by the size of the network: a step of
        a couple of seconds at road speed reaches only a handful of junctions.
        """
        a, b = self.node_of[seg]
        dist = {}
        pq = [(off_from_a, a), (max(self.lens[seg] - off_from_a, 0.0), b)]
        heapq.heapify(pq)
        while pq:
            d, n = heapq.heappop(pq)
            if n in dist or d > limit_m:
                continue
            dist[n] = d
            for j in self.at_node.get(n, ()):
                u, v = self.node_of[j]
                other = v if u == n else u
                nd = d + self.lens[j]
                if nd <= limit_m and other not in dist:
                    heapq.heappush(pq, (nd, other))
        return dist

    def route_distance(self, from_seg, from_off, to_seg, to_off, limit_m, cache):
        """On-road distance between two points, or inf beyond `limit_m`."""
        if from_seg == to_seg:
            return abs(to_off - from_off)
        key = (from_seg, round(from_off, 1))
        if key not in cache:
            cache[key] = self.node_distances(from_seg, from_off, limit_m)
        dist = cache[key]
        a, b = self.node_of[to_seg]
        best = math.inf
        if a in dist:
            best = min(best, dist[a] + to_off)
        if b in dist:
            best = min(best, dist[b] + max(self.lens[to_seg] - to_off, 0.0))
        return best


class Matcher:
    """MapMatcher.kt, with the two proposed changes behind flags."""

    def __init__(self, roads, connectivity=False, cap_budget=False, gate_m=0.0):
        self.roads = roads
        self.connectivity = connectivity
        self.cap_budget = cap_budget
        # Below this uncertainty the integrator is better than the map and snapping is a
        # downgrade: road centrelines sit a lane-width from the driven line and OSM geometry
        # carries its own error, so "correcting" a 5 m fix onto a road costs accuracy.
        self.gate_m = gate_m
        self.beam = []
        self.last = None

    def update(self, lat, lon, course, speed, uncertainty):
        if uncertainty < self.gate_m:
            return None
        radius = min(BASE_SEARCH_RADIUS_M + uncertainty, MAX_SEARCH_RADIUS_M)
        cands = sorted(self.roads.near(lat, lon, radius), key=lambda c: c[3])[:MAX_CANDIDATES]
        if not cands:
            self.beam, self.last = [], (lat, lon)
            return None

        step = 0.0 if self.last is None else math.hypot(
            (lon - self.last[1]) * mlon(lat), (lat - self.last[0]) * M_PER_DEG_LAT)
        use_heading = course is not None and speed >= MIN_SPEED_FOR_HEADING
        limit = step + ROUTE_SLACK_M + 2 * TRANSITION_SIGMA_M
        cache = {}

        nxt = []
        for seg, clat, clon, dist, off in cands:
            sc = -0.5 * (dist / EMISSION_SIGMA_M) ** 2
            if use_heading:
                sc += -0.5 * (undirected_delta(course, self.roads.segs[seg][4])
                              / HEADING_SIGMA_DEG) ** 2
            best = 0.0
            if self.beam:
                best = -math.inf
                for h in self.beam:
                    if self.connectivity:
                        rd = self.roads.route_distance(h["seg"], h["off"], seg, off, limit, cache)
                        if math.isinf(rd):
                            t = UNREACHABLE_PENALTY
                        else:
                            t = -0.5 * (abs(rd - step) / TRANSITION_SIGMA_M) ** 2
                    else:
                        hop = math.hypot((clon - h["lon"]) * mlon(clat),
                                         (clat - h["lat"]) * M_PER_DEG_LAT)
                        t = -0.5 * (abs(hop - step) / TRANSITION_SIGMA_M) ** 2
                    if h["seg"] == seg:
                        t += 0.5
                    best = max(best, h["lp"] + t)
            nxt.append({"lat": clat, "lon": clon, "seg": seg, "off": off, "lp": sc + best})

        top = max(n["lp"] for n in nxt)
        for n in nxt:
            n["lp"] -= top
        self.beam = sorted(nxt, key=lambda n: -n["lp"])[:BEAM]
        self.last = (lat, lon)

        head = self.beam[0]
        rival = next((h for h in self.beam
                      if undirected_delta(self.roads.segs[h["seg"]][4],
                                          self.roads.segs[head["seg"]][4]) > RIVAL_BEARING_DEG),
                     None)
        if rival is None:
            conf = 1.0
        else:
            h, r = math.exp(head["lp"]), math.exp(rival["lp"])
            conf = h / (h + r) if h + r > 0 else 0.0

        corr = math.hypot((head["lon"] - lon) * mlon(lat),
                          (head["lat"] - lat) * M_PER_DEG_LAT)
        budget = max(uncertainty, MIN_CORRECTION_BUDGET_M)
        if self.cap_budget:
            budget = min(budget, MAX_CORRECTION_BUDGET_M)
        if corr > budget:
            return None
        return {"lat": head["lat"], "lon": head["lon"], "corr": corr, "conf": conf,
                "cls": self.roads.cls[head["seg"]], "seg": head["seg"]}


def load_track(d):
    """Integrator track, its logged uncertainty, and GNSS truth on the same clock."""
    import pandas as pd
    dr = pd.read_csv(os.path.join(d, "deadreckon.csv"))
    gps = pd.read_csv(os.path.join(d, "gps.csv"))
    gps = gps[(gps.acc_m <= 30.0) & gps.speed_mps.notna()]
    t = dr.t_ns.values / 1e9
    tg = gps.t_ns.values / 1e9
    moving = gps.speed_mps.values > 2.0
    lo, hi = tg[moving][0], tg[moving][-1]
    keep = (t >= lo) & (t <= hi)
    return {
        "t": t[keep], "lat": dr.lat.values[keep], "lon": dr.lon.values[keep],
        "speed": dr.speed_mps.values[keep], "unc": dr.drift_m.values[keep],
        "glat": np.interp(t[keep], tg, gps.lat.values),
        "glon": np.interp(t[keep], tg, gps.lon.values),
    }


def replay(roads, tr, connectivity, cap_budget, interval=2.0, gate_m=0.0):
    m = Matcher(roads, connectivity, cap_budget, gate_m)
    out = []
    nxt = tr["t"][0]
    for i in range(1, len(tr["t"])):
        if tr["t"][i] < nxt:
            continue
        nxt = tr["t"][i] + interval
        course = None
        j = max(i - 10, 0)
        dy = (tr["lat"][i] - tr["lat"][j]) * M_PER_DEG_LAT
        dx = (tr["lon"][i] - tr["lon"][j]) * mlon(tr["lat"][i])
        if math.hypot(dx, dy) > 1.0:
            course = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        r = m.update(tr["lat"][i], tr["lon"][i], course, tr["speed"][i], tr["unc"][i])
        ml = mlon(tr["lat"][i])
        e_dr = math.hypot((tr["lon"][i] - tr["glon"][i]) * ml,
                          (tr["lat"][i] - tr["glat"][i]) * M_PER_DEG_LAT)
        if r is None:
            out.append({"i": i, "matched": False, "e_dr": e_dr, "e_snap": e_dr})
        else:
            e_sn = math.hypot((r["lon"] - tr["glon"][i]) * ml,
                              (r["lat"] - tr["glat"][i]) * M_PER_DEG_LAT)
            out.append({"i": i, "matched": True, "e_dr": e_dr, "e_snap": e_sn,
                        "corr": r["corr"], "conf": r["conf"], "seg": r["seg"],
                        "lat": r["lat"], "lon": r["lon"]})
    return out


# ---------------------------------------------------------------- outage simulation

def drifted_track(tr, i0, i1, drift_frac, sign=1.0):
    """The true track as a drifting integrator would have seen it over [i0, i1).

    Additive noise is the wrong model. The dominant dead-reckoning error is HEADING, and a
    heading error does not scatter the track, it bends it: the estimate stays the right
    length and curves away from the truth. So the outage is simulated by rotating the
    travelled path about the anchor at a constant yaw-rate error, chosen so the endpoint
    lands `drift_frac` of the distance travelled away from truth. That reproduces both the
    magnitude and the SHAPE of the error the matcher has to undo, and it is the shape that
    decides whether a road can be identified at all.
    """
    lat, lon = tr["glat"].copy(), tr["glon"].copy()
    ml = mlon(lat[i0])
    ax, ay = lon[i0] * ml, lat[i0] * M_PER_DEG_LAT
    x = lon[i0:i1] * ml - ax
    y = lat[i0:i1] * M_PER_DEG_LAT - ay
    n = i1 - i0
    if n < 2:
        return lat, lon
    step = np.hypot(np.diff(x), np.diff(y))
    dist = np.concatenate([[0.0], np.cumsum(step)])
    total = dist[-1]
    if total < 1.0:
        return lat, lon
    # Small-angle: a constant yaw-rate error theta(t) = k * s(t) puts the endpoint about
    # total * k * total / 2 off course, so solve k from the requested fraction.
    chord = np.hypot(x[-1], y[-1])
    if chord < 1.0:
        return lat, lon
    theta = sign * 2.0 * drift_frac * total / max(chord, 1.0) * (dist / max(total, 1e-9))
    c, sn = np.cos(theta), np.sin(theta)
    xr = c * x - sn * y
    yr = sn * x + c * y
    lon[i0:i1] = (xr + ax) / ml
    lat[i0:i1] = (yr + ay) / M_PER_DEG_LAT
    return lat, lon


def replay_outage(roads, tr, connectivity, cap_budget, dur_s, drift_frac,
                  interval=2.0, unc_frac=0.18, gate_m=0.0):
    """Snap a drifting track back onto the road, over every outage window of `dur_s`.

    The correction budget needs a real uncertainty, and there are three candidates:

      driftMetres           what the app passes. |position - anchor|, i.e. DISPLACEMENT. It
                            is ~5x too large during an outage and grows even when GNSS is
                            healthy, so it licenses corrections that are not warranted.
      k * elapsed seconds   dimensionally reasonable but wrong: a vehicle stopped at lights
                            accumulates budget while accumulating no error.
      k * distance driven   what this uses. Free-running drift is measured at 17-20% of
                            distance travelled, so uncertainty is `unc_frac` of the distance
                            since the outage began. It is the only one of the three that
                            tracks the error it is supposed to bound.
    """
    n = len(tr["t"])
    w = int(dur_s * 10)
    out = []
    for i0 in range(0, n - w, w):
        i1 = i0 + w
        lat, lon = drifted_track(tr, i0, i1, drift_frac, sign=1.0 if (i0 // w) % 2 == 0 else -1.0)
        m = Matcher(roads, connectivity, cap_budget, gate_m)
        nxt = tr["t"][i0]
        for i in range(i0, i1):
            if tr["t"][i] < nxt:
                continue
            nxt = tr["t"][i] + interval
            travelled = float(np.sum(np.abs(tr["speed"][i0:i + 1])) * 0.1)
            unc = max(unc_frac * travelled, MIN_CORRECTION_BUDGET_M)
            j = max(i - 10, i0)
            dy = (lat[i] - lat[j]) * M_PER_DEG_LAT
            dx = (lon[i] - lon[j]) * mlon(lat[i])
            course = ((math.degrees(math.atan2(dx, dy)) + 360) % 360
                      if math.hypot(dx, dy) > 1.0 else None)
            r = m.update(lat[i], lon[i], course, tr["speed"][i], unc)
            ml = mlon(lat[i])
            e_dr = math.hypot((lon[i] - tr["glon"][i]) * ml,
                              (lat[i] - tr["glat"][i]) * M_PER_DEG_LAT)
            if r is None:
                out.append({"i": i, "matched": False, "e_dr": e_dr, "e_snap": e_dr})
            else:
                e_sn = math.hypot((r["lon"] - tr["glon"][i]) * ml,
                                  (r["lat"] - tr["glat"][i]) * M_PER_DEG_LAT)
                out.append({"i": i, "matched": True, "e_dr": e_dr, "e_snap": e_sn,
                            "corr": r["corr"], "conf": r["conf"], "seg": r["seg"]})
    return out


def summarise(name, res, roads):
    if not res:
        print(f"  {name:34s} no windows")
        return {}
    e_dr = np.array([r["e_dr"] for r in res])
    e_sn = np.array([r["e_snap"] for r in res])
    matched = np.array([r["matched"] for r in res])
    helped = np.mean(e_sn[matched] < e_dr[matched]) * 100 if matched.any() else float("nan")

    # The screenshot symptom: consecutive accepted matches that land on segments the graph
    # cannot connect. That is a physically impossible vehicle path, whatever it costs in metres.
    jumps = 0
    pairs = 0
    ms = [r for r in res if r["matched"]]
    for a, b in zip(ms, ms[1:]):
        if b["i"] - a["i"] > 40:
            continue
        pairs += 1
        if a["seg"] == b["seg"]:
            continue
        d = roads.node_distances(a["seg"], 0.0, 150.0)
        u, v = roads.node_of[b["seg"]]
        if u not in d and v not in d:
            jumps += 1

    print(f"  {name:34s} matched {int(matched.sum()):3d}/{len(res):3d}  "
          f"mean err {e_sn.mean():6.2f} m (DR {e_dr.mean():6.2f})  "
          f"p90 {np.percentile(e_sn, 90):6.2f}  helped {helped:4.0f}%  "
          f"disconnected hops {jumps}/{pairs}")
    return {"err": e_sn.mean(), "jumps": jumps, "pairs": pairs}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--roads", default="dataset/kgp_roads.csv")
    args = ap.parse_args(argv)

    roads = Roads(args.roads)
    print(f"roads: {len(roads.segs)} segments, {roads.n_nodes} nodes")
    tr = load_track(args.session)
    print(f"track: {len(tr['t'])} samples over {tr['t'][-1] - tr['t'][0]:.0f} s of driving\n")

    print("A. as recorded - GNSS healthy throughout, so the integrator is already good")
    for name, conn, cap, gate in (("baseline (shipped)", False, False, 0.0),
                                  ("+ connectivity", True, False, 0.0),
                                  ("+ capped budget", False, True, 0.0),
                                  ("+ gate at 15 m", False, False, 15.0),
                                  ("+ gate at 25 m", False, False, 25.0)):
        summarise(name, replay(roads, tr, conn, cap, gate_m=gate), roads)

    # The matcher's whole purpose is the outage, and this session never has a real one, so
    # measuring only case A measures the matcher outside its operating range.
    for dur, frac in ((60, 0.18), (120, 0.18), (300, 0.18)):
        print(f"{chr(10)}B. simulated {dur} s outage, heading-error drift to "
              f"{frac * 100:.0f}% of distance travelled")
        for name, conn, cap, gate in (("baseline (shipped)", False, False, 0.0),
                                      ("+ connectivity", True, False, 0.0),
                                      ("+ gate at 15 m", False, False, 15.0),
                                      ("+ connectivity + gate", True, False, 15.0)):
            summarise(name, replay_outage(roads, tr, conn, cap, dur, frac, gate_m=gate), roads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
