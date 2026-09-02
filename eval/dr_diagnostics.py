"""Why does the integrator come up 37% short on distance?

Free-running over a 60 s outage, DeadReckoner accumulates about 37% less
distance than the vehicle actually travelled - 74 m short over 60 s. That
shortfall is the single largest error source in the system and the reason
AlongRoadTracker ships disabled, since along-road tracking converts it directly
into along-track error with no cross-track saving left to pay for it.

This measures where it goes. Three candidates, all cheap to separate:

  ZUPT firing during real motion
      Stand-still is detected from |‖a‖ - g| and ‖ω‖ over a half-second hold.
      With a phone at walking pace the acceleration norm sits near g for long
      stretches, so the gate may be latching while the vehicle is genuinely
      moving - and a zero-velocity update during motion does not merely fail to
      help, it deletes velocity that was correct.

  Samples dropped by the dt gate
      Anything over MAX_DT_S is discarded as a FIFO stall. Every drop is motion
      that is never integrated, so a high drop rate is distance thrown away.

  Bias learning poisoned by false stand-still
      The bias and gravity estimators only update when stationary. If they are
      updating during motion they absorb real acceleration into the bias, which
      then gets subtracted from every subsequent sample.

Run:  python -m eval.dr_diagnostics <sessions_dir>
"""

from __future__ import annotations

import bisect
import math
import os
import sys

import numpy as np

from .metrics import geodesic_distance_m
from .outage_eval import (M_PER_DEG_LAT, G_NOMINAL, K_BIAS, K_GRAVITY, MAX_DT_S,
                          STILL_ACCEL_TOL, STILL_GYRO_TOL, STILL_HOLD_S,
                          load, rot_from_rv)

# Speed above which GPS is unambiguously reporting motion, m/s. Below this a
# stand-still call is at worst harmless, so it is excluded from the false-ZUPT
# count rather than being allowed to flatter or damn the gate.
MOVING_MS = 1.0


def integrate(session, t0_ns, t1_ns, accel_tol=STILL_ACCEL_TOL,
              gyro_tol=STILL_GYRO_TOL, hold_s=STILL_HOLD_S,
              max_dt=MAX_DT_S, zupt=True, seed=None):
    """Free-run the integrator, returning per-sample traces.

    A faithful mirror of DeadReckoner.onAccel with the stand-still gate exposed
    as parameters, so the thresholds can be swept without touching the app.
    """
    vE, vN = seed if seed else (0.0, 0.0)
    vU = 0.0
    bE = bN = bU = 0.0
    grav, still, gyron, lastT, R = G_NOMINAL, 0.0, 0.0, 0, None
    dropped = total = 0
    distance = 0.0

    t_out, spd_out, still_out = [], [], []

    for t, kind, v in session.events:
        if t > t1_ns:
            break
        if kind == "rv":
            R = rot_from_rv(*v)
            continue
        if kind == "gyro":
            gyron = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
            continue
        if t < t0_ns or R is None:
            continue
        if lastT == 0:
            lastT = t
            continue

        dt = (t - lastT) / 1e9
        lastT = t
        total += 1
        if dt <= 0 or dt > max_dt:
            dropped += 1
            continue

        ax, ay, az = v
        aE = R[0] * ax + R[1] * ay + R[2] * az
        aN = R[3] * ax + R[4] * ay + R[5] * az
        aU = R[6] * ax + R[7] * ay + R[8] * az
        norm = math.sqrt(ax * ax + ay * ay + az * az)

        moving_gate = abs(norm - grav) < accel_tol and gyron < gyro_tol
        still = still + dt if moving_gate else 0.0
        stationary = zupt and still > hold_s

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
        speed = math.hypot(vE, vN)
        distance += speed * dt

        t_out.append(t)
        spd_out.append(speed)
        still_out.append(1.0 if stationary else 0.0)

    return {
        "t": np.array(t_out, dtype=float),
        "speed": np.array(spd_out),
        "stationary": np.array(still_out),
        "distance_m": distance,
        "dropped": dropped,
        "total": total,
    }


def true_distance(session, t0_ns, t1_ns):
    """Geodesic path length of the GPS track over the window."""
    t = np.array(session.gt, dtype=float)
    keep = (t >= t0_ns) & (t <= t1_ns)
    lat = np.array(session.glat)[keep]
    lon = np.array(session.glon)[keep]
    if len(lat) < 2:
        return 0.0
    return float(np.sum(geodesic_distance_m(lat[:-1], lon[:-1], lat[1:], lon[1:])))


