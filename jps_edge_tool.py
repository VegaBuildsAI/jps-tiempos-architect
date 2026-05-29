#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           JPS TIEMPOS LAB — EDGE TOOL  v2.0                     ║
║  Análisis estadístico · Bet Engine · Audit · Monte Carlo        ║
║  Nuevos Tiempos Reventados — JPS Costa Rica                     ║
╚══════════════════════════════════════════════════════════════════╝

MODOS DE USO:
  python jps_edge_tool.py fetch   --mode last
  python jps_edge_tool.py fetch   --mode history --days 60
  python jps_edge_tool.py analyze
  python jps_edge_tool.py bet     --budget 5000 --n 5 --profile balanced
  python jps_edge_tool.py audit   --exacto 47 --reventada SI
  python jps_edge_tool.py run     --budget 5000 --n 5 --days 60

FLUJO RECOMENDADO:
  1. Fetch histórico:   python jps_edge_tool.py fetch --mode history --days 60
  2. Analizar:          python jps_edge_tool.py analyze
  3. Construir apuesta: python jps_edge_tool.py bet --budget 5000 --n 5
  4. (Tras el sorteo):  python jps_edge_tool.py audit --exacto 47 --reventada SI
"""

import argparse
import json
import math
import os
import random
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Tuple

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
BASE_URL     = "https://integration.jps.go.cr"
EXACTO_MULT  = 70   # Per reglas oficiales JPS Costa Rica
REV_MULT     = 200  # Reventados (condicional a acertar Exacto + bola Reventada)
P_EXACTO     = 1 / 100
P_REV        = 1 / 3
N_MONTE      = 20_000
SEED_DEFAULT = 42

HERE = os.path.dirname(os.path.abspath(__file__))

# DATA_DIR: dónde viven los archivos de datos (JSONL, JSON, reports).
# Local: mismo dir que el código (HERE). Railway/container: /data (env var).
# Esto permite que el código corra IDÉNTICO local y en deploy persistente.
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)


def path(filename: str) -> str:
    """Resuelve la ruta de un archivo de DATOS.

    Si JPS_DATA_DIR está configurada (Railway), usa ese directorio.
    Si no, usa el directorio del código (modo local clásico).
    """
    return os.path.join(DATA_DIR, filename)


def save_json(data: dict, filename: str):
    with open(path(filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False, default=str)
    print(f"  ✓ Guardado: {filename}")


def load_json(filename: str) -> dict:
    fp = path(filename)
    if not os.path.exists(fp):
        raise FileNotFoundError(f"No existe '{filename}'. Ejecuta el paso previo primero.")
    with open(fp, "rb") as f:
        raw = f.read().rstrip(b"\x00")   # strip trailing null bytes (artifact del acumulador)
    return json.loads(raw.decode("utf-8"))


# ─────────────────────────────────────────────
# SECTION 1 — API FETCH  (corre localmente)
# ─────────────────────────────────────────────

def api_get(endpoint: str) -> dict:
    import gzip as _gzip
    url = BASE_URL + endpoint
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Origin": "https://www.jps.go.cr",
        "Referer": "https://www.jps.go.cr/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw_bytes = r.read()
            # Detect gzip by magic bytes (server omits Content-Encoding header)
            if raw_bytes[:2] == b'\x1f\x8b':
                raw_bytes = _gzip.decompress(raw_bytes)
            raw = raw_bytes.decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} al llamar {url}\n  Respuesta: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"No se pudo conectar a {url}: {e.reason}")


def cmd_fetch(args):
    print("\n═══ FETCH ═══")
    if args.mode == "last":
        print("Consultando último resultado...")
        data = api_get("/api/App/nuevostiempos/last")
        save_json(data, "last_result.json")
        _print_last_result(data)

    elif args.mode == "history":
        days = getattr(args, "days", 60)
        end   = datetime.now()
        start = end - timedelta(days=days)
        fmt   = "%Y-%m-%dT%H:%M:%S"
        print(f"Consultando histórico: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')} ({days} días)...")
        endpoint = (
            f"/api/App/nuevostiempos/historical"
            f"?fechaInicio={start.strftime(fmt)}&fechaFin={end.strftime(fmt)}"
        )
        data = api_get(endpoint)
        save_json(data, "historical_data.json")
        items = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(items, list):
            print(f"  Registros recibidos: {len(items)}")
        else:
            print("  Respuesta guardada (verificar estructura en historical_data.json)")
    else:
        print(f"Modo fetch desconocido: {args.mode}")


def _print_last_result(data: dict):
    dia = data.get("dia", "?")
    print(f"\n  Día: {dia}")
    print(f"  {'Sorteo':<12} {'Hora':<8} {'Exacto':<8} {'Mega':<8} {'Reventada':<12} {'Bolita'}")
    print("  " + "─" * 60)
    for slot_name in ["manana", "mediaTarde", "tarde"]:
        slot = data.get(slot_name)
        if not slot:
            label = {"manana": "Mañana", "mediaTarde": "Media tarde", "tarde": "Tarde"}.get(slot_name, slot_name)
            print(f"  {label:<12} (sin resultado)")
            continue
        label = {"manana": "Mañana", "mediaTarde": "Media tarde", "tarde": "Tarde"}.get(slot_name, slot_name)
        hora   = slot.get("hora", "?")
        numero = str(slot.get("numero", "?")).zfill(2)
        mega   = str(slot.get("meganNumero", "?")).zfill(2)
        rev    = "SI" if slot.get("in_reventado", 0) == 1 else "NO"
        bolita = slot.get("colorBolita", slot.get("descripcionBolita", "?"))
        print(f"  {label:<12} {str(hora):<8} {numero:<8} {mega:<8} {rev:<12} {bolita}")


# ─────────────────────────────────────────────
# SECTION 2 — STATISTICAL ANALYSIS
# ─────────────────────────────────────────────

SESSION_KEYS = ("manana", "mediaTarde", "tarde")

def _infer_session(draw: dict) -> Optional[str]:
    """Infiere la sesión desde el campo 'hora' de un sorteo plano.

    Rangos: <12h → manana | 12–16h → mediaTarde | >=16h → tarde.
    Acepta 'HH:MM', 'HH:MM:SS', o 'YYYY-MM-DDTHH:MM:SS'.
    Retorna None si hora no está presente o no es parseable.
    """
    hora = draw.get("hora", "")
    if not hora:
        return None
    try:
        time_part = hora.split("T")[-1] if "T" in str(hora) else str(hora)
        hour = int(time_part.split(":")[0])
        if hour < 12:
            return "manana"
        elif hour < 16:
            return "mediaTarde"
        else:
            return "tarde"
    except (ValueError, IndexError, AttributeError):
        return None


def _extract_draws(data, session_filter: Optional[str] = None) -> List[dict]:
    """Normaliza el histórico a lista plana de sorteos con campo 'session'.

    Soporta dos formatos del API:
    • Lista de objetos-día  {dia, manana:{numero,…}, mediaTarde:{…}, tarde:{…}}
    • Lista plana de sorteos {numero, in_reventado, …}

    Agrega campo 'session' ("manana"|"mediaTarde"|"tarde") en formato día.
    session_filter: si se especifica, devuelve solo los sorteos de esa sesión.
    """
    draws: List[dict] = []

    if isinstance(data, list):
        if data and isinstance(data[0], dict) and any(k in data[0] for k in SESSION_KEYS):
            # Formato día → aplanar con etiqueta de sesión
            for day in data:
                dia = day.get("dia", "")
                for slot in SESSION_KEYS:
                    s = day.get(slot)
                    if s and isinstance(s, dict) and s.get("numero") is not None:
                        draws.append({**s, "session": slot, "dia": dia})
        else:
            # Formato plano: intentar inferir sesión desde campo 'hora'
            for item in data:
                inferred = _infer_session(item)
                draws.append({**item, "session": inferred} if inferred else item)
    elif isinstance(data, dict):
        for key in ("data", "results", "items", "sorteos"):
            if key in data and isinstance(data[key], list):
                return _extract_draws(data[key], session_filter)
        # Objeto-día individual
        dia = data.get("dia", "")
        for slot in SESSION_KEYS:
            s = data.get(slot)
            if s and isinstance(s, dict) and s.get("numero") is not None:
                draws.append({**s, "session": slot, "dia": dia})

    if session_filter and session_filter != "all":
        draws = [d for d in draws if d.get("session") == session_filter]

    return draws


def _chi_square_uniform(observed: List[int], n_categories: int) -> Tuple[float, float]:
    """Chi-cuadrado vs distribución uniforme. Devuelve (chi2, p_approx)."""
    total = sum(observed)
    expected = total / n_categories
    if expected == 0:
        return 0.0, 1.0
    chi2 = sum((o - expected) ** 2 / expected for o in observed)
    # p-value aproximado por complemento CDF chi2 (df = n_categories-1)
    # Usamos aproximación Wilson-Hilferty para no depender de scipy
    df = n_categories - 1
    z = ((chi2 / df) ** (1/3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    p_approx = _normal_sf(z)
    return round(chi2, 4), round(p_approx, 6)


def _normal_sf(z: float) -> float:
    """Survival function N(0,1): P(Z > z)."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def _binom_ci(k: int, n: int, confidence: float = 0.99) -> Tuple[float, float]:
    """Intervalo de confianza binomial (Wilson score)."""
    if n == 0:
        return 0.0, 1.0
    alpha = 1 - confidence
    z = 2.576  # 99%
    p_hat = k / n
    denom = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) / denom
    return round(max(0, center - margin), 6), round(min(1, center + margin), 6)


