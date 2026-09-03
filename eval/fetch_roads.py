"""Pull a drivable road network out of OpenStreetMap in the CSV the matcher reads.

Everything in `eval/` that does map matching - outage_eval.Roads and
alongroad.RoadGraph - wants the same five columns, one row per straight piece of
a way:

    alat,alon,blat,blon,cls

On the phone those rows come out of the Mapsforge .map file (RoadNetwork.kt).
There is no Mapsforge map for the IO-VNBD driving area, and the evaluation only
needs the geometry, not a renderable map, so this fetches the same content from
Overpass instead. The road classes below are copied from RoadNetwork.DRIVABLE so
the Python harness and the app see the same network and not two different ones.

The part that matters is *which* area to ask for. IO-VNBD's ten accepted runs
span roughly 0.2 x 0.37 degrees, but the driving inside that span is a handful of
routes: only 92 of the 220 cells of a 0.02-degree grid are ever entered. So the
track is reduced to the cells it actually passes through, those cells are merged
into a small number of rectangles, and each rectangle is one request - 13 boxes
covering 0.050 deg^2 against the enclosing rectangle's 0.075 deg^2, margins
included. The saving in area is a third; the saving in wall clock is larger,
because one request that size is what makes the public endpoint start answering
429 and 504.

Ways are selected by "at least one node inside the box", and `out geom` returns
the *whole* way, not the part inside the box. That is deliberate: geometry is
never cut at a box edge, so a road crossing between two rectangles still ends up
as one continuous chain of segments and the topology alongroad.RoadGraph rebuilds
by snapping endpoints is not broken along seams.

Responses are cached per bounding box under dataset/overpass_cache/, so an
interrupted run resumes instead of re-fetching, and a re-run with a slightly
different margin only pays for the boxes that changed.

Run:  python -m eval.fetch_roads --npz dataset/iovnbd_train.npz \
                                 --out dataset/uk_roads.csv
      python -m eval.fetch_roads --bbox 22.28,87.28,22.36,87.36 \
                                 --out dataset/kgp_roads.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# RoadNetwork.kt's DRIVABLE, verbatim. Anything a car cannot legally drive on -
# footway, cycleway, path, steps, track - is not here, because a candidate the
# vehicle could never have been on only ever costs the matcher accuracy.
DRIVABLE = (
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "road",
)

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"

# One cell is ~2.2 km north-south. Fine enough that unvisited country is dropped,
# coarse enough that a cell is never entered without its roads being wanted.
DEFAULT_CELL_DEG = 0.02

# Boxes are grown by this much so a track running along a cell edge still gets
# the roads on the far side. ~330 m north-south, which comfortably covers the
# 400 m search radius outage_eval uses at its widest.
DEFAULT_MARGIN_DEG = 0.003

# Overpass answers a request of this size in seconds; much larger and it starts
# timing out and being retried, which is slower overall than splitting up front.
DEFAULT_MAX_BOX_DEG2 = 0.02

DEFAULT_SLEEP_S = 3.0
DEFAULT_TIMEOUT_S = 180
DEFAULT_RETRIES = 5


# ------------------------------------------------------------------ the area

def track_cells(lats, lons, cell_deg):
    """The set of grid cells the driven track passes through."""
    return {(int(la // cell_deg), int(lo // cell_deg))
            for la, lo in zip(lats, lons)}


def merge_cells(cells, cell_deg):
    """Cover the cells with as few rectangles as possible.

    Contiguous runs within a row first, then rows stacked where their runs line
    up exactly. That is not the minimal rectangle cover, but it is deterministic
    and on the IO-VNBD tracks it turns 92 cells into 13 requests, which is
    already well past the point where request count stops mattering.
    """
    runs = {}                                   # row -> [(col_start, col_end)]
    for i in sorted({c[0] for c in cells}):
        cols = sorted(c[1] for c in cells if c[0] == i)
        row, start, prev = [], cols[0], cols[0]
        for j in cols[1:]:
            if j != prev + 1:
                row.append((start, prev))
                start = j
            prev = j
        row.append((start, prev))
        runs[i] = row

    boxes, used = [], set()
    for i in sorted(runs):
        for r in runs[i]:
            if (i, r) in used:
                continue
            top = i
            while (top + 1) in runs and r in runs[top + 1] and (top + 1, r) not in used:
                top += 1
                used.add((top, r))
            boxes.append((i * cell_deg, r[0] * cell_deg,
                          (top + 1) * cell_deg, (r[1] + 1) * cell_deg))
    return boxes


def split_large(box, max_deg2):
    """Halve a box along its longer side until each piece is small enough."""
    s, w, n, e = box
    if (n - s) * (e - w) <= max_deg2:
        return [box]
    if (n - s) >= (e - w):
        mid = (s + n) / 2
        return split_large((s, w, mid, e), max_deg2) + split_large((mid, w, n, e), max_deg2)
    mid = (w + e) / 2
    return split_large((s, w, n, mid), max_deg2) + split_large((s, mid, n, e), max_deg2)


def plan_boxes(lats, lons, cell_deg, margin_deg, max_deg2):
    """Request boxes covering the track, in south-to-north order."""
    boxes = merge_cells(track_cells(lats, lons, cell_deg), cell_deg)
    out = []
    for (s, w, n, e) in boxes:
        out += split_large((s - margin_deg, w - margin_deg,
                            n + margin_deg, e + margin_deg), max_deg2)
    return sorted(out)


# --------------------------------------------------------------- the fetching

def build_query(box, timeout_s):
    s, w, n, e = box
    classes = "|".join(DRIVABLE)
    # area=yes marks pedestrian squares and parking aisles drawn as polygons;
    # their "segments" are the outline of a shape, not a line a car drives along.
    return (f"[out:json][timeout:{timeout_s}];"
            f'way["highway"~"^({classes})$"]["area"!="yes"]'
            f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});"
            f"out geom;")


def cache_path(cache_dir, box, query):
    # The query text is part of the key: change the class list and the cached
    # answer is no longer an answer to the question being asked.
    tag = hashlib.sha1(query.encode()).hexdigest()[:8]
    s, w, n, e = box
    return os.path.join(cache_dir,
                        f"{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}_{tag}.json")


def overpass(query, endpoint, timeout_s, retries, sleep_s):
    """POST one query, backing off on the failures Overpass actually returns.

    429 (too many requests) and 504 (gateway timeout) are the server saying
    "later", not "no", so they are retried with a doubling wait. Anything else
    is raised: silently returning no roads for a box would look like an area
    with no roads in it, which is the one failure this script must not hide.
    """
    body = urllib.parse.urlencode({"data": query}).encode()
    wait = sleep_s
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                endpoint, data=body,
                headers={"User-Agent": "SIH2026-eval fetch_roads.py",
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout_s + 60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", None)
            if code is not None and code not in (429, 502, 503, 504):
                raise
            if attempt == retries - 1:
                raise
            print(f"    {exc} - retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
            wait *= 2
    raise RuntimeError("unreachable")


def fetch_box(box, cache_dir, endpoint, timeout_s, retries, sleep_s):
    """One box's ways, from the cache when it is there."""
    query = build_query(box, timeout_s)
    path = cache_path(cache_dir, box, query)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), True
    payload = overpass(query, endpoint, timeout_s, retries, sleep_s)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return payload, False


