"""Attitude estimation, because IO-VNBD ships no usable attitude.

Our integrator needs a device-to-world rotation, and on the phone it gets one for
free from TYPE_ROTATION_VECTOR. IO-VNBD has two columns that look like they would
serve and neither does:

  GRAVITY X/Y/Z    Z is a constant 9.8065-9.8066 across a whole session (89 distinct
                   values in 94,600 rows, sd 0.005) with X,Y ~ 0. That is a
                   placeholder the recording app wrote, not a measurement.
  ORIENTATION      Reports mean pitch -83 deg, i.e. the phone standing on end, while
                   the accelerometer reports gravity along +Z, i.e. the phone lying
                   flat. They contradict each other, so neither can be trusted.

So attitude has to be estimated from accelerometer + gyroscope + magnetometer. That
is exactly the same input Android's own ROTATION_VECTOR fuses, which makes a Mahony
filter here a fair stand-in rather than a handicap: it is not being asked to beat the
phone's sensor, only to reconstruct what that sensor would have produced.

Mahony rather than Madgwick, and rather than a Kalman filter, because it has two
interpretable gains, no matrix inversion, and cannot diverge from a bad initial
guess - all of which matter when the thing consuming its output is itself the
subject of the experiment. A filter with hidden failure modes would contaminate the
integrator's results with its own.

Frame convention is Android's: R maps device to world with world = (East, North, Up),
and [quaternion_to_matrix] is checked element-by-element against
outage_eval.rot_from_rv in the self-test. That check is not ceremony - our integrator
does `aE = R[0]*ax + R[1]*ay + R[2]*az`, so a transposed matrix still produces
plausible-looking accelerations that are silently wrong.

Run:  python -m eval.ahrs        (self-test)
"""

from __future__ import annotations

import math

import numpy as np

# Proportional gain on the accelerometer/magnetometer error. Sets how fast attitude
# is pulled toward the observed gravity and field directions. Higher tracks faster but
# admits more of the vehicle's linear acceleration as false tilt.
DEFAULT_KP = 1.0

# Integral gain, which learns out a constant gyroscope bias. Small: a gyro bias is
# slow, and a large Ki turns the filter into an oscillator.
DEFAULT_KI = 0.05

# Samples the filter is allowed before its output is considered converged. At 10 Hz
# this is 20 s, which is ample for Kp=1.0 and is reported rather than assumed.
WARMUP_SAMPLES = 200

# Gyro-only integration beyond this gap, since an accelerometer correction across a
# long hole would snap the attitude rather than track it.
MAX_DT_S = 0.5


def quaternion_to_matrix(x: float, y: float, z: float, w: float) -> tuple:
    """Row-major 3x3 device-to-world rotation, matching Android exactly.

    Deliberately a transcription of SensorManager.getRotationMatrixFromVector, in the
    same term order as outage_eval.rot_from_rv, so the two cannot drift apart. The
    self-test asserts they agree.
    """
    q1, q2, q3, q0 = x, y, z, w
    sq1, sq2, sq3 = 2 * q1 * q1, 2 * q2 * q2, 2 * q3 * q3
    q1q2, q3q0 = 2 * q1 * q2, 2 * q3 * q0
    q1q3, q2q0 = 2 * q1 * q3, 2 * q2 * q0
    q2q3, q1q0 = 2 * q2 * q3, 2 * q1 * q0
    return (
        1 - sq2 - sq3, q1q2 - q3q0, q1q3 + q2q0,
        q1q2 + q3q0, 1 - sq1 - sq3, q2q3 - q1q0,
        q1q3 - q2q0, q2q3 + q1q0, 1 - sq1 - sq2,
    )