def _z_score(observed: int, total: int, p_expected: float) -> float:
    """Z-score para una proporción observada vs esperada."""
    if total == 0:
        return 0.0
    p_hat = observed / total
    se = math.sqrt(p_expected * (1 - p_expected) / total)
    if se == 0:
        return 0.0
    return round((p_hat - p_expected) / se, 4)


def cmd_analyze(args, _draws: Optional[List[dict]] = None, _label: str = "TODOS",
                _no_save: bool = False, _mc_iterations: Optional[int] = None) -> Optional[dict]:
    """Análisis estadístico. Acepta sorteos pre-filtrados via _draws.

    _no_save: si True, no escribe analysis_report.json (útil para callers
              programáticos como el backtester que invocan cientos de veces).
    _mc_iterations: override del Monte Carlo interno para los IC de Reventada.
              Default = N_MONTE (20,000). Útil bajarlo a ~200 desde el backtester
              porque esos IC no afectan las decisiones del bet engine.
    Retorna el report dict (o None si no se pudo generar).
    """
    session_tag = f" [{_label}]" if _label != "TODOS" else ""
    print(f"\n═══ ANÁLISIS ESTADÍSTICO{session_tag} ═══")
    if _draws is not None:
        draws = _draws
    else:
        data = load_json("historical_data.json")
        draws = _extract_draws(data)

    if not draws:
        print("  ERROR: No se encontraron sorteos en historical_data.json")
        print("  Verifica la estructura del archivo.")
        sys.exit(1)

    n_draws = len(draws)
    print(f"  Sorteos encontrados: {n_draws}")

    # Count frecuencias
    freq_num  = [0] * 100   # índice = número 00-99
    freq_mega = [0] * 100   # índice = meganNumero 00-99
    rev_count = 0
    valid     = 0
    valid_mega = 0

    for d in draws:
        num  = d.get("numero")
        mega = d.get("meganNumero")
        rev  = d.get("in_reventado")
        if num is None:
            continue
        try:
            num = int(num)
        except (ValueError, TypeError):
            continue
        if 0 <= num <= 99:
            freq_num[num] += 1
            valid += 1
        if mega is not None:
            try:
                mega = int(mega)
                if 0 <= mega <= 99:
                    freq_mega[mega] += 1
                    valid_mega += 1
            except (ValueError, TypeError):
                pass
        if rev is not None:
            try:
                rev_count += int(rev)
            except (ValueError, TypeError):
                pass

    if valid == 0:
        print("  ERROR: No se pudo extraer números válidos del histórico.")
        sys.exit(1)

    expected_per_num  = valid / 100
    expected_per_mega = valid_mega / 100 if valid_mega > 0 else 1

    # ── Números: Chi-cuadrado
    chi2_num, p_num = _chi_square_uniform(freq_num, 100)

    # ── Reventada: Prueba binomial (z-test)
    rev_observed_rate = rev_count / valid if valid > 0 else 0
    rev_z = _z_score(rev_count, valid, P_REV)
    rev_ci_lo, rev_ci_hi = _binom_ci(rev_count, valid)

    # ── Rankings
    ranked = sorted(
        [(i, freq_num[i]) for i in range(100)],
        key=lambda x: x[1],
        reverse=True
    )

    # ── Mega Rankings
    ranked_mega = sorted(
        [(i, freq_mega[i]) for i in range(100)],
        key=lambda x: x[1],
        reverse=True
    )
    chi2_mega, p_mega = _chi_square_uniform(freq_mega, 100) if valid_mega > 0 else (0.0, 1.0)

    # ── Weights exacto (para Monte Carlo ponderado)
    # Peso = frecuencia_observada / frecuencia_esperada (capped para evitar extremos)
    weights = {}
    for num, freq in enumerate(freq_num):
        raw_w = (freq / expected_per_num) if expected_per_num > 0 else 1.0
        # Suavizado bayesiano: mezcla 80% observado + 20% uniforme
        smoothed = 0.80 * raw_w + 0.20 * 1.0
        smoothed = max(0.5, min(2.0, smoothed))
        weights[str(num).zfill(2)] = round(smoothed, 4)

    # ── Weights mega (para selección secundaria)
    mega_weights = {}
    for num, freq in enumerate(freq_mega):
        raw_w = (freq / expected_per_mega) if expected_per_mega > 0 else 1.0
        smoothed = 0.80 * raw_w + 0.20 * 1.0
        smoothed = max(0.5, min(2.0, smoothed))
        mega_weights[str(num).zfill(2)] = round(smoothed, 4)

    # ── Combined weight: exacto 75% + mega 25%
    combined_weights = {}
    for k in weights:
        combined_weights[k] = round(0.75 * weights[k] + 0.25 * mega_weights.get(k, 1.0), 4)

    # ── Streak analysis para Reventada
    rev_sequence = []
    for d in draws:
        r = d.get("in_reventado")
        if r is not None:
            try:
                rev_sequence.append(int(r) == 1)
            except:
                pass

    max_streak_si = max_streak_no = 0
    cur_si = cur_no = 0
    for is_rev in rev_sequence:
        if is_rev:
            cur_si += 1
            cur_no  = 0
            max_streak_si = max(max_streak_si, cur_si)
        else:
            cur_no += 1
            cur_si  = 0
            max_streak_no = max(max_streak_no, cur_no)

    # ── Percentiles de frecuencia
    all_freqs = sorted(freq_num)
    p5_freq   = all_freqs[int(0.05 * 100)]
    p95_freq  = all_freqs[int(0.95 * 100)]

    # ── Monte Carlo Reventada
    n_mc = _mc_iterations if _mc_iterations is not None else N_MONTE
    random.seed(SEED_DEFAULT)
    if n_mc > 1:
        mc_rev_rates = []
        for _ in range(n_mc):
            mc_hits = sum(1 for _ in range(valid) if random.randint(1, 3) == 1)
            mc_rev_rates.append(mc_hits / valid)
        mc_mean  = sum(mc_rev_rates) / n_mc
        mc_std   = math.sqrt(sum((x - mc_mean)**2 for x in mc_rev_rates) / (n_mc - 1))
        mc_lo    = sorted(mc_rev_rates)[int(0.025 * n_mc)]
        mc_hi    = sorted(mc_rev_rates)[int(0.975 * n_mc)]
    else:
        mc_mean = mc_std = mc_lo = mc_hi = 0.0

    # Interpretation
    significance = "SIGNIFICATIVO (p < 0.05)" if p_num < 0.05 else "dentro de variabilidad esperada"
    rev_sig = abs(rev_z) > 2.576  # 99% CI

    print(f"\n  ┌─ REVENTADA ─────────────────────────────────────────┐")
    print(f"  │ Sorteos totales analizados : {n_draws:<6}               │")
    print(f"  │ Reventada SI observado     : {rev_count:<6} ({rev_observed_rate*100:.2f}%)        │")
    print(f"  │ Esperado (teórico)         : {valid/3:.1f}  (33.33%)         │")
    print(f"  │ Desviación                 : {(rev_observed_rate - P_REV)*100:+.2f}%                 │")
    print(f"  │ Z-score                    : {rev_z:+.4f}                │")
    print(f"  │ IC 99% Wilson              : [{rev_ci_lo:.4f}, {rev_ci_hi:.4f}]      │")
    print(f"  │ Monte Carlo IC 95%         : [{mc_lo:.4f}, {mc_hi:.4f}]      │")
    print(f"  │ {'⚠ DESVIACIÓN SIGNIFICATIVA' if rev_sig else '✓ Dentro de rango normal':<49}│")
    print(f"  │ Racha máx SI               : {max_streak_si:<4}                  │")
    print(f"  │ Racha máx NO               : {max_streak_no:<4}                  │")
    print(f"  └─────────────────────────────────────────────────────┘")

    print(f"\n  ┌─ NÚMEROS 00–99 ──────────────────────────────────────┐")
    print(f"  │ Chi-cuadrado               : {chi2_num:<10.4f}           │")
    print(f"  │ p-value aproximado         : {p_num:<10.6f}           │")
    print(f"  │ Esperado por número        : {expected_per_num:<10.2f}           │")
    print(f"  │ Frecuencia P5 / P95        : {p5_freq} / {p95_freq:<20}  │")
    print(f"  │ Distribución               : {significance:<31}│")
    print(f"  └─────────────────────────────────────────────────────┘")

    print(f"\n  TOP 15 NÚMEROS (más frecuentes):")
    print(f"  {'#':<4} {'Núm':<6} {'Frec':<6} {'Vs Esp':>8} {'Z-score':>9} {'Peso':>7}")
    print(f"  {'─'*4} {'─'*6} {'─'*6} {'─'*8} {'─'*9} {'─'*7}")
    for rank, (num, freq) in enumerate(ranked[:15], 1):
        z  = _z_score(freq, valid, P_EXACTO)
        vs = freq - expected_per_num
        w  = weights[str(num).zfill(2)]
        print(f"  {rank:<4} {str(num).zfill(2):<6} {freq:<6} {vs:>+8.1f} {z:>+9.4f} {w:>7.4f}")

    print(f"\n  BOTTOM 10 NÚMEROS (menos frecuentes):")
    print(f"  {'#':<4} {'Núm':<6} {'Frec':<6} {'Vs Esp':>8} {'Z-score':>9}")
    print(f"  {'─'*4} {'─'*6} {'─'*6} {'─'*8} {'─'*9}")
    for rank, (num, freq) in enumerate(ranked[-10:], 91):
        z  = _z_score(freq, valid, P_EXACTO)
        vs = freq - expected_per_num
        print(f"  {rank:<4} {str(num).zfill(2):<6} {freq:<6} {vs:>+8.1f} {z:>+9.4f}")

    # ── MEGA block
    if valid_mega > 0:
        mega_sig = p_mega < 0.05
        print(f"\n  ┌─ MEGA REVENTADOS ────────────────────────────────────┐")
        print(f"  │ Sorteos con Mega        : {valid_mega:<6}                  │")
        print(f"  │ Esperado por número     : {expected_per_mega:<10.2f}             │")
        print(f"  │ Chi-cuadrado Mega       : {chi2_mega:<10.4f}             │")
        print(f"  │ p-value Mega            : {p_mega:<10.6f}             │")
        print(f"  │ Distribución Mega       : {'SIGNIFICATIVA (p<0.05)' if mega_sig else 'dentro de rango normal':<31}│")
        print(f"  └─────────────────────────────────────────────────────┘")
        print(f"\n  TOP 15 MEGA (más frecuentes):")
        print(f"  {'#':<4} {'Mega':<6} {'Frec':<6} {'Vs Esp':>8} {'Z-score':>9} {'PesoM':>7}")
        print(f"  {'─'*4} {'─'*6} {'─'*6} {'─'*8} {'─'*9} {'─'*7}")
        for rank, (num, freq) in enumerate(ranked_mega[:15], 1):
            z  = _z_score(freq, valid_mega, 1/100)
            vs = freq - expected_per_mega
            mw = mega_weights[str(num).zfill(2)]
            print(f"  {rank:<4} {str(num).zfill(2):<6} {freq:<6} {vs:>+8.1f} {z:>+9.4f} {mw:>7.4f}")
        print(f"\n  TOP 10 COMBINADOS (peso exacto 75% + mega 25%):")
        print(f"  {'#':<4} {'Núm':<6} {'PesoE':>7} {'PesoM':>7} {'PesoC':>7}")
        print(f"  {'─'*4} {'─'*6} {'─'*7} {'─'*7} {'─'*7}")
        ranked_combined = sorted(combined_weights.items(), key=lambda x: x[1], reverse=True)
        for rank, (k, cw) in enumerate(ranked_combined[:10], 1):
            we = weights[k]
            wm = mega_weights.get(k, 1.0)
            print(f"  {rank:<4} {k:<6} {we:>7.4f} {wm:>7.4f} {cw:>7.4f}")

    # Save analysis report
    report = {
        "generated_at": datetime.now().isoformat(),
        "n_draws_analyzed": n_draws,
        "n_valid_numbers": valid,
        "reventada": {
            "count_si": rev_count,
            "observed_rate": round(rev_observed_rate, 6),
            "expected_rate": round(P_REV, 6),
            "deviation_pct": round((rev_observed_rate - P_REV) * 100, 4),
            "z_score": rev_z,
            "ci_99_wilson": [rev_ci_lo, rev_ci_hi],
            "monte_carlo_ci_95": [round(mc_lo, 6), round(mc_hi, 6)],
            "significant": rev_sig,
            "max_streak_si": max_streak_si,
            "max_streak_no": max_streak_no,
        },
        "numbers": {
            "chi2": chi2_num,
            "p_value": p_num,
            "expected_per_number": round(expected_per_num, 4),
            "significant": p_num < 0.05,
        },
        "ranked_numbers": [
            {
                "rank": r + 1,
                "numero": str(num).zfill(2),
                "frecuencia": freq,
                "vs_esperado": round(freq - expected_per_num, 2),
                "z_score": _z_score(freq, valid, P_EXACTO),
                "peso": weights[str(num).zfill(2)],
            }
            for r, (num, freq) in enumerate(ranked)
        ],
        "weights": weights,
        "mega_weights": mega_weights,
        "combined_weights": combined_weights,
        "mega_numbers": {
            "chi2": chi2_mega,
            "p_value": p_mega,
            "n_draws": valid_mega,
            "expected_per_number": round(expected_per_mega, 4),
            "significant": p_mega < 0.05,
        },
        "ranked_mega": [
            {
                "rank": r + 1,
                "meganNumero": str(num).zfill(2),
                "frecuencia": freq,
                "vs_esperado": round(freq - expected_per_mega, 2),
                "z_score": _z_score(freq, valid_mega, 1/100) if valid_mega > 0 else 0,
                "peso_mega": mega_weights[str(num).zfill(2)],
            }
            for r, (num, freq) in enumerate(ranked_mega)
        ],
        "disclaimer": "Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado.",
    }
    if not _no_save:
        save_json(report, "analysis_report.json")
    print(f"\n  DISCLAIMER: {report['disclaimer']}\n")
    return report