def gps_speed_at(session, times):
    t = np.array(session.gt, dtype=float)
    return np.interp(times, t, np.array(session.gspd))


def report_session(session):
    t0, t1 = session.gt[0], session.gt[-1]
    r = integrate(session, t0, t1)
    if len(r["t"]) < 50:
        return None

    gps = gps_speed_at(session, r["t"])
    moving = gps > MOVING_MS
    truth = true_distance(session, t0, t1)

    return {
        "name": session.name,
        "samples": r["total"],
        "drop_pct": 100.0 * r["dropped"] / max(r["total"], 1),
        "zupt_pct": 100.0 * r["stationary"].mean(),
        "false_zupt_pct": (100.0 * r["stationary"][moving].mean()
                           if moving.any() else float("nan")),
        "moving_pct": 100.0 * moving.mean(),
        "dr_speed_moving": (float(r["speed"][moving].mean())
                            if moving.any() else float("nan")),
        "gps_speed_moving": float(gps[moving].mean()) if moving.any() else float("nan"),
        "dr_distance": r["distance_m"],
        "true_distance": truth,
        "ratio": r["distance_m"] / truth if truth > 0 else float("nan"),
    }


def sweep(sessions):
    """Vary the stand-still gate and watch the distance ratio move."""
    configs = [
        ("shipped", dict()),
        ("ZUPT off", dict(zupt=False)),
        ("accel_tol 0.05", dict(accel_tol=0.05)),
        ("accel_tol 0.30", dict(accel_tol=0.30)),
        ("gyro_tol 0.02", dict(gyro_tol=0.02)),
        ("gyro_tol 0.15", dict(gyro_tol=0.15)),
        ("hold 2.0 s", dict(hold_s=2.0)),
        ("accel 0.05 + hold 2.0", dict(accel_tol=0.05, hold_s=2.0)),
        ("max_dt 0.10", dict(max_dt=0.10)),
    ]
    print(f"{'config':<24}{'ZUPT%':>8}{'false%':>8}{'drop%':>8}"
          f"{'DR dist':>10}{'true':>9}{'ratio':>8}")
    print("-" * 75)
    for label, kw in configs:
        dr_tot = tr_tot = 0.0
        zupt, false_zupt, drop, n = [], [], [], 0
        for s in sessions:
            t0, t1 = s.gt[0], s.gt[-1]
            r = integrate(s, t0, t1, **kw)
            if len(r["t"]) < 50:
                continue
            gps = gps_speed_at(s, r["t"])
            moving = gps > MOVING_MS
            dr_tot += r["distance_m"]
            tr_tot += true_distance(s, t0, t1)
            zupt.append(100.0 * r["stationary"].mean())
            if moving.any():
                false_zupt.append(100.0 * r["stationary"][moving].mean())
            drop.append(100.0 * r["dropped"] / max(r["total"], 1))
            n += 1
        if not n:
            continue
        print(f"{label:<24}{np.mean(zupt):>8.1f}{np.mean(false_zupt):>8.1f}"
              f"{np.mean(drop):>8.1f}{dr_tot:>10.0f}{tr_tot:>9.0f}"
              f"{dr_tot / tr_tot if tr_tot else float('nan'):>8.3f}")


# ------------------------------------------------------- outage-window mode

def seed_velocity(session, i0):
    """GPS velocity at the outage start, as DeadReckoner.anchorTo would set it."""
    lat, lon = session.glat[i0], session.glon[i0]
    if i0 + 1 >= len(session.gt):
        return (0.0, 0.0)
    ml = M_PER_DEG_LAT * math.cos(math.radians(lat))
    brg = math.atan2((session.glon[i0 + 1] - lon) * ml,
                     (session.glat[i0 + 1] - lat) * M_PER_DEG_LAT)
    sp = session.gspd[i0]
    if not math.isfinite(sp):
        return (0.0, 0.0)
    return (sp * math.sin(brg), sp * math.cos(brg))