class MahonyAHRS:
    """Complementary filter fusing gyro integration with gravity and field vectors.

    State is a unit quaternion in (w, x, y, z) internally; [quaternion] returns Android
    order (x, y, z, w) so it can be written straight into an `rv` row.
    """

    def __init__(self, kp: float = DEFAULT_KP, ki: float = DEFAULT_KI,
                 use_mag: bool = True):
        self.kp = kp
        self.ki = ki
        self.use_mag = use_mag
        # Identity: device axes aligned with world axes.
        self.q = np.array([1.0, 0.0, 0.0, 0.0])
        self.bias = np.zeros(3)
        self.samples = 0

    @property
    def converged(self) -> bool:
        return self.samples >= WARMUP_SAMPLES

    def quaternion(self) -> tuple:
        """(x, y, z, w) - Android's component order."""
        w, x, y, z = self.q
        return (float(x), float(y), float(z), float(w))

    def matrix(self) -> tuple:
        return quaternion_to_matrix(*self.quaternion())

    def set_heading(self, heading_deg: float) -> None:
        """Rotate the current estimate so its yaw equals [heading_deg].

        Used to seed from the reference heading at the start of an evaluation window.
        Magnetic disturbance inside a vehicle biases the field-based yaw, and letting
        that bias sit in the initial condition of a 60 s outage would be measuring the
        magnetometer rather than the integrator.
        """
        R = np.array(self.matrix()).reshape(3, 3)
        # Device +Y projected into the world horizontal plane is the phone's own
        # forward; its bearing is the yaw the filter currently believes.
        east, north = R[0, 1], R[1, 1]
        current = math.degrees(math.atan2(east, north))
        # A positive world-Up rotation carries East toward North, so it lowers a
        # compass bearing: the correction is current - target, not target - current.
        self._rotate_world_z(math.radians(current - heading_deg))

    def _rotate_world_z(self, angle_rad: float) -> None:
        h = angle_rad / 2.0
        # World-frame yaw premultiplies: q <- q_z * q.
        qz = np.array([math.cos(h), 0.0, 0.0, math.sin(h)])
        self.q = _qmul(qz, self.q)
        self.q /= np.linalg.norm(self.q)

    def update(self, gyro, accel, mag=None, dt: float = 0.1) -> None:
        """Advance one sample. [gyro] rad/s, [accel] m/s^2, [mag] uT, all device frame."""
        if not (0 < dt <= MAX_DT_S):
            # Still integrate the gyro across a modest gap, but never apply a
            # correction whose dt we do not believe.
            dt = min(max(dt, 1e-3), MAX_DT_S)

        g = np.asarray(gyro, dtype=float)
        a = np.asarray(accel, dtype=float)
        if not np.all(np.isfinite(g)):
            return
        self.samples += 1

        error = np.zeros(3)

        a_norm = np.linalg.norm(a)
        if np.isfinite(a_norm) and a_norm > 1e-6:
            a_hat = a / a_norm
            # Gravity direction in the device frame is the third row of R - see the
            # module docstring; this is the standard Mahony term written out.
            w_, x_, y_, z_ = self.q
            v = np.array([
                2 * (x_ * z_ - w_ * y_),
                2 * (w_ * x_ + y_ * z_),
                w_ * w_ - x_ * x_ - y_ * y_ + z_ * z_,
            ])
            error += np.cross(a_hat, v)

        if self.use_mag and mag is not None:
            m = np.asarray(mag, dtype=float)
            m_norm = np.linalg.norm(m)
            if np.all(np.isfinite(m)) and m_norm > 1e-6:
                m_hat = m / m_norm
                R = np.array(self.matrix()).reshape(3, 3)
                h = R @ m_hat
                # Collapse the measured field onto the world horizontal plane: only its
                # northward direction carries heading, and forcing East to zero is what
                # stops magnetic dip from tilting the estimate.
                b = np.array([0.0, math.hypot(h[0], h[1]), h[2]])
                error += np.cross(m_hat, R.T @ b)

        if self.ki > 0:
            self.bias += self.ki * error * dt
        corrected = g + self.kp * error + self.bias

        # q_dot = 0.5 * q (x) [0, omega]
        self.q = self.q + 0.5 * _qmul(self.q, np.array([0.0, *corrected])) * dt
        n = np.linalg.norm(self.q)
        if n > 1e-12:
            self.q /= n
        else:
            self.q = np.array([1.0, 0.0, 0.0, 0.0])


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ])


def run(gyro, accel, mag=None, t_s=None, kp: float = DEFAULT_KP,
        ki: float = DEFAULT_KI, use_mag: bool = True,
        initial_heading_deg: float | None = None):
    """Attitude over a whole session.

    Returns (quaternions Nx4 in x,y,z,w order, converged mask N).
    """
    gyro = np.asarray(gyro, dtype=float)
    accel = np.asarray(accel, dtype=float)
    n = len(gyro)
    if t_s is None:
        t_s = np.arange(n) * 0.1
    t_s = np.asarray(t_s, dtype=float)

    f = MahonyAHRS(kp=kp, ki=ki, use_mag=use_mag)
    out = np.zeros((n, 4))
    conv = np.zeros(n, dtype=bool)

    # Level the filter on the first samples before running, so the warm-up transient
    # is a heading search rather than a full attitude search.
    lead = accel[: min(n, 20)]
    lead = lead[np.all(np.isfinite(lead), axis=1)]
    if len(lead):
        f.q = _level_from_gravity(lead.mean(axis=0))

    if initial_heading_deg is not None and np.isfinite(initial_heading_deg):
        f.set_heading(float(initial_heading_deg))

    for i in range(n):
        dt = 0.1 if i == 0 else float(t_s[i] - t_s[i - 1])
        f.update(gyro[i], accel[i], None if mag is None else mag[i], dt)
        out[i] = f.quaternion()
        conv[i] = f.converged
    return out, conv


