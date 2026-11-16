"""
quality.py
==========
The checks that decide WHEN and WHY a part or a plug is defective.

Every station has one function. It receives the unit it just made and the
wear of its tool (0.0 = new, 1.0 = end of life), adds the measurement it takes,
and returns the reason code if the unit fails, or None if it passes.

Wear is the link to maintenance: a worn tool shifts measured values or makes a
failure more likely, which is what a maintenance work order is meant to prevent.
"""

from . import config as C

# reason codes, one place so the HMI, the database and the tests agree
THREAD_OUT_OF_TOLERANCE = "THREAD_OUT_OF_TOLERANCE"
CRACKED_INSULATOR = "CRACKED_INSULATOR"
ELECTRODE_TOO_SHORT = "ELECTRODE_TOO_SHORT"
WELD_FAILURE = "WELD_FAILURE"
GAP_PRESET_OUT_OF_RANGE = "GAP_PRESET_OUT_OF_RANGE"
SEAL_LEAK = "SEAL_LEAK"
GAP_OUT_OF_TOLERANCE = "GAP_OUT_OF_TOLERANCE"

REASONS = {
    THREAD_OUT_OF_TOLERANCE: ("shell", "Thread diameter outside 13.97 +/- 0.05 mm (worn cutting tool)"),
    CRACKED_INSULATOR: ("insulator", "Crack in the ceramic after sintering"),
    ELECTRODE_TOO_SHORT: ("center", "Center electrode shorter than 18.85 mm"),
    WELD_FAILURE: ("ground", "Ground electrode weld does not hold"),
    GAP_PRESET_OUT_OF_RANGE: ("ground", "Gap pre-set more than 0.15 mm from target (bending error)"),
    SEAL_LEAK: ("assembly", "Seal between insulator and shell leaks (crimp force too low)"),
    GAP_OUT_OF_TOLERANCE: ("final_qc", "Measured gap outside 0.80 +/- 0.05 mm"),
}


def _w(wear):
    """Wear above end of life still counts, but capped so numbers stay sane."""
    return min(max(wear, 0.0), 1.5)


def check_shell(rng, unit, wear):
    mean = C.THREAD_NOMINAL_MM + C.THREAD_DRIFT_AT_LIFE * _w(wear)
    d = rng.gauss(mean, C.THREAD_SIGMA)
    unit["thread_mm"] = round(d, 4)
    if abs(d - C.THREAD_NOMINAL_MM) > C.THREAD_TOL_MM:
        return THREAD_OUT_OF_TOLERANCE
    return None


def check_insulator(rng, unit, wear):
    p = C.CRACK_P + C.CRACK_WEAR_P * _w(wear) ** 2
    unit["cracked"] = rng.random() < p
    return CRACKED_INSULATOR if unit["cracked"] else None


def check_center(rng, unit, wear):
    mean = C.ELECTRODE_NOMINAL_MM + C.ELECTRODE_DRIFT_AT_LIFE * _w(wear)
    length = rng.gauss(mean, C.ELECTRODE_SIGMA)
    unit["electrode_mm"] = round(length, 3)
    return ELECTRODE_TOO_SHORT if length < C.ELECTRODE_MIN_MM else None


def check_ground(rng, unit, wear):
    w = _w(wear)
    if rng.random() < C.WELD_FAIL_P + C.WELD_FAIL_WEAR_P * w ** 2:
        unit["gap_preset_mm"] = None
        return WELD_FAILURE
    if rng.random() < C.GAP_GROSS_P:
        # bending error: the electrode is bent far too much or too little
        off = rng.uniform(C.GAP_GROSS_MM + 0.01, 0.30) * rng.choice((-1, 1))
        gap = C.GAP_TARGET_MM + off
    else:
        gap = rng.gauss(C.GAP_TARGET_MM, C.GAP_SIGMA + C.GAP_SIGMA_WEAR * w)
    unit["gap_preset_mm"] = round(gap, 4)
    if abs(gap - C.GAP_TARGET_MM) > C.GAP_GROSS_MM:
        return GAP_PRESET_OUT_OF_RANGE
    return None


def check_assembly(rng, unit, wear):
    # crimping the shell moves the ground electrode slightly
    unit["gap_mm"] = round(unit["gap_preset_mm"] + rng.gauss(0.0, C.CRIMP_GAP_SIGMA), 4)
    p = C.SEAL_LEAK_P + C.SEAL_LEAK_WEAR_P * _w(wear) ** 2
    return SEAL_LEAK if rng.random() < p else None


def check_final_qc(rng, unit, wear):
    # a worn gauge adds measurement noise; the true gap is unit["gap_mm"]
    measured = unit["gap_mm"] + rng.gauss(0.0, C.GAUGE_NOISE * (1 + _w(wear)))
    unit["gap_measured_mm"] = round(measured, 4)
    if abs(measured - C.GAP_TARGET_MM) > C.GAP_TOL_MM:
        return GAP_OUT_OF_TOLERANCE
    return None


def check_packaging(rng, unit, wear):
    return None


CHECKS = {
    "shell": check_shell,
    "insulator": check_insulator,
    "center": check_center,
    "ground": check_ground,
    "assembly": check_assembly,
    "final_qc": check_final_qc,
    "packaging": check_packaging,
}
