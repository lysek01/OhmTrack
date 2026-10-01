#!/usr/bin/env python3
"""Resistance -> temperature conversions.

Pt100 / Pt1000 use the Callendar-Van Dusen equation (IEC 60751 defaults, but the
A/B/C coefficients and R0 are caller-supplied so other sensor curves work too):
    R(T) = R0 (1 + A*T + B*T^2)                for T >= 0 C
    R(T) = R0 (1 + A*T + B*T^2 + C*(T-100)*T^3) for T <  0 C

Also provided: a generic linear metal model, the copper-winding resistance method,
and a free calibration curve fitted through user-supplied (R, T) points.
"""

from __future__ import annotations
import math

# IEC 60751 coefficients (standard Pt, alpha = 0.00385)
A_IEC = 3.9083e-3
B_IEC = -5.775e-7
C_IEC = -4.183e-12  # only used below 0 C


def rtd_temperature(r_ohms: float, r0: float = 100.0,
                    A: float = A_IEC, B: float = B_IEC, C: float = C_IEC) -> float:
    """Temperature (°C) for a platinum-style RTD with coefficients A, B, C and R0."""
    if r_ohms is None or not math.isfinite(r_ohms) or r0 <= 0 or B == 0:
        return float("nan")
    ratio = r_ohms / r0
    if ratio >= 1.0:  # T >= 0 C, closed-form inverse of R = R0(1 + A T + B T^2)
        disc = A * A - 4.0 * B * (1.0 - ratio)
        if disc < 0:
            return float("nan")
        return (-A + math.sqrt(disc)) / (2.0 * B)
    # T < 0 C: DIN 60751 inverse polynomial on the Pt100-normalised resistance.
    # Exact for standard coefficients; a good approximation for mild custom ones.
    rn = ratio * 100.0
    return (-242.02 + 2.2228 * rn + 2.5859e-3 * rn**2 - 4.8260e-6 * rn**3
            - 2.8183e-8 * rn**4 + 1.5243e-10 * rn**5)


def linear_temperature(r_ohms: float, r0: float, t0: float, alpha: float) -> float:
    """Generic linear metal: T = T0 + (R/R0 - 1)/alpha."""
    if r_ohms is None or not math.isfinite(r_ohms) or r0 <= 0 or alpha == 0:
        return float("nan")
    return t0 + (r_ohms / r0 - 1.0) / alpha


def copper_winding_rise(r2: float, r1: float, t1: float, k: float = 235.0) -> float:
    """Resistance method for winding temperature: T2 = (R2/R1)*(k+T1) - k.
    k = 235 for copper, 225 for aluminium."""
    if r1 is None or not math.isfinite(r1) or r1 <= 0:
        return float("nan")
    return (r2 / r1) * (k + t1) - k