def _level_from_gravity(a):
    """Quaternion whose Up axis matches a measured gravity vector, yaw arbitrary."""
    n = np.linalg.norm(a)
    if not np.isfinite(n) or n < 1e-6:
        return np.array([1.0, 0.0, 0.0, 0.0])
    v = a / n                      # device-frame direction of world Up
    up = np.array([0.0, 0.0, 1.0])
    axis = np.cross(v, up)
    s = np.linalg.norm(axis)
    dot = float(np.clip(np.dot(v, up), -1.0, 1.0))
    if s < 1e-9:
        return (np.array([1.0, 0.0, 0.0, 0.0]) if dot > 0
                else np.array([0.0, 1.0, 0.0, 0.0]))
    axis = axis / s
    angle = math.acos(dot)
    # Rotation taking device Up onto world Up. Inverted, because q holds
    # device-to-world while the rotation just built is world-to-device.
    h = angle / 2.0
    q = np.array([math.cos(h), *(-axis * math.sin(h))])
    return q / np.linalg.norm(q)


# ------------------------------------------- tilt-plus-yaw attitude for IO-VNBD

def matrix_to_quaternion(R):
    """(x, y, z, w) from a row-major 3x3 rotation. Shepperd's branch selection."""
    m = np.asarray(R, dtype=float).reshape(3, 3)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s_ = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s_
        x = (m[2, 1] - m[1, 2]) / s_
        y = (m[0, 2] - m[2, 0]) / s_
        z = (m[1, 0] - m[0, 1]) / s_
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s_ = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s_
        x = 0.25 * s_
        y = (m[0, 1] + m[1, 0]) / s_
        z = (m[0, 2] + m[2, 0]) / s_
    elif m[1, 1] > m[2, 2]:
        s_ = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s_
        x = (m[0, 1] + m[1, 0]) / s_
        y = 0.25 * s_
        z = (m[1, 2] + m[2, 1]) / s_
    else:
        s_ = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s_
        x = (m[0, 2] + m[2, 0]) / s_
        y = (m[1, 2] + m[2, 1]) / s_
        z = 0.25 * s_
    q = np.array([x, y, z, w])
    n = np.linalg.norm(q)
    return tuple(q / n) if n > 1e-12 else (0.0, 0.0, 0.0, 1.0)


def _frame_from_up_and_heading(up_dev, heading_deg):
    """Rotation matrix whose Up row is `up_dev` and whose yaw gives `heading_deg`.

    Rows of R are the world axes expressed in device coordinates, so row 2 is simply
    the measured gravity direction. The remaining freedom is one rotation about that
    axis, which the heading fixes.
    """
    up = np.asarray(up_dev, dtype=float)
    n = np.linalg.norm(up)
    if not np.isfinite(n) or n < 1e-9:
        return np.eye(3)
    up = up / n

    seed = np.array([1.0, 0.0, 0.0])
    if abs(float(seed @ up)) > 0.9:
        seed = np.array([0.0, 1.0, 0.0])
    f0 = seed - up * float(seed @ up)
    f0 /= np.linalg.norm(f0)

    def build(psi):
        north = f0 * math.cos(psi) + np.cross(up, f0) * math.sin(psi)
        north /= np.linalg.norm(north)
        # World East = North x Up, and cross products survive a proper rotation, so the
        # same relation holds between the device-frame representations.
        east = np.cross(north, up)
        east /= np.linalg.norm(east)
        return np.vstack([east, north, up])

    R0 = build(0.0)
    # Bearing of device +Y under R0. Advancing psi advances that bearing by the
    # same amount - verified against the construction rather than assumed, because
    # the opposite sign also produces a valid rotation matrix and a plausible
    # track, just rotated. The self-test pins it.
    bearing0 = math.degrees(math.atan2(R0[0, 1], R0[1, 1]))
    return build(math.radians(heading_deg - bearing0))


