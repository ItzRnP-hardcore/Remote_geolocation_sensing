"""Vehicle velocity from fewer than four satellites, and what it is worth.

DRAFT. Nothing here is wired into the app. The point is to establish, before writing any
Android code, whether sub-four-satellite GNSS can hold the channel that dead reckoning
actually loses - speed - and by how much.

The mathematics, and why n < 4 is not the wall it looks like
------------------------------------------------------------
A *position* fix has four unknowns (x, y, z, receiver clock bias) and needs four
pseudoranges. That is the familiar rule, and it is why an app watching `satellitesUsedInFix`
gives up below four. But position is not what an inertial navigator is short of. Over a
60 s outage the integrator's heading is good to a few degrees once the gyro is debiased;
what ruins it is speed, which is exactly what the *Doppler* observable measures directly.

For each satellite i the pseudorange rate is

    rhodot_i = u_i . (v_rx - v_sat_i) + bdot                                       (1)

with u_i the unit vector from satellite to receiver and bdot the receiver clock drift.
Moving the known satellite motion to the left gives a linear system in four unknowns
(v_rx, bdot):

    z_i = rhodot_i + u_i . v_sat_i = u_i . v_rx + bdot                             (2)

Four unknowns again - but every unknown here can be retired by something a vehicle IMU
already knows, and each one bought back drops the satellite requirement by one:

  n >= 3   FLAT. A road vehicle's vertical velocity is ~0 over a second. Drop v_up.
  n >= 2   NHC. A car cannot move sideways, so v_rx = s * [sin psi, cos psi, 0] with psi
           the heading the debiased gyro already supplies. Two unknowns left: s and bdot.
  n >= 1   COAST. A TCXO's drift is stable over tens of seconds. Estimate bdot from the
           last epoch with four satellites and hold it. One unknown: the speed itself.

With NHC and a coasted clock, (2) collapses to a scalar per satellite:

    s_hat = (z_i - bdot) / (u_i . d(psi)),    d(psi) = [sin psi, cos psi, 0]       (3)

and n satellites combine by weighted least squares. The denominator is the whole story:
u_i . d is the cosine between the line of sight and the direction of travel, so a satellite
directly overhead tells you nothing about ground speed and one low on the horizon ahead or
behind tells you almost everything. Geometry, not count, is the binding constraint - which
is the part the "you need four satellites" framing hides.

What this script measures
-------------------------
The device records no raw GNSS today (`GnssMeasurement` is not subscribed), so there is no
Doppler on disk to test against. Everything below is therefore SIMULATED on the real driven
track: a GPS-like constellation supplies true geometry, equation (1) supplies the noiseless
observable, and measurement noise is added at a smartphone-realistic level. That measures
the estimator and the geometry honestly; it does NOT measure a real receiver's outliers,
multipath or half-cycle slips, and no number here should be quoted as an on-road result.

Run:  python -m eval.scarce_gnss extracted_sessions/20260904_195146
"""

from __future__ import annotations

import argparse
import math
import os

import numpy as np

# WGS-84
A_EARTH = 6_378_137.0
E2 = 6.694379990141e-3
OMEGA_E = 7.2921151467e-5          # earth rotation, rad/s
MU_EARTH = 3.986005e14             # m^3/s^2

# GPS constellation, nominal: 6 planes, 55 deg inclination, 4 satellites per plane.
GPS_A = 26_559_800.0               # semi-major axis, m
N_PLANES = 6
SATS_PER_PLANE = 4
INCLINATION = math.radians(55.0)

# Smartphone pseudorange-rate noise, and the number the entire result rests on.
#
# MEASURED, on session 20260905_025543 (SM-G990E, Broadcom BCM4775, 306 measurements over
# 26 epochs): median reported uncertainty 1.17 m/s at a mean C/N0 of 22.5 dB-Hz. That was
# recorded INDOORS, which is close to worst case - C/N0 outdoors is typically 40+ dB-Hz and
# the uncertainty falls accordingly. Note also that ~10% of measurements report an
# uncertainty of exactly 299792458 (the speed of light), which is Android's sentinel for
# "unknown" and must be discarded rather than believed.
#
# The default stays 0.5 as a plausible open-sky value, but every conclusion should be read
# with the sweep: at 0.30 a single satellite reaches 0.40 m/s, at 0.50 it reaches 0.74 m/s
# with 27% availability, and at the measured indoor 1.17 the covariance gate rejects almost
# everything and the method is worth nothing. Which regime a real drive sits in is not yet
# known, because no outdoor recording with raw measurements exists.
SIGMA_RHODOT_MPS = 0.50

