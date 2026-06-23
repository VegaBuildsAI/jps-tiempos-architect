#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║       JPS TIEMPOS LAB — Tests de Aleatoriedad Rigurosos          ║
║  Batería de tests estadísticos para detectar si la secuencia de  ║
║  sorteos JPS es genuinamente aleatoria o tiene estructura oculta. ║
╚══════════════════════════════════════════════════════════════════╝

USO:
  python3 jps_randomness_tests.py

Tests implementados (todos pure-Python, sin dependencias externas):

  1. Chi² goodness of fit — distribución uniforme global
  2. Runs test (Wald-Wolfowitz) — independencia de la secuencia
  3. Ljung-Box test — autocorrelación en múltiples lags
  4. Shannon entropy — qué tan "aleatoria" es la secuencia
  5. Autocorrelation per lag — correlación con sorteos anteriores
  6. Spectral / FFT analysis — periodicidades ocultas
  7. Run-length analysis — longitudes de rachas

VEREDICTO FINAL:
  Si la mayoría de tests aceptan H0 (uniforme/independiente):
    → No hay señal detectable → ningún algoritmo va a ganar dinero
    → Recomendación: paper trading, no apostar dinero real

  Si algún test rechaza H0 con p<0.05:
    → Hay estructura detectable → vale la pena explorar más algoritmos
    → Próximo paso: HMM, Kalman filter, change-point detection