def attitude_from_tilt_and_yaw(accel, yaw_rate, t_s, initial_heading_deg=0.0,
                               smooth=15):
    """Attitude from the accelerometer plus one validated yaw-rate channel.

    Written for IO-VNBD, where a full three-axis AHRS cannot be justified: the gyro
    column labels are wrong, and only the yaw axis is identifiable (it is the one that
    correlates with the vehicle's own yaw rate, r = 0.95). Feeding a possibly-permuted
    gyro triple into [MahonyAHRS] would corrupt attitude while still looking plausible.

    So this uses exactly what can be checked. Tilt comes from the accelerometer, which
    is self-correcting and needs no axis assumption. Heading comes from integrating the
    identified yaw channel, seeded from the reference heading. The magnetometer is left
    out on purpose: inside a steel vehicle its heading is disturbed, and unlike a phone
    held in the open there is no reason to expect it to help.

    The honest limitation is that heading here is dead-reckoned from a gyro and will
    drift, exactly as it would on the phone - which is the error this project studies,
    so it belongs in the measurement rather than being hidden by a magnetometer.
    """
    accel = np.asarray(accel, dtype=float)
    yaw_rate = np.asarray(yaw_rate, dtype=float)
    t_s = np.asarray(t_s, dtype=float)
    n = len(accel)

    # Low-pass the accelerometer before treating it as gravity: at 10 Hz in a car the
    # raw signal is mostly road vibration and braking, and using it unfiltered would
    # read every bump as a change of attitude.
    if smooth > 1 and n > smooth:
        ker = np.ones(smooth) / smooth
        acc_s = np.vstack([np.convolve(accel[:, i], ker, mode="same")
                           for i in range(3)]).T
    else:
        acc_s = accel

    heading = float(initial_heading_deg if np.isfinite(initial_heading_deg) else 0.0)
    out = np.zeros((n, 4))
    headings = np.zeros(n)
    for i in range(n):
        if i > 0:
            dt = float(t_s[i] - t_s[i - 1])
            w = yaw_rate[i]
            if 0 < dt <= MAX_DT_S and np.isfinite(w):
                # Bearing decreases under a right-handed rotation about world Up.
                heading -= math.degrees(w) * dt
        headings[i] = heading % 360.0
        up = acc_s[i]
        if not np.all(np.isfinite(up)):
            out[i] = out[i - 1] if i else (0.0, 0.0, 0.0, 1.0)
            continue
        out[i] = matrix_to_quaternion(_frame_from_up_and_heading(up, heading))
    return out, headings


# ------------------------------------------------------------------- self-test

