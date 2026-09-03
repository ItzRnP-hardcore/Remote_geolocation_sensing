"""Score the app's dead reckoning over simulated GNSS outages.

Mirrors the harness contract in pranjali2105/SIH_2026: a predictor returns
per-second displacements and headings, and *this* file accumulates position, so
every predictor is scored on identical arithmetic and the numbers line up with
hers in definition even though the dataset differs.

Why a separate harness rather than reusing hers directly: IO-VNBD carries
vehicle CAN and GPS but no phone IMU — no accelerometer triad, no rotation
vector — so the app's DeadReckoner, which levels a handset accelerometer into
ENU, has no inputs there. These sessions are the only data it can run on.

Predictors here replay the real recorded IMU through a Python mirror of
DeadReckoner.kt and MapMatcher.kt, anchoring on the GPS fix at the outage start
and then free-running, exactly as the app does when GNSS drops.

Run:  python -m eval.outage_eval <sessions_dir> [roads.csv]
"""

from __future__ import annotations

import bisect
import csv
import math
import os
import sys
from dataclasses import dataclass

import numpy as np

from .metrics import geodesic_distance_m, summarise

# Hers: GPS heading is meaningless at rest and the paper's scenarios are all
# in-motion. Our recordings are walking/cycling pace, so the same 5.0 m/s gate
# would skip every outage we have — both thresholds are reported rather than
# quietly moving the goalposts.
STRICT_MIN_START_SPEED_MS = 5.0
RELAXED_MIN_START_SPEED_MS = 1.5

M_PER_DEG_LAT = 111_132.0

# DeadReckoner.kt
G_NOMINAL, K_GRAVITY, K_BIAS = 9.80665, 0.02, 0.02
STILL_ACCEL_TOL, STILL_GYRO_TOL, STILL_HOLD_S, MAX_DT_S = 0.15, 0.05, 0.5, 0.05
MIN_SPEED_FOR_HEADING_FIX, MAX_HEADING_STEP_DEG = 2.0, 4.0

# MapMatcher.kt
ES, TS, HS, BEAM, MAXC = 30.0, 25.0, 35.0, 12, 24
MIN_BUDGET, MIN_CONF, GAIN, RIVAL_DEG = 12.0, 0.6, 0.35, 30.0


class OutageSkipped(RuntimeError):
    pass


def mlon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def local_metres(a_lat, a_lon, b_lat, b_lon):
    ml = mlon((a_lat + b_lat) / 2)
    return math.hypot((b_lon - a_lon) * ml, (b_lat - a_lat) * M_PER_DEG_LAT)


def signed_delta(target, frm):
    d = (target - frm) % 360.0
    if d > 180:
        d -= 360.0
    if d < -180:
        d += 360.0
    return d


def undirected_delta(a, b):
    d = abs(signed_delta(a, b))
    return 180 - d if d > 90 else d


# --------------------------------------------------------------------- roads