# --------------------------------------------------------------- the segments

def ways_to_segments(payload, seen_ids):
    """Split each way's polyline into the straight pieces the matcher stores.

    Boxes overlap by their margin and a way is returned whole by every box it
    touches, so the same way arrives several times; keying on the OSM way id
    drops those repeats exactly, without any coordinate comparison.
    """
    out = []
    for el in payload.get("elements", []):
        if el.get("type") != "way" or el["id"] in seen_ids:
            continue
        cls = el.get("tags", {}).get("highway")
        if cls not in DRIVABLE:
            continue
        seen_ids.add(el["id"])
        geom = el.get("geometry") or []
        for a, b in zip(geom, geom[1:]):
            # Zero-length pieces carry no bearing; Roads drops them on load, so
            # there is no reason to write them out.
            if a["lat"] == b["lat"] and a["lon"] == b["lon"]:
                continue
            out.append((a["lat"], a["lon"], b["lat"], b["lon"], cls))
    return out


def write_csv(path, segs):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["alat", "alon", "blat", "blon", "cls"])
        for (alat, alon, blat, blon, cls) in segs:
            # 7 decimals is ~1 cm, past the precision of OSM's own data, and it
            # keeps endpoints bit-identical where two ways shared an OSM node -
            # which is what RoadGraph's 0.5 m endpoint snapping relies on.
            w.writerow([f"{alat:.7f}", f"{alon:.7f}",
                        f"{blat:.7f}", f"{blon:.7f}", cls])


# --------------------------------------------------------------------- driver

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--npz", help="converter output; cells come from "
                                   "truth_lat/truth_lon")
    src.add_argument("--bbox", help="south,west,north,east instead of a track")
    p.add_argument("--out", required=True, help="destination CSV")
    p.add_argument("--cache", default=os.path.join("dataset", "overpass_cache"))
    p.add_argument("--cell", type=float, default=DEFAULT_CELL_DEG)
    p.add_argument("--margin", type=float, default=DEFAULT_MARGIN_DEG)
    p.add_argument("--max-box", type=float, default=DEFAULT_MAX_BOX_DEG2,
                   help="split any request box larger than this many deg^2")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S,
                   help="pause between requests that actually hit the server")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    a = p.parse_args(argv)

    if a.npz:
        import numpy as np
        d = np.load(a.npz, allow_pickle=True)
        lats, lons = d["truth_lat"], d["truth_lon"]
        ok = np.isfinite(lats) & np.isfinite(lons)
        boxes = plan_boxes(lats[ok], lons[ok], a.cell, a.margin, a.max_box)
        print(f"{ok.sum()} track samples -> {len(boxes)} request boxes")
    else:
        s, w, n, e = (float(x) for x in a.bbox.split(","))
        boxes = split_large((s, w, n, e), a.max_box)
        print(f"bbox -> {len(boxes)} request boxes")

    segs, seen_ids = [], set()
    for i, box in enumerate(boxes, 1):
        payload, cached = fetch_box(box, a.cache, a.endpoint, a.timeout,
                                    a.retries, a.sleep)
        new = ways_to_segments(payload, seen_ids)
        segs += new
        s, w, n, e = box
        print(f"  [{i}/{len(boxes)}] {s:.3f},{w:.3f},{n:.3f},{e:.3f}  "
              f"{len(payload.get('elements', [])):5d} ways  "
              f"+{len(new):6d} segments  {'(cached)' if cached else ''}",
              flush=True)
        if not cached and i < len(boxes):
            time.sleep(a.sleep)

    write_csv(a.out, segs)
    print(f"{len(segs)} segments from {len(seen_ids)} ways -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