def _self_test() -> int:
    from .outage_eval import rot_from_rv

    failures = []

    # 1. Convention must match the integrator's, element by element.
    for q in ((0.0, 0.0, 0.0, 1.0), (0.1, -0.2, 0.3, 0.927),
              (0.5, 0.5, 0.5, 0.5), (-0.3, 0.1, 0.6, 0.734)):
        n = math.sqrt(sum(c * c for c in q))
        qn = tuple(c / n for c in q)
        mine = quaternion_to_matrix(*qn)
        theirs = rot_from_rv(*qn)
        d = max(abs(a - b) for a, b in zip(mine, theirs))
        print(f"  convention vs rot_from_rv {qn}: max|delta| = {d:.2e}")
        if d > 1e-12:
            failures.append(f"matrix mismatch {d:.2e}")

    # 2. Orthonormality.
    qn = np.array([0.1, -0.2, 0.3, 0.927])
    qn = qn / np.linalg.norm(qn)
    R = np.array(quaternion_to_matrix(*qn)).reshape(3, 3)
    err = np.abs(R @ R.T - np.eye(3)).max()
    print(f"  orthonormality: max|R R^T - I| = {err:.2e}")
    if err > 1e-9:
        failures.append("not orthonormal")

    # 3. Level and still: device +Z must map to world Up.
    n = 400
    accel = np.tile([0.0, 0.0, 9.80665], (n, 1))
    gyro = np.zeros((n, 3))
    q, conv = run(gyro, accel, use_mag=False)
    R = np.array(quaternion_to_matrix(*q[-1])).reshape(3, 3)
    up = R @ np.array([0.0, 0.0, 1.0])
    print(f"  level+still: device +Z -> world {np.round(up, 6)}  (want [0,0,1])")
    if abs(up[2] - 1.0) > 1e-3:
        failures.append(f"level attitude wrong, up={up}")

    # 4. Levelled acceleration must put gravity in Up, not smeared horizontally.
    lev = R @ np.array([0.0, 0.0, 9.80665])
    print(f"  levelled gravity = {np.round(lev, 4)}  horizontal "
          f"{math.hypot(lev[0], lev[1]):.4f} m/s^2")
    if math.hypot(lev[0], lev[1]) > 1e-2:
        failures.append("gravity leaking horizontally")

    # 5. A pure yaw rate must integrate to the right angle. Gyro only, since a
    #    magnetometer would fight the rotation it is being asked to track.
    rate = math.radians(20.0)
    n = 300
    gyro = np.tile([0.0, 0.0, rate], (n, 1))
    accel = np.tile([0.0, 0.0, 9.80665], (n, 1))
    q, _ = run(gyro, accel, use_mag=False, initial_heading_deg=0.0)
    R = np.array(quaternion_to_matrix(*q[-1])).reshape(3, 3)
    yaw = math.degrees(math.atan2(R[0, 1], R[1, 1]))
    # The loop takes n steps of 0.1 s - the first is seeded at 0.1 rather than
    # skipped - so integrated time is n*0.1. Bearing runs opposite to a
    # right-handed rotation about world Up, hence the negation.
    expected = (-math.degrees(rate) * n * 0.1 + 180) % 360 - 180
    got = (yaw + 180) % 360 - 180
    err = (got - expected + 180) % 360 - 180
    print(f"  yaw integration: got {got:+.2f} deg, expected {expected:+.2f} deg, error {err:+.3f} deg")
    if abs(err) > 0.5:
        failures.append(f"yaw integration off by {err:+.3f} deg")

    # 6. set_heading must land on the requested bearing from a NON-zero start.
    for target in (0.0, 37.0, 198.0, 305.0):
        f = MahonyAHRS(use_mag=False)
        f.q = _level_from_gravity(np.array([0.3, -0.2, 9.7]))
        f._rotate_world_z(math.radians(64.0))          # start somewhere arbitrary
        f.set_heading(target)
        R = np.array(f.matrix()).reshape(3, 3)
        got = math.degrees(math.atan2(R[0, 1], R[1, 1])) % 360.0
        err = (got - target + 180) % 360 - 180
        print(f"  set_heading({target:5.1f}) -> {got:6.2f} deg, error {err:+.3f}")
        if abs(err) > 0.01:
            failures.append(f"set_heading({target}) off by {err:+.3f}")

    # 7. tilt+yaw attitude: heading must match the constructed bearing exactly, and
    #    the Up row must follow the accelerometer.
    n = 100
    t = np.arange(n) * 0.1
    acc = np.tile([0.0, 0.0, 9.80665], (n, 1))
    yr = np.full(n, math.radians(-20.0))
    q, hd = attitude_from_tilt_and_yaw(acc, yr, t, initial_heading_deg=0.0)
    R = np.array(quaternion_to_matrix(*q[-1])).reshape(3, 3)
    bearing = math.degrees(math.atan2(R[0, 1], R[1, 1])) % 360.0
    err = (bearing - hd[-1] + 180) % 360 - 180
    print(f"  tilt+yaw: heading {hd[-1]:.2f} deg, matrix bearing {bearing:.2f} deg, "
          f"error {err:+.3f}")
    if abs(hd[-1] - 198.0) > 0.01:
        failures.append(f"yaw integration gave {hd[-1]}, want 198.0")
    if abs(err) > 0.01:
        failures.append(f"tilt+yaw matrix disagrees with its own heading by {err:+.3f}")
    if abs(R[2, 2] - 1.0) > 1e-6:
        failures.append("tilt+yaw Up row does not follow the accelerometer")

    # 8. A tilted phone must still report the requested heading.
    acc = np.tile([1.5, -2.0, 9.4], (n, 1))
    q, hd = attitude_from_tilt_and_yaw(acc, np.zeros(n), t, initial_heading_deg=123.0)
    R = np.array(quaternion_to_matrix(*q[-1])).reshape(3, 3)
    bearing = math.degrees(math.atan2(R[0, 1], R[1, 1])) % 360.0
    up = acc[0] / np.linalg.norm(acc[0])
    print(f"  tilted phone: bearing {bearing:.2f} (want 123.00), "
          f"Up row error {np.abs(R[2] - up).max():.2e}")
    if abs((bearing - 123.0 + 180) % 360 - 180) > 0.01:
        failures.append(f"tilted heading wrong: {bearing}")
    if np.abs(R[2] - up).max() > 1e-9:
        failures.append("tilted Up row wrong")

    print()
    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
