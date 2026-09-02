"""Outage error metrics, matching pranjali2105/SIH_2026 so numbers are comparable.

Definitions are from Onyekpe et al. 2021 (Appl. Sci. 11, 1270), with the same
correction her `src/eval/metrics.py` documents:

  CRSE  the norm of the ACCUMULATED 2-D error vector at the end of the outage,
        not the scalar sum of per-second errors. The scalar reading inverts the
        paper's published difficulty ordering (it makes motorway the worst
        scenario and roundabout among the best) because it cannot see heading.
  CAE   signed sum of per-second displacement errors. Kept signed on purpose:
        CRSE with |CAE|/CRSE near 1 is a systematic bias, near 0 is scatter.
  AEPS  mean absolute per-second displacement error.

Ground truth distance is geodesic on WGS84, as in hers.
"""

from __future__ import annotations

import numpy as np
from pyproj import Geod

_GEOD = Geod(ellps="WGS84")


def geodesic_distance_m(lat1, lon1, lat2, lon2):
    """Geodesic distance in metres. Scalars or arrays."""
    return _GEOD.inv(lon1, lat1, lon2, lat2)[2]


def crse(position_errors) -> float:
    """Accumulated 2-D position error at the end of the outage."""
    e = np.asarray(position_errors, dtype=float)
    e = e[np.isfinite(e)]
    return float(e[-1]) if e.size else float("nan")


def cae(displacement_errors) -> float:
    """Signed cumulative displacement error."""
    e = np.asarray(displacement_errors, dtype=float)
    return float(np.nansum(e))


def aeps(displacement_errors) -> float:
    """Mean absolute per-second displacement error."""
    e = np.asarray(displacement_errors, dtype=float)
    e = e[np.isfinite(e)]
    return float(np.mean(np.abs(e))) if e.size else float("nan")


def summarise(displacement_errors, position_errors) -> dict:
    return {
        "crse": crse(position_errors),
        "cae": cae(displacement_errors),
        "aeps": aeps(displacement_errors),
        "n_seconds": int(np.size(displacement_errors)),
    }