# --- calibration curve: fit T = f(R) through measured (R, T) points -----------
def _solve(A, b):
    """Gaussian elimination with partial pivoting. Returns x for A x = b, or None."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-15:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def polyfit(xs, ys, degree):
    """Least-squares polynomial coefficients [c0, c1, ...] (c0 + c1 x + ...),
    computed about the mean of xs for numerical stability. Returns (coeffs, x0)."""
    m = len(xs)
    degree = min(degree, m - 1)
    n = degree + 1
    x0 = sum(xs) / m
    xc = [x - x0 for x in xs]
    sx = [0.0] * (2 * degree + 1)
    for x in xc:
        p = 1.0
        for k in range(2 * degree + 1):
            sx[k] += p
            p *= x
    Amat = [[sx[i + j] for j in range(n)] for i in range(n)]
    bvec = [0.0] * n
    for xi, yi in zip(xc, ys):
        p = 1.0
        for i in range(n):
            bvec[i] += yi * p
            p *= xi
    coeffs = _solve(Amat, bvec)
    return coeffs, x0


def cvd_coeffs_from_points(points):
    """Solve the Callendar-Van Dusen coefficients (R0, A, B) of
    R = R0 (1 + A*T + B*T^2) from calibration points (T_celsius, R_ohms).
    2 points -> (R0, A, 0); 3+ points -> quadratic (exact for 3). Returns
    (R0, A, B) or None. This is how an RTD is calibrated against a cal sheet."""
    pts = [(float(T), float(R)) for (T, R) in points]
    if len(pts) < 2:
        return None
    deg = 2 if len(pts) >= 3 else 1
    nd = deg + 1
    Ts = [p[0] for p in pts]
    Rs = [p[1] for p in pts]
    sx = [0.0] * (2 * deg + 1)
    for T in Ts:
        p = 1.0
        for k in range(2 * deg + 1):
            sx[k] += p
            p *= T
    Amat = [[sx[i + j] for j in range(nd)] for i in range(nd)]
    bvec = [0.0] * nd
    for T, R in zip(Ts, Rs):
        p = 1.0
        for i in range(nd):
            bvec[i] += R * p
            p *= T
    sol = _solve(Amat, bvec)
    if sol is None or sol[0] == 0:
        return None
    a = sol[0]
    b = sol[1]
    c = sol[2] if nd > 2 else 0.0
    return (a, b / a, c / a)


def calibration_temperature(r_ohms: float, points, max_degree: int = 2) -> float:
    """Fit T(R) through the (R, T) calibration points and evaluate at r_ohms.
    `points` = iterable of (resistance_ohms, temperature_C). Needs >= 2 points."""
    pts = [(float(r), float(t)) for (r, t) in points]
    if r_ohms is None or not math.isfinite(r_ohms) or len(pts) < 2:
        return float("nan")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    degree = min(max_degree, len(pts) - 1)
    fit = polyfit(xs, ys, degree)
    if fit[0] is None:
        return float("nan")
    coeffs, x0 = fit
    x = r_ohms - x0
    t, p = 0.0, 1.0
    for c in coeffs:
        t += c * p
        p *= x
    return t


# --- NTC thermistors ----------------------------------------------------------
def ntc_beta_temperature(r_ohms, r0, t0_c, beta):
    """NTC Beta model: 1/T = 1/T0 + ln(R/R0)/B   (T in kelvin). R0 at T0 (°C)."""
    if r_ohms is None or not math.isfinite(r_ohms) or r_ohms <= 0 or r0 <= 0 or beta == 0:
        return float("nan")
    t0k = t0_c + 273.15
    inv = 1.0 / t0k + math.log(r_ohms / r0) / beta
    if inv == 0:
        return float("nan")
    return 1.0 / inv - 273.15


def steinhart_hart_from_points(points):
    """Fit Steinhart-Hart 1/T = A + B·ln(R) + C·ln(R)^3 from (T_celsius, R_ohms)
    points. 3+ points -> (A,B,C) exact/LSQ; 2 points -> (A,B,0). Returns tuple or None."""
    pts = [(float(T), float(R)) for (T, R) in points if float(R) > 0]
    if len(pts) < 2:
        return None
    cubic = len(pts) >= 3
    n = 3 if cubic else 2
    xs = [math.log(R) for (_, R) in pts]
    ys = [1.0 / (T + 273.15) for (T, _) in pts]
    basis = (lambda x: [1.0, x, x ** 3]) if cubic else (lambda x: [1.0, x])
    Amat = [[0.0] * n for _ in range(n)]
    bvec = [0.0] * n
    for x, y in zip(xs, ys):
        ph = basis(x)
        for i in range(n):
            bvec[i] += ph[i] * y
            for j in range(n):
                Amat[i][j] += ph[i] * ph[j]
    sol = _solve(Amat, bvec)
    if sol is None:
        return None
    return (sol[0], sol[1], sol[2] if cubic else 0.0)


def ntc_sh_temperature(r_ohms, A, B, C):
    """Steinhart-Hart: 1/T = A + B·ln(R) + C·ln(R)^3 ; returns °C."""
    if r_ohms is None or not math.isfinite(r_ohms) or r_ohms <= 0:
        return float("nan")
    x = math.log(r_ohms)
    inv = A + B * x + C * x ** 3
    if inv == 0:
        return float("nan")
    return 1.0 / inv - 273.15


if __name__ == "__main__":
    for r in (100.0, 138.5055, 119.4, 80.31):
        print(f"Pt100  R={r:8.3f} Ω -> {rtd_temperature(r, 100):8.3f} °C")
    for r in (1000.0, 1385.06):
        print(f"Pt1000 R={r:8.2f} Ω -> {rtd_temperature(r, 1000):8.3f} °C")
    # calibration curve through 3 sheet points
    cal = [(100.0, 0.0), (119.4, 50.0), (138.5, 100.0)]
    for r in (100.0, 119.4, 130.0):
        print(f"Cal    R={r:8.3f} Ω -> {calibration_temperature(r, cal):8.3f} °C")
