#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║       JPS TIEMPOS LAB — NIST SP 800-22 Statistical Test Suite    ║
║   Batería estándar de criptografía para detectar bias en RNGs.   ║
╚══════════════════════════════════════════════════════════════════╝

Referencia: NIST Special Publication 800-22 Rev 1a
            https://nvlpubs.nist.gov/nistpubs/legacy/sp/nistspecialpublication800-22r1a.pdf

USO:
  python3 jps_nist_sts.py

Tests implementados aquí (los que faltaban del 800-22 + complementan a
jps_randomness_tests.py):

  1. Frequency Monobit              ← bias de 0s vs 1s
  2. Block Frequency                ← uniformidad por bloques
  3. Longest Run in Block           ← racha máxima de 1s
  4. Cumulative Sums (CUSUM)        ← random walk del cumsum
  5. Approximate Entropy            ← regularidad de patrones m-bit
  6. Serial Test                    ← uniformidad de m-tuplas overlapping
  7. Non-Overlapping Template       ← búsqueda de templates específicos
  8. Maurer's Universal Statistical ← compresibilidad estilo Lempel-Ziv

Los tests del NIST se aplican a secuencias BINARIAS. Convertimos la
secuencia de números 0-99 a binario (7 bits por número, descartando
ceros leading para uniformidad).

Veredicto:
  - 0 tests rechazan H0 → RNG indistinguible de aleatorio
  - 1+ tests rechazan → estructura detectada