"""

import json
import math
import os
import sys
from collections import Counter
from datetime import datetime
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
HISTORICAL = "historical_data.json"

# ════════════════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════════════════

def load_chronological() -> List[int]:
    """Devuelve los exactos en orden cronológico (mañana, mediaTarde, tarde)."""
    with open(os.path.join(DATA_DIR, HISTORICAL), "r", encoding="utf-8") as f:
        data = json.load(f)
    sequence = []
    session_order = {"manana": 1, "mediaTarde": 2, "tarde": 3}
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


# ════════════════════════════════════════════════════════════════════
# DISTRIBUCIONES Y P-VALUES (pure Python)
# ════════════════════════════════════════════════════════════════════

def normal_sf(z: float) -> float:
    """Survival function de la normal estándar: P(Z > z)."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_sf(x: float, df: int) -> float:
    """Aprox. survival function chi² con Wilson-Hilferty (suficientemente preciso)."""
    if df <= 0 or x <= 0:
        return 1.0
    z = ((x / df) ** (1/3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return normal_sf(z)


# ════════════════════════════════════════════════════════════════════
# TEST 1 — Chi² goodness of fit
# ════════════════════════════════════════════════════════════════════

def chi_square_uniform(seq: List[int], n_categories: int = 100) -> dict:
    """¿La distribución de números 00-99 es uniforme?"""
    n = len(seq)
    expected = n / n_categories
    counts = [0] * n_categories
    for x in seq:
        if 0 <= x < n_categories:
            counts[x] += 1
    chi2 = sum((c - expected) ** 2 / expected for c in counts) if expected else 0
    df = n_categories - 1
    p = chi2_sf(chi2, df)
    return {
        "name": "Chi² goodness of fit",
        "test_stat": round(chi2, 4),
        "df": df,
        "p_value": round(p, 6),
        "n_samples": n,
        "interpretation": "rechaza uniforme" if p < 0.05 else "compatible con uniforme",
        "p_threshold": 0.05,
    }


# ════════════════════════════════════════════════════════════════════
# TEST 2 — Runs test (Wald-Wolfowitz)
# ════════════════════════════════════════════════════════════════════

def runs_test_above_below_median(seq: List[int]) -> dict:
    """Convierte la secuencia en binaria (arriba/abajo de la mediana) y cuenta runs.
    Si los sorteos son IID, el número de runs sigue distribución conocida."""
    if len(seq) < 20:
        return {"name": "Runs test", "error": "n < 20"}
    sorted_seq = sorted(seq)
    median = sorted_seq[len(seq) // 2]
    # Excluye exactos == median para tener dos grupos limpios
    bits = [1 if x > median else 0 for x in seq if x != median]
    n = len(bits)
    n1 = sum(bits)
    n2 = n - n1
    if n1 == 0 or n2 == 0:
        return {"name": "Runs test", "error": "split degenerado"}
    # Contar runs (cambios de bit)
    runs = 1
    for i in range(1, n):
        if bits[i] != bits[i-1]:
            runs += 1
    # Distribución asintótica del runs test
    mu = (2 * n1 * n2) / n + 1
    var = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1))
    z = (runs - mu) / math.sqrt(var) if var > 0 else 0
    p = 2 * normal_sf(abs(z))  # two-tailed
    return {
        "name": "Runs test (above/below median)",
        "test_stat": round(z, 4),
        "p_value": round(p, 6),
        "n_runs_observed": runs,
        "n_runs_expected": round(mu, 2),
        "n_samples": n,
        "interpretation": "secuencia NO independiente" if p < 0.05 else "compatible con secuencia independiente",
        "p_threshold": 0.05,
    }


# ════════════════════════════════════════════════════════════════════
# TEST 3 — Ljung-Box (autocorrelación multi-lag)
# ════════════════════════════════════════════════════════════════════

def autocorrelation(seq: List[float], lag: int) -> float:
    """ACF lag-h, normalizado por varianza."""
    n = len(seq)
    if lag >= n:
        return 0.0
    mean = sum(seq) / n
    var = sum((x - mean) ** 2 for x in seq) / n
    if var == 0:
        return 0.0
    cov = sum((seq[i] - mean) * (seq[i + lag] - mean) for i in range(n - lag)) / n
    return cov / var


def ljung_box(seq: List[int], lags: int = 20) -> dict:
    """H0: no hay autocorrelación en ningún lag hasta `lags`.
    Q = n(n+2) Σ r_h² / (n-h) ~ chi²(lags) bajo H0."""
    seq_f = [float(x) for x in seq]
    n = len(seq_f)
    rs = [autocorrelation(seq_f, h) for h in range(1, lags + 1)]
    Q = n * (n + 2) * sum(rs[h-1] ** 2 / (n - h) for h in range(1, lags + 1))
    p = chi2_sf(Q, lags)
    # Top 5 lags con mayor autocorrelation absoluta
    rs_abs = [(i+1, r) for i, r in enumerate(rs)]
    rs_abs.sort(key=lambda x: abs(x[1]), reverse=True)
    top_lags = [{"lag": l, "acf": round(r, 4)} for l, r in rs_abs[:5]]
    return {
        "name": f"Ljung-Box (lags 1-{lags})",
        "test_stat": round(Q, 4),
        "df": lags,
        "p_value": round(p, 6),
        "n_samples": n,
        "top_lags_by_abs_acf": top_lags,
        "interpretation": "hay autocorrelación detectable" if p < 0.05 else "compatible con cero autocorrelación",
        "p_threshold": 0.05,
    }


# ════════════════════════════════════════════════════════════════════
# TEST 4 — Shannon entropy
# ════════════════════════════════════════════════════════════════════

def shannon_entropy(seq: List[int], n_symbols: int = 100) -> dict:
    """Entropía empírica vs máxima posible. Cerca del máximo → distribución uniforme."""
    counts = Counter(seq)
    n = len(seq)
    H = 0.0
    for c in counts.values():
        p = c / n
        if p > 0:
            H -= p * math.log2(p)
    H_max = math.log2(n_symbols)
    ratio = H / H_max
    return {
        "name": "Shannon entropy",
        "entropy_observed": round(H, 4),
        "entropy_max_uniform": round(H_max, 4),
        "ratio": round(ratio, 6),
        "n_unique_symbols": len(counts),
        "interpretation": (
            f"distribución muy uniforme (entropía {ratio*100:.1f}% del máximo)"
            if ratio > 0.99 else
            f"distribución sesgada (entropía solo {ratio*100:.1f}% del máximo)"
        ),
        "p_threshold": 0.99,
    }


# ════════════════════════════════════════════════════════════════════
# TEST 5 — Per-lag autocorrelation (visual)
# ════════════════════════════════════════════════════════════════════

def per_lag_acf(seq: List[int], max_lag: int = 30) -> dict:
    """ACF para cada lag 1..max_lag, con banda de confianza 95%."""
    seq_f = [float(x) for x in seq]
    n = len(seq_f)
    ci_95 = 1.96 / math.sqrt(n)
    lags_data = []
    for h in range(1, max_lag + 1):
        r = autocorrelation(seq_f, h)
        outside_ci = abs(r) > ci_95
        lags_data.append({"lag": h, "acf": round(r, 4), "outside_95_ci": outside_ci})
    n_outside = sum(1 for x in lags_data if x["outside_95_ci"])
    # Bajo H0 (random) esperamos ~5% outside CI = ~max_lag*0.05
    expected_outside = max_lag * 0.05
    return {
        "name": f"Per-lag ACF (1-{max_lag})",
        "ci_95_band": round(ci_95, 4),
        "n_lags_outside_ci": n_outside,
        "expected_outside_if_random": round(expected_outside, 2),
        "lags": lags_data,
        "interpretation": (
            f"{n_outside} lags fuera del CI 95% (esperado por azar: {expected_outside:.1f}) — "
            + ("posible estructura" if n_outside > expected_outside * 2 else "compatible con random")
        ),
    }


# ════════════════════════════════════════════════════════════════════
# TEST 6 — Spectral / FFT analysis
# ════════════════════════════════════════════════════════════════════

def spectral_analysis(seq: List[int], top_k: int = 5) -> dict:
    """DFT manual para detectar periodicidades. Si todas las frecuencias tienen
    poder similar → secuencia aleatoria. Si una se destaca → patrón periódico."""
    n = len(seq)
    seq_f = [float(x) for x in seq]
    mean = sum(seq_f) / n
    centered = [x - mean for x in seq_f]
    # DFT: solo frecuencias 1..n/2 (Nyquist)
    half = n // 2
    power = []
    for k in range(1, min(half, 200) + 1):  # limito a 200 frecs para velocidad
        wk = 2 * math.pi * k / n
        re = sum(centered[t] * math.cos(wk * t) for t in range(n))
        im = sum(centered[t] * math.sin(wk * t) for t in range(n))
        power.append((k, (re*re + im*im) / n))
    power.sort(key=lambda x: x[1], reverse=True)
    top = [{"frequency_k": k, "period_approx": round(n/k, 1), "power": round(p, 2)}
           for k, p in power[:top_k]]
    # Bajo white noise, las powers tienen distribución exponencial con mean = var(centered)
    var = sum(c*c for c in centered) / n
    # Una power > 3*var es sospechosa
    max_power = power[0][1]
    sospechosa = max_power > 3 * var
    return {
        "name": "Spectral / FFT analysis",
        "top_periodicities": top,
        "noise_floor_estimate": round(var, 2),
        "max_power": round(max_power, 2),
        "interpretation": (
            "frecuencia destacada — posible periodicidad" if sospechosa
            else "espectro plano — compatible con ruido blanco"
        ),
    }


# ════════════════════════════════════════════════════════════════════
# TEST 7B — Compression test (information-theoretic, [arxiv:cs/0504006])
# ════════════════════════════════════════════════════════════════════

def compression_test(seq: List[int], n_baseline: int = 200) -> dict:
    """Test info-teórico: si la secuencia es comprimible, NO es aleatoria.

    Comprime con 3 algoritmos (zlib/bz2/lzma) y compara con baseline empírico
    generado a partir de secuencias IID uniformes del mismo largo. Si nuestro
    ratio es significativamente menor (z<-2) → hay estructura compresible.
    """
    import zlib, bz2, lzma
    import random as _r

    # Pasar secuencia a bytes (números 0-99 caben en 1 byte cada uno)
    data = bytes(seq)
    n = len(data)
    if n < 100:
        return {"name": "Compression test", "error": "n<100"}

    def _ratios(d: bytes) -> dict:
        return {
            "zlib": len(zlib.compress(d, 9)) / len(d),
            "bz2":  len(bz2.compress(d, 9))  / len(d),
            "lzma": len(lzma.compress(d, preset=9)) / len(d),
        }

    observed = _ratios(data)

    # Baseline empírico — generar N secuencias uniformes y medir ratio
    rng = _r.Random(42)
    baselines = {"zlib": [], "bz2": [], "lzma": []}
    for _ in range(n_baseline):
        rand_seq = bytes(rng.randint(0, 99) for _ in range(n))
        for k, v in _ratios(rand_seq).items():
            baselines[k].append(v)

    def _stats(lst):
        m = sum(lst) / len(lst)
        var = sum((x-m)**2 for x in lst) / max(1, len(lst)-1)
        return m, math.sqrt(var)

    rejected_any = False
    per_algo = {}
    for k in ("zlib", "bz2", "lzma"):
        mean, std = _stats(baselines[k])
        obs = observed[k]
        z = (obs - mean) / std if std > 0 else 0
        # P-value two-tailed (obs muy bajo → comprime más → patrón)
        p = 2 * normal_sf(abs(z))
        rej = p < 0.05 and z < 0   # solo rechazamos si comprime MÁS que esperado
        if rej:
            rejected_any = True
        per_algo[k] = {
            "observed_ratio": round(obs, 5),
            "baseline_mean": round(mean, 5),
            "baseline_std": round(std, 5),
            "z_score": round(z, 4),
            "p_value": round(p, 6),
            "rejected": rej,
        }

    return {
        "name": "Compression test (zlib/bz2/lzma)",
        "n_samples": n,
        "n_baseline_runs": n_baseline,
        "per_algorithm": per_algo,
        "p_value": min(a["p_value"] for a in per_algo.values()),
        "interpretation": (
            "secuencia comprime MÁS que random → hay estructura"
            if rejected_any else
            "comprime igual que random → sin estructura compresible"
        ),
        "p_threshold": 0.05,
    }


# ════════════════════════════════════════════════════════════════════
# TEST 7 — Run-length analysis
# ════════════════════════════════════════════════════════════════════

def run_length_analysis(seq: List[int]) -> dict:
    """Longitudes de rachas arriba/abajo de la mediana. Bajo IID, las longitudes
    siguen geométrica con p=0.5 → esperado mean=2, max ~log2(n)."""
    sorted_seq = sorted(seq)
    median = sorted_seq[len(seq) // 2]
    bits = [1 if x > median else 0 for x in seq if x != median]
    if not bits:
        return {"name": "Run-length analysis", "error": "no data"}
    run_lengths = []
    cur_bit = bits[0]
    cur_len = 1
    for b in bits[1:]:
        if b == cur_bit:
            cur_len += 1
        else:
            run_lengths.append(cur_len)
            cur_bit = b
            cur_len = 1
    run_lengths.append(cur_len)
    n = len(bits)
    expected_max = math.log2(n) + 1  # heurística para max bajo IID
    observed_max = max(run_lengths)
    observed_mean = sum(run_lengths) / len(run_lengths)
    return {
        "name": "Run-length analysis",
        "n_runs": len(run_lengths),
        "max_run_length_observed": observed_max,
        "max_run_length_expected_iid": round(expected_max, 2),
        "mean_run_length_observed": round(observed_mean, 2),
        "mean_run_length_expected_iid": 2.0,
        "interpretation": (
            f"max racha {observed_max} >> esperado {expected_max:.1f} — posible clustering"
            if observed_max > expected_max * 1.5
            else "longitudes compatibles con IID"
        ),
    }


# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    print("\n╔══════════════════════════════════════════════════════════════════╗")
    print("║   JPS TIEMPOS LAB — Tests de Aleatoriedad Rigurosos              ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    if not os.path.exists(os.path.join(DATA_DIR, HISTORICAL)):
        print(f"\n  ⚠ No existe {HISTORICAL}. Corré: python3 jps_edge_tool.py fetch --mode history --days 180")
        return

    seq = load_chronological()
    print(f"\n  Secuencia cargada: {len(seq)} sorteos en orden cronológico")
    print(f"  Rango: 00-99  ·  Test: ¿es uniforme + independiente?")
    print(f"  H0 (hipótesis nula): la secuencia es IID uniforme")
    print(f"  Rechazamos H0 si p < 0.05")

    results = []
    results.append(chi_square_uniform(seq))
    results.append(runs_test_above_below_median(seq))
    results.append(ljung_box(seq, lags=20))
    results.append(shannon_entropy(seq))
    results.append(per_lag_acf(seq, max_lag=30))
    results.append(spectral_analysis(seq))
    results.append(run_length_analysis(seq))
    results.append(compression_test(seq))

    print(f"\n  ──────────────────────────────────────────────────────────────────")
    print(f"  {'TEST':<35} {'STAT':>12} {'P-VALUE':>10}  RESULT")
    print(f"  ──────────────────────────────────────────────────────────────────")
    rejected = 0
    accepted = 0
    for r in results:
        name = r["name"][:34]
        stat = r.get("test_stat") or r.get("entropy_observed") or r.get("max_power") or ""
        p = r.get("p_value")
        if p is not None:
            verdict = "REJECT" if p < r.get("p_threshold", 0.05) else "accept"
            if verdict == "REJECT":
                rejected += 1
            else:
                accepted += 1
            stat_str = f"{stat:.2f}" if isinstance(stat, (int, float)) else str(stat)
            p_str = f"{p:.4f}"
            print(f"  {name:<35} {stat_str:>12} {p_str:>10}  {verdict}")
        else:
            stat_str = f"{stat:.2f}" if isinstance(stat, (int, float)) else str(stat)
            print(f"  {name:<35} {stat_str:>12} {'—':>10}  (info)")
    print(f"  ──────────────────────────────────────────────────────────────────")

    # Reporte detallado
    print(f"\n  ── DETALLE DE CADA TEST ──\n")
    for r in results:
        print(f"  [{r['name']}]")
        for k, v in r.items():
            if k == "name":
                continue
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"    {k}:")
                for item in v[:5]:
                    print(f"      • {item}")
            else:
                print(f"    {k}: {v}")
        print()

    # Veredicto final
    print(f"\n  ╔══════════════════════════════════════════════════════════════════╗")
    print(f"  ║   VEREDICTO ESTADÍSTICO                                          ║")
    print(f"  ╚══════════════════════════════════════════════════════════════════╝")
    print(f"  Tests con p-value:")
    print(f"    Rechazaron H0 (p < 0.05): {rejected}")
    print(f"    Aceptaron H0:             {accepted}")
    print()
    if rejected == 0:
        print(f"  🎲 NINGÚN test detecta estructura en la secuencia.")
        print(f"     La secuencia es estadísticamente indistinguible de IID uniforme.")
        print(f"     → Ningún algoritmo va a generar +EV en este juego.")
        print(f"     → Recomendación: paper trading, NO apostar dinero real.")
    elif rejected <= 1:
        print(f"  ⚠ {rejected} test rechazó H0 — podría ser falso positivo o señal real.")
        print(f"     Con 7 tests y α=0.05 se esperan ~0.35 rechazos por puro azar.")
        print(f"     {rejected} rechazo está al borde — vale la pena investigar el test específico.")
    else:
        print(f"  🔍 {rejected} tests rechazaron H0 — hay evidencia de estructura.")
        print(f"     Vale la pena explorar HMM, change-point, Kalman.")
        print(f"     Justifica seguir con algoritmos más sofisticados.")

    print()
    # Guardar reporte JSON
    report = {
        "generated_at": datetime.now().isoformat(),
        "n_samples": len(seq),
        "tests": results,
        "n_rejected": rejected,
        "n_accepted": accepted,
    }
    out_path = os.path.join(DATA_DIR, "randomness_tests_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"  ✓ Reporte completo guardado en: randomness_tests_report.json\n")


if __name__ == "__main__":
    main()