def outage_stats(sessions, duration_s, **kw):
    """Distance ratio and false-ZUPT rate over synthetic outages.

    This is the regime the 37% shortfall was measured in: velocity seeded from
    the last GNSS fix, then free-running. It is not the same as integrating a
    whole session from rest, and the two disagree sharply.
    """
    ratios, false_z, zupts = [], [], []
    for s in sessions:
        t0 = s.gt[0]
        while t0 + duration_s * 1_000_000_000 <= s.gt[-1]:
            i0 = bisect.bisect_left(s.gt, t0)
            t1 = t0 + duration_s * 1_000_000_000
            if i0 < len(s.gt) - 1 and s.gspd[i0] >= 1.5:
                r = integrate(s, t0, t1, seed=seed_velocity(s, i0), **kw)
                truth = true_distance(s, t0, t1)
                if truth > 5.0 and len(r["t"]) > 20:
                    ratios.append(r["distance_m"] / truth)
                    gps = gps_speed_at(s, r["t"])
                    moving = gps > MOVING_MS
                    zupts.append(100.0 * r["stationary"].mean())
                    if moving.any():
                        false_z.append(100.0 * r["stationary"][moving].mean())
            t0 += 5 * 1_000_000_000
    if not ratios:
        return None
    return {
        "n": len(ratios),
        "ratio_mean": float(np.mean(ratios)),
        "ratio_median": float(np.median(ratios)),
        "shortfall_pct": 100.0 * (1.0 - float(np.mean(ratios))),
        "zupt_pct": float(np.mean(zupts)) if zupts else 0.0,
        "false_zupt_pct": float(np.mean(false_z)) if false_z else 0.0,
    }


def outage_report(sessions):
    print(f"{'duration':<12}{'n':>5}{'ZUPT%':>8}{'false%':>8}"
          f"{'ratio':>9}{'median':>9}{'shortfall':>11}")
    print("-" * 62)
    for duration in (10, 30, 60):
        st = outage_stats(sessions, duration)
        if st:
            print(f"{str(duration) + ' s':<12}{st['n']:>5}{st['zupt_pct']:>8.1f}"
                  f"{st['false_zupt_pct']:>8.1f}{st['ratio_mean']:>9.3f}"
                  f"{st['ratio_median']:>9.3f}{st['shortfall_pct']:>10.1f}%")

    print()
    print("=== 60 s outages, stand-still gate swept ===")
    print(f"{'config':<24}{'n':>5}{'ZUPT%':>8}{'false%':>8}{'ratio':>9}{'shortfall':>11}")
    print("-" * 65)
    for label, kw in (("shipped", dict()),
                      ("ZUPT off", dict(zupt=False)),
                      ("accel_tol 0.05", dict(accel_tol=0.05)),
                      ("accel_tol 0.30", dict(accel_tol=0.30)),
                      ("hold 2.0 s", dict(hold_s=2.0)),
                      ("gyro_tol 0.02", dict(gyro_tol=0.02))):
        st = outage_stats(sessions, 60, **kw)
        if st:
            print(f"{label:<24}{st['n']:>5}{st['zupt_pct']:>8.1f}"
                  f"{st['false_zupt_pct']:>8.1f}{st['ratio_mean']:>9.3f}"
                  f"{st['shortfall_pct']:>10.1f}%")


def main(sessions_dir: str) -> int:
    sessions = []
    for name in sorted(os.listdir(sessions_dir)):
        d = os.path.join(sessions_dir, name)
        if os.path.isfile(os.path.join(d, "imu.csv")):
            s = load(d)
            if len(s.gt) >= 20:
                sessions.append(s)
    if not sessions:
        print(f"No sessions in {sessions_dir}")
        return 2

    print("=== per session, shipped thresholds ===")
    header = (f"{'session':<20}{'samp':>7}{'drop%':>7}{'ZUPT%':>7}{'false%':>8}"
              f"{'mov%':>6}{'DRspd':>7}{'GPSspd':>8}{'DRdist':>8}{'true':>8}{'ratio':>7}")
    print(header)
    print("-" * len(header))
    for s in sessions:
        r = report_session(s)
        if r is None:
            continue
        print(f"{r['name']:<20}{r['samples']:>7}{r['drop_pct']:>7.1f}"
              f"{r['zupt_pct']:>7.1f}{r['false_zupt_pct']:>8.1f}{r['moving_pct']:>6.1f}"
              f"{r['dr_speed_moving']:>7.2f}{r['gps_speed_moving']:>8.2f}"
              f"{r['dr_distance']:>8.0f}{r['true_distance']:>8.0f}{r['ratio']:>7.3f}")

    print()
    print("=== free-running outages, velocity seeded from the last GNSS fix ===")
    print("This is the regime the shortfall was measured in.")
    print()
    outage_report(sessions)

    print()
    print("=== whole-session sweep, integrated from rest ===")
    print("false% = share of samples called stationary while GPS says "
          f"> {MOVING_MS} m/s")
    print()
    sweep(sessions)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