"""

import json
import math
import os
from datetime import datetime
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
HISTORICAL = "historical_data.json"


# ────────────────────────────────────────────────────────────────────
# Helpers comunes
# ────────────────────────────────────────────────────────────────────

def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_sf(x: float, df: int) -> float:
    """Wilson-Hilferty para chi² survival function."""
    if df <= 0 or x <= 0:
        return 1.0
    z = ((x / df) ** (1/3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return normal_sf(z)


def gamma_lower_regularized(a: float, x: float, terms: int = 200) -> float:
    """Lower regularized gamma function P(a, x) via series expansion.

    Usada por algunos tests del NIST (igamc = 1 - P(a, x))."""
    if x <= 0:
        return 0.0
    # Series of e^-x * x^a * sum(x^n / Gamma(a+1+n))
    log_pre = -x + a * math.log(x) - math.lgamma(a + 1)
    s = 1.0
    term = 1.0
    for n in range(1, terms):
        term *= x / (a + n)
        s += term
        if term < 1e-15:
            break
    return math.exp(log_pre) * s


def gamma_upper_regularized(a: float, x: float) -> float:
    """igamc(a, x) = 1 - P(a, x) — usada por varios tests NIST."""
    return max(0.0, 1.0 - gamma_lower_regularized(a, x))


def to_bitstring(seq: List[int], bits_per_num: int = 3) -> List[int]:
    """Convierte secuencia 0-99 a lista de bits sin sesgo de representación.

    IMPORTANTE: Para 0-99, solo los bits 0,1,2 (LSB) están perfectamente
    50/50 distribuidos. Los bits 3,4 tienen sesgo leve (48%/52%) y los bits
    5,6 tienen sesgo fuerte (36%/64%) porque los valores 100-127 nunca
    aparecen en el rango lottery.

    Si usamos los 7 bits naturales, los tests NIST detectarían el sesgo
    de la representación binaria, no de la lotería. Por eso usamos solo
    los bits LSB perfectamente balanceados.

    bits_per_num=3 → 3 bits × n por número (sin sesgo)
    bits_per_num=5 → 5 bits con sesgo leve <2pp (aproximación útil para más data)
    """
    out = []
    for x in seq:
        for i in range(bits_per_num):
            out.append((x >> i) & 1)
    return out


# ────────────────────────────────────────────────────────────────────
# TEST 1 — Frequency (Monobit)
# ────────────────────────────────────────────────────────────────────

def frequency_monobit(bits: List[int]) -> dict:
    """¿Hay aprox. igual cantidad de 0s y 1s? (NIST 2.1)"""
    n = len(bits)
    s = sum(1 if b == 1 else -1 for b in bits)
    s_obs = abs(s) / math.sqrt(n)
    p = math.erfc(s_obs / math.sqrt(2))
    return {
        "name": "Frequency Monobit",
        "n_bits": n,
        "n_ones": sum(bits),
        "n_zeros": n - sum(bits),
        "S_obs": round(s_obs, 4),
        "p_value": round(p, 6),
        "interpretation": "bias de bits" if p < 0.01 else "balance de bits ok",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 2 — Block Frequency
# ────────────────────────────────────────────────────────────────────

def block_frequency(bits: List[int], M: int = 128) -> dict:
    """¿La proporción de 1s es uniforme dentro de cada bloque de M bits? (NIST 2.2)"""
    n = len(bits)
    N = n // M
    if N < 1:
        return {"name": "Block Frequency", "error": f"n<M (need n≥{M})"}
    chi2 = 0
    for i in range(N):
        block = bits[i*M:(i+1)*M]
        pi = sum(block) / M
        chi2 += (pi - 0.5) ** 2
    chi2 *= 4 * M
    p = gamma_upper_regularized(N / 2, chi2 / 2)
    return {
        "name": "Block Frequency",
        "block_size_M": M,
        "n_blocks": N,
        "chi2": round(chi2, 4),
        "p_value": round(p, 6),
        "interpretation": "no uniforme dentro de bloques" if p < 0.01 else "uniforme dentro de bloques",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 3 — Longest Run of Ones in a Block
# ────────────────────────────────────────────────────────────────────

def longest_run_of_ones(bits: List[int]) -> dict:
    """¿La racha más larga de 1s es consistente con random? (NIST 2.4)

    Para n=128 → M=8 bloques. Categorías observed vs expected.
    Para n=6272 → M=128. Para n=750000+ → M=10000.
    """
    n = len(bits)
    if n < 128:
        return {"name": "Longest Run of Ones", "error": "n<128"}

    if n < 6272:
        M = 8;   K = 3
        probs = [0.21484375, 0.3671875, 0.23046875, 0.1875]
        categories_max = [1, 2, 3, 4]  # ≤1, =2, =3, ≥4
    elif n < 750000:
        M = 128; K = 5
        probs = [0.1174035788, 0.242955959, 0.249363483, 0.17517706, 0.102701071, 0.112398847]
        categories_max = [4, 5, 6, 7, 8, 9]  # ≤4, 5, 6, 7, 8, ≥9
    else:
        M = 10000; K = 6
        probs = [0.0882, 0.2092, 0.2483, 0.1933, 0.1208, 0.0675, 0.0727]
        categories_max = [10, 11, 12, 13, 14, 15, 16]
    N = n // M

    counts = [0] * (K + 1)
    for i in range(N):
        block = bits[i*M:(i+1)*M]
        # Find longest run of 1s
        max_run = 0; cur = 0
        for b in block:
            if b == 1:
                cur += 1
                if cur > max_run:
                    max_run = cur
            else:
                cur = 0
        # Asignar a categoría
        idx = K
        for j, lim in enumerate(categories_max[:-1] if N == M else categories_max):
            if max_run <= lim:
                idx = j
                break
        counts[idx] += 1

    chi2 = sum((counts[i] - N * probs[i]) ** 2 / (N * probs[i]) for i in range(len(probs)) if probs[i] > 0)
    p = gamma_upper_regularized(K / 2, chi2 / 2)
    return {
        "name": "Longest Run of Ones",
        "M": M, "N": N, "K": K,
        "counts": counts,
        "chi2": round(chi2, 4),
        "p_value": round(p, 6),
        "interpretation": "racha más larga inconsistente" if p < 0.01 else "rachas compatibles con random",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 4 — Cumulative Sums (CUSUM)
# ────────────────────────────────────────────────────────────────────

def cumulative_sums(bits: List[int], mode: str = "forward") -> dict:
    """Random walk con ±1 según bits. El máximo cumsum NO debe ser muy grande
    si los bits son random. (NIST 2.13)"""
    n = len(bits)
    walks = [1 if b == 1 else -1 for b in bits]
    if mode == "backward":
        walks = walks[::-1]
    s = 0
    cs = []
    for w in walks:
        s += w
        cs.append(s)
    z = max(abs(x) for x in cs)
    # Aproximación de NIST para p-value
    sum1 = sum(
        normal_sf(((4*k+1)*z) / math.sqrt(n)) - normal_sf(((4*k-1)*z) / math.sqrt(n))
        for k in range(int((-n/z + 1) / 4), int((n/z - 1) / 4) + 1)
    )
    sum2 = sum(
        normal_sf(((4*k+3)*z) / math.sqrt(n)) - normal_sf(((4*k+1)*z) / math.sqrt(n))
        for k in range(int((-n/z - 3) / 4), int((n/z - 1) / 4) + 1)
    )
    p = 1 - sum1 + sum2
    return {
        "name": f"Cumulative Sums ({mode})",
        "n_bits": n,
        "max_abs_cusum": z,
        "p_value": round(max(0, min(1, p)), 6),
        "interpretation": "drift en cumsum" if p < 0.01 else "cumsum compatible con random walk",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 5 — Approximate Entropy (ApEn)
# ────────────────────────────────────────────────────────────────────

def approximate_entropy(bits: List[int], m: int = 10) -> dict:
    """Compara frecuencia de bloques de m vs m+1 bits. Si la secuencia es
    random, ApEn ≈ log(2). (NIST 2.12)"""
    n = len(bits)
    if n < (m + 1) ** 2:
        # Reducimos m si la secuencia es chica
        m = max(2, int(math.log2(n)) - 5)
        if m < 2:
            return {"name": "Approximate Entropy", "error": f"n muy chico para m≥2"}

    def _phi(M: int) -> float:
        # Secuencia circular
        seq = bits + bits[:M - 1]
        counts = {}
        for i in range(n):
            pat = tuple(seq[i:i + M])
            counts[pat] = counts.get(pat, 0) + 1
        s = 0.0
        for v in counts.values():
            p = v / n
            s += p * math.log(p)
        return s

    phi_m = _phi(m)
    phi_m1 = _phi(m + 1)
    apen = phi_m - phi_m1
    chi2 = 2 * n * (math.log(2) - apen)
    df = 2 ** m
    p = gamma_upper_regularized(df / 2, chi2 / 2)
    return {
        "name": "Approximate Entropy",
        "m": m,
        "phi_m": round(phi_m, 4),
        "phi_m+1": round(phi_m1, 4),
        "ApEn": round(apen, 4),
        "expected_for_random": round(math.log(2), 4),
        "chi2": round(chi2, 4),
        "p_value": round(p, 6),
        "interpretation": "regularidad inesperada" if p < 0.01 else "ApEn compatible con random",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 6 — Serial Test
# ────────────────────────────────────────────────────────────────────

def serial_test(bits: List[int], m: int = 16) -> dict:
    """Frecuencia de m-tuplas overlapping. Si todas las 2^m tuplas tienen
    aprox. la misma frecuencia → secuencia es random. (NIST 2.11)"""
    n = len(bits)
    if n < 2 ** m:
        m = max(2, int(math.log2(n)) - 4)

    def _psi2(M: int) -> float:
        if M <= 0:
            return 0.0
        seq = bits + bits[:M - 1]
        counts = {}
        for i in range(n):
            pat = tuple(seq[i:i + M])
            counts[pat] = counts.get(pat, 0) + 1
        return (2 ** M / n) * sum(v * v for v in counts.values()) - n

    psi2_m   = _psi2(m)
    psi2_m1  = _psi2(m - 1)
    psi2_m2  = _psi2(m - 2)
    delta1   = psi2_m - psi2_m1
    delta2   = psi2_m - 2 * psi2_m1 + psi2_m2
    p1 = gamma_upper_regularized(2 ** (m - 2), delta1 / 2)
    p2 = gamma_upper_regularized(2 ** (m - 3), delta2 / 2) if m >= 3 else None
    rej = p1 < 0.01 or (p2 is not None and p2 < 0.01)
    return {
        "name": "Serial Test",
        "m": m,
        "psi2_m": round(psi2_m, 2),
        "psi2_m-1": round(psi2_m1, 2),
        "delta1": round(delta1, 2),
        "delta2": round(delta2, 2),
        "p_value_1": round(p1, 6),
        "p_value_2": round(p2, 6) if p2 is not None else None,
        "p_value": min(p1, p2) if p2 is not None else p1,
        "interpretation": "m-tuplas con frecuencia inesperada" if rej else "m-tuplas uniformes",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 7 — Non-Overlapping Template Matching
# ────────────────────────────────────────────────────────────────────

def non_overlapping_template(bits: List[int], template: List[int] = None) -> dict:
    """Cuenta apariciones de un template específico en bloques no-solapantes.
    Si la secuencia es random, conteo sigue distribución binomial aproximada
    a normal. (NIST 2.7)"""
    if template is None:
        template = [0, 0, 0, 0, 0, 0, 0, 1, 1]   # template estándar NIST
    m = len(template)
    n = len(bits)
    N = 8     # bloques
    M = n // N
    if M < m:
        return {"name": "Non-Overlapping Template", "error": "secuencia muy corta"}

    # Probabilidad teórica de que el template aparezca en una posición específica
    mu = (M - m + 1) / (2 ** m)
    var = M * (1.0 / (2 ** m) - (2 * m - 1) / (2 ** (2 * m)))

    chi2 = 0
    counts = []
    for i in range(N):
        block = bits[i * M:(i + 1) * M]
        cnt = 0
        j = 0
        while j <= len(block) - m:
            if block[j:j + m] == template:
                cnt += 1
                j += m   # non-overlapping
            else:
                j += 1
        counts.append(cnt)
        chi2 += (cnt - mu) ** 2 / var
    p = gamma_upper_regularized(N / 2, chi2 / 2)
    return {
        "name": "Non-Overlapping Template",
        "template": template,
        "m": m, "N": N, "M": M,
        "expected_mu": round(mu, 4),
        "var": round(var, 4),
        "counts_per_block": counts,
        "chi2": round(chi2, 4),
        "p_value": round(p, 6),
        "interpretation": "template aparece más/menos de lo esperado" if p < 0.01 else "frecuencia compatible",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# TEST 8 — Maurer's Universal Statistical Test (simplificado)
# ────────────────────────────────────────────────────────────────────

def maurers_universal(bits: List[int], L: int = 7, Q: int = 1280) -> dict:
    """Mide distancia entre repeticiones de bloques L-bit.
    Si la secuencia es random, distancia promedio sigue distribución conocida.
    Detecta compresibilidad estilo Lempel-Ziv. (NIST 2.9)"""
    n = len(bits)
    K = n // L - Q
    if K < 1000:
        # Reducir L
        L = 6
        Q = 640
        K = n // L - Q
        if K < 100:
            return {"name": "Maurer's Universal", "error": "n insuficiente"}

    # Expected values & variances from NIST table (for L=6,7,...)
    expected_table = {
        6: (5.2177052, 2.954),
        7: (6.1962507, 3.125),
        8: (7.1836656, 3.238),
        9: (8.1764248, 3.311),
    }
    if L not in expected_table:
        L = 7
        Q = 1280
        K = n // L - Q
    expected, var = expected_table[L]

    # Initialization
    last_pos = {}
    for i in range(Q):
        pat = tuple(bits[i * L:(i + 1) * L])
        last_pos[pat] = i + 1

    # Test
    fn = 0.0
    for i in range(Q, Q + K):
        pat = tuple(bits[i * L:(i + 1) * L])
        prev = last_pos.get(pat, 0)
        fn += math.log2((i + 1) - prev)
        last_pos[pat] = i + 1
    fn /= K

    # P-value
    c = 0.7 - 0.8 / L + (4 + 32 / L) * (K ** (-3 / L)) / 15
    sigma = c * math.sqrt(var / K)
    z = abs((fn - expected) / sigma)
    p = math.erfc(z / math.sqrt(2))
    return {
        "name": "Maurer's Universal",
        "L": L, "Q": Q, "K": K,
        "fn_observed": round(fn, 4),
        "fn_expected": round(expected, 4),
        "sigma": round(sigma, 4),
        "z_stat": round(z, 4),
        "p_value": round(p, 6),
        "interpretation": "compresibilidad detectada" if p < 0.01 else "incompresible (random)",
        "p_threshold": 0.01,
    }


# ────────────────────────────────────────────────────────────────────
# DATA LOADING
# ────────────────────────────────────────────────────────────────────

def load_chronological() -> List[int]:
    with open(os.path.join(DATA_DIR, HISTORICAL), "r", encoding="utf-8") as f:
        data = json.load(f)
    sequence = []
    sorted_days = sorted(data, key=lambda d: (d.get("dia", "")))
    for day in sorted_days:
        for sess in ("manana", "mediaTarde", "tarde"):
            draw = day.get(sess)
            if draw and draw.get("numero") is not None:
                try:
                    sequence.append(int(draw["numero"]))
                except (ValueError, TypeError):
                    pass
    return sequence


# ────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────

def main():
    print("\n╔══════════════════════════════════════════════════════════════════╗")
    print("║   NIST SP 800-22 Statistical Test Suite                          ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    if not os.path.exists(os.path.join(DATA_DIR, HISTORICAL)):
        print(f"\n  ⚠ No existe {HISTORICAL}. Corré fetch primero.")
        return

    seq = load_chronological()
    # 3 bits LSB sin sesgo (vs 7 bits naturales que introducen sesgo artificial
    # porque 100-127 nunca aparecen en lottery 0-99)
    bits = to_bitstring(seq, bits_per_num=3)
    print(f"\n  Secuencia: {len(seq)} sorteos → {len(bits)} bits (3 LSB sin sesgo)")
    print(f"  α = 0.01 (más estricto que tests anteriores con α=0.05)")
    print(f"  H0: bits son IID Bernoulli(0.5)")
    print(f"  Nota: usamos solo bits 0-2 porque están perfectamente 50/50 para 0-99.")
    print(f"        Bits 5-6 tienen sesgo artificial (36% vs 64%) del encoding.")

    results = []
    results.append(frequency_monobit(bits))
    results.append(block_frequency(bits, M=128))
    results.append(longest_run_of_ones(bits))
    results.append(cumulative_sums(bits, "forward"))
    results.append(cumulative_sums(bits, "backward"))
    results.append(approximate_entropy(bits, m=10))
    results.append(serial_test(bits, m=16))
    results.append(non_overlapping_template(bits))
    results.append(maurers_universal(bits))

    print(f"\n  ──────────────────────────────────────────────────────────────────")
    print(f"  {'TEST':<35} {'P-VALUE':>10}  VEREDICTO")
    print(f"  ──────────────────────────────────────────────────────────────────")
    rejected = 0
    accepted = 0
    for r in results:
        name = r["name"][:34]
        p = r.get("p_value")
        if "error" in r:
            print(f"  {name:<35} {'—':>10}  SKIP: {r['error']}")
            continue
        if p is None:
            continue
        verdict = "REJECT" if p < r.get("p_threshold", 0.01) else "accept"
        if verdict == "REJECT":
            rejected += 1
        else:
            accepted += 1
        p_str = f"{p:.6f}" if isinstance(p, (int, float)) else "—"
        print(f"  {name:<35} {p_str:>10}  {verdict}")
    print(f"  ──────────────────────────────────────────────────────────────────")

    # Detalle (compacto)
    print(f"\n  ── DETALLE ──")
    for r in results:
        if "error" in r:
            continue
        name = r["name"]
        p = r.get("p_value")
        interp = r.get("interpretation", "")
        if isinstance(p, (int, float)):
            p_str = f"p={p:.4f}"
        else:
            p_str = "—"
        print(f"    {name:<34} {p_str:<12}  {interp}")

    print(f"\n  ╔══════════════════════════════════════════════════════════════════╗")
    print(f"  ║   VEREDICTO NIST SP 800-22                                       ║")
    print(f"  ╚══════════════════════════════════════════════════════════════════╝")
    print(f"  Rechazaron H0 (p < 0.01): {rejected}")
    print(f"  Aceptaron H0:             {accepted}")

    if rejected == 0:
        print(f"\n  🎲 La secuencia pasa el estándar criptográfico NIST SP 800-22.")
        print(f"     Es indistinguible de un RNG criptográficamente seguro.")
    elif rejected <= 1:
        print(f"\n  ⚠ {rejected} test rechazó — al borde de falso positivo (esperado ~0.09 con α=0.01).")
    else:
        print(f"\n  🔍 {rejected} tests rechazaron — evidencia clara de estructura no-aleatoria.")

    report = {
        "generated_at": datetime.now().isoformat(),
        "n_samples": len(seq),
        "n_bits": len(bits),
        "tests": results,
        "n_rejected": rejected,
        "n_accepted": accepted,
    }
    with open(os.path.join(DATA_DIR, "nist_sts_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  ✓ Reporte completo: nist_sts_report.json\n")


if __name__ == "__main__":
    main()