def cmd_session_analyze(args):
    """Análisis estadístico aislado por sesión (manana | mediaTarde | tarde)."""
    session = args.session
    LABELS = {
        "manana":     "MAÑANA     (~10:55)",
        "mediaTarde": "MEDIA TARDE (~14:00)",
        "tarde":      "TARDE       (~18:00)",
    }
    label = LABELS.get(session, session.upper())
    print(f"\n╔══════════════════════════════════════════════════════╗")
    print(f"║  ANÁLISIS AISLADO · SESIÓN: {label:<24}║")
    print(f"╚══════════════════════════════════════════════════════╝")

    data = load_json("historical_data.json")
    draws = _extract_draws(data, session_filter=session)

    if not draws:
        print(f"\n  ERROR: No se encontraron sorteos para sesión '{session}'.")
        print(f"  Asegúrate de haber corrido: python jps_accumulate.py")
        sys.exit(1)

    print(f"  Sub-corpus: {len(draws)} sorteos · solo sesión {label}")
    print(f"  (El análisis global usa {len(_extract_draws(data))} sorteos)")

    class FakeArgs:
        pass

    # ── Back up the global analysis_report.json before session analysis overwrites it.
    # cmd_analyze() always saves to analysis_report.json, so running it with session-
    # filtered draws would contaminate the global report used by cmd_bet().
    global_report_path = path("analysis_report.json")
    global_report_backup = None
    if os.path.exists(global_report_path):
        with open(global_report_path, "rb") as _f:
            global_report_backup = _f.read()

    # Run full analysis on session-filtered draws (writes session data to analysis_report.json)
    cmd_analyze(FakeArgs(), _draws=draws, _label=label)

    # Read the session-specific report that cmd_analyze just wrote
    session_report_file = f"analysis_session_{session}.json"
    try:
        session_report_content = load_json("analysis_report.json")
        session_data = {
            "session": session,
            "session_label": label,
            "n_draws_session": len(draws),
            "report": session_report_content,
        }
        save_json(session_data, session_report_file)
    except Exception:
        pass

    # ── Restore the global analysis_report.json so cmd_bet() is unaffected.
    if global_report_backup is not None:
        with open(global_report_path, "wb") as _f:
            _f.write(global_report_backup)
        print(f"  ✓ analysis_report.json global restaurado (no contaminado por filtro de sesión)")
    else:
        # No prior global report existed — remove the session-only one to avoid confusion
        try:
            os.remove(global_report_path)
        except OSError:
            pass
        print(f"  ⚠ No había analysis_report.json global previo. Corre 'analyze' antes de 'bet'.")


