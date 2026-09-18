"""Which heading source should steer dead reckoning through an outage, on OUR device.

Motivation. With the defaults shipped in c0c7ddc, an outage takes its turn rate from the
model's yaw head (`useModelYawHead = true`) and rotates the accelerometer by `game_rv`
(`useMagnetometerYaw = false`). Both are measured failures on this phone:

  * the yaw head recorded in session 20260904_195146 is never negative, so every outage
    turns clockwise regardless of which way the car turns;
  * game_rv has no absolute yaw reference. Its "north" sits -19.9 +/- 38.2 deg from the
    magnetometer's over a three-minute drive, and the NHC integrator projects acceleration
    onto a GNSS-referenced heading - so while turning, a quarter of the centripetal
    acceleration leaks into forward speed.

The proposed replacement is the compass the map already shows, calibrated twice: once for
the phone (the figure-eight gesture, which fits the magnetometer's hard/soft-iron offset)
and once for the MOUNT, because the phone's azimuth on a stand is not the car's heading.
The mount offset is learned from GNSS while it is still available.

Every source below is scored identically: GNSS truth SPEED, so only heading differs; the
outage starts from the GNSS position and bearing; drift is endpoint error over distance.

Run:  python -m eval.compass_heading_eval extracted_sessions/20260904_195146
"""

from __future__ import annotations

import argparse
import math
import os

import numpy as np

from .session_eval import DT, build_grid, load_session, quat_to_matrix, resample

# Mount-offset calibration: GNSS bearing is only a trustworthy reference while moving
# briskly in a straight line, where bearing noise is small and the car's heading equals
# its course over ground.
CAL_MIN_SPEED = 4.0          # m/s
CAL_MAX_YAW_RATE = 0.05      # rad/s, i.e. not turning
CAL_SECONDS = 60.0           # of driving used to learn the offset

# Complementary filter: gyro carries the heading between compass looks, the compass pulls
# it back. The time constant sets how long a magnetic disturbance must last to bend the
# estimate - long enough to ride through a passing lorry, short enough to cancel gyro drift.
TAU_S = 8.0
# Reject the compass while the field magnitude is off its calibrated value by more than
# this fraction: steel bridges, tunnel reinforcement and nearby vehicles all do this.
MAG_DISTURB_FRAC = 0.15


def wrap180(a):
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def circular_mean_deg(a):
    r = np.radians(np.asarray(a))
    return math.degrees(math.atan2(np.nanmean(np.sin(r)), np.nanmean(np.cos(r))))


def forward_axis_azimuth(R, gravity_dev):
    """Azimuth of the phone's most horizontal body axis, robust to how it is mounted.

    `SensorManager.getOrientation` azimuth is the direction of the device Y axis and becomes
    ill-conditioned as the phone stands upright, which is exactly how it sits on a dash
    stand. Picking whichever of +Y or -Z (the camera side) lies flattest, and projecting it
    onto the horizontal, gives a well-defined azimuth for any mounting. Which axis it is does
    not matter: the mount offset absorbs the constant difference to the car's heading.
    """
    up = -gravity_dev / np.linalg.norm(gravity_dev)
    cand = {"+Y": np.array([0.0, 1.0, 0.0]), "-Z": np.array([0.0, 0.0, -1.0])}
    name = min(cand, key=lambda k: abs(float(cand[k] @ up)))
    ax_world = np.einsum("nij,j->ni", R, cand[name])          # device axis in ENU
    return np.degrees(np.arctan2(ax_world[:, 0], ax_world[:, 1])) % 360.0, name