class Roads:
    """Drivable segments, from the CSV dumped out of the Mapsforge map."""

    CELL = 0.002

    def __init__(self, path: str | None):
        self.segs, self.grid = [], {}
        if not path or not os.path.exists(path):
            return
        for r in csv.DictReader(open(path)):
            a = (float(r["alat"]), float(r["alon"]))
            b = (float(r["blat"]), float(r["blon"]))
            if a == b:
                continue
            e = (b[1] - a[1]) * mlon((a[0] + b[0]) / 2)
            n = (b[0] - a[0]) * M_PER_DEG_LAT
            self.segs.append((a[0], a[1], b[0], b[1],
                              (math.degrees(math.atan2(e, n)) + 360) % 360))
        # Index every cell a segment's bounding box touches, not just the cells its
        # endpoints land in. Endpoint indexing silently loses long segments: a 1.3 km
        # motorway link can pass metres from a query point with both ends outside the
        # 3x3 search block, and near() would never consider it. Measured on the UK
        # network, that produced 46 false outliers in one run, the index reporting
        # 199.5 m where a brute-force scan gives 2.2 m.
        for i, s in enumerate(self.segs):
            i0, i1 = sorted((int(s[0] / self.CELL), int(s[2] / self.CELL)))
            j0, j1 = sorted((int(s[1] / self.CELL), int(s[3] / self.CELL)))
            for ci in range(i0, i1 + 1):
                for cj in range(j0, j1 + 1):
                    self.grid.setdefault((ci, cj), set()).add(i)

    def __bool__(self):
        return bool(self.segs)

    def near(self, lat, lon, radius):
        ml = mlon(lat)
        ci, cj = int(lat / self.CELL), int(lon / self.CELL)
        seen = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                seen |= self.grid.get((ci + di, cj + dj), set())
        out = []
        for i in seen:
            alat, alon, blat, blon, brg = self.segs[i]
            ax, ay = (alon - lon) * ml, (alat - lat) * M_PER_DEG_LAT
            bx, by = (blon - lon) * ml, (blat - lat) * M_PER_DEG_LAT
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            if l2 < 1e-9:
                continue
            t = max(0.0, min(1.0, ((-ax) * dx + (-ay) * dy) / l2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(px, py)
            if d <= radius:
                out.append((d, lat + py / M_PER_DEG_LAT, lon + px / ml, brg, i))
        out.sort()
        return out[:MAXC]


# ------------------------------------------------------------------- session

@dataclass
class Session:
    name: str
    events: list          # (t_ns, kind, values) sorted, kind in accel/gyro/rv
    gt: list              # GPS t_ns
    glat: list
    glon: list
    gspd: list


def load(session_dir: str) -> Session:
    events = []
    with open(os.path.join(session_dir, "imu.csv")) as f:
        for r in csv.DictReader(f):
            s = r["sensor"]
            if s == "accel" or s == "gyro":
                events.append((int(r["t_ns"]), s,
                               (float(r["v0"]), float(r["v1"]), float(r["v2"]))))
            elif s == "rv":
                w = float(r["v3"]) if r["v3"] != "" else None
                events.append((int(r["t_ns"]), "rv",
                               (float(r["v0"]), float(r["v1"]), float(r["v2"]), w)))
    events.sort(key=lambda e: e[0])

    gt, glat, glon, gspd = [], [], [], []
    with open(os.path.join(session_dir, "gps.csv")) as f:
        for r in csv.DictReader(f):
            gt.append(int(r["t_ns"]))
            glat.append(float(r["lat"]))
            glon.append(float(r["lon"]))
            gspd.append(float(r["speed_mps"]) if r.get("speed_mps") else 0.0)
    return Session(os.path.basename(session_dir), events, gt, glat, glon, gspd)


def rot_from_rv(x, y, z, w):
    """Android SensorManager.getRotationMatrixFromVector, row-major."""
    if w is None:
        t = 1.0 - x * x - y * y - z * z
        w = math.sqrt(t) if t > 0 else 0.0
    q1, q2, q3, q0 = x, y, z, w
    sq1, sq2, sq3 = 2 * q1 * q1, 2 * q2 * q2, 2 * q3 * q3
    q1q2, q3q0 = 2 * q1 * q2, 2 * q3 * q0
    q1q3, q2q0 = 2 * q1 * q3, 2 * q2 * q0
    q2q3, q1q0 = 2 * q2 * q3, 2 * q1 * q0
    return (1 - sq2 - sq3, q1q2 - q3q0, q1q3 + q2q0,
            q1q2 + q3q0, 1 - sq1 - sq3, q2q3 - q1q0,
            q1q3 - q2q0, q2q3 + q1q0, 1 - sq1 - sq2)


# ----------------------------------------------------------------- predictor

def predict(session: Session, t0_ns: int, duration_s: int,
            roads: Roads, feedback: bool):
    """Free-run the integrator from the fix at t0 and report per-second motion.

    Returns (displacements, headings) with one entry per second, or raises
    OutageSkipped when the window cannot be integrated.
    """
    i0 = bisect.bisect_left(session.gt, t0_ns)
    if i0 >= len(session.gt):
        raise OutageSkipped("outage starts after the last fix")

    olat, olon = session.glat[i0], session.glon[i0]
    phi = math.radians(olat)
    m_lat = 111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    m_lon = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)

    pE = pN = 0.0
    spd0 = session.gspd[i0]
    # Seed the velocity from the GPS course at the outage start, as anchorTo does.
    if i0 + 1 < len(session.gt):
        brg = math.atan2((session.glon[i0 + 1] - olon) * m_lon,
                         (session.glat[i0 + 1] - olat) * m_lat)
    else:
        brg = 0.0
    vE, vN, vU = spd0 * math.sin(brg), spd0 * math.cos(brg), 0.0
    bE = bN = bU = 0.0
    grav, still, gyron, lastT, R = G_NOMINAL, 0.0, 0.0, 0, None
    beam, last_match, last_snap = [], None, 0

    end_ns = t0_ns + duration_s * 1_000_000_000
    marks = [t0_ns + k * 1_000_000_000 for k in range(1, duration_s + 1)]
    out_pos, mi = [], 0

    for t, kind, v in session.events:
        if t < t0_ns:
            if kind == "rv":
                R = rot_from_rv(*v)
            continue
        if t > end_ns + 100_000_000:
            break

        if kind == "rv":
            R = rot_from_rv(*v)
        elif kind == "gyro":
            gyron = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        else:
            if R is None:
                continue
            if lastT == 0:
                lastT = t
                continue
            dt = (t - lastT) / 1e9
            lastT = t
            if dt <= 0 or dt > MAX_DT_S:
                continue
            ax, ay, az = v
            aE = R[0] * ax + R[1] * ay + R[2] * az
            aN = R[3] * ax + R[4] * ay + R[5] * az
            aU = R[6] * ax + R[7] * ay + R[8] * az
            norm = math.sqrt(ax * ax + ay * ay + az * az)
            still = still + dt if (abs(norm - grav) < STILL_ACCEL_TOL
                                   and gyron < STILL_GYRO_TOL) else 0.0
            stationary = still > STILL_HOLD_S
            lE, lN, lU = aE - bE, aN - bN, aU - grav - bU
            if stationary:
                grav += K_GRAVITY * (norm - grav)
                bE += K_BIAS * lE
                bN += K_BIAS * lN
                bU += K_BIAS * lU
                vE = vN = vU = 0.0
                lE = lN = lU = 0.0
            vE += lE * dt
            vN += lN * dt
            vU += lU * dt
            pE += vE * dt
            pN += vN * dt

            if feedback and roads and t - last_snap >= 400_000_000:
                last_snap = t
                lat, lon = olat + pN / m_lat, olon + pE / m_lon
                drift = math.hypot(pE, pN)
                spd = math.hypot(vE, vN)
                cands = roads.near(lat, lon, min(60.0 + drift, 400.0))
                if cands:
                    course = ((math.degrees(math.atan2(vE, vN)) + 360) % 360
                              if spd > 0.3 else None)
                    step = 0.0 if last_match is None else local_metres(
                        last_match[0], last_match[1], lat, lon)
                    new = []
                    for dd, clat, clon, cbrg, si in cands:
                        sc = -0.5 * (dd / ES) ** 2
                        if course is not None and spd >= 1.5:
                            x = undirected_delta(course, cbrg)
                            sc += -0.5 * (x / HS) ** 2
                        best = 0.0
                        if beam:
                            best = max(h[3]
                                       - 0.5 * (abs(local_metres(h[0], h[1], clat, clon) - step) / TS) ** 2
                                       + (0.5 if h[2] == si else 0.0) for h in beam)
                        new.append((clat, clon, si, sc + best, cbrg))
                    top = max(h[3] for h in new)
                    beam = sorted([(a, b, c, s - top, g) for a, b, c, s, g in new],
                                  key=lambda h: -h[3])[:BEAM]
                    head = beam[0]
                    rival = next((h for h in beam
                                  if undirected_delta(h[4], head[4]) > RIVAL_DEG), None)
                    if rival is None:
                        conf = 1.0
                    else:
                        hh, rr = math.exp(head[3]), math.exp(rival[3])
                        conf = hh / (hh + rr) if hh + rr > 0 else 0.0
                    corr = local_metres(lat, lon, head[0], head[1])
                    if corr <= max(drift, MIN_BUDGET) and conf >= MIN_CONF \
                            and spd >= MIN_SPEED_FOR_HEADING_FIX:
                        c2 = (math.degrees(math.atan2(vE, vN)) + 360) % 360
                        f1, b1 = signed_delta(head[4], c2), signed_delta(head[4] + 180, c2)
                        dlt = f1 if abs(f1) <= abs(b1) else b1
                        a2 = max(-MAX_HEADING_STEP_DEG,
                                 min(MAX_HEADING_STEP_DEG, GAIN * dlt))
                        cc = math.radians(c2 + a2)
                        vE, vN = spd * math.sin(cc), spd * math.cos(cc)
                    last_match = (lat, lon)

            while mi < len(marks) and t >= marks[mi]:
                out_pos.append((pE, pN))
                mi += 1

    # The loop stops at the last event inside the window, which can fall a few
    # milliseconds before the final one-second mark. Carrying the last position
    # forward across that gap is exact to well under a centimetre at these
    # speeds; anything larger than one sample period is a real shortfall.
    if len(out_pos) == duration_s - 1 and out_pos:
        out_pos.append(out_pos[-1])
    if len(out_pos) < duration_s:
        raise OutageSkipped(
            f"integrator produced {len(out_pos)} of {duration_s} seconds")

    disp, head = [], []
    prevE = prevN = 0.0
    for (e, n) in out_pos[:duration_s]:
        de, dn = e - prevE, n - prevN
        disp.append(math.hypot(de, dn))
        head.append(math.atan2(de, dn))
        prevE, prevN = e, n
    return np.array(disp), np.array(head)


# -------------------------------------------------------------------- scoring

def score_outage(session: Session, t0_ns: int, duration_s: int,
                 roads: Roads, feedback: bool, min_speed: float) -> dict:
    i0 = bisect.bisect_left(session.gt, t0_ns)
    if i0 >= len(session.gt) - 1:
        raise OutageSkipped("no fixes after t0")
    start_speed = session.gspd[i0]
    if not math.isfinite(start_speed) or start_speed < min_speed:
        raise OutageSkipped(f"start speed {start_speed:.2f} m/s below {min_speed}")

    marks = [t0_ns + k * 1_000_000_000 for k in range(duration_s + 1)]
    if marks[-1] > session.gt[-1]:
        raise OutageSkipped("outage window falls outside the session")

    gt = np.array(session.gt, dtype=float)
    tlat = np.interp(marks, gt, session.glat)
    tlon = np.interp(marks, gt, session.glon)
    true_step = geodesic_distance_m(tlat[:-1], tlon[:-1], tlat[1:], tlon[1:])

    disp, head = predict(session, t0_ns, duration_s, roads, feedback)

    north = np.cumsum(disp * np.cos(head))
    east = np.cumsum(disp * np.sin(head))
    lat0, lon0 = tlat[0], tlon[0]
    mlat = geodesic_distance_m(lat0, lon0, lat0 + 1e-4, lon0) / 1e-4
    mlonn = geodesic_distance_m(lat0, lon0, lat0, lon0 + 1e-4) / 1e-4
    true_north = (tlat[1:] - lat0) * mlat
    true_east = (tlon[1:] - lon0) * mlonn
    position_errors = np.sqrt((north - true_north) ** 2 + (east - true_east) ** 2)

    m = summarise(disp - true_step, position_errors)
    m["start_speed_ms"] = start_speed
    m["true_distance_m"] = float(np.sum(true_step))
    return m


def run(sessions_dir: str, roads_csv: str | None):
    roads = Roads(roads_csv)
    sessions = []
    for name in sorted(os.listdir(sessions_dir)):
        d = os.path.join(sessions_dir, name)
        if os.path.isfile(os.path.join(d, "imu.csv")):
            try:
                s = load(d)
                if len(s.gt) >= 20:
                    sessions.append(s)
            except Exception as exc:
                print(f"  skipping {name}: {exc}")

    print(f"sessions: {len(sessions)}   roads: "
          f"{'loaded ' + str(len(roads.segs)) + ' segments' if roads else 'NONE (map matching disabled)'}")
    print()

    for min_speed, label in ((STRICT_MIN_START_SPEED_MS, "her 5.0 m/s gate"),
                             (RELAXED_MIN_START_SPEED_MS, "relaxed 1.5 m/s gate")):
        print(f"=== start-speed gate: {label} ===")
        header = f"{'predictor':<24}{'dur':>5}{'n':>5}{'CRSE mean':>11}{'median':>9}{'AEPS':>8}{'|CAE|/CRSE':>12}"
        print(header)
        any_row = False
        for duration in (10, 30, 60):
            for feedback in (False, True):
                if feedback and not roads:
                    continue
                rows = []
                for s in sessions:
                    t0 = s.gt[0]
                    while t0 + duration * 1_000_000_000 <= s.gt[-1]:
                        try:
                            rows.append(score_outage(s, t0, duration, roads,
                                                     feedback, min_speed))
                        except OutageSkipped:
                            pass
                        t0 += duration * 1_000_000_000
                if not rows:
                    continue
                any_row = True
                crses = [r["crse"] for r in rows]
                name = "dr + map heading" if feedback else "dead reckoning"
                ratio = (abs(np.mean([r["cae"] for r in rows])) / np.mean(crses)
                         if np.mean(crses) else float("nan"))
                print(f"{name:<24}{duration:>4}s{len(rows):>5}{np.mean(crses):>11.1f}"
                      f"{np.median(crses):>9.1f}{np.mean([r['aeps'] for r in rows]):>8.2f}"
                      f"{ratio:>12.2f}")
        if not any_row:
            print("  (every outage skipped at this threshold)")
        print()


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