# Elevation below which a satellite is not usable in an urban canyon or tunnel mouth.
MIN_ELEVATION_DEG = 15.0

DT = 0.1


# --------------------------------------------------------------------- geometry

def lla_to_ecef(lat_deg, lon_deg, alt_m):
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    s = np.sin(lat)
    n = A_EARTH / np.sqrt(1.0 - E2 * s * s)
    x = (n + alt_m) * np.cos(lat) * np.cos(lon)
    y = (n + alt_m) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - E2) + alt_m) * s
    return np.stack([x, y, z], axis=-1)


def enu_basis(lat_deg, lon_deg):
    """Rows are the east, north and up unit vectors expressed in ECEF."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    return np.array([
        [-so, co, 0.0],
        [-sl * co, -sl * so, cl],
        [cl * co, cl * so, sl],
    ])


def constellation(t_s):
    """Nominal-GPS satellite positions and velocities in ECEF at time `t_s`.

    Circular orbits are enough: this exists to produce a REALISTIC SPREAD OF LINE-OF-SIGHT
    DIRECTIONS, which is what equation (3) is sensitive to. Eccentricity and the harmonic
    corrections in a broadcast ephemeris move a satellite along its track, not across the
    sky, so they change the geometry negligibly for this purpose.
    """
    n = math.sqrt(MU_EARTH / GPS_A ** 3)
    pos, vel = [], []
    for p in range(N_PLANES):
        raan = 2.0 * math.pi * p / N_PLANES
        for k in range(SATS_PER_PLANE):
            m0 = 2.0 * math.pi * (k / SATS_PER_PLANE + p / (N_PLANES * SATS_PER_PLANE))
            u = m0 + n * t_s
            # In-plane, then rotate by inclination and RAAN, then earth rotation.
            xp, yp = GPS_A * math.cos(u), GPS_A * math.sin(u)
            vxp, vyp = -GPS_A * n * math.sin(u), GPS_A * n * math.cos(u)
            ci, si = math.cos(INCLINATION), math.sin(INCLINATION)
            x1, y1, z1 = xp, yp * ci, yp * si
            vx1, vy1, vz1 = vxp, vyp * ci, vyp * si
            th = raan - OMEGA_E * t_s
            ct, st = math.cos(th), math.sin(th)
            pos.append([x1 * ct - y1 * st, x1 * st + y1 * ct, z1])
            vel.append([vx1 * ct - vy1 * st, vx1 * st + vy1 * ct, vz1])
    return np.array(pos), np.array(vel)


def visible(sat_pos, rx_ecef, R_enu, min_elev_deg=MIN_ELEVATION_DEG):
    """Indices and receiver-frame unit line-of-sight vectors for satellites above the mask."""
    d = rx_ecef[None, :] - sat_pos                     # satellite -> receiver
    rng = np.linalg.norm(d, axis=1)
    u_ecef = d / rng[:, None]
    # Elevation uses the direction receiver -> satellite, which is -u.
    up = R_enu[2]
    elev = np.degrees(np.arcsin(np.clip(-(u_ecef @ up), -1.0, 1.0)))
    keep = np.where(elev >= min_elev_deg)[0]
    u_enu = (R_enu @ u_ecef[keep].T).T                 # (k, 3) in east/north/up
    return keep, u_enu, elev[keep]


# -------------------------------------------------------------------- estimators

def solve_speed(u_enu, z, psi_deg, bdot, mode, sigma=SIGMA_RHODOT_MPS):
    """Estimate ground speed from `len(z)` Doppler observables.

    `mode` selects how many unknowns are retired by vehicle constraints:
      "full"  (v_e, v_n, v_u, bdot)   needs 4
      "flat"  (v_e, v_n, bdot)        needs 3
      "nhc"   (s, bdot)               needs 2
      "coast" (s)                     needs 1
    Returns (speed, sigma_speed) or (nan, inf) when the geometry is singular, which is a real
    outcome and not a failure: satellites clustered near the zenith carry no ground-speed
    information however many of them there are.
    """
    n = len(z)
    d = np.array([math.sin(math.radians(psi_deg)), math.cos(math.radians(psi_deg)), 0.0])

    if mode == "coast":
        if n < 1:
            return float("nan"), float("inf")
        g = u_enu @ d                                   # cosine to the direction of travel
        w = g / sigma
        denom = float(w @ w)
        if denom < 1e-12:
            return float("nan"), float("inf")
        s = float(w @ ((z - bdot) / sigma)) / denom
        return s, math.sqrt(1.0 / denom)

    if mode == "nhc":
        if n < 2:
            return float("nan"), float("inf")
        H = np.column_stack([u_enu @ d, np.ones(n)])
        y = z
    elif mode == "flat":
        if n < 3:
            return float("nan"), float("inf")
        H = np.column_stack([u_enu[:, 0], u_enu[:, 1], np.ones(n)])
        y = z
    else:
        if n < 4:
            return float("nan"), float("inf")
        H = np.column_stack([u_enu, np.ones(n)])
        y = z

    try:
        cov = np.linalg.inv(H.T @ H) * sigma ** 2
    except np.linalg.LinAlgError:
        return float("nan"), float("inf")
    x = np.linalg.lstsq(H, y, rcond=None)[0]
    if mode == "nhc":
        return float(x[0]), float(math.sqrt(max(cov[0, 0], 0.0)))
    if mode == "flat":
        s = float(math.hypot(x[0], x[1]))
        return s, float(math.sqrt(max(cov[0, 0] + cov[1, 1], 0.0)))
    s = float(math.hypot(x[0], x[1]))
    return s, float(math.sqrt(max(cov[0, 0] + cov[1, 1], 0.0)))


# ------------------------------------------------------------------------- data

def load_track(session_dir):
    """Real driven track: position, ground speed and heading, on the driving span only."""
    import pandas as pd
    g = pd.read_csv(os.path.join(session_dir, "gps.csv"))
    g = g[(g.acc_m <= 30.0) & g.speed_mps.notna() & g.bearing_deg.notna()]
    g = g[g.speed_mps > 2.0]
    if len(g) < 30:
        raise SystemExit("not enough moving fixes with a bearing in this session")
    return {
        "t": g.t_ns.values / 1e9,
        "lat": g.lat.values, "lon": g.lon.values,
        "alt": np.nan_to_num(g.alt_m.values, nan=0.0),
        "speed": g.speed_mps.values, "bearing": g.bearing_deg.values,
    }


def imu_only_speed_error(session_dir, horizon_s):
    """The baseline this has to beat: integrator speed error after `horizon_s` unaided.

    Taken from the session's own logs rather than assumed. `deadreckon.csv` records the
    integrator's speed and `gps.csv` the truth, so the growth of |error| with time since the
    last anchor is measurable directly.
    """
    import pandas as pd
    dr = pd.read_csv(os.path.join(session_dir, "deadreckon.csv"))
    g = pd.read_csv(os.path.join(session_dir, "gps.csv"))
    g = g[(g.acc_m <= 30.0) & g.speed_mps.notna()]
    t = dr.t_ns.values / 1e9
    truth = np.interp(t, g.t_ns.values / 1e9, g.speed_mps.values)
    moving = truth > 2.0
    if moving.sum() < 50:
        return float("nan")
    err = np.abs(dr.speed_mps.values - truth)[moving]
    # The recorded free-run segments are short, so this is the aided error and therefore a
    # LOWER bound on what an outage would produce - which makes the comparison conservative.
    return float(np.sqrt(np.mean(err ** 2)))


# ------------------------------------------------------------------------ report

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--sigma", type=float, default=SIGMA_RHODOT_MPS,
                    help="pseudorange-rate noise, m/s")
    ap.add_argument("--trials", type=int, default=40, help="noise draws per epoch")
    ap.add_argument("--heading-err", type=float, default=3.0,
                    help="1-sigma heading error fed to the NHC modes, degrees")
    ap.add_argument("--bdot-err", type=float, default=0.05,
                    help="1-sigma coasted clock-drift error, m/s")
    ap.add_argument("--sigma-gate", type=float, default=1.0,
                    help="reject estimates whose own 1-sigma exceeds this, m/s")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    tr = load_track(args.session)
    rng = np.random.default_rng(args.seed)
    base = imu_only_speed_error(args.session, 60)

    print(f"session {os.path.basename(os.path.normpath(args.session))}: "
          f"{len(tr['t'])} moving fixes, mean speed {tr['speed'].mean():.2f} m/s")
    print(f"simulated constellation: {N_PLANES * SATS_PER_PLANE} satellites, "
          f"{MIN_ELEVATION_DEG:.0f} deg mask, pseudorange-rate noise {args.sigma:.2f} m/s")
    print(f"integrator speed RMSE on this session (aided, so a LOWER bound): {base:.3f} m/s\n")

    modes = [("full", 4, "unconstrained (v_e,v_n,v_u,bdot)"),
             ("flat", 3, "+ flat road      (v_e,v_n,bdot)"),
             ("nhc", 2, "+ non-holonomic  (s,bdot)"),
             ("coast", 1, "+ clock coasting (s)")]

    # How many satellites the geometry actually offers, before any masking down.
    counts = []
    for k in range(0, len(tr["t"]), max(1, len(tr["t"]) // 60)):
        R = enu_basis(tr["lat"][k], tr["lon"][k])
        rx = lla_to_ecef(tr["lat"][k], tr["lon"][k], tr["alt"][k])
        sp, _ = constellation(tr["t"][k] % 86164.0)
        keep, _, _ = visible(sp, rx, R)
        counts.append(len(keep))
    print(f"satellites above the mask on this track: median {int(np.median(counts))} "
          f"(min {min(counts)}, max {max(counts)})")
    print("A tunnel does not thin that set gradually - it cuts to whatever a mouth, gap or\n"
          "canyon leaves. WHICH satellites survive matters more than how many, because\n"
          "equation (3) divides by the cosine between the line of sight and the direction of\n"
          "travel: a satellite overhead carries no ground-speed information at all.\n")

    # Two sky shapes bracket the real cases. An urban canyon leaves a strip of sky OVERHEAD,
    # the worst case for ground speed; a tunnel mouth or cutting leaves sky LOW along the road
    # axis, the best. Reporting one without the other would be picking a conclusion.
    strategies = [
        ("urban canyon (highest elevation - worst)", lambda e, u, d: np.argsort(-e)),
        ("tunnel mouth (low, along track - best)", lambda e, u, d: np.argsort(-np.abs(u @ d))),
        ("random subset", None),
    ]

    # An estimate whose own reported sigma is large is not a measurement, it is a division by a
    # small number. Gating on THAT rather than on satellite count is the practical answer to
    # "can n < 4 be used": yes, whenever the geometry supports it, and the covariance says when.
    sigma_gate = args.sigma_gate

    for sname, order in strategies:
        print(f"\n--- {sname} ---")
        print(f"{'estimator':38s}{'n=1':>16}{'n=2':>16}{'n=3':>16}{'n=4':>16}")
        print("-" * 102)
        for mode, need, label in modes:
            cells = ""
            for n_sat in (1, 2, 3, 4):
                if n_sat < need:
                    cells += f"{'-':>16}"
                    continue
                errs, kept, total = [], 0, 0
                for k in range(0, len(tr["t"]), max(1, len(tr["t"]) // 40)):
                    lat, lon, alt = tr["lat"][k], tr["lon"][k], tr["alt"][k]
                    R = enu_basis(lat, lon)
                    rx = lla_to_ecef(lat, lon, alt)
                    sp, sv = constellation(tr["t"][k] % 86164.0)
                    keep, u_enu, elev = visible(sp, rx, R)
                    if len(keep) < n_sat:
                        continue
                    s_true, psi = tr["speed"][k], tr["bearing"][k]
                    d = np.array([math.sin(math.radians(psi)),
                                  math.cos(math.radians(psi)), 0.0])
                    idx = (rng.permutation(len(keep)) if order is None
                           else order(elev, u_enu, d))[:n_sat]
                    u = u_enu[idx]
                    v_true = s_true * d
                    for _ in range(args.trials):
                        total += 1
                        bdot_true = rng.normal(0.0, 1.0)
                        # z = u.v_rx + bdot, per equation (2), plus measurement noise.
                        z = u @ v_true + bdot_true + rng.normal(0.0, args.sigma, size=n_sat)
                        psi_hat = psi + rng.normal(0.0, args.heading_err)
                        bdot_hat = (bdot_true + rng.normal(0.0, args.bdot_err)
                                    if mode == "coast" else 0.0)
                        s_hat, sig = solve_speed(u, z, psi_hat, bdot_hat, mode, args.sigma)
                        if np.isfinite(s_hat) and sig <= sigma_gate:
                            kept += 1
                            errs.append(s_hat - s_true)
                if errs:
                    rmse = float(np.sqrt(np.mean(np.square(errs))))
                    cells += f"{rmse:>10.3f}/{kept / max(total, 1) * 100:>4.0f}%"
                else:
                    cells += f"{'never usable':>16}"
            print(f"{label:38s}{cells}")

    print(f"\ncell = speed RMSE (m/s) / share of epochs whose covariance passes the "
          f"{sigma_gate:.1f} m/s gate")
    print(f"integrator alone, no GNSS: {base:.3f} m/s speed RMSE "
          f"(aided figure, so a LOWER bound on an outage)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