# ─────────────────────────────────────────────
# SECTION 3 — BET ENGINE
# ─────────────────────────────────────────────

@dataclass
class Ticket:
    num_exacto: int
    base: int
    rev: int
    tipo: str = "REVENTADO"


def _round100(x) -> int:
    return (int(x) // 100) * 100


def _build_tickets(
    numeros: List[int],
    budget: int,
    base_fixed: Optional[int] = None,
    rev_fixed: Optional[int] = None,
    rev_ratio: float = 0.5,
) -> Tuple[List[Ticket], int]:
    tickets = []
    if base_fixed is not None and rev_fixed is not None:
        for n in numeros:
            b = _round100(base_fixed)
            r = _round100(rev_fixed)
            if r > b:
                r = b
            tickets.append(Ticket(num_exacto=n, base=b, rev=r))
        total = sum(t.base + t.rev for t in tickets)
        if total > budget:
            raise ValueError(f"Presupuesto insuficiente: {total} > {budget}")
        return tickets, budget - total

    n = len(numeros)
    ticket_amount = _round100(budget // n)
    if ticket_amount < 200:
        raise ValueError("Budget muy bajo — se necesitan al menos ₡200 por ticket (base + rev mínimo).")

    for num in numeros:
        rev  = _round100(ticket_amount * rev_ratio)
        base = ticket_amount - rev
        base = _round100(base)
        rev  = ticket_amount - base
        # Enforce rev <= base
        while base > 0 and rev > base and rev >= 100:
            rev  -= 100
            base += 100
        base = ticket_amount - rev
        base = _round100(base)
        rev  = ticket_amount - base
        if base < 100:
            base = 100
            rev  = ticket_amount - base
            rev  = _round100(rev)
        if rev < 100:
            rev  = 100
            base = ticket_amount - rev
            base = _round100(base)
        tickets.append(Ticket(num_exacto=num, base=max(0, base), rev=max(0, rev)))

    total = sum(t.base + t.rev for t in tickets)
    return tickets, budget - total


def _profile_to_ratio(profile: str) -> float:
    return {"conservative": 0.25, "balanced": 0.45, "aggressive": 0.65}.get(profile, 0.45)


def _ev_ticket(t: Ticket) -> float:
    """Expected Value neto de un ticket (sistema aleatorio)."""
    cost = t.base + t.rev
    ev = P_EXACTO * (EXACTO_MULT * t.base + P_REV * REV_MULT * t.rev) - cost
    return round(ev, 4)


def cmd_bet(args):
    print("\n═══ BET ENGINE ═══")
    budget  = args.budget
    n_tix   = getattr(args, "n", 5)
    profile = getattr(args, "profile", "balanced")
    numbers = getattr(args, "numbers", None)

    # Load weights if analysis exists
    weights = None
    try:
        report = load_json("analysis_report.json")
        # Use combined_weights (exacto 75% + mega 25%) if available, else exacto only
        weights = report.get("combined_weights") or report.get("weights", None)
        has_mega = bool(report.get("mega_weights"))
        src = "combinado (Exacto 75% + Mega 25%)" if has_mega else "exacto"
        print(f"  ✓ Pesos desde análisis histórico cargados [{src}]")
    except FileNotFoundError:
        print("  (Sin análisis previo — usando pesos uniformes)")

    # Select numbers
    if numbers:
        try:
            selected = [int(x.strip().lstrip("0") or "0") for x in numbers.split(",")]
        except ValueError:
            print(f"  ERROR: --numbers contiene un valor inválido. Usa formato: '04,69,91'")
            sys.exit(1)
        selected = [n for n in selected if 0 <= n <= 99]
        if not selected:
            print(f"  ERROR: Ningún número en rango válido (00–99) después de parsear --numbers.")
            sys.exit(1)
        n_tix = len(selected)
        print(f"  Números especificados: {[str(n).zfill(2) for n in selected]}")
    elif weights:
        # Top numbers by weight
        ranked = sorted(weights.items(), key=lambda x: x[1], reverse=True)
        selected = [int(k) for k, _ in ranked[:n_tix]]
        print(f"  Números seleccionados (top peso): {[str(n).zfill(2) for n in selected]}")
    else:
        # Random uniform
        random.seed(SEED_DEFAULT)
        selected = random.sample(range(100), n_tix)
        print(f"  Números seleccionados (aleatorio): {[str(n).zfill(2) for n in selected]}")

    rev_ratio = _profile_to_ratio(profile)

    try:
        tickets, remanente = _build_tickets(selected, budget, rev_ratio=rev_ratio)
    except ValueError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    total_apostado = sum(t.base + t.rev for t in tickets)
    rev_exposure   = sum(t.rev for t in tickets)

    # Compute actual achieved rev ratio (may differ from requested due to ₡100 rounding)
    actual_rev_ratio = rev_exposure / total_apostado if total_apostado else 0
    ratio_note = ""
    if abs(actual_rev_ratio - rev_ratio) > 0.02:
        ratio_note = f"  ⚠ Rev ratio real: {actual_rev_ratio:.0%} (solicitado: {rev_ratio:.0%}, ajustado por restricción rev≤base y múltiplos de ₡100)"

    print(f"  Perfil: {profile.upper()}  |  Rev ratio solicitado: {rev_ratio:.0%}  |  Budget: ₡{budget:,}")
    if ratio_note:
        print(ratio_note)

    print(f"\n  ┌─ TICKETS ──────────────────────────────────────────────┐")
    print(f"  │ {'#':<4} {'Núm':<6} {'Base':>8} {'Rev':>8} {'Total':>8} {'EV_est':>10} │")
    print(f"  │ {'─'*4} {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*10} │")
    for i, t in enumerate(tickets, 1):
        ev = _ev_ticket(t)
        print(f"  │ {i:<4} {str(t.num_exacto).zfill(2):<6} ₡{t.base:>7,} ₡{t.rev:>7,} ₡{t.base+t.rev:>7,} ₡{ev:>9.2f} │")
    print(f"  └───────────────────────────────────────────────────────┘")

    # Risk rating
    rev_pct = rev_exposure / total_apostado if total_apostado else 0
    if rev_pct >= 0.55 or profile == "aggressive":
        risk = "ALTO"
    elif rev_pct >= 0.35:
        risk = "MEDIO"
    else:
        risk = "BAJO"

    print(f"\n  Budget total     : ₡{budget:,}")
    print(f"  Total apostado   : ₡{total_apostado:,}")
    print(f"  Remanente        : ₡{remanente:,}")
    print(f"  Exposición Rev   : ₡{rev_exposure:,}  ({rev_pct:.0%})")
    print(f"  Riesgo           : {risk}")

    # Save input.json for simulator
    w_selected = {}
    if weights:
        for n in selected:
            k = str(n).zfill(2)
            w_selected[k] = weights.get(k, 1.0)

    input_payload = {
        "budget_total": budget,
        "n_apuestas": n_tix,
        "rev_ratio": rev_ratio,
        "numeros_exacto": [str(n).zfill(2) for n in selected],
        "ticket_structure": {
            "base_por_ticket": tickets[0].base if tickets else 200,
            "rev_por_ticket_reventado": tickets[0].rev if tickets else 100,
        },
        "weights": w_selected if w_selected else None,
        "n_simulaciones": N_MONTE,
        "seed": SEED_DEFAULT,
    }
    # Clean nulls
    if not input_payload["weights"]:
        del input_payload["weights"]

    save_json(input_payload, "input.json")

    # Run Monte Carlo immediately
    _run_monte_carlo(tickets, w_selected if w_selected else None, budget, remanente)


# ─────────────────────────────────────────────
# SECTION 4 — MONTE CARLO SIMULATION
# ─────────────────────────────────────────────

def _parse_weights_vector(w: Optional[Dict]) -> Optional[List[float]]:
    if not w:
        return None
    vec = [1.0] * 100
    for k, v in w.items():
        try:
            idx = int(k)
            if 0 <= idx <= 99:
                vec[idx] = max(0.01, float(v))
        except:
            pass
    return vec


def _simulate_once(tickets: List[Ticket], weights_vec: Optional[List[float]]) -> int:
    cost = sum(t.base + t.rev for t in tickets)
    if weights_vec:
        exacto = random.choices(range(100), weights=weights_vec, k=1)[0]
    else:
        exacto = random.randint(0, 99)
    reventada = (random.randint(1, 3) == 1)
    recovered = 0
    for t in tickets:
        if t.num_exacto == exacto:
            recovered += EXACTO_MULT * t.base
            if reventada:
                recovered += REV_MULT * t.rev
    return recovered - cost


def _run_monte_carlo(
    tickets: List[Ticket],
    weights: Optional[Dict],
    budget: int,
    remanente: int,
    n: int = N_MONTE,
    seed: int = SEED_DEFAULT,
) -> dict:
    random.seed(seed)
    wvec = _parse_weights_vector(weights)
    nets = []
    wins = 0
    for _ in range(n):
        net = _simulate_once(tickets, wvec)
        nets.append(net)
        if net > 0:
            wins += 1

    avg  = sum(nets) / n
    var  = sum((x - avg) ** 2 for x in nets) / (n - 1)
    std  = math.sqrt(var)
    nets_sorted = sorted(nets)
    p5  = nets_sorted[int(0.05 * n)]
    med = nets_sorted[int(0.50 * n)]
    p95 = nets_sorted[int(0.95 * n)]
    total_apostado = sum(t.base + t.rev for t in tickets)

    # Profile diagnosis
    p_ganar = wins / n
    if p_ganar < 0.06 or med == -total_apostado:
        diag = "⚠ Alta probabilidad de perder todo (normal para este tipo de juego)"
    elif std >= 3 * total_apostado:
        diag = "⚠ Volatilidad extrema — resultado muy variable"
    else:
        diag = "✓ Perfil dentro de parámetros esperados"

    roi = avg / total_apostado if total_apostado else 0

    print(f"\n  ┌─ MONTE CARLO ({n:,} simulaciones) ────────────────────┐")
    print(f"  │ Promedio Neto      : ₡{avg:>10,.2f}                    │")
    print(f"  │ ROI estimado       : {roi:>10.2%}                    │")
    print(f"  │ Prob. Ganar        : {p_ganar:>10.2%}                    │")
    print(f"  │ Desviación Std     : ₡{std:>10,.2f}                    │")
    print(f"  │ P5 Neto (peor 5%)  : ₡{p5:>10,}                    │")
    print(f"  │ Mediana (típico)   : ₡{med:>10,}                    │")
    print(f"  │ P95 Neto (mejor5%) : ₡{p95:>10,}                    │")
    print(f"  │ {diag:<50}│")
    print(f"  └───────────────────────────────────────────────────────┘")

    result = {
        "Simulaciones": n,
        "Promedio Neto": round(avg, 2),
        "ROI_estimado": round(roi, 6),
        "Desviacion Std": round(std, 2),
        "Probabilidad Ganar": round(p_ganar, 4),
        "P5 Neto": p5,
        "Mediana Neto": med,
        "P95 Neto": p95,
        "diagnostico": diag,
    }

    output = {
        "budget_total": budget,
        "total_apostado": total_apostado,
        "remanente": remanente,
        "tickets": [asdict(t) for t in tickets],
        "monte_carlo_result": result,
    }
    save_json(output, "output.json")
    return result


def cmd_simulate(args):
    """Corre simulación sobre input.json existente."""
    print("\n═══ SIMULACIÓN MONTE CARLO ═══")
    payload = load_json("input.json")
    budget = int(payload["budget_total"])
    nums   = [int(x) for x in payload["numeros_exacto"]]
    ts     = payload.get("ticket_structure", {})
    base_f = int(ts.get("base_por_ticket", 0))
    rev_f  = int(ts.get("rev_por_ticket_reventado", 0))
    weights = payload.get("weights", None)

    if base_f > 0:
        tickets, remanente = _build_tickets(nums, budget, base_fixed=base_f, rev_fixed=rev_f)
    else:
        rev_ratio = float(payload.get("rev_ratio", 0.45))
        tickets, remanente = _build_tickets(nums, budget, rev_ratio=rev_ratio)

    _run_monte_carlo(tickets, weights, budget, remanente,
                     n=int(payload.get("n_simulaciones", N_MONTE)),
                     seed=int(payload.get("seed", SEED_DEFAULT)))


# ─────────────────────────────────────────────
# SECTION 5 — AUDIT ENGINE
# ─────────────────────────────────────────────

def payout_ticket(num_exacto: int, base: int, rev: int,
                  drawn_exacto: int, drawn_reventada: str) -> dict:
    """Calcula el outcome de un ticket dado un resultado de sorteo.

    Pura — sin I/O, sin globals. Reusable desde backtester, reconciler, etc.
    drawn_reventada: "SI" o "NO".
    Retorna: hit_exacto, exacto_win, rev_win, recuperado, cost, neto, roi.
    """
    cost = base + rev
    hit = (num_exacto == drawn_exacto)
    exacto_win = EXACTO_MULT * base if hit else 0
    rev_win    = REV_MULT * rev if (hit and drawn_reventada == "SI") else 0
    recuperado = exacto_win + rev_win
    neto       = recuperado - cost
    roi        = neto / cost if cost else 0
    return {
        "hit_exacto": hit,
        "exacto_win": exacto_win,
        "rev_win": rev_win,
        "recuperado": recuperado,
        "cost": cost,
        "neto": neto,
        "roi": roi,
    }


def cmd_audit(args):
    print("\n═══ AUDITORÍA ═══")
    resultado_exacto  = int(args.exacto.lstrip("0") or "0")
    resultado_rev     = args.reventada.strip().upper()  # SI / NO

    if resultado_rev not in ("SI", "NO"):
        print("  ERROR: --reventada debe ser SI o NO")
        sys.exit(1)

    # Load tickets from output.json or input.json
    try:
        data = load_json("output.json")
        tickets_raw = data["tickets"]
    except FileNotFoundError:
        data = load_json("input.json")
        tickets_raw = []

    if not tickets_raw:
        print("  No hay tickets guardados. Ejecuta 'bet' primero.")
        sys.exit(1)

    print(f"\n  Resultado oficial: Exacto = {str(resultado_exacto).zfill(2)}  |  Reventada = {resultado_rev}")
    print(f"\n  {'#':<4} {'Núm':<6} {'Base':>8} {'Rev':>8} {'Total':>8} {'Recuperado':>12} {'Neto':>10} {'ROI':>8}")
    print(f"  {'─'*4} {'─'*6} {'─'*8} {'─'*8} {'─'*8} {'─'*12} {'─'*10} {'─'*8}")

    total_apostado   = 0
    total_recuperado = 0
    audit_rows       = []

    for i, t in enumerate(tickets_raw, 1):
        num  = int(t.get("num_exacto", t.get("numero", 0)))
        base = int(t.get("base", 0))
        rev  = int(t.get("rev", 0))

        result = payout_ticket(num, base, rev, resultado_exacto, resultado_rev)
        cost       = result["cost"]
        hit        = result["hit_exacto"]
        recuperado = result["recuperado"]
        neto       = result["neto"]
        roi        = result["roi"]

        total_apostado   += cost
        total_recuperado += recuperado

        marker = " ◄ HIT!" if hit else ""
        print(f"  {i:<4} {str(num).zfill(2):<6} ₡{base:>7,} ₡{rev:>7,} ₡{cost:>7,} ₡{recuperado:>11,} ₡{neto:>9,} {roi:>7.0%}{marker}")

        audit_rows.append({
            "ticket": i,
            "num_exacto": str(num).zfill(2),
            "base": base,
            "rev": rev,
            "ticket_total": cost,
            "hit_exacto": hit,
            "recuperado": recuperado,
            "neto": neto,
            "roi": round(roi, 4),
        })

    neto_total = total_recuperado - total_apostado
    roi_total  = neto_total / total_apostado if total_apostado else 0
    estado     = "WIN ✓" if neto_total > 0 else ("EMPATE" if neto_total == 0 else "LOSS ✗")

    print(f"\n  {'─'*70}")
    print(f"  Total apostado   : ₡{total_apostado:>10,}")
    print(f"  Total recuperado : ₡{total_recuperado:>10,}")
    print(f"  Neto total       : ₡{neto_total:>+10,}")
    print(f"  ROI total        : {roi_total:>10.2%}")
    print(f"  Estado sesión    : {estado}")

    audit_output = {
        "generated_at": datetime.now().isoformat(),
        "resultado_exacto": str(resultado_exacto).zfill(2),
        "resultado_reventada": resultado_rev,
        "tickets": audit_rows,
        "resumen": {
            "total_apostado": total_apostado,
            "total_recuperado": total_recuperado,
            "neto_total": neto_total,
            "roi_total": round(roi_total, 4),
            "estado": estado,
        },
    }
    save_json(audit_output, "audit_result.json")


# ─────────────────────────────────────────────
# SECTION 6 — FULL PIPELINE
# ─────────────────────────────────────────────

def cmd_run(args):
    """Pipeline completo: analyze → bet → simulate."""
    print("\n═══ PIPELINE COMPLETO ═══")
    # Check historical data exists
    try:
        load_json("historical_data.json")
    except FileNotFoundError:
        print(f"\n  Primero debes obtener datos históricos.")
        print(f"  Ejecuta en tu computadora:")
        print(f"    python jps_edge_tool.py fetch --mode history --days {getattr(args, 'days', 60)}")
        sys.exit(1)

    # Analyze
    class FakeArgs:
        pass
    cmd_analyze(FakeArgs())

    # Bet
    bet_args = FakeArgs()
    bet_args.budget  = args.budget
    bet_args.n       = getattr(args, "n", 5)
    bet_args.profile = getattr(args, "profile", "balanced")
    bet_args.numbers = getattr(args, "numbers", None)
    cmd_bet(bet_args)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="JPS Tiempos Lab — Edge Tool v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Obtener datos del API JPS (corre localmente)")
    p_fetch.add_argument("--mode", choices=["last", "history"], default="last")
    p_fetch.add_argument("--days", type=int, default=60, help="Días de histórico (default: 60)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Análisis estadístico de historical_data.json (todas las sesiones)")

    # session_analyze
    p_sa = sub.add_parser("session_analyze", help="Análisis aislado por sesión individual")
    p_sa.add_argument("--session", required=True,
                      choices=["manana", "mediaTarde", "tarde"],
                      help="Sesión a analizar: manana | mediaTarde | tarde")

    # bet
    p_bet = sub.add_parser("bet", help="Construir tickets + Monte Carlo")
    p_bet.add_argument("--budget", type=int, required=True, help="Presupuesto total en colones")
    p_bet.add_argument("--n", type=int, default=5, help="Número de tickets (default: 5)")
    p_bet.add_argument("--profile", choices=["conservative", "balanced", "aggressive"], default="balanced")
    p_bet.add_argument("--numbers", type=str, default=None, help="Números específicos: '04,69,91'")

    # audit
    p_audit = sub.add_parser("audit", help="Auditar tickets vs resultado oficial")
    p_audit.add_argument("--exacto", required=True, help="Número exacto ganador (ej: 47)")
    p_audit.add_argument("--reventada", required=True, help="SI o NO")

    # simulate
    p_sim = sub.add_parser("simulate", help="Re-simular sobre input.json existente")

    # run
    p_run = sub.add_parser("run", help="Pipeline completo: analyze → bet → simulate")
    p_run.add_argument("--budget", type=int, required=True)
    p_run.add_argument("--n", type=int, default=5)
    p_run.add_argument("--profile", choices=["conservative", "balanced", "aggressive"], default="balanced")
    p_run.add_argument("--numbers", type=str, default=None)
    p_run.add_argument("--days", type=int, default=60)

    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════╗")
    print("║        JPS TIEMPOS LAB — EDGE TOOL  v2.0            ║")
    print("╚══════════════════════════════════════════════════════╝")

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "session_analyze":
        cmd_session_analyze(args)
    elif args.command == "bet":
        cmd_bet(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        print("\n  DISCLAIMER: Todos los números tienen exactamente la misma probabilidad")
        print("  en un sistema aleatorio. No se garantiza ningún resultado.")


if __name__ == "__main__":
    main()