def integrate_track(speed, heading_deg, dt=DT):
    h = np.radians(heading_deg)
    e = np.cumsum(speed * np.sin(h)) * dt
    n = np.cumsum(speed * np.cos(h)) * dt
    return e, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--durations", default="30,60")
    args = ap.parse_args(argv)
    durs = [int(x) for x in args.durations.split(",")]

    s = load_session(args.session)
    g = build_grid(s)
    t, drive = g["t"], g["drive"]
    speed = np.nan_to_num(g["speed"])
    bearing = g["bearing"]
    imu = s["imu"]

    R_rv = quat_to_matrix(resample(imu["rv"][0], imu["rv"][1][:, :4], t))
    R_game = quat_to_matrix(resample(imu["game_rv"][0], imu["game_rv"][1][:, :4], t))
    acc = g["acc"]
    grav_dev = np.nanmedian(acc[drive], axis=0)
    az_rv, axis_name = forward_axis_azimuth(R_rv, grav_dev)
    az_game, _ = forward_axis_azimuth(R_game, grav_dev)

    # Heading rate candidates, clockwise-positive to match a compass bearing.
    w_up = np.einsum("nij,nj->ni", R_rv, g["gyro"])[:, 2]
    anorm = np.linalg.norm(acc, axis=1)
    still = (np.abs(anorm - 9.80665) < 0.15) & (np.linalg.norm(g["gyro"], axis=1) < 0.05)
    bias_dev = np.nanmean(g["gyro"][still], axis=0) if still.sum() > 50 else np.zeros(3)
    w_up_db = np.einsum("nij,nj->ni", R_rv, g["gyro"] - bias_dev)[:, 2]
    gyro_cw = -w_up_db

    model_cw = None
    if "ml" in s:
        tm = s["ml"]["t_ns"] / 1e9
        model_cw = np.interp(t, tm, s["ml"]["yaw_rate"])          # the app treats it as CW
        neg = float(np.mean(s["ml"]["yaw_rate"] < 0))
    # Field magnitude, for disturbance rejection.
    tm_, mg = imu["mag"]
    bmag = np.linalg.norm(resample(tm_, mg[:, :3], t), axis=1)

    # --- mount-offset calibration on the first CAL_SECONDS of straight, brisk driving ---
    brate = np.abs(np.gradient(np.unwrap(np.radians(np.nan_to_num(bearing))), t))
    good = drive & np.isfinite(bearing) & (speed > CAL_MIN_SPEED) & (brate < CAL_MAX_YAW_RATE)
    idx = np.where(good)[0]
    cal = idx[:int(CAL_SECONDS / DT)]
    if len(cal) < 50:
        raise SystemExit("not enough straight, brisk driving to calibrate the mount")
    off_rv = circular_mean_deg(wrap180(bearing[cal] - az_rv[cal]))
    off_game = circular_mean_deg(wrap180(bearing[cal] - az_game[cal]))
    resid = wrap180(bearing[cal] - az_rv[cal] - off_rv)
    b_ref = float(np.nanmedian(bmag[cal]))
    t_cal_end = t[cal[-1]]

    print(f"session {os.path.basename(os.path.normpath(args.session))}: "
          f"phone forward axis {axis_name}; mount offset learned from "
          f"{len(cal) * DT:.0f} s of straight driving")
    print(f"  mount offset (rv compass)   {off_rv:+7.1f} deg   residual sd {np.std(resid):.1f} deg")
    print(f"  mount offset (game_rv)      {off_game:+7.1f} deg")
    print(f"  field magnitude reference   {b_ref:.1f} uT")
    if model_cw is not None:
        print(f"  logged yaw head: {neg * 100:.1f}% of samples negative "
              f"(a real turn rate is negative about half the time)")

    comp = (az_rv + off_rv) % 360.0
    disturbed = np.abs(bmag / b_ref - 1.0) > MAG_DISTURB_FRAC
    print(f"  compass flagged disturbed on {np.mean(disturbed[drive]) * 100:.0f}% of the drive")

    def complementary(i0, i1, h0):
        h = np.empty(i1 - i0)
        cur = h0
        k = DT / (TAU_S + DT)
        for j, i in enumerate(range(i0, i1)):
            cur = cur + gyro_cw[i] * math.degrees(DT)
            if not disturbed[i]:
                cur = cur + k * wrap180(comp[i] - cur)
            h[j] = cur
        return h

    sources = {
        "model yaw head (app default)":
            (lambda i0, i1, h0: h0 + np.cumsum(np.degrees(model_cw[i0:i1])) * DT)
            if model_cw is not None else None,
        "game_rv azimuth + offset":
            lambda i0, i1, h0: (az_game[i0:i1] + off_game) % 360.0,
        "gyro, debiased (integrated)":
            lambda i0, i1, h0: h0 + np.cumsum(np.degrees(gyro_cw[i0:i1])) * DT,
        "compass, NO mount offset":
            lambda i0, i1, h0: az_rv[i0:i1] % 360.0,
        "compass + mount offset":
            lambda i0, i1, h0: comp[i0:i1],
        "compass+gyro complementary":
            complementary,
    }

    print(f"\nsimulated outages after calibration, GNSS truth SPEED so only heading differs")
    hdr = "".join(f"{f'{d} s drift':>13}" for d in durs)
    print(f"{'heading source':32s}{hdr}{'heading RMS':>13}")
    for name, fn in sources.items():
        if fn is None:
            continue
        cells, herr = "", []
        for dur in durs:
            n = int(dur / DT)
            drifts = []
            for i0 in range(0, len(t) - n, int(10 / DT)):
                i1 = i0 + n
                if t[i0] <= t_cal_end or not drive[i0:i1].all():
                    continue
                if not np.isfinite(bearing[i0]):
                    continue
                h = fn(i0, i1, bearing[i0])
                e, nn = integrate_track(speed[i0:i1], h)
                te = g["east"][i0:i1] - g["east"][i0]
                tn = g["north"][i0:i1] - g["north"][i0]
                dist = float(np.sum(speed[i0:i1]) * DT)
                if dist < 50:
                    continue
                drifts.append(math.hypot(e[-1] - te[-1], nn[-1] - tn[-1]) / dist * 100)
                ok = np.isfinite(bearing[i0:i1])
                herr.extend(np.abs(wrap180(h[ok] - bearing[i0:i1][ok])))
            cells += f"{np.median(drifts):12.1f}%" if drifts else f"{'-':>13}"
        rms = math.sqrt(np.mean(np.square(herr))) if herr else float("nan")
        print(f"{name:32s}{cells}{rms:11.1f} deg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
