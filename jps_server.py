#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPS TIEMPOS LAB - LOCAL DASHBOARD SERVER v1.0
Pipeline automatico: API -> Top25 -> simulador.py -> Apuesta

USO:
    python jps_server.py              -> inicia en http://localhost:7788
    python jps_server.py 8080         -> puerto personalizado

El navegador se abre automaticamente.
Presiona Ctrl+C para detener el servidor.
"""

import sys as _sys
import io as _io
# Force UTF-8 stdout on Windows (cp1252 can't print box-drawing chars)
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    _sys.stdout = _io.TextIOWrapper(
        _sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

import base64
import hashlib
import hmac
import json
import math
import os
import random
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

# ─── CONFIG ────────────────────────────────────────────────────────────────────
def _resolve_port():
    # Prioridad: env PORT (Railway lo inyecta) → arg CLI numérico (local) → 7788.
    # Railway pasa el startCommand sin shell, así que un "${PORT:-7788}" llegaría
    # como literal — por eso leemos el env directamente y validamos que sea dígito.
    env_p = os.environ.get("PORT", "")
    if env_p.isdigit():
        return int(env_p)
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1])
    return 7788

PORT     = _resolve_port()
JPS_BASE = "https://integration.jps.go.cr"
HERE     = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR: dónde viven los datos persistentes. En Railway será /data (volume).
# Default a HERE para correr local sin cambios.
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# Env para subprocesos Python (simulador/reconcile/backtest/predict/fetch): fuerza
# UTF-8 en el stdio del hijo. En Windows el default es cp1252 y estos scripts
# imprimen box-drawing/✓/₡/★ → UnicodeEncodeError cuando su stdout es un pipe
# (subprocess.run con capture_output). En Linux/Railway ya es UTF-8; esto sólo
# lo hace idéntico y robusto en ambos entornos.
_CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

# Logging de observabilidad (Workstream B). Import guardado: si el módulo no está
# (deploy viejo), degrada a no-op sin romper el server.
try:
    from jps_logging import log_event as _log_event
except Exception:
    def _log_event(*_a, **_k):
        pass

# In-memory state (persists while server runs)
STATE = {
    "draws": [], "top25": [], "last": None, "output": None,
    "auto_status": "idle",   # idle | running | done | error
    "auto_log":    [],
    "auto_result": None,
    "auto_started_at": None,  # epoch segundos de la última corrida auto completada
    "backtest_status": "idle",   # idle | running | done | error
    "backtest_log":    [],
    "backtest_started_at": None,  # epoch segundos del último backtest completado
}

# Auto-pipeline: lock para evitar corridas concurrentes + TTL de caché (segundos).
# Si alguien entra y el pipeline corrió hace < TTL, se reusa el resultado en vez
# de re-consultar el API JPS en cada entrada.
_AUTO_LOCK = threading.Lock()
AUTO_CACHE_TTL = int(os.environ.get("JPS_AUTO_TTL", "600"))  # 10 min por defecto

# Token de sesión para el login screen. Se regenera en cada arranque del server
# (reiniciar = cerrar todas las sesiones). Un solo token compartido es suficiente
# para esta app de pocos usuarios.
SESSION_COOKIE = "jps_session"
SESSION_TOKEN  = secrets.token_urlsafe(32)

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def jps_get(endpoint, timeout=20):
    url = JPS_BASE + endpoint
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Origin": "https://juegosdeazar.jps.go.cr",
        "Referer": "https://juegosdeazar.jps.go.cr/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_draws(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "results", "sorteos", "items", "historico"):
            v = data.get(k)
            if isinstance(v, list) and v:
                return v
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "numero" in v[0]:
                return v
    return []


def expand_slots(draws):
    """Convierte registros por día (con slots manana/mediaTarde/tarde) en lista plana de sorteos."""
    flat = []
    for rec in draws:
        # Si el registro ya tiene 'numero' directamente, es un sorteo plano
        if "numero" in rec:
            flat.append(rec)
            continue
        # Si es un registro por día con slots anidados, expandir cada slot
        for slot in ("manana", "mediaTarde", "tarde"):
            s = rec.get(slot)
            if s and isinstance(s, dict) and s.get("numero") is not None:
                flat.append(s)
    return flat


def compute_top25(draws):
    # Expandir slots anidados antes de contar
    flat = expand_slots(draws)

    freq = defaultdict(lambda: {"total": 0, "si": 0, "no": 0})
    rev_si = total = 0
    for d in flat:
        try:
            raw = d.get("numero", -1)
            num = int(str(raw).strip().lstrip("0") or "0") % 100
            rev = int(d.get("in_reventado", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= num <= 99):
            continue
        freq[num]["total"] += 1
        total += 1
        if rev == 1:
            freq[num]["si"] += 1
            rev_si += 1
        else:
            freq[num]["no"] += 1

    rev_rate = rev_si / total if total else 1 / 3
    exp      = total / 100 if total else 1

    sorted_n = sorted(freq.items(), key=lambda x: x[1]["total"], reverse=True)[:25]
    top25_nums = {num for num, _ in sorted_n}

    result = []
    for rank, (num, d) in enumerate(sorted_n, 1):
        t = d["total"]
        rev_num = int(str(num).zfill(2)[::-1])   # espejo: 02→20, 12→21
        rev_d   = freq.get(rev_num, {"total": 0})
        rev_total = rev_d["total"]
        # z-score del reverso vs uniforme
        p_rev = rev_total / total if total else 0
        se    = (0.01 * 0.99 / total) ** 0.5 if total else 1
        rev_z = round((p_rev - 0.01) / se, 2) if se else 0
        result.append({
            "rank":        rank,
            "num":         num,
            "num_str":     str(num).zfill(2),
            "total":       t,
            "si":          d["si"],
            "no":          d["no"],
            "si_pct":      round(d["si"] / t * 100) if t else 0,
            "weight":      round(t / exp, 4),
            "reverso":     str(rev_num).zfill(2),
            "rev_total":   rev_total,
            "rev_z":       rev_z,
            "rev_in_top":  rev_num in top25_nums and rev_num != num,
        })
    return result, rev_rate, total, rev_si, dict(freq)


# ─── MOTOR DE ANOMALÍAS ────────────────────────────────────────────────────────
def compute_anomalies(freq, total, rev_si):
    """Detecta desviaciones estadísticas en múltiples dimensiones."""
    if total < 10:
        return {"error": "Datos insuficientes para análisis de anomalías"}

    exp     = total / 100.0
    se      = (0.01 * 0.99 / total) ** 0.5
    rev_rate_global = rev_si / total if total else 1 / 3

    def znum(n):
        t = freq.get(n, {"total": 0})["total"]
        return round((t / total - 0.01) / se, 2) if se else 0

    # 1. Outliers individuales (|z| ≥ 1.5)
    all_nums = []
    for n in range(100):
        t = freq.get(n, {"total": 0, "si": 0, "no": 0})["total"]
        z = round((t / total - 0.01) / se, 2) if se else 0
        all_nums.append({"num_str": str(n).zfill(2), "total": t, "z": z,
                         "direction": "alto" if z > 0 else "bajo"})
    outliers = sorted([x for x in all_nums if abs(x["z"]) >= 1.5],
                      key=lambda x: abs(x["z"]), reverse=True)

    # 2. Pares reverso — todos los pares (no solo top25)
    seen = set()
    rev_pairs = []
    for n in range(100):
        r = int(str(n).zfill(2)[::-1])
        if n == r:
            continue  # palíndromos (00, 11, … 99) no tienen reverso distinto
        key = tuple(sorted([n, r]))
        if key in seen:
            continue
        seen.add(key)
        za, zb = znum(n), znum(r)
        ta = freq.get(n, {"total": 0})["total"]
        tb = freq.get(r, {"total": 0})["total"]
        if abs(za) < 0.7 and abs(zb) < 0.7:
            continue  # ambos sin interés
        combined = round((za + zb) / (2 ** 0.5), 2)
        direction = ("ambos altos"  if za > 0 and zb > 0 else
                     "ambos bajos"  if za < 0 and zb < 0 else
                     "opuestos")
        rev_pairs.append({
            "a": str(n).zfill(2), "a_z": za, "a_total": ta,
            "b": str(r).zfill(2), "b_z": zb, "b_total": tb,
            "combined_z": combined, "direction": direction,
        })
    rev_pairs.sort(key=lambda x: abs(x["combined_z"]), reverse=True)

    # 3. Reventada por número — tasa observada vs esperada global
    rev_outliers = []
    for n in range(100):
        d  = freq.get(n, {"total": 0, "si": 0})
        t  = d["total"]
        si = d["si"]
        if t < 5:
            continue
        p_obs  = si / t
        se_rev = (rev_rate_global * (1 - rev_rate_global) / t) ** 0.5
        z_rev  = round((p_obs - rev_rate_global) / se_rev, 2) if se_rev else 0
        if abs(z_rev) < 1.5:
            continue
        rev_outliers.append({
            "num_str": str(n).zfill(2), "total": t, "si": si,
            "rate_pct": round(p_obs * 100, 1),
            "exp_pct":  round(rev_rate_global * 100, 1),
            "z": z_rev,
            "direction": "alto" if z_rev > 0 else "bajo",
        })
    rev_outliers.sort(key=lambda x: abs(x["z"]), reverse=True)

    # 4. Sesgo por decena (00-09, 10-19 … 90-99)
    exp_dec = total / 10.0
    se_dec  = (exp_dec * 0.9) ** 0.5  # binomial aprox
    decade_bias = []
    for d0 in range(0, 100, 10):
        obs = sum(freq.get(n, {"total": 0})["total"] for n in range(d0, d0 + 10))
        z   = round((obs - exp_dec) / se_dec, 2) if se_dec else 0
        decade_bias.append({
            "label":    f"{str(d0).zfill(2)}-{str(d0+9).zfill(2)}",
            "observed": obs, "expected": round(exp_dec, 1), "z": z,
            "direction": "alto" if z > 0 else "bajo",
        })
    decade_bias.sort(key=lambda x: abs(x["z"]), reverse=True)

    # 5. Sesgo por dígito final (unidades 0-9)
    last_digit = []
    exp_ld = total / 10.0
    for digit in range(10):
        nums = [n for n in range(100) if n % 10 == digit]
        obs  = sum(freq.get(n, {"total": 0})["total"] for n in nums)
        z    = round((obs - exp_ld) / se_dec, 2) if se_dec else 0
        last_digit.append({
            "digit": digit, "observed": obs, "expected": round(exp_ld, 1), "z": z,
            "direction": "alto" if z > 0 else "bajo",
        })
    last_digit.sort(key=lambda x: abs(x["z"]), reverse=True)

    # 6. Sesgo por dígito inicial (decenas 0-9)
    first_digit = []
    for digit in range(10):
        nums = [n for n in range(100) if int(str(n).zfill(2)[0]) == digit]
        obs  = sum(freq.get(n, {"total": 0})["total"] for n in nums)
        z    = round((obs - exp_ld) / se_dec, 2) if se_dec else 0
        first_digit.append({
            "digit": digit, "observed": obs, "expected": round(exp_ld, 1), "z": z,
            "direction": "alto" if z > 0 else "bajo",
        })
    first_digit.sort(key=lambda x: abs(x["z"]), reverse=True)

    # 7. Chi-cuadrado global
    chi2 = sum(
        (freq.get(n, {"total": 0})["total"] - exp) ** 2 / exp
        for n in range(100)
    ) if exp > 0 else 0

    n_sig = sum(1 for x in all_nums if abs(x["z"]) >= 2.576)  # p<0.01

    return {
        "total":           total,
        "exp_per_num":     round(exp, 2),
        "rev_rate_global": round(rev_rate_global * 100, 2),
        "chi2":            round(chi2, 2),
        "chi2_df":         99,
        "n_sig_individual":n_sig,
        "outliers":        outliers[:20],
        "rev_pairs":       rev_pairs[:12],
        "rev_outliers":    rev_outliers[:10],
        "decade_bias":     decade_bias[:10],
        "last_digit":      last_digit[:10],
        "first_digit":     first_digit[:10],
    }


def save_json(data, filename):
    with open(os.path.join(HERE, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def load_json(filename):
    with open(os.path.join(HERE, filename), "r", encoding="utf-8") as f:
        return json.load(f)


def build_input_json(top25, n_tickets, budget, profile, n_sim):
    ratios    = {"conservative": 0.25, "balanced": 0.45, "aggressive": 0.65}
    rev_ratio = ratios.get(profile, 0.45)
    selected  = top25[:n_tickets]
    weights   = {n["num_str"]: n["weight"] for n in top25}  # all 25 for Monte Carlo context
    return {
        "budget_total":    budget,
        "n_apuestas":      n_tickets,
        "rev_ratio":       rev_ratio,
        "numeros_exacto":  [n["num_str"] for n in selected],
        "weights":         weights,
        "n_simulaciones":  n_sim,
        "seed":            42,
    }


def run_simulador():
    sim_path = os.path.join(HERE, "simulador.py")
    if not os.path.exists(sim_path):
        raise FileNotFoundError("simulador.py no encontrado en " + HERE)
    result = subprocess.run(
        [sys.executable, sim_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=HERE, timeout=90, env=_CHILD_ENV
    )
    if result.returncode != 0:
        raise RuntimeError("simulador.py salió con error:\n" + result.stderr[-800:])
    return result.stdout.strip()


def accumulate_history(draws=None):
    """Mantiene historical_accumulated.json (merge incremental por fecha en DATA_DIR).

    El auto-pipeline sólo bajaba un snapshot (historical_data.json) y nunca
    construía el histórico acumulado, así que en Railway quedaba en 'no existe'.
    Este paso fusiona los día-registros recién bajados contra el acumulado y lo
    persiste en DATA_DIR (el volumen). Idempotente y tolerante a fallos: es un
    artefacto secundario, su error NO debe tumbar el pipeline.

    draws: lista de día-registros ya parseada. Si es None, se cargan desde
    historical_data.json (DATA_DIR→HERE). Devuelve {before, added, corrected,
    total} o {error}.
    """
    try:
        from jps_accumulate import merge_records
        if draws is None:
            raw = _load_data_json("historical_data.json")
            draws = parse_draws(raw) if raw is not None else []
        accum_path = os.path.join(DATA_DIR, "historical_accumulated.json")
        accumulated = {}
        if os.path.exists(accum_path):
            try:
                with open(accum_path, "rb") as f:
                    raw_bytes = f.read().rstrip(b"\x00")  # tolera null bytes viejos
                loaded = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else {}
                if isinstance(loaded, dict):
                    accumulated = loaded
            except Exception:
                accumulated = {}
        before = len(accumulated)
        added, corrected = merge_records(accumulated, draws or [])
        with open(accum_path, "w", encoding="utf-8") as f:
            json.dump(accumulated, f, ensure_ascii=False, indent=2, default=str)
        return {"before": before, "added": added, "corrected": corrected, "total": len(accumulated)}
    except Exception as e:
        return {"error": str(e)}


# ─── PIPELINE ──────────────────────────────────────────────────────────────────
def pipeline(params):
    days      = int(params.get("days", 60))
    budget    = int(params.get("budget", 5000))
    n_tickets = int(params.get("n", 5))
    profile   = params.get("profile", "balanced")
    n_sim     = int(params.get("nsim", 20000))

    log_lines = []
    def lg(msg): log_lines.append(msg)

    # ── 1. Fetch histórico ────────────────────────────────────────────────────
    lg(f"[1/5] Consultando API JPS histórico ({days} días)...")
    end   = datetime.now()
    start = end - timedelta(days=days)
    fmt   = "%Y-%m-%dT%H:%M:%S"
    ep    = (f"/api/App/nuevostiempos/historical"
             f"?fechaInicio={start.strftime(fmt)}&fechaFin={end.strftime(fmt)}")
    hist  = jps_get(ep)
    draws = parse_draws(hist)
    if not draws:
        raise ValueError("API JPS devolvió 0 sorteos — verifica fechas o conexión")
    STATE["draws"] = draws
    save_json(hist, "historical_data.json")
    lg(f"    ✓ {len(draws)} sorteos recibidos y guardados en historical_data.json")

    # ── 1b. Acumular histórico (merge incremental persistente en DATA_DIR) ────
    acc = accumulate_history(draws)
    if "error" in acc:
        lg(f"    ⚠ historical_accumulated.json no se pudo actualizar: {acc['error']}")
    else:
        lg(f"    ✓ Histórico acumulado: {acc['total']} días "
           f"(+{acc['added']} nuevos · {acc['corrected']} correcciones)")

    # ── 2. Fetch último resultado ────────────────────────────────────────────
    lg("[2/5] Consultando último resultado...")
    try:
        last = jps_get("/api/App/nuevostiempos/last")
        STATE["last"] = last
        save_json(last, "last_result.json")
        lg("    ✓ Último resultado guardado en last_result.json")
    except Exception as e:
        lg(f"    ⚠ No se pudo obtener último resultado: {e}")
        last = None

    # ── 3. Análisis estadístico ───────────────────────────────────────────────
    lg("[3/5] Calculando frecuencias, top 25 y anomalías...")
    top25, rev_rate, total, rev_si, freq = compute_top25(draws)
    STATE["top25"] = top25
    anomalies = compute_anomalies(freq, total, rev_si)
    save_json({
        "generated_at": datetime.now().isoformat(),
        "n_draws":       len(draws),
        "n_valid":       total,
        "rev_rate_pct":  round(rev_rate * 100, 4),
        "rev_si":        rev_si,
        "top25":         top25,
        "weights":       {n["num_str"]: n["weight"] for n in top25},
        "anomalies":     anomalies,
    }, "analysis_report.json")
    t3 = " · ".join(f"{n['num_str']}({n['total']})" for n in top25[:5])
    lg(f"    ✓ Top 5: {t3}")
    lg(f"    ✓ Reventada observada: {rev_rate*100:.2f}% (esperado 33.33%)")
    n_anom = len(anomalies.get("outliers", []))
    lg(f"    ✓ Anomalías detectadas: {n_anom} números fuera de rango | Chi²={anomalies.get('chi2','?')}")

    # ── 4. Escribir input.json y correr simulador.py ──────────────────────────
    lg(f"[4/5] Generando input.json (top {n_tickets} nums, ₡{budget:,}, {profile})...")
    input_data = build_input_json(top25, n_tickets, budget, profile, n_sim)
    save_json(input_data, "input.json")
    lg(f"    Números: {', '.join(input_data['numeros_exacto'])}")
    lg(f"    Corriendo simulador.py ({n_sim:,} simulaciones)...")
    sim_log = run_simulador()
    lg("    ✓ simulador.py terminó → output.json generado")

    # ── 5. Leer output y retornar ────────────────────────────────────────────
    lg("[5/5] Leyendo output.json y armando respuesta...")
    output = load_json("output.json")
    STATE["output"] = output
    lg("    ✓ Pipeline completo")
    _log_event("analyze", "pipeline", {"n_draws": len(draws), "n_valid": total,
                                       "rev_rate_pct": round(rev_rate * 100, 2),
                                       "budget": budget, "n_tickets": n_tickets, "profile": profile})

    return {
        "ok":           True,
        "log":          log_lines,
        "draws":        len(draws),
        "days":         days,
        "total_valid":  total,
        "rev_rate":     round(rev_rate * 100, 4),
        "rev_si":       rev_si,
        "top25":        top25,
        "anomalies":    anomalies,
        "input":        input_data,
        "output":       output,
        "last_result":  last,
        # Histórico crudo (mismo formato que historical_data.json) para que el
        # dashboard cargue S.draws directo del resultado, sin depender del archivo
        # en disco (que en Railway vive en DATA_DIR ≠ app dir).
        "historical":   hist,
    }


# ─── AUTO PIPELINE (run-on-entry, background + caché TTL) ──────────────────────
def _auto_age():
    """Segundos desde la última corrida completada, o None si nunca corrió."""
    ts = STATE.get("auto_started_at")
    return None if ts is None else round(time.time() - ts, 1)


def _auto_is_fresh():
    """True si hay un resultado 'done' reciente dentro del TTL → se puede reusar."""
    ts = STATE.get("auto_started_at")
    return (
        ts is not None
        and STATE["auto_status"] == "done"
        and (time.time() - ts) < AUTO_CACHE_TTL
    )


def _run_auto_pipeline(params):
    """Corre el pipeline end-to-end + reconcile en un thread de fondo."""
    try:
        STATE["auto_status"] = "running"
        STATE["auto_log"] = ["[auto] Iniciando pipeline end-to-end…"]
        result = pipeline(params)  # fetch JPS → análisis → tickets → Monte Carlo
        STATE["auto_log"] = list(result.get("log", []))
        STATE["auto_log"].append("[auto] Reconciliando predicciones con resultados…")
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, "jps_reconcile.py")],
                cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_CHILD_ENV,
            )
            if r.returncode == 0:
                STATE["auto_log"].append("[auto] ✓ reconcile ok")
            else:
                STATE["auto_log"].append(f"[auto] ⚠ reconcile rc={r.returncode}: {(r.stderr or '')[-160:]}")
        except Exception as e:
            STATE["auto_log"].append(f"[auto] ⚠ reconcile error: {e}")
        # Reconcile de apuestas por usuario (Workstream D) — separado del bandit.
        try:
            from jps_reconcile_user import reconcile_users
            n_user = reconcile_users()
            STATE["auto_log"].append(f"[auto] ✓ user-bets reconciliadas: {n_user}")
        except Exception as e:
            STATE["auto_log"].append(f"[auto] ⚠ user-reconcile error: {e}")
        STATE["auto_result"] = result
        STATE["auto_status"] = "done"
        STATE["auto_started_at"] = time.time()
        STATE["auto_log"].append("[auto] ✓ Pipeline completo")
        _log_event("server", "auto_pipeline_done", {"draws": result.get("draws"),
                                                     "total_valid": result.get("total_valid")})
    except Exception as e:
        import traceback
        STATE["auto_status"] = "error"
        STATE["auto_log"].append(f"[auto] ✗ Error: {e}")
        _log_event("server", "auto_pipeline_error", {"error": str(e)})
        STATE["auto_result"] = {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1200:]}


def trigger_auto(params):
    """Punto de entrada del dashboard. Reusa caché si está fresca, si no lanza
    el pipeline en background (sin bloquear). Idempotente ante entradas paralelas."""
    if _auto_is_fresh():
        return {"status": "done", "cached": True, "age": _auto_age(), "result": STATE["auto_result"]}
    with _AUTO_LOCK:
        if STATE["auto_status"] == "running":
            return {"status": "running", "cached": False, "age": _auto_age()}
        if _auto_is_fresh():
            return {"status": "done", "cached": True, "age": _auto_age(), "result": STATE["auto_result"]}
        STATE["auto_status"] = "running"
        STATE["auto_log"] = ["[auto] En cola…"]
        threading.Thread(target=_run_auto_pipeline, args=(params,), daemon=True).start()
    return {"status": "running", "cached": False, "age": _auto_age()}


# ─── BACKTEST ON-DEMAND (background) ───────────────────────────────────────────
# El backtest walk-forward es pesado (varios segundos) y de ejecución bajo
# demanda; no forma parte del pipeline automático. Se dispara desde el monitor y
# corre en un thread para no bloquear el request. jps_backtest.py escribe
# backtest_report.json vía el save_json de jps_edge_tool → respeta JPS_DATA_DIR.
_BACKTEST_LOCK = threading.Lock()


def _run_backtest_bg(params):
    """Corre jps_backtest.py en subprocess y refleja el progreso en STATE."""
    try:
        budget = str(int(params.get("budget", 5000) or 5000))
        n = str(int(params.get("n", 5) or 5))
        STATE["backtest_status"] = "running"
        STATE["backtest_log"] = [f"[backtest] Iniciando walk-forward 80/20 (₡{budget}, n={n})…"]
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "jps_backtest.py"),
             "--budget", budget, "--n", n, "--quiet"],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, env=_CHILD_ENV,
        )
        tail = [l for l in (r.stdout or "").strip().splitlines() if l.strip()][-12:]
        STATE["backtest_log"] += tail
        if r.returncode == 0:
            STATE["backtest_status"] = "done"
            STATE["backtest_started_at"] = time.time()
            STATE["backtest_log"].append("[backtest] ✓ Completo → backtest_report.json")
            _log_event("backtest", "done", {"budget": budget, "n": n})
        else:
            STATE["backtest_status"] = "error"
            STATE["backtest_log"].append(f"[backtest] ✗ rc={r.returncode}: {(r.stderr or '')[-300:]}")
            _log_event("backtest", "error", {"rc": r.returncode})
    except subprocess.TimeoutExpired:
        STATE["backtest_status"] = "error"
        STATE["backtest_log"].append("[backtest] ✗ timeout (>600 s)")
    except Exception as e:
        STATE["backtest_status"] = "error"
        STATE["backtest_log"].append(f"[backtest] ✗ error: {e}")


def trigger_backtest(params):
    """Lanza el backtest en background si no hay uno corriendo. Idempotente."""
    with _BACKTEST_LOCK:
        if STATE.get("backtest_status") == "running":
            return {"status": "running", "already": True}
        STATE["backtest_status"] = "running"
        STATE["backtest_log"] = ["[backtest] En cola…"]
        threading.Thread(target=_run_backtest_bg, args=(params,), daemon=True).start()
    return {"status": "running"}


# ─── MONITOR HELPERS ───────────────────────────────────────────────────────────
# "Mission Control": lee SÓLO archivos locales (sin internet) y expone el estado
# del pipeline, bandit, predicciones, análisis, backtest y P&L. Tolerante a
# archivos ausentes/corruptos — el monitor nunca debe tumbar el server.
_SERVER_START = time.time()

# Orden cronológico de sesiones dentro de un mismo día (para series y sorting).
_SESSION_ORDER = {"manana": 0, "mediatarde": 1, "tarde": 2}


def _data_path(filename):
    """Resuelve un archivo de datos: DATA_DIR primero, HERE como fallback.
    El código histórico escribe unos archivos en DATA_DIR (predictions_log) y
    otros en HERE (analysis_report, etc.); este resolver los encuentra en ambos."""
    p = os.path.join(DATA_DIR, filename)
    if os.path.exists(p):
        return p
    return os.path.join(HERE, filename)


def _file_info(filename):
    """Metadata básica: existe + antigüedad en minutos del mtime."""
    p = _data_path(filename)
    if not os.path.exists(p):
        return {"exists": False, "age_minutes": None}
    age_min = (time.time() - os.path.getmtime(p)) / 60
    return {"exists": True, "age_minutes": round(age_min, 1)}


def _load_data_json(filename, default=None):
    """Carga un JSON de datos (DATA_DIR→HERE). Nunca lanza: devuelve default."""
    p = _data_path(filename)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _flatten_history(data):
    """Normaliza CUALQUIER formato de histórico a (slots_planos, fechas).
    Soporta: dict keyed-by-date (historical_accumulated.json), lista de
    día-registros, lista plana de sorteos y dict con clave data/results/…"""
    dates = set()
    if isinstance(data, dict):
        vals = [v for v in data.values() if isinstance(v, dict)]
        # ¿dict keyed por fecha YYYY-MM-DD → día-registro con slots?
        looks_dated = vals and any(
            ("dia" in v or "manana" in v or "numero" in v) for v in vals
        )
        if looks_dated:
            day_records = list(data.values())
            dates |= {str(k)[:10] for k in data.keys()
                      if isinstance(k, str) and k[:4].isdigit()}
        else:
            day_records = parse_draws(data)
    elif isinstance(data, list):
        day_records = data
    else:
        day_records = []
    for rec in day_records:
        if isinstance(rec, dict) and rec.get("dia"):
            dates.add(str(rec["dia"])[:10])
    return expand_slots([r for r in day_records if isinstance(r, dict)]), dates


def _count_draws_and_last(data):
    slots, dates = _flatten_history(data)
    return len(slots), (max(dates) if dates else None)


def _beta_ci(alpha, beta):
    """Intervalo de confianza 95% (aprox normal) para Beta(α,β)."""
    denom = (alpha + beta)
    mean = alpha / denom if denom else 0.0
    var = (alpha * beta) / (denom ** 2 * (denom + 1)) if denom else 0.0
    std = math.sqrt(var)
    return round(max(0.0, mean - 1.96 * std), 4), round(min(1.0, mean + 1.96 * std), 4)


def _pipeline_health(data_age, analysis_age, has_data):
    """green: datos <4h y análisis <8h · yellow: datos <24h · red: resto."""
    if not has_data:
        return "red"
    da = 9e9 if data_age is None else data_age
    aa = 9e9 if analysis_age is None else analysis_age
    if da < 240 and aa < 480:
        return "green"
    if da < 1440:
        return "yellow"
    return "red"


def _merge_predictions_by_id(filename="predictions_log.jsonl"):
    """Lee el JSONL append-only y MERGE-a todas las líneas por id
    (pending → committed → reconciled). Devuelve (merged_por_id, total_lineas).
    El merge preserva `tickets`/`budget` del pending y agrega `result` del
    reconciled — misma lógica que jps_reconcile._latest_status_by_id. Tomar sólo
    la última línea perdería los tickets (la línea reconciled no los repite)."""
    p = _data_path(filename)
    merged, total_lines = {}, 0
    if not os.path.exists(p):
        return merged, total_lines
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            total_lines += 1
            rid = rec.get("id")
            if not rid:
                continue
            if rid in merged:
                cur = dict(merged[rid]); cur.update(rec); merged[rid] = cur
            else:
                merged[rid] = dict(rec)
    return merged, total_lines


def _pred_chrono_key(rec):
    """Clave cronológica (draw_date, orden_sesión, predicted_at/id)."""
    return (
        rec.get("draw_date") or "",
        _SESSION_ORDER.get((rec.get("session") or "").lower(), 9),
        rec.get("predicted_at") or rec.get("id") or "",
    )


# ─── MONITOR ENDPOINT BUILDERS ─────────────────────────────────────────────────
# Cada builder devuelve un dict JSON-serializable leído de archivos locales.
def build_monitor_pipeline():
    files = {}
    for fname, key in (("historical_accumulated.json", "historical_accumulated"),
                       ("historical_data.json",        "historical_data")):
        data = _load_data_json(fname)
        n_draws, last = _count_draws_and_last(data) if data is not None else (0, None)
        fi = _file_info(fname)
        fi.update(n_draws=n_draws, last_draw_date=last)
        files[key] = fi

    ar = _load_data_json("analysis_report.json") or {}
    fi = _file_info("analysis_report.json")
    fi.update(generated_at=ar.get("generated_at"), n_draws=ar.get("n_draws"),
              n_valid=ar.get("n_valid"))
    files["analysis_report"] = fi

    bs = _load_data_json("bandit_state.json") or {}
    strategies = bs.get("strategies", bs.get("arms", {})) or {}
    strat_vals = list(strategies.values()) if isinstance(strategies, dict) else []
    fi = _file_info("bandit_state.json")
    fi.update(n_strategies=len(strategies),
              total_picks=sum((s.get("n_picks", 0) or 0) for s in strat_vals),
              total_observations=sum((s.get("n_observations", 0) or 0) for s in strat_vals))
    files["bandit_state"] = fi

    merged, total_lines = _merge_predictions_by_id()
    statuses = [r.get("status") for r in merged.values()]
    fi = _file_info("predictions_log.jsonl")
    fi.update(total_lines=total_lines, n_ids=len(merged),
              pending=statuses.count("pending"), committed=statuses.count("committed"),
              reconciled=statuses.count("reconciled"), skipped=statuses.count("skipped"))
    files["predictions_log"] = fi

    lr = _load_data_json("last_result.json") or {}
    lr_session = lr_exacto = lr_mega = lr_rev = None
    for sess in ("tarde", "mediaTarde", "manana"):
        slot = lr.get(sess)
        if isinstance(slot, dict) and slot.get("numero") is not None:
            lr_session = sess
            lr_exacto = str(slot.get("numero")).zfill(2)
            lr_mega = slot.get("meganNumero")
            lr_rev = int(slot.get("in_reventado", 0) or 0) == 1
            break
    fi = _file_info("last_result.json")
    fi.update(date=(str(lr.get("dia"))[:10] if lr.get("dia") else None),
              session=lr_session, exacto=lr_exacto, mega=lr_mega, reventada=lr_rev)
    files["last_result"] = fi

    bt = _load_data_json("backtest_report.json") or {}
    fi = _file_info("backtest_report.json")
    fi.update(n_strategies=len(bt.get("strategies", {}) or {}))
    files["backtest_report"] = fi
    files["output"] = _file_info("output.json")
    au = _load_data_json("audit_result.json") or {}
    fi = _file_info("audit_result.json")
    _ares = au.get("resumen", {}) if isinstance(au, dict) else {}
    fi.update(estado=_ares.get("estado"), neto=_ares.get("neto_total"),
              draw_date=(au.get("draw_date") if isinstance(au, dict) else None),
              session=(au.get("session") if isinstance(au, dict) else None))
    files["audit_result"] = fi

    data_ages = [files[k]["age_minutes"] for k in ("historical_accumulated", "historical_data")
                 if files[k]["exists"] and files[k]["age_minutes"] is not None]
    data_age = min(data_ages) if data_ages else None
    analysis_age = files["analysis_report"]["age_minutes"] if files["analysis_report"]["exists"] else None
    started = STATE.get("auto_started_at")
    return {
        "files": files,
        "data_age_minutes": data_age,
        "analysis_age_minutes": analysis_age,
        "pipeline_health": _pipeline_health(data_age, analysis_age, bool(data_ages)),
        "server_uptime_minutes": round((time.time() - _SERVER_START) / 60, 1),
        "auto_status": STATE.get("auto_status"),
        "auto_started_at": (datetime.fromtimestamp(started).isoformat() if started else None),
        "auto_age_minutes": (round((time.time() - started) / 60, 1) if started else None),
        "now": datetime.now().isoformat(),
    }


def build_monitor_bandit():
    bs = _load_data_json("bandit_state.json") or {}
    raw = bs.get("strategies", bs.get("arms", {})) or {}
    arms = []
    for name, s in (raw.items() if isinstance(raw, dict) else []):
        alpha = float(s.get("alpha", 1) or 1)
        beta = float(s.get("beta", 1) or 1)
        denom = alpha + beta
        mean = alpha / denom
        std = math.sqrt((alpha * beta) / (denom ** 2 * (denom + 1)))
        ci_low, ci_high = _beta_ci(alpha, beta)
        arms.append({
            "strategy": name, "alpha": alpha, "beta": beta,
            "posterior_mean": round(mean, 4), "posterior_std": round(std, 4),
            "ci_low": ci_low, "ci_high": ci_high,
            "n_trials": int(round(alpha + beta - 2)), "n_hits": int(round(alpha - 1)),
            "n_picks": s.get("n_picks", 0) or 0,
            "n_observations": s.get("n_observations", 0) or 0,
            "last_hit_dia": s.get("last_hit_dia"), "last_miss_dia": s.get("last_miss_dia"),
        })
    arms.sort(key=lambda a: (a["posterior_mean"], a["n_observations"], a["n_hits"]), reverse=True)
    has_data = any(a["n_observations"] for a in arms)
    return {
        "total_strategies": len(arms),
        "total_picks": sum(a["n_picks"] for a in arms),
        "total_observations": sum(a["n_observations"] for a in arms),
        "created_at": bs.get("created_at"), "updated_at": bs.get("updated_at"),
        "has_data": has_data, "arms": arms,
        "top_strategy": (arms[0]["strategy"] if arms and has_data else None),
    }


def build_monitor_predictions():
    merged, total_lines = _merge_predictions_by_id()
    recs = sorted(merged.values(), key=_pred_chrono_key, reverse=True)
    reconciled = [r for r in recs if r.get("status") == "reconciled"]
    n_hits = sum(1 for r in reconciled if (r.get("result") or {}).get("any_hit"))
    statuses = [r.get("status") for r in recs]

    recent = []
    for r in recs[:20]:
        res = r.get("result") or None
        recent.append({
            "id": r.get("id"), "draw_date": r.get("draw_date"), "session": r.get("session"),
            "strategy": r.get("strategy"), "profile": r.get("profile"),
            "budget": r.get("budget"),
            "n_tickets": r.get("n_tickets") or (len(r.get("tickets", [])) or None),
            "tickets": r.get("tickets", []), "status": r.get("status"),
            "predicted_at": r.get("predicted_at"), "reconciled_at": r.get("reconciled_at"),
            "result": ({
                "exacto": res.get("drawn_exacto"),
                "reventada": res.get("drawn_reventada") == "SI",
                "mega": res.get("drawn_mega"), "hit": res.get("any_hit"),
                "cost": res.get("total_cost"), "recuperado": res.get("total_recuperado"),
                "net": res.get("total_neto"), "roi": res.get("roi"),
            } if res else None),
        })

    by_strategy = {}
    for r in reconciled:
        s = r.get("strategy") or "?"
        res = r.get("result") or {}
        d = by_strategy.setdefault(s, {"n": 0, "hits": 0, "net_total": 0, "cost_total": 0})
        d["n"] += 1
        d["net_total"] += res.get("total_neto", 0) or 0
        d["cost_total"] += res.get("total_cost", 0) or 0
        if res.get("any_hit"):
            d["hits"] += 1
    for d in by_strategy.values():
        d["hit_rate"] = round(d["hits"] / d["n"] * 100, 2) if d["n"] else 0
        d["roi"] = round(d["net_total"] / d["cost_total"] * 100, 2) if d["cost_total"] else 0

    return {
        "total": len(recs), "total_lines": total_lines,
        "pending": statuses.count("pending"), "committed": statuses.count("committed"),
        "skipped": statuses.count("skipped"), "reconciled": len(reconciled),
        "n_hits": n_hits,
        "hit_rate_pct": round(n_hits / len(reconciled) * 100, 2) if reconciled else 0,
        "recent": recent, "by_strategy": by_strategy,
    }


def build_monitor_analysis():
    ar = _load_data_json("analysis_report.json")
    if not ar:
        return {"exists": False}
    n_valid = ar.get("n_valid") or 0
    rev_pct = ar.get("rev_rate_pct", 0) or 0
    p0 = 1.0 / 3.0
    rev_z = 0.0
    if n_valid > 0:
        se = math.sqrt(p0 * (1 - p0) / n_valid)
        rev_z = round((rev_pct / 100.0 - p0) / se, 2) if se else 0.0
    anomalies = ar.get("anomalies", {}) or {}
    outliers = anomalies.get("outliers", []) or []
    top25 = ar.get("top25", []) or []
    sig = [o for o in outliers if abs(o.get("z", 0) or 0) >= 2.576]
    return {
        "exists": True,
        "generated_at": ar.get("generated_at"),
        "n_draws": ar.get("n_draws"), "n_valid": n_valid,
        "rev_rate_pct": round(rev_pct, 2), "rev_expected_pct": 33.33,
        "rev_si": ar.get("rev_si"), "rev_z": rev_z, "rev_anomaly": abs(rev_z) > 2.576,
        "chi2": anomalies.get("chi2"), "chi2_df": anomalies.get("chi2_df", 99),
        "n_sig_individual": anomalies.get("n_sig_individual", len(sig)),
        "top15": [{"num": t.get("num_str"), "freq": t.get("total"),
                   "weight": t.get("weight"), "si_pct": t.get("si_pct"),
                   "rev_z": t.get("rev_z")} for t in top25[:15]],
        "anomalies": [{"num": o.get("num_str"), "freq": o.get("total"),
                       "z": o.get("z"), "direction": o.get("direction")} for o in sig[:15]],
        "outliers": [{"num": o.get("num_str"), "freq": o.get("total"),
                      "z": o.get("z"), "direction": o.get("direction")} for o in outliers[:12]],
        "decade_bias": (anomalies.get("decade_bias", []) or [])[:10],
        "rev_outliers": (anomalies.get("rev_outliers", []) or [])[:8],
    }


def build_monitor_backtest():
    bt = _load_data_json("backtest_report.json")
    if not bt:
        return {"exists": False}
    cfg = bt.get("config", {}) or {}
    strategies = []
    for name, s in (bt.get("strategies", {}) or {}).items():
        strategies.append({
            "name": name, "profile": s.get("profile"),
            "n_sessions": s.get("n_sessions"), "play_rate": s.get("play_rate"),
            "n_hits": s.get("n_hits"),
            "hit_rate_pct": round((s.get("hit_rate", 0) or 0) * 100, 2),
            "roi_total_pct": round((s.get("roi_total", 0) or 0) * 100, 2),
            "mean_per_session": s.get("mean_net_per_session"), "std": s.get("std_net"),
            "median": s.get("median_net"), "p95": s.get("p95_net"),
            "max_drawdown": s.get("max_drawdown_cumulative"),
            "z_vs_baseline": s.get("z_vs_baseline"),
            "p_value": s.get("p_value_vs_baseline_permtest"),
            "is_baseline": name == "random_uniform",
        })
    strategies.sort(key=lambda x: (x["roi_total_pct"] is not None, x["roi_total_pct"] or -9e9),
                    reverse=True)
    baseline = next((s for s in strategies if s["is_baseline"]), None)
    return {
        "exists": True,
        "config": {"budget": cfg.get("budget"), "n_tickets": cfg.get("n_tickets"),
                   "n_train": cfg.get("n_train"), "n_test": cfg.get("n_test"),
                   "n_total_draws": cfg.get("n_total_draws"),
                   "generated_at": bt.get("generated_at")},
        "strategies": strategies,
        "baseline_roi": (baseline["roi_total_pct"] if baseline else None),
        "best_strategy": (strategies[0]["name"] if strategies else None),
        "ev_teorico": bt.get("expected_ev_per_session_balanced"),
        "disclaimer": bt.get("disclaimer"),
    }


def build_monitor_pnl():
    merged, _ = _merge_predictions_by_id()
    reconciled = sorted(
        [r for r in merged.values() if r.get("status") == "reconciled" and r.get("result")],
        key=_pred_chrono_key,
    )
    total_bet = total_payout = net = n_hits = cum = 0
    series, by_strategy = [], {}
    for r in reconciled:
        res = r.get("result") or {}
        cost = res.get("total_cost", 0) or 0
        payout = res.get("total_recuperado", 0) or 0
        net_s = res.get("total_neto", 0) or 0
        total_bet += cost; total_payout += payout; net += net_s; cum += net_s
        if res.get("any_hit"):
            n_hits += 1
        series.append({
            "date": r.get("draw_date"), "session": r.get("session"),
            "strategy": r.get("strategy"), "bet": cost, "payout": payout,
            "net_session": net_s, "hit": bool(res.get("any_hit")), "cumulative_net": cum,
        })
        s = r.get("strategy") or "?"
        d = by_strategy.setdefault(s, {"n": 0, "bet": 0, "net": 0, "hits": 0})
        d["n"] += 1; d["bet"] += cost; d["net"] += net_s
        if res.get("any_hit"):
            d["hits"] += 1
    for d in by_strategy.values():
        d["roi"] = round(d["net"] / d["bet"] * 100, 2) if d["bet"] else 0
    n = len(reconciled)
    return {
        "total_bet": total_bet, "total_payout": total_payout, "net": net,
        "roi_pct": round(net / total_bet * 100, 2) if total_bet else 0,
        "n_sessions": n, "n_hits": n_hits,
        "hit_rate_pct": round(n_hits / n * 100, 2) if n else 0,
        "series": series, "by_strategy": by_strategy,
    }


# ─── HTTP SERVER ───────────────────────────────────────────────────────────────
CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

# Auth opcional via env vars. Soporta múltiples usuarios:
#   - BASIC_AUTH_USER / BASIC_AUTH_PASS  → un usuario (compat hacia atrás).
#   - JPS_USERS = "user1:pass1,user2:pass2"  → varios usuarios.
# Si no hay ninguno configurado, el server queda abierto (modo local).
def _parse_users():
    users = {}
    u = os.environ.get("BASIC_AUTH_USER", "").strip()
    p = os.environ.get("BASIC_AUTH_PASS", "").strip()
    if u and p:
        users[u] = p
    for pair in os.environ.get("JPS_USERS", "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        name, _, pw = pair.partition(":")
        name, pw = name.strip(), pw.strip()
        if name and pw:
            users[name] = pw
    return users


USERS = _parse_users()
BASIC_AUTH_ENABLED = bool(USERS)


# ─── IDENTIDAD DE USUARIO (Workstream C) ────────────────────────────────────────
# El login viejo entregaba el MISMO token global a cualquier usuario → el server
# no sabía quién actuaba. Ahora: cookie FIRMADA (HMAC) que codifica username+expiry
# (stateless, sobrevive restarts) RESPALDADA por una tabla `sessions` en SQLite
# (revocable/observable). current_user() resuelve identidad por request.
try:
    import jps_db as _db
    _db.init_db()
except Exception as _e:   # degradación: sin DB, se cae a Basic Auth / modo abierto
    _db = None
    print(f"[warn] jps_db no disponible: {_e}")


def _session_secret():
    """Secret para firmar cookies: env JPS_SESSION_SECRET o uno persistido en la DB
    (así las firmas sobreviven reinicios aun sin la env var)."""
    env = os.environ.get("JPS_SESSION_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    if _db is not None:
        try:
            return _db.get_or_create_secret().encode("utf-8")
        except Exception:
            pass
    # último recurso: token efímero del proceso (sesiones no sobreviven restart)
    return SESSION_TOKEN.encode("utf-8")


def _sign(payload_b64: str) -> str:
    return hmac.new(_session_secret(), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_cookie(username, ttl_seconds=43200):
    """Crea una sesión (fila en SQLite) y devuelve el valor de cookie firmado:
    base64url(json{sid,u,exp}).hmac_sig"""
    sid, expires = (None, None)
    if _db is not None:
        try:
            sid, expires = _db.create_session(username, ttl_seconds=ttl_seconds)
        except Exception:
            sid = None
    if sid is None:
        # Fallback stateless si la DB no está: sid aleatorio sin registro persistente.
        sid = secrets.token_urlsafe(24)
        from datetime import timezone as _tz
        expires = (datetime.now(_tz.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sid": sid, "u": username, "exp": expires}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.{_sign(payload)}"


def _user_from_signed_cookie(headers):
    """Devuelve el username si la cookie de sesión firmada es válida (firma OK,
    no expirada, y —si hay DB— con fila de sesión viva). Si no, None."""
    raw = headers.get("Cookie", "")
    token = ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE and v:
            token = v
            break
    if not token or "." not in token:
        return None
    payload_b64, _, sig = token.rpartition(".")
    try:
        if not hmac.compare_digest(sig, _sign(payload_b64)):
            return None
        pad = "=" * (-len(payload_b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
    except Exception:
        return None
    # expiry (stateless): la cookie carga un ISO tz-aware (UTC)
    try:
        if datetime.fromisoformat(data["exp"]) < datetime.now(timezone.utc):
            return None
    except Exception:
        return None
    # revocación/observabilidad (stateful) — si hay DB, la fila debe seguir viva
    if _db is not None:
        try:
            if _db.get_session(data.get("sid")) is None:
                return None
        except Exception:
            pass
    user = data.get("u")
    return user if user in USERS else None


def _user_from_basic(headers):
    auth_header = headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1].strip()).decode("utf-8")
        user, _, pw = decoded.partition(":")
        return user if _creds_ok(user, pw) else None
    except Exception:
        return None


def current_user(headers):
    """Identidad del que hace el request, o None. En modo abierto (sin JPS_USERS)
    devuelve 'local' para que el betting module sea usable en dev sin auth."""
    if not BASIC_AUTH_ENABLED:
        return "local"
    return _user_from_signed_cookie(headers) or _user_from_basic(headers)


def _session_id_from_cookie(headers):
    """Extrae el sid de la cookie firmada (para logout/revocación)."""
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE and v and "." in v:
            payload_b64 = v.rpartition(".")[0]
            try:
                pad = "=" * (-len(payload_b64) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
                return data.get("sid")
            except Exception:
                return None
    return None


# ─── BETTING MODULE — validación (Workstream D) ─────────────────────────────────
# Cutoffs oficiales JPS (para el registro de "sesión en vivo": minutos antes del sorteo).
_SESSION_CUTOFF = {"manana": (12, 55), "mediaTarde": (16, 30), "tarde": (19, 30)}


def _minutes_before_cutoff(session, draw_date):
    try:
        h, m = _SESSION_CUTOFF.get(session, (23, 59))
        d = datetime.strptime(draw_date, "%Y-%m-%d")
        cutoff = d.replace(hour=h, minute=m, second=0, microsecond=0)
        return int((cutoff - datetime.now()).total_seconds() // 60)
    except Exception:
        return None


def _validate_user_bet(payload):
    """Valida y normaliza una apuesta de usuario. Aplica los invariantes del juego:
    número en rango 00-99, base≥₡100 múltiplo de 100, rev 0 o múltiplo de 100,
    rev ≤ base. El usuario puede elegir CUALQUIER número (no solo el top-25); cada
    ticket lleva `in_top25` como marca informativa para comparar vs. el sistema.
    Devuelve (session, draw_date, tickets, amount, rev_ratio) o ValueError."""
    session = str(payload.get("session", "")).strip()
    if session not in ("manana", "mediaTarde", "tarde"):
        raise ValueError("session inválida (manana|mediaTarde|tarde)")
    draw_date = str(payload.get("draw_date") or datetime.now().strftime("%Y-%m-%d"))[:10]
    raw_tickets = payload.get("tickets") or []
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise ValueError("tickets requerido (lista no vacía)")
    top_set = {n.get("num_str") for n in STATE.get("top25", [])}
    tickets = []
    seen = set()
    for t in raw_tickets:
        try:
            n_int = int(str(t.get("num")).strip())
            num = str(n_int).zfill(2)
            base = int(t.get("base", 0)); rev = int(t.get("rev", 0))
        except (TypeError, ValueError):
            raise ValueError("ticket con num/base/rev inválido")
        if not (0 <= n_int <= 99):
            raise ValueError(f"número {t.get('num')} fuera de rango (00-99)")
        if num in seen:
            continue  # dedup: un número una sola vez por apuesta
        seen.add(num)
        if base < 100 or base % 100 != 0:
            raise ValueError("base debe ser ≥₡100 y múltiplo de 100")
        if rev < 0 or rev % 100 != 0:
            raise ValueError("rev debe ser 0 o múltiplo de ₡100")
        if rev > base:
            raise ValueError("rev no puede superar base (rev ≤ base)")
        tickets.append({"num": num, "base": base, "rev": rev, "in_top25": num in top_set})
    if not tickets:
        raise ValueError("sin tickets válidos")
    amount = sum(t["base"] + t["rev"] for t in tickets)
    base_sum = sum(t["base"] for t in tickets)
    rev_ratio = round(sum(t["rev"] for t in tickets) / base_sum, 4) if base_sum else 0
    return session, draw_date, tickets, amount, rev_ratio


def _result_index():
    """Índice {YYYY-MM-DD-session: draw} de resultados reales ya publicados.
    Reusa la misma fuente de verdad que el reconcile del sistema."""
    try:
        from jps_reconcile import _build_results_index
        return _build_results_index()
    except Exception:
        return {}


def _bet_locked(bet, idx=None):
    """Una apuesta queda BLOQUEADA (no editable ni eliminable) una vez que el
    resultado de su sorteo salió, o ya fue reconciliada."""
    if bet.get("status") == "reconciled":
        return True
    if idx is None:
        idx = _result_index()
    return f"{bet.get('draw_date')}-{bet.get('session')}" in idx


def _creds_ok(user, pw) -> bool:
    """Valida usuario+contraseña contra el dict de usuarios (tiempo constante)."""
    if not user or user not in USERS:
        return False
    try:
        return secrets.compare_digest(pw or "", USERS[user])
    except Exception:
        return False


def _check_basic_auth(headers) -> bool:
    """Devuelve True si está autenticado o auth está desactivado."""
    if not BASIC_AUTH_ENABLED:
        return True
    import base64
    auth_header = headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        encoded = auth_header.split(" ", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-8")
        user, _, pw = decoded.partition(":")
        return _creds_ok(user, pw)
    except Exception:
        return False


def _check_session_cookie(headers) -> bool:
    """True si la cookie de sesión coincide con el token actual del server."""
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE and v:
            try:
                return secrets.compare_digest(v, SESSION_TOKEN)
            except Exception:
                return False
    return False


def _is_authed(headers) -> bool:
    """Autenticado si: auth desactivado, o hay una identidad resoluble
    (cookie de sesión firmada válida, o Basic Auth). Delega en current_user()."""
    if not BASIC_AUTH_ENABLED:
        return True
    return current_user(headers) is not None


def _validate_login(user, pw) -> bool:
    """Valida credenciales del formulario de login contra el dict de usuarios."""
    if not BASIC_AUTH_ENABLED:
        return True
    return _creds_ok(user, pw)


# ─── LOGIN SCREEN ──────────────────────────────────────────────────────────────
def render_login(error=False) -> str:
    err_html = (
        '<div class="err">Usuario o contraseña incorrectos.</div>' if error else ""
    )
    return """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JPS Tiempos Lab · Acceso</title>
<style>
:root{--green:#0F6E56;--green-d:#0a5443;--bg:#0d1117;--card:#161b22;--bd:#283041;--txt:#e6edf3;--mut:#8b949e}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:radial-gradient(1200px 600px at 50% -10%,#13301f,#0d1117);color:var(--txt);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{width:100%;max-width:380px;background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:34px 30px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.logo .dot{width:11px;height:11px;border-radius:50%;background:var(--green);box-shadow:0 0 14px var(--green)}
.logo h1{font-size:17px;font-weight:800;letter-spacing:.2px}
.sub{color:var(--mut);font-size:12px;margin-bottom:22px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);margin:14px 0 6px}
input{width:100%;background:#0d1117;border:1px solid var(--bd);border-radius:9px;padding:11px 13px;color:var(--txt);font-size:14px;outline:none;transition:border .15s}
input:focus{border-color:var(--green)}
button{width:100%;margin-top:22px;background:linear-gradient(180deg,var(--green),var(--green-d));color:#fff;border:0;border-radius:9px;padding:12px;font-size:14px;font-weight:700;cursor:pointer;transition:filter .15s}
button:hover{filter:brightness(1.08)}
.err{background:rgba(226,75,74,.12);border:1px solid rgba(226,75,74,.4);color:#ff9a9a;font-size:12.5px;border-radius:8px;padding:9px 12px;margin-bottom:14px}
.foot{margin-top:18px;font-size:10.5px;color:var(--mut);text-align:center;line-height:1.5}
</style></head>
<body>
<form class="card" method="POST" action="/login">
  <div class="logo"><span class="dot"></span><h1>JPS Tiempos Lab</h1></div>
  <div class="sub">Architect · acceso restringido</div>
  """ + err_html + """
  <label for="user">Usuario</label>
  <input id="user" name="user" autocomplete="username" autofocus required>
  <label for="pass">Contraseña</label>
  <input id="pass" name="pass" type="password" autocomplete="current-password" required>
  <button type="submit">Entrar</button>
  <div class="foot">Todos los números tienen la misma probabilidad.<br>No se garantiza ningún resultado.</div>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # silence default log

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, abs_path, content_type):
        """Sirve un archivo arbitrario del proyecto (HTML, CSS, PNG, etc.)."""
        if not os.path.exists(abs_path):
            self.send_json({"error": f"Not found: {os.path.basename(abs_path)}"}, 404)
            return
        with open(abs_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            self._handle_login_post()
            return
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body_raw = self.rfile.read(length) if length else b""
            payload = json.loads(body_raw.decode("utf-8")) if body_raw else {}
        except Exception as e:
            self.send_json({"error": f"invalid JSON body: {e}"}, 400)
            return

        try:
            if parsed.path == "/api/commit":
                pred_id = payload.get("id")
                bet_per_ticket = int(payload.get("bet_per_ticket", 0))
                if not pred_id or bet_per_ticket < 100:
                    self.send_json({"error": "id y bet_per_ticket (>=100) requeridos"}, 400)
                    return
                record = {
                    "id": pred_id,
                    "supersedes_status": "pending",
                    "status": "committed",
                    "actual_bet_per_ticket": bet_per_ticket,
                    "committed_at": datetime.now().isoformat(),
                }
                with open(os.path.join(DATA_DIR, "predictions_log.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                _log_event("commit", "user_commit", {"id": pred_id, "bet_per_ticket": bet_per_ticket,
                                                     "user": current_user(self.headers)})
                self.send_json({"ok": True, "record": record})

            elif parsed.path == "/api/skip":
                pred_id = payload.get("id")
                if not pred_id:
                    self.send_json({"error": "id requerido"}, 400)
                    return
                record = {
                    "id": pred_id,
                    "supersedes_status": "pending",
                    "status": "skipped",
                    "skipped_at": datetime.now().isoformat(),
                }
                with open(os.path.join(DATA_DIR, "predictions_log.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                _log_event("skip", "user_skip", {"id": pred_id, "user": current_user(self.headers)})
                self.send_json({"ok": True, "record": record})

            elif parsed.path == "/api/backtest/run":
                # Dispara el backtest walk-forward en background y regenera
                # backtest_report.json. No bloquea: el monitor polea /status.
                self.send_json(trigger_backtest(payload))

            elif parsed.path == "/api/user-bet":
                # Betting module: registra la apuesta MANUAL del usuario logueado
                # en SQLite (aislado por usuario, separado del predictions_log del
                # sistema para no contaminar el bandit).
                user = current_user(self.headers)
                if not user:
                    self.send_json({"error": "no autenticado"}, 401)
                    return
                if _db is None:
                    self.send_json({"error": "DB no disponible"}, 500)
                    return
                try:
                    session, draw_date, tickets, amount, rev_ratio = _validate_user_bet(payload)
                except ValueError as ve:
                    self.send_json({"error": str(ve)}, 400)
                    return
                # Lock: no se puede crear NI editar una apuesta cuyo resultado ya salió
                # (o cuya apuesta previa ya fue reconciliada).
                idx = _result_index()
                existing = _db.get_user_bet(f"{draw_date}-{session}-{user}")
                if f"{draw_date}-{session}" in idx or (existing and _bet_locked(existing, idx)):
                    self.send_json({"error": "el resultado de esa sesión ya salió — la apuesta está cerrada"}, 403)
                    return
                bet_id = _db.upsert_user_bet(
                    user, draw_date, session, tickets, amount, rev_ratio,
                    anomalies_snapshot=payload.get("anomalies_snapshot"),
                    architect_snapshot=payload.get("architect_snapshot"),
                    minutes_before_cutoff=_minutes_before_cutoff(session, draw_date))
                _log_event("user_bet", "placed", {"user": user, "draw_date": draw_date,
                                                  "session": session, "amount": amount,
                                                  "n_tickets": len(tickets),
                                                  "edited": bool(existing)})
                self.send_json({"ok": True, "bet_id": bet_id, "amount": amount,
                                "user": user, "edited": bool(existing)})

            elif parsed.path == "/api/user-bet/delete":
                # Elimina la apuesta del usuario si aún NO salió el resultado.
                user = current_user(self.headers)
                if not user:
                    self.send_json({"error": "no autenticado"}, 401)
                    return
                if _db is None:
                    self.send_json({"error": "DB no disponible"}, 500)
                    return
                bet_id = payload.get("bet_id")
                if not bet_id and payload.get("draw_date") and payload.get("session"):
                    bet_id = f"{payload['draw_date']}-{payload['session']}-{user}"
                bet = _db.get_user_bet(bet_id) if bet_id else None
                if not bet or bet.get("username") != user:
                    self.send_json({"error": "apuesta no encontrada"}, 404)
                    return
                if _bet_locked(bet):
                    self.send_json({"error": "el resultado ya salió — la apuesta está cerrada"}, 403)
                    return
                deleted = _db.delete_user_bet(bet_id, user)
                _log_event("user_bet", "deleted", {"user": user, "bet_id": bet_id})
                self.send_json({"ok": True, "deleted": deleted})

            else:
                self.send_json({"error": "Not found"}, 404)

        except Exception as e:
            import traceback
            self.send_json({"error": str(e), "trace": traceback.format_exc()[-1500:]}, 500)

    def _require_auth(self, html=False) -> bool:
        """True si autenticado (cookie de sesión o Basic Auth). Si falla:
        - html=True  → redirige al /login (páginas del navegador)
        - html=False → 401 JSON sin WWW-Authenticate (evita el popup nativo)."""
        if _is_authed(self.headers):
            return True
        if html:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return False
        body = b'{"error": "authentication required"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        return False

    def _handle_login_post(self):
        """Procesa el formulario de login: valida credenciales, setea cookie de
        sesión y redirige al dashboard. Acepta form-urlencoded o JSON."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
        except Exception:
            raw = ""
        ct = self.headers.get("Content-Type", "")
        if ct.startswith("application/json"):
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
        else:
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        user = (data.get("user") or data.get("username") or "").strip()
        pw   = data.get("pass") or data.get("password") or ""
        if _validate_login(user, pw):
            cookie_val = make_session_cookie(user)
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={cookie_val}; Path=/; HttpOnly; "
                f"SameSite=Lax; Max-Age=43200",
            )
            self.send_header("Location", "/")
            self.end_headers()
            _log_event("server", "login", {"user": user})
        else:
            self.send_response(302)
            self.send_header("Location", "/login?error=1")
            self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path

        # Rutas públicas (sin auth): healthcheck Railway, login y logout.
        if path == "/login":
            self.send_html(render_login(error=bool(params.get("error"))))
            return
        if path == "/logout":
            # Revocar la sesión persistente (además de limpiar la cookie).
            if _db is not None:
                try:
                    _db.delete_session(_session_id_from_cookie(self.headers))
                except Exception:
                    pass
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path != "/api/status":
            # Páginas del navegador → redirige al login; API/datos → 401 JSON.
            is_page = path in ("/", "/monitor") or path.startswith("/assets/")
            if not self._require_auth(html=is_page):
                return
        try:
            if parsed.path == "/":
                # Dashboard principal: la consola analítica v2 (jps_console_v2.html),
                # que ahora corre el pipeline automáticamente al entrar. Fallbacks:
                # dashboard.html editorial → HTML embebido.
                console_path = os.path.join(HERE, "jps_console_v2.html")
                dash_path = os.path.join(HERE, "dashboard.html")
                if os.path.exists(console_path):
                    self.send_file(console_path, "text/html; charset=utf-8")
                elif os.path.exists(dash_path):
                    self.send_file(dash_path, "text/html; charset=utf-8")
                else:
                    self.send_html(DASHBOARD_HTML)  # fallback al embedded

            elif parsed.path.startswith("/assets/"):
                # Sirve assets de marca (CSS, logo). Path tipo /assets/brand/preset-editorial.css
                rel = parsed.path.lstrip("/")
                abs_path = os.path.join(HERE, rel)
                # Validar que sigue dentro del HERE para evitar path traversal
                if not os.path.abspath(abs_path).startswith(os.path.abspath(HERE)):
                    self.send_json({"error": "forbidden"}, 403)
                else:
                    ct = "application/octet-stream"
                    if rel.endswith(".css"): ct = "text/css; charset=utf-8"
                    elif rel.endswith(".png"): ct = "image/png"
                    elif rel.endswith(".svg"): ct = "image/svg+xml"
                    elif rel.endswith(".js"): ct = "application/javascript"
                    self.send_file(abs_path, ct)

            elif parsed.path == "/predictions_log.jsonl":
                # Sirve el JSONL para que dashboard.html lo lea con fetch()
                p = os.path.join(DATA_DIR, "predictions_log.jsonl")
                if os.path.exists(p):
                    self.send_file(p, "application/x-ndjson; charset=utf-8")
                else:
                    self.send_file(p, "text/plain")  # 404 via send_file

            elif parsed.path == "/backtest_report.json":
                p = os.path.join(DATA_DIR, "backtest_report.json")
                self.send_file(p, "application/json; charset=utf-8")

            elif parsed.path == "/historical_data.json":
                p = os.path.join(DATA_DIR, "historical_data.json")
                self.send_file(p, "application/json; charset=utf-8")

            elif parsed.path == "/bandit_state.json":
                p = os.path.join(DATA_DIR, "bandit_state.json")
                self.send_file(p, "application/json; charset=utf-8")

            elif parsed.path == "/api/status":
                self.send_json({
                    "draws":   len(STATE["draws"]),
                    "top25":   len(STATE["top25"]),
                    "has_out": STATE["output"] is not None,
                    "has_last": STATE["last"] is not None,
                })

            elif parsed.path == "/api/last":
                data = jps_get("/api/App/nuevostiempos/last")
                STATE["last"] = data
                save_json(data, "last_result.json")
                self.send_json({"ok": True, "data": data})

            elif parsed.path == "/api/pipeline":
                result = pipeline(params)
                self.send_json(result)

            elif parsed.path == "/api/state":
                self.send_json({
                    "top25":  STATE["top25"],
                    "output": STATE["output"],
                    "last":   STATE["last"],
                })

            elif parsed.path == "/api/auto/run":
                # El dashboard llama esto al entrar: corre el pipeline end-to-end
                # en background (o reusa caché < TTL). No bloquea.
                self.send_json(trigger_auto(params))

            elif parsed.path == "/api/auto":
                # Browser polls this to track auto-pipeline progress
                self.send_json({
                    "status": STATE["auto_status"],
                    "log":    STATE["auto_log"],
                    "result": STATE["auto_result"],
                    "fresh":  _auto_is_fresh(),
                    "age":    _auto_age(),
                })

            # ─── MONITOR (Mission Control) ───────────────────────────────────
            elif parsed.path == "/monitor":
                self.send_html(MONITOR_HTML)
            elif parsed.path == "/api/monitor/pipeline":
                self.send_json(build_monitor_pipeline())
            elif parsed.path == "/api/monitor/bandit":
                self.send_json(build_monitor_bandit())
            elif parsed.path == "/api/monitor/predictions":
                self.send_json(build_monitor_predictions())
            elif parsed.path == "/api/monitor/analysis":
                self.send_json(build_monitor_analysis())
            elif parsed.path == "/api/monitor/backtest":
                self.send_json(build_monitor_backtest())
            elif parsed.path == "/api/monitor/pnl":
                self.send_json(build_monitor_pnl())

            elif parsed.path == "/api/backtest/status":
                self.send_json({
                    "status": STATE.get("backtest_status", "idle"),
                    "log": STATE.get("backtest_log", []),
                    "started_at": STATE.get("backtest_started_at"),
                    "age_minutes": (round((time.time() - STATE["backtest_started_at"]) / 60, 1)
                                    if STATE.get("backtest_started_at") else None),
                })

            elif parsed.path == "/api/user-bets":
                # Historial de apuestas + auditorías DEL usuario logueado (aislado).
                user = current_user(self.headers)
                if not user:
                    self.send_json({"error": "no autenticado"}, 401)
                    return
                if _db is None:
                    self.send_json({"error": "DB no disponible"}, 500)
                    return
                bets = _db.get_user_bets(user)
                audits = _db.get_user_audits(user)
                # Marca cuáles apuestas están cerradas (resultado salió / reconciliadas).
                _idx = _result_index()
                for _b in bets:
                    _b["locked"] = _bet_locked(_b, _idx)
                n = hits = 0
                net = cost = 0
                for a in audits:
                    res = a.get("result") or {}
                    rs = res.get("resumen", {}) or {}
                    net += rs.get("neto_total", 0) or 0
                    cost += rs.get("total_apostado", 0) or 0
                    n += 1
                    if res.get("any_hit"):
                        hits += 1
                self.send_json({
                    "user": user, "bets": bets, "audits": audits,
                    "summary": {"n_audited": n, "hits": hits, "net_total": net,
                                "cost_total": cost,
                                "roi_pct": round(net / cost * 100, 2) if cost else 0,
                                "hit_rate_pct": round(hits / n * 100, 2) if n else 0},
                })

            elif parsed.path == "/api/monitor/day" or parsed.path.startswith("/api/monitor/day/"):
                # Resumen de observabilidad del día: ?date=YYYY-MM-DD o /api/monitor/day/<fecha>.
                date = params.get("date")
                if not date and parsed.path.startswith("/api/monitor/day/"):
                    date = parsed.path.rsplit("/", 1)[-1] or None
                try:
                    from jps_logging import summarize_day
                    self.send_json(summarize_day(date))
                except Exception as e:
                    self.send_json({"error": f"logging no disponible: {e}"}, 500)

            else:
                self.send_json({"error": "Not found"}, 404)

        except urllib.error.URLError as e:
            self.send_json({"error": f"No se pudo conectar al API JPS: {e.reason}"}, 503)
        except subprocess.TimeoutExpired:
            self.send_json({"error": "simulador.py tardó más de 90 s — reduce n_sim"}, 500)
        except FileNotFoundError as e:
            self.send_json({"error": str(e)}, 500)
        except Exception as e:
            import traceback
            self.send_json({"error": str(e), "trace": traceback.format_exc()[-1500:]}, 500)


# ─── EMBEDDED DASHBOARD HTML ───────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JPS Tiempos Lab</title>
<style>
:root{color-scheme:light;--green:#0F6E56;--green-lt:#EAF3DE;--green-bd:#C0DD97;--red:#E24B4A;--blue:#378ADD;--amber:#C86A00;--violet:#6D28D9;--bg:#f2f2f4;--card:#fff;--border:#e2e2e4}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;background:var(--bg);color:#111;min-height:100vh}
.wrap{max-width:950px;margin:0 auto;padding:12px 14px}
.card{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:14px 16px;margin-bottom:10px}

/* HEADER */
.hdr{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.hdr h1{font-size:16px;font-weight:700;line-height:1.3}
.hdr .sub{font-size:11px;color:#888;margin-top:2px}
.srv-badge{display:flex;align-items:center;gap:6px;background:#f5f5f7;border:1px solid #e5e5e7;border-radius:20px;padding:5px 12px;font-size:11px;font-weight:600;color:#555;white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:#ddd;display:inline-block}
.dot.ok{background:#1D9E75} .dot.err{background:#E24B4A}

/* STATS BAR */
.stats-bar{display:flex;flex-wrap:wrap;gap:7px;padding:4px 0}
.stat-pill{display:flex;align-items:center;gap:5px;background:#f5f5f7;border:1px solid #e5e5e7;border-radius:20px;padding:4px 11px;font-size:11px;font-weight:600;color:#333;white-space:nowrap}
.sp-val{color:var(--green);font-weight:700}
.sp-warn{color:var(--amber);font-weight:700}
.sp-alert{color:var(--red);font-weight:700}

/* REVERSO PAIRS CARD */
.rev-card{background:linear-gradient(135deg,#F0FDF7,#E6F9F0);border:1.5px solid #A7F3D0;border-radius:9px;padding:11px 14px;margin-bottom:10px}
.rev-card-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;color:#065F46;margin-bottom:7px}
.rev-pairs{display:flex;flex-wrap:wrap;gap:6px}
.rev-pair{display:inline-flex;align-items:center;gap:4px;background:#fff;border:1.5px solid #6EE7B7;border-radius:7px;padding:4px 10px;font-family:monospace;font-size:13px;font-weight:700}
.rev-pair .rp-num{color:var(--green)}
.rev-pair .rp-z{font-size:10px;font-weight:600;color:#888;font-family:inherit;margin-left:2px}
.anom{display:inline-flex;align-items:center;background:#FEF3C7;border:1px solid #FCD34D;color:#92400E;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700;font-family:inherit}
.anom-hi{background:#D1FAE5;border-color:#6EE7B7;color:#065F46}

/* CONFIG */
.cfg{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.cfg-g{display:flex;flex-direction:column;gap:4px}
.cfg-l{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px}
select,input[type=number]{border:1.5px solid #ddd;border-radius:7px;padding:5px 9px;font-size:13px;background:#fff;color:#111;outline:none;transition:border .15s}
select:focus,input[type=number]:focus{border-color:var(--green)}
input[type=range]{width:72px;accent-color:var(--green)}
.cfg-val{font-size:12px;font-weight:700;color:#111}

/* BUTTONS */
.brow{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
button{font-family:inherit;border:1.5px solid #ddd;border-radius:7px;padding:7px 14px;font-size:12px;font-weight:600;cursor:pointer;background:#fff;color:#111;transition:all .12s}
button:hover{background:#f3f3f3;border-color:#bbb}
button:active{transform:scale(.97)}
.btn-go{background:var(--green);color:#fff;border-color:var(--green);font-size:14px;padding:9px 22px;flex:1}
.btn-go:hover{background:#085041}
.btn-go:disabled{background:#8fbdaf;border-color:#8fbdaf;cursor:wait}
.btn-last{border-color:#B5D4F4;color:#185FA5}

/* CONSOLE */
.con-wrap{background:#0d1117;border-radius:8px;overflow:hidden}
.con-head{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;border-bottom:1px solid #1a2332}
.con-title{font-size:10px;font-weight:700;color:#444;text-transform:uppercase;letter-spacing:.5px;font-family:monospace}
.con-clr{font-size:10px;color:#444;cursor:pointer;border:none;background:none;font-family:monospace;padding:0}
.con-clr:hover{color:#888}
.con{height:120px;overflow-y:auto;padding:8px 12px;font-family:'Consolas','Monaco',monospace;font-size:11px;line-height:1.6;color:#adb5bd}
.lh{color:#79c0ff;font-weight:700} .lok{color:#3fb950} .lw{color:#d29922} .le{color:#f85149} .lt{color:#444}
@keyframes sp{to{transform:rotate(360deg)}}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #ffffff44;border-top-color:#fff;border-radius:50%;animation:sp .6s linear infinite;vertical-align:middle;margin-right:5px}

/* TABS */
.tabs{display:flex;border-bottom:1.5px solid #ebebeb;margin:-14px -16px 14px;padding:0 16px}
.tb{padding:8px 14px;font-size:12px;font-weight:600;border:none;background:none;color:#999;cursor:pointer;border-bottom:2.5px solid transparent;margin-bottom:-1.5px;border-radius:0;transition:color .15s}
.tb.on{color:var(--green);border-bottom-color:var(--green)}
.tb:hover:not(.on){color:#333;background:#f8f8f8}
.tp{display:none}.tp.on{display:block}

/* LAST RESULT */
.lsg{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:8px}
.lsc{background:#f8f8f8;border-radius:8px;padding:12px 10px;border:1px solid #e8e8e8;text-align:center}
.lsl{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.3px;margin-bottom:6px}
.lsn{font-size:40px;font-weight:800;color:var(--green);font-family:monospace;line-height:1}
.lsm{font-size:11px;color:#666;margin-top:5px}
.rsi{color:var(--green);font-weight:700} .rno{color:#bbb}
.bola{display:inline-block;padding:2px 9px;border-radius:20px;font-size:10px;font-weight:700;margin-top:5px}

/* TABLES */
.twrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
thead tr{background:#f7f7f7}
th{text-align:left;padding:6px 8px;font-weight:700;color:#555;font-size:11px;border-bottom:1px solid #e8e8e8;white-space:nowrap}
td{padding:5px 8px;border-bottom:.5px solid #f0f0f0;vertical-align:middle}
tr:hover td{background:#fafafa}
.nm{font-family:monospace;font-weight:800;font-size:15px;color:var(--green)}
.mu{color:#bbb;font-size:11px}
.bdg{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700}
.bg{background:#E1F5EE;color:#085041} .ba{background:#FAEEDA;color:#633806} .br{background:#FCEBEB;color:#791F1F}
.mb{display:inline-block;height:7px;border-radius:2px;vertical-align:middle}

/* CHARTS */
.ch{position:relative;height:195px;margin:8px 0 10px}

/* MC METRICS */
.mc-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.mm{background:#f7f7f7;border:1px solid #e8e8e8;border-radius:8px;padding:10px;text-align:center}
.ml{font-size:10px;color:#888;font-weight:700;text-transform:uppercase;margin-bottom:3px}
.mv{font-size:15px;font-weight:700}

/* STRATEGY SUMMARY */
.sg{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.sc2{grid-column:span 2}
.sc{background:#f7f7f7;border:1px solid #e8e8e8;border-radius:8px;padding:10px;text-align:center}
.sl{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.3px;margin-bottom:4px}
.sv{font-size:16px;font-weight:700;color:#111}
.ss{font-size:10px;color:#888;margin-top:3px}

.disc{margin-top:10px;padding:8px 12px;background:#fffbf0;border-left:3px solid #EF9F27;border-radius:0 7px 7px 0;font-size:11px;color:#633806}
.empty{text-align:center;padding:28px;color:#ccc}
.empty .ico{font-size:26px;margin-bottom:6px}
.inf{font-size:11px;color:#888;margin-bottom:8px}

/* ANOMALY ENGINE */
.anom-sec{margin-bottom:14px}
.anom-sec-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;color:#555;margin-bottom:7px;padding-bottom:4px;border-bottom:1px solid #ebebeb}
.anom-chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px}
.ac-chip{display:inline-flex;align-items:center;gap:4px;border-radius:6px;padding:4px 10px;font-family:monospace;font-size:12px;font-weight:700;border:1.5px solid transparent}
.ac-hi{background:#D1FAE5;border-color:#6EE7B7;color:#065F46}
.ac-lo{background:#FEE2E2;border-color:#FCA5A5;color:#7F1D1D}
.ac-neu{background:#F3F4F6;border-color:#D1D5DB;color:#374151}
.ac-z{font-size:10px;font-weight:600;opacity:.75;margin-left:2px}
.ac-star{color:#D97706;font-size:11px}
.anom-table{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:4px}
.anom-table th{text-align:left;padding:5px 7px;font-weight:700;color:#666;font-size:10px;border-bottom:1px solid #e8e8e8;white-space:nowrap;background:#f7f7f7}
.anom-table td{padding:4px 7px;border-bottom:.5px solid #f0f0f0;vertical-align:middle}
.anom-table tr:hover td{background:#fafafa}
.anom-badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700}
.ab-hi{background:#D1FAE5;color:#065F46} .ab-lo{background:#FEE2E2;color:#7F1D1D}
.ab-warn{background:#FEF3C7;color:#92400E} .ab-ok{background:#F3F4F6;color:#374151}
.anom-summary-box{display:flex;flex-wrap:wrap;gap:6px;padding:10px 0 6px;border-bottom:1px solid #ebebeb;margin-bottom:12px}
.asb-pill{display:flex;align-items:center;gap:4px;background:#f5f5f7;border:1px solid #e5e5e7;border-radius:16px;padding:3px 10px;font-size:11px}
.asb-val{font-weight:700;color:var(--green)}
.asb-warn{font-weight:700;color:var(--amber)}
.asb-ok{font-weight:700;color:#666}
.dec-bar-wrap{display:flex;flex-direction:column;gap:3px;margin-top:6px}
.dec-bar-row{display:flex;align-items:center;gap:6px;font-size:11px}
.dec-lbl{font-family:monospace;font-weight:700;color:#444;width:54px;flex-shrink:0}
.dec-bar{height:10px;border-radius:3px;flex-shrink:0;min-width:4px}
.dec-meta{font-size:10px;color:#888}

/* ── THE ARCHITECT ──────────────────────────────────────── */
.arch-header{display:flex;flex-wrap:wrap;gap:7px;align-items:center;padding:2px 0 14px;border-bottom:1.5px solid #ebebeb;margin-bottom:14px}
.arch-h-pill{display:flex;align-items:center;gap:4px;background:#f5f5f7;border:1px solid #e5e5e7;border-radius:16px;padding:3px 10px;font-size:11px;font-weight:600}
.arch-h-val{font-weight:800}
.arch-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:11px}
@media(max-width:720px){.arch-grid{grid-template-columns:1fr}}
.arch-card{border-radius:10px;overflow:hidden;border:2px solid #e5e5e7;display:flex;flex-direction:column}
.arch-card-a{border-color:var(--green)}
.arch-card-b{border-color:var(--blue)}
.arch-card-c{border-color:var(--amber)}
.arch-card-d{border-color:var(--violet)}
.arch-card-head{padding:9px 12px;display:flex;flex-direction:column;gap:2px}
.arch-card-head-a{background:var(--green);color:#fff}
.arch-card-head-b{background:var(--blue);color:#fff}
.arch-card-head-c{background:#C86A00;color:#fff}
.arch-card-head-d{background:var(--violet);color:#fff}
.arch-set-name{font-size:12px;font-weight:800;letter-spacing:.2px}
.arch-set-sub{font-size:10px;opacity:.8;font-weight:500}
.arch-num-list{flex:1;padding:0;margin:0;list-style:none}
.arch-num-row{padding:8px 10px;border-bottom:.5px solid #f0f0f0;display:flex;flex-direction:column;gap:3px}
.arch-num-row:last-child{border-bottom:none}
.arch-num-row:hover{background:#fafafa}
.arch-num-line{display:flex;align-items:center;gap:8px}
.arch-num-big{font-family:monospace;font-size:22px;font-weight:900;line-height:1;min-width:30px}
.arch-num-big-a{color:var(--green)}
.arch-num-big-b{color:var(--blue)}
.arch-num-big-c{color:#C86A00}
.arch-num-big-d{color:var(--violet)}
.arch-chips{display:flex;flex-wrap:wrap;gap:3px}
.arch-chip{display:inline-flex;align-items:center;gap:2px;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap}
.ach-freq{background:#EAF3DE;color:#065F46}
.ach-si{background:#D1FAE5;color:#065F46}
.ach-si-lo{background:#FEE2E2;color:#7F1D1D}
.ach-si-mid{background:#FEF3C7;color:#92400E}
.ach-w{background:#EEF2FF;color:#312E81}
.ach-z-hi{background:#D1FAE5;color:#065F46}
.ach-z-lo{background:#FEE2E2;color:#7F1D1D}
.ach-z-neu{background:#F3F4F6;color:#6B7280}
.ach-rev{background:#FEF3C7;color:#92400E;font-family:monospace}
.ach-cz{background:#E0E7FF;color:#3730A3}
.ach-bet{background:#F0FDF4;color:#166534;font-family:monospace}
.ach-ev-pos{background:#D1FAE5;color:#065F46;font-family:monospace}
.ach-ev-neg{background:#F3F4F6;color:#6B7280;font-family:monospace}
.ach-genie{background:#EDE9FE;color:#5B21B6;font-size:9px;border:1px solid #C4B5FD}
.arch-card-foot{padding:9px 12px;background:#f7f7f7;border-top:1px solid #ebebeb;display:flex;flex-direction:column;gap:4px}
.arch-foot-row{display:flex;justify-content:space-between;align-items:center;font-size:11px}
.arch-foot-lbl{color:#888;font-weight:600}
.arch-foot-val{font-weight:800;color:#111;font-family:monospace}
.arch-edge-bar{display:flex;align-items:center;gap:4px;margin-top:3px}
.arch-edge-lbl{font-size:10px;color:#888;font-weight:600}
.arch-edge-track{flex:1;height:5px;background:#e5e5e7;border-radius:3px;overflow:hidden}
.arch-edge-fill-a{height:100%;background:var(--green);border-radius:3px}
.arch-edge-fill-b{height:100%;background:var(--blue);border-radius:3px}
.arch-edge-fill-c{height:100%;background:#C86A00;border-radius:3px}
.arch-edge-fill-d{height:100%;background:var(--violet);border-radius:3px}
.arch-edge-pct{font-size:10px;font-weight:800}
.arch-compare{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
@media(max-width:720px){.arch-compare{grid-template-columns:repeat(2,1fr)}}
.arch-cmp-card{border-radius:8px;padding:10px 12px;text-align:center;border:1px solid #e5e5e7}
.arch-cmp-a{background:#F0FDF4;border-color:#A7F3D0}
.arch-cmp-b{background:#EFF6FF;border-color:#BFDBFE}
.arch-cmp-c{background:#FFFBEB;border-color:#FDE68A}
.arch-cmp-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px}
.arch-cmp-title-a{color:#065F46} .arch-cmp-title-b{color:#1E40AF} .arch-cmp-title-c{color:#92400E} .arch-cmp-title-d{color:#5B21B6}
.arch-cmp-d{background:#F5F3FF;border-color:#C4B5FD}
.arch-cmp-nums{font-family:monospace;font-size:14px;font-weight:800;color:#111;letter-spacing:1px}
.arch-cmp-meta{font-size:10px;color:#888;margin-top:3px}

/* anomaly table — renderAnomalies writes class="anom-tbl" */
.anom-tbl{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:4px}
.anom-tbl th{text-align:left;padding:5px 7px;font-weight:700;color:#666;font-size:10px;border-bottom:1px solid #e8e8e8;background:#f7f7f7;white-space:nowrap}
.anom-tbl td{padding:4px 7px;border-bottom:.5px solid #f0f0f0;vertical-align:middle}
.anom-tbl tr:hover td{background:#fafafa}

/* anomaly summary pills — renderAnomalies writes class="asp"/"asp-v"/"asp-w" */
.asp{display:flex;align-items:center;gap:4px;background:#f5f5f7;border:1px solid #e5e5e7;border-radius:16px;padding:3px 10px;font-size:11px}
.asp-v{font-weight:800;color:var(--green)}
.asp-w{font-weight:800;color:var(--amber)}
</style>

</head>
<body>
<div class="wrap">

<!-- HEADER -->
<div class="card">
  <div class="hdr">
    <div>
      <h1>🎯 JPS Tiempos Lab — Dashboard Automático</h1>
      <div class="sub">Nuevos Tiempos Reventados · JPS Costa Rica</div>
    </div>
    <div class="srv-badge">
      <span class="dot" id="dot"></span>
      <span id="srv">Conectando...</span>
    </div>
  </div>
</div>

<!-- STATS BAR -->
<div class="card" id="stats-bar-card" style="display:none;padding:10px 16px">
  <div class="stats-bar" id="stats-bar"></div>
</div>

<!-- CONFIG + PIPELINE -->
<div class="card">
  <div class="cfg">
    <div class="cfg-g">
      <span class="cfg-l">Días histórico</span>
      <select id="days">
        <option value="30">30 días</option>
        <option value="60" selected>60 días</option>
        <option value="90">90 días</option>
        <option value="180">180 días</option>
      </select>
    </div>
    <div class="cfg-g">
      <span class="cfg-l">Budget (₡)</span>
      <input type="number" id="budget" value="5000" min="1000" max="500000" step="500" style="width:88px">
    </div>
    <div class="cfg-g">
      <span class="cfg-l">Tickets &nbsp;<span class="cfg-val" id="tv">5</span></span>
      <input type="range" id="tickets" min="2" max="10" value="5" oninput="document.getElementById('tv').textContent=this.value">
    </div>
    <div class="cfg-g">
      <span class="cfg-l">Perfil de riesgo</span>
      <select id="profile">
        <option value="conservative">Conservador (25% Rev)</option>
        <option value="balanced" selected>Balanceado (45% Rev)</option>
        <option value="aggressive">Agresivo (65% Rev)</option>
      </select>
    </div>
    <div class="cfg-g">
      <span class="cfg-l">Simulaciones MC</span>
      <select id="nsim">
        <option value="10000">10,000</option>
        <option value="20000" selected>20,000</option>
        <option value="50000">50,000</option>
      </select>
    </div>
  </div>
  <div class="brow">
    <button class="btn-go" id="btnGo" onclick="runPipeline()">▶ Ejecutar Pipeline Completo</button>
    <button class="btn-last" onclick="fetchLast()">🔄 Solo último resultado</button>
  </div>
</div>

<!-- CONSOLE -->
<div class="card" style="padding:0;overflow:hidden">
  <div class="con-wrap">
    <div class="con-head">
      <span class="con-title">▸ consola</span>
      <button class="con-clr" onclick="document.getElementById('con').innerHTML=''">limpiar</button>
    </div>
    <div class="con" id="con">
<span class="lh">JPS Tiempos Lab — Servidor local activo</span>
<br><span class="lt">&gt;</span> Configura los parámetros arriba y presiona ▶ Pipeline Completo.
<br><span class="lt">&gt;</span> El pipeline llama el API JPS → calcula top 25 → corre simulador.py → muestra estrategia.
    </div>
  </div>
</div>

<!-- RESULT TABS -->
<div class="card">
  <div class="tabs">
    <button class="tb on" data-t="last"  onclick="gTab(this)">📅 Último Sorteo</button>
    <button class="tb" data-t="freq"     onclick="gTab(this)">📊 Top 25 Frecuencias</button>
    <button class="tb" data-t="mc"       onclick="gTab(this)">🎲 Monte Carlo</button>
    <button class="tb" data-t="strat"    onclick="gTab(this)">🎯 Estrategia</button>
    <button class="tb" data-t="anom"     onclick="gTab(this)">🔬 Anomalías</button>
    <button class="tb" data-t="arch"     onclick="gTab(this)">🏛️ The Architect</button>
  </div>

  <!-- TAB: ÚLTIMO RESULTADO -->
  <div class="tp on" id="tp-last">
    <div class="empty" id="le"><div class="ico">📅</div>Presiona "Solo último resultado" o corre el Pipeline</div>
    <div id="lc" style="display:none">
      <p id="lday" style="font-size:11px;color:#888;margin-bottom:8px"></p>
      <div class="lsg" id="lslots"></div>
    </div>
  </div>

  <!-- TAB: FRECUENCIAS -->
  <div class="tp" id="tp-freq">
    <div class="empty" id="fe"><div class="ico">📊</div>Ejecuta el Pipeline para ver frecuencias históricas</div>
    <div id="fc" style="display:none">
      <div class="rev-card" id="rev-pairs-card" style="display:none">
        <div class="rev-card-title">↔ Pares Reverso Elevados</div>
        <div class="rev-pairs" id="rev-pairs-body"></div>
      </div>
      <div class="ch"><canvas id="fchart"></canvas></div>
      <div class="twrap">
        <table>
          <thead><tr><th>#</th><th>Núm</th><th>Total</th><th>SI Rev</th><th>NO Rev</th><th>% SI</th><th>Visual</th><th>Peso</th><th>↔ Reverso</th></tr></thead>
          <tbody id="ftb"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB: MONTE CARLO -->
  <div class="tp" id="tp-mc">
    <div class="empty" id="me"><div class="ico">🎲</div>Ejecuta el Pipeline para ver resultados de Monte Carlo</div>
    <div id="mcc" style="display:none">
      <p id="mc-info" style="font-size:11px;color:#888;margin-bottom:8px"></p>
      <div class="mc-metrics" id="mc-metrics"></div>
      <div class="ch"><canvas id="mcchart"></canvas></div>
      <div class="twrap">
        <table>
          <thead><tr><th>MC#</th><th>Núm</th><th>Freq#</th><th>Base</th><th>Rev</th><th>Total</th></tr></thead>
          <tbody id="mctb"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB: ESTRATEGIA -->
  <div class="tp" id="tp-strat">
    <div class="empty" id="se"><div class="ico">🎯</div>Ejecuta el Pipeline para generar la estrategia</div>
    <div id="sd" style="display:none">
      <div class="twrap">
        <table>
          <thead><tr><th>#</th><th>Núm</th><th>Base</th><th>Reventado</th><th>Total</th><th>EV estimado</th></tr></thead>
          <tbody id="stb"></tbody>
        </table>
      </div>
      <div class="sg" id="sumg"></div>
      <div style="font-size:10px;color:#aaa;margin-top:10px;padding:8px 10px;background:#fafafa;border-radius:6px;border:1px solid #eee">
        ⚠ Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. El EV negativo es el costo estadístico esperado de jugar.
      </div>
    </div>
  </div>

  <!-- TAB: ANOMALÍAS -->
  <div class="tp" id="tp-anom">
    <div class="empty" id="anome"><div class="ico">🔬</div>Ejecuta el Pipeline para ver el motor de anomalías</div>
    <div id="anomc" style="display:none">
      <div class="anom-sum" id="anom-sum"></div>
      <div class="anom-sec" id="anom-ind"></div>
      <div class="anom-sec" id="anom-rev"></div>
      <div class="anom-sec" id="anom-rr"></div>
      <div class="anom-sec" id="anom-dec"></div>
      <div class="anom-sec" id="anom-ld"></div>
      <div style="font-size:10px;color:#aaa;margin-top:10px;padding:8px 10px;background:#fafafa;border-radius:6px;border:1px solid #eee">
        ⚠ Anomalías estadísticas en datos históricos — no implican causalidad ni predicción futura.
      </div>
    </div>
  </div>

  <!-- TAB: THE ARCHITECT -->
  <div class="tp" id="tp-arch">
    <div class="empty" id="arche"><div class="ico">🏛️</div>Ejecuta el Pipeline para ver The Architect</div>
    <div id="archc" style="display:none">
      <div class="arch-compare" id="arch-cmp"></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px;font-size:11px;color:#666" id="arch-hdr"></div>
      <div class="arch-grid">
        <div class="arch-card arch-card-a">
          <div class="arch-head arch-head-a"><div class="arch-name">🎯 Set A — Estrategia</div><div class="arch-sub">Pipeline · MC top peso</div></div>
          <ul class="arch-list" id="alist-a"></ul>
          <div class="arch-foot" id="afoot-a"></div>
        </div>
        <div class="arch-card arch-card-b">
          <div class="arch-head arch-head-b"><div class="arch-name">📊 Set B — Freq Elite</div><div class="arch-sub">Top peso+SI% · sin A</div></div>
          <ul class="arch-list" id="alist-b"></ul>
          <div class="arch-foot" id="afoot-b"></div>
        </div>
        <div class="arch-card arch-card-c">
          <div class="arch-head arch-head-c"><div class="arch-name">↔ Set C — Reverso Edge</div><div class="arch-sub">Espejos de A+B · sin A∪B</div></div>
          <ul class="arch-list" id="alist-c"></ul>
          <div class="arch-foot" id="afoot-c"></div>
        </div>
        <div class="arch-card arch-card-d">
          <div class="arch-head arch-head-d"><div class="arch-name">🧞 Set D — The Genie</div><div class="arch-sub">Desplazados + iteración</div></div>
          <ul class="arch-list" id="alist-d"></ul>
          <div class="arch-foot" id="afoot-d"></div>
        </div>
      </div>
      <div style="font-size:10px;color:#aaa;margin-top:12px;padding:8px 10px;background:#fafafa;border-radius:6px;border:1px solid #eee">
        ⚠ The Architect organiza 4 perspectivas estadísticas. P(exacto)=1/100 por sorteo — ningún sistema garantiza resultados.
      </div>
    </div>
  </div>

</div><!-- end card tabs -->
</div><!-- end wrap -->

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<script>
// ── STATE & UTILS ──────────────────────────────────────────────────────────────
let fCI=null,mcCI=null;
const ts=()=>new Date().toTimeString().slice(0,8);
function log(msg,cls=''){
  const c=document.getElementById('con');
  const d=document.createElement('div');
  d.innerHTML='<span class="lt">['+ts()+']</span> <span class="'+(cls||'')+'">'+(msg||'')+'</span>';
  c.appendChild(d);c.scrollTop=c.scrollHeight;
}
function gTab(btn){
  document.querySelectorAll('.tb').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.tp').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on');
  document.getElementById('tp-'+btn.dataset.t).classList.add('on');
}
function showTab(t){const b=document.querySelector('[data-t="'+t+'"]');if(b)b.click();}
function fmtC(n){return '₡'+Math.round(n).toLocaleString();}

// ── STATUS POLL ────────────────────────────────────────────────────────────────
async function poll(){
  try{
    const r=await fetch('/api/status');const d=await r.json();
    const dot=document.getElementById('dot'),srv=document.getElementById('srv');
    dot.style.background='#1D9E75';
    srv.textContent='Servidor activo · '+d.draws+' sorteos en memoria';
    if(d.has_out||d.top25>0){
      const sr=await fetch('/api/state');const sd=await sr.json();
      if(sd.last)renderLast(sd.last);
    }
  }catch(e){
    document.getElementById('dot').style.background='#E24B4A';
    document.getElementById('srv').textContent='Sin conexión — ¿está corriendo el servidor?';
  }
}
poll();setInterval(poll,5000);

// ── FETCH LAST ─────────────────────────────────────────────────────────────────
async function fetchLast(){
  log('Consultando último resultado del API JPS...');
  try{
    const r=await fetch('/api/last');const d=await r.json();
    if(!d.ok)throw new Error(d.error||'Error');
    renderLast(d.data);showTab('last');
    log('✓ Último resultado actualizado','lok');
  }catch(e){log('Error: '+e.message,'le');}
}

// ── RUN PIPELINE ───────────────────────────────────────────────────────────────
async function runPipeline(){
  const btn=document.getElementById('btnGo');
  btn.disabled=true;btn.textContent='⏳ Ejecutando...';
  const budget=document.getElementById('budget').value||5000;
  const n=document.getElementById('tickets').value||5;
  const profile=document.getElementById('profile').value;
  const days=document.getElementById('days').value||60;
  const nSim=document.getElementById('nsim').value||20000;
  log('══════ PIPELINE COMPLETO ══════','lh');
  log('[1/5] Descargando histórico ('+days+' días) + último resultado...');
  try{
    const t0=Date.now();
    const url='/api/pipeline?budget='+budget+'&n='+n+'&profile='+profile+'&days='+days+'&nsim='+nSim;
    const r=await fetch(url);const d=await r.json();
    if(!d.ok)throw new Error(d.error||'Pipeline falló');
    (d.log||[]).forEach(l=>log(l));
    log('══ Pipeline OK en '+((Date.now()-t0)/1000).toFixed(1)+'s — 6 pestañas listas ══','lok');
    const rrFrac=(d.rev_rate||33.33)/100;
    if(d.last_result)renderLast(d.last_result);
    if(d.top25&&d.top25.length){
      enrichTop25(d.top25);
      renderFreq(d.top25);
      renderRevPairs(d.top25);
      updateStatsBar(d.total_valid,d.rev_si,rrFrac,d.top25);
    }
    if(d.output){renderMC(d.output,d.top25||[]);renderStrat(d.output);}
    if(d.anomalies)renderAnomalies(d.anomalies);
    if(d.top25&&d.anomalies&&d.output)renderArchitect(d.top25,d.anomalies,d.output,parseInt(budget),profile);
    showTab('arch');
  }catch(e){log('Error en pipeline: '+e.message,'le');}
  btn.disabled=false;btn.textContent='▶ Ejecutar Pipeline Completo';
}

// ── ENRICH TOP25 ───────────────────────────────────────────────────────────────
function enrichTop25(top25){
  const t25set=new Set(top25.map(n=>String(n.num_str||'').padStart(2,'0')));
  top25.forEach(n=>{
    n.num_str=String(n.num_str||n.num||0).padStart(2,'0');
    if(n.reverso==null){
      const r=n.num_str[1]+n.num_str[0];
      n.reverso=r;n.rev_total=top25.find(x=>x.num_str===r)?.total||0;
      n.rev_z=0;n.rev_in_top=t25set.has(r)&&r!==n.num_str;
    }
  });
}

// ── RENDER: ÚLTIMO SORTEO ──────────────────────────────────────────────────────
function renderLast(data){
  document.getElementById('lday').textContent='Fecha del sorteo: '+(data.dia||'—');
  const slots={manana:'Mañana',mediaTarde:'Media Tarde',tarde:'Tarde'};
  const bColors={ROJA:'#E24B4A',AZUL:'#378ADD',VERDE:'#1D9E75',AMARILLA:'#BA7517'};
  let html='';
  for(const[key,label] of Object.entries(slots)){
    const s=data[key];
    if(!s||s.estado===0){
      html+='<div class="lsc"><div class="lsl">'+label+'</div><div style="color:#ddd;padding:18px 0;font-size:11px">Sin resultado</div></div>';
    }else{
      const num=String(s.numero??'?').padStart(2,'0');
      const mega=String(s.meganNumero??'?').padStart(2,'0');
      const rev=s.in_reventado===1;
      const b=(s.colorBolita||s.descripcionBolita||'').toUpperCase();
      const bc=bColors[b]||'#888';
      html+='<div class="lsc"><div class="lsl">'+label+'</div>'
        +'<div class="lsn">'+num+'</div>'
        +'<div class="lsm">Mega <strong>'+mega+'</strong> &nbsp;|&nbsp; Rev: '
        +(rev?'<span class="rsi">✓ SI</span>':'<span class="rno">NO</span>')+'</div>'
        +(b?'<div><span class="bola" style="background:'+bc+'22;color:'+bc+'">⬤ '+(s.colorBolita||s.descripcionBolita||'')+'</span></div>':'')
        +'</div>';
    }
  }
  document.getElementById('lslots').innerHTML=html;
  document.getElementById('le').style.display='none';
  document.getElementById('lc').style.display='block';
}

// ── RENDER: FRECUENCIAS ────────────────────────────────────────────────────────
function renderFreq(top25){
  const maxF=top25[0]?.total||1;
  const ctx=document.getElementById('fchart').getContext('2d');
  if(fCI)fCI.destroy();
  fCI=new Chart(ctx,{type:'bar',data:{
    labels:top25.map(n=>n.num_str),
    datasets:[
      {label:'Con Reventada (SI)',data:top25.map(n=>n.si),backgroundColor:'#1D9E75',borderRadius:2},
      {label:'Sin Reventada (NO)',data:top25.map(n=>n.no),backgroundColor:'#B5D4F4',borderRadius:2}
    ]
  },options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'top',labels:{font:{size:10},boxWidth:10,padding:6}}},
    scales:{x:{stacked:true,ticks:{font:{size:10},color:'#555'},grid:{display:false}},
            y:{stacked:true,ticks:{font:{size:10}}}}}});
  const tb=document.getElementById('ftb');tb.innerHTML='';
  const t25set=new Set(top25.map(x=>x.num_str));
  top25.forEach(n=>{
    const sw=Math.round(n.si/maxF*55),nw=Math.round(n.no/maxF*55);
    const bc=n.si_pct>=42?'bg':n.si_pct<=24?'br':'ba';
    const wc=n.weight>=1.3?'#0F6E56':n.weight<=0.75?'#E24B4A':'#666';
    const revStr=n.reverso||(n.num_str[1]+n.num_str[0]);
    const revT=n.rev_total||0,revZ=n.rev_z||null;
    const revInTop=n.rev_in_top||t25set.has(revStr)&&revStr!==n.num_str;
    const revZCol=revZ>=2.0?'#0F6E56':revZ>=1.0?'#E8A020':'#aaa';
    const revCell=revInTop
      ?'<span style="font-family:monospace;font-weight:800;color:#0F6E56;background:#EAF3DE;border-radius:4px;padding:1px 6px">'+revStr+' ★</span>'+(revZ!=null?' <span style="font-size:10px;color:'+revZCol+'">z='+revZ+'</span>':'')
      :'<span style="font-family:monospace;color:#666">'+revStr+'</span> <span style="font-size:10px;color:'+revZCol+'">('+revT+')'+(revZ!=null?' z='+revZ:'')+'</span>';
    tb.insertAdjacentHTML('beforeend','<tr>'
      +'<td class="mu">#'+n.rank+'</td><td class="nm">'+n.num_str+'</td>'
      +'<td><strong>'+n.total+'</strong></td>'
      +'<td style="color:#0F6E56;font-weight:700">'+n.si+'</td>'
      +'<td style="color:#378ADD;font-weight:700">'+n.no+'</td>'
      +'<td><span class="bdg '+bc+'">'+n.si_pct+'%</span></td>'
      +'<td><div style="display:flex;gap:1px">'
      +'<div class="mb" style="width:'+sw+'px;background:#1D9E75"></div>'
      +'<div class="mb" style="width:'+nw+'px;background:#B5D4F4"></div>'
      +'</div></td>'
      +'<td style="font-family:monospace;font-weight:700;color:'+wc+'">'+n.weight+'x</td>'
      +'<td>'+revCell+'</td></tr>');
  });
  document.getElementById('fe').style.display='none';
  document.getElementById('fc').style.display='block';
}

// ── STATS BAR ──────────────────────────────────────────────────────────────────
function updateStatsBar(total,revSI,revRate,top25){
  document.getElementById('stats-bar-card').style.display='block';
  const rrPct=(revRate*100).toFixed(1)+'%';
  const revZ=(revRate-1/3)/Math.sqrt((1/3)*(2/3)/total);
  const revCls=Math.abs(revZ)>2.576?'color:#E24B4A':'color:#0F6E56';
  const se=Math.sqrt(0.01*0.99/total);
  const maxZ=Math.max(...top25.map(n=>Math.abs((n.total/total-0.01)/se)));
  const zCls=maxZ>2.576?'color:#E24B4A':maxZ>1.96?'color:#C86A00':'color:#0F6E56';
  const pairs=[];const seen=new Set();
  top25.forEach(n=>{
    const key=[n.num_str,n.reverso].sort().join('-');
    if(!seen.has(key)&&n.rev_in_top){seen.add(key);pairs.push(n.num_str+'↔'+n.reverso);}
  });
  document.getElementById('stats-bar').innerHTML=
    '<div class="stat-pill">📅 ~<strong>'+Math.round(total/3)+'</strong> días · <strong>'+total.toLocaleString()+'</strong> sorteos</div>'
   +'<div class="stat-pill">🔄 Rev <strong style="'+revCls+'">'+rrPct+'</strong> <span style="color:#aaa">(esp.33.3%)</span></div>'
   +'<div class="stat-pill">⚡ z-max <strong style="'+zCls+'">'+maxZ.toFixed(2)+'</strong></div>'
   +(pairs.length?'<div class="stat-pill">↔ Reversos <strong style="color:#0F6E56">'+pairs.slice(0,3).join(' ')+'</strong></div>':'');
}

// ── REVERSO PAIRS CARD ─────────────────────────────────────────────────────────
function renderRevPairs(top25){
  const pairs=[];const seen=new Set();
  top25.forEach(n=>{
    if(!n.rev_in_top)return;
    const key=[n.num_str,n.reverso].sort().join('-');
    if(seen.has(key))return;seen.add(key);
    const revEntry=top25.find(x=>x.num_str===n.reverso);
    pairs.push({a:n.num_str,b:n.reverso,za:n.rev_z||0,zb:revEntry?revEntry.rev_z||0:0,fa:n.total,fb:n.rev_total||0});
  });
  const card=document.getElementById('rev-pairs-card');
  const body=document.getElementById('rev-pairs-body');
  if(!pairs.length){card.style.display='none';return;}
  card.style.display='block';
  body.innerHTML=pairs.map(p=>{
    const zMax=Math.max(Math.abs(p.za),Math.abs(p.zb));
    const badge=zMax>=2.576?'<span class="anom-hi">z&gt;2.58 ★</span>':zMax>=1.96?'<span class="anom">z&gt;1.96</span>':'';
    return '<div class="rev-pair"><span class="rp-num">'+p.a+'</span><span class="rp-arrow">↔</span><span class="rp-num">'+p.b+'</span><span class="rp-z">('+p.fa+'↔'+p.fb+')</span>'+badge+'</div>';
  }).join('');
}

// ── RENDER: MONTE CARLO ────────────────────────────────────────────────────────
function renderMC(output,top25){
  const mc=output.monte_carlo_result||{};
  document.getElementById('mc-info').textContent=
    (mc.Simulaciones||0).toLocaleString()+' simulaciones · Total apostado: '+fmtC(output.total_apostado||0);
  const pG=(mc['Probabilidad Ganar']||0)*100,pMed=mc['Mediana Neto']||0;
  const pP95=mc['P95 Neto']||0,pAvg=mc['Promedio Neto']||0;
  const pStd=mc['Desviacion Std']||0,pP5=mc['P5 Neto']||0;
  document.getElementById('mc-metrics').innerHTML=
    '<div class="mcc"><div class="mcl">Prob. Ganar</div><div class="mcv" style="color:'+(pG>7?'#0F6E56':'#E24B4A')+'">'+pG.toFixed(2)+'%</div></div>'
   +'<div class="mcc"><div class="mcl">Mediana</div><div class="mcv" style="color:'+(pMed>=0?'#0F6E56':'#888')+'">'+fmtC(pMed)+'</div></div>'
   +'<div class="mcc"><div class="mcl">P95 Upside</div><div class="mcv" style="color:#0F6E56">'+fmtC(pP95)+'</div></div>'
   +'<div class="mcc"><div class="mcl">Promedio Neto</div><div class="mcv">'+fmtC(pAvg)+'</div></div>'
   +'<div class="mcc"><div class="mcl">P5 Peor 5%</div><div class="mcv" style="color:#E24B4A">'+fmtC(pP5)+'</div></div>'
   +'<div class="mcc"><div class="mcl">Desv. Std</div><div class="mcv">'+fmtC(pStd)+'</div></div>';
  const ctx=document.getElementById('mcchart').getContext('2d');
  if(mcCI)mcCI.destroy();
  mcCI=new Chart(ctx,{type:'bar',data:{
    labels:['P5','Promedio','Mediana','P95'],
    datasets:[{data:[pP5,Math.round(pAvg),pMed,pP95],
      backgroundColor:[pP5>=0?'#1D9E75':'#E24B4A',pAvg>=0?'#5DCAA5':'#F09595','#B5D4F4','#1D9E75'],borderRadius:4}]
  },options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'₡'+Math.round(c.parsed.y).toLocaleString()}}},
    scales:{x:{ticks:{font:{size:10},color:'#555'},grid:{display:false}},
            y:{ticks:{font:{size:10},callback:v=>'₡'+Math.round(v).toLocaleString()}}}}});
  const tb=document.getElementById('mctb');tb.innerHTML='';
  (output.tickets||[]).forEach((t,i)=>{
    const num=String(t.num_exacto??0).padStart(2,'0');
    const top=(top25||[]).find(x=>x.num_str===num)||{};
    tb.insertAdjacentHTML('beforeend','<tr>'
      +'<td><strong>#'+(i+1)+'</strong></td><td class="nm">'+num+'</td>'
      +'<td class="mu">#'+(top.rank||'—')+'</td>'
      +'<td style="font-family:monospace">'+fmtC(t.base)+'</td>'
      +'<td style="font-family:monospace;color:#0F6E56;font-weight:700">'+fmtC(t.rev)+'</td>'
      +'<td style="font-family:monospace;font-weight:700">'+fmtC(t.base+t.rev)+'</td>'
      +'</tr>');
  });
  document.getElementById('me').style.display='none';
  document.getElementById('mcc').style.display='block';
}

// ── RENDER: ESTRATEGIA ─────────────────────────────────────────────────────────
function renderStrat(output){
  const mc=output.monte_carlo_result||{};
  const tb=document.getElementById('stb');tb.innerHTML='';
  let totS=0,totR=0,totEV=0;
  (output.tickets||[]).forEach((t,i)=>{
    const num=String(t.num_exacto??0).padStart(2,'0');
    const ev=(1/100)*(70*t.base+(1/3)*200*t.rev)-(t.base+t.rev);
    totS+=t.base+t.rev;totR+=t.rev;totEV+=ev;
    tb.insertAdjacentHTML('beforeend','<tr>'
      +'<td class="mu">'+(i+1)+'</td><td class="nm">'+num+'</td>'
      +'<td style="font-family:monospace">'+fmtC(t.base)+'</td>'
      +'<td style="font-family:monospace;color:#0F6E56;font-weight:700">'+fmtC(t.rev)+'</td>'
      +'<td style="font-family:monospace;font-weight:700">'+fmtC(t.base+t.rev)+'</td>'
      +'<td style="font-family:monospace;color:'+(ev>=0?'#0F6E56':'#888')+'">'+fmtC(ev)+'</td>'
      +'</tr>');
  });
  const pG=(mc['Probabilidad Ganar']||0)*100,p95=mc['P95 Neto']||0;
  const pMed=mc['Mediana Neto']||0,pAvg=mc['Promedio Neto']||0;
  const rp=totS>0?totR/totS:0;
  const risk=rp>=.55?'ALTO':rp>=.35?'MEDIO':'BAJO';
  const rc=risk==='ALTO'?'br':risk==='MEDIO'?'ba':'bg';
  document.getElementById('sumg').innerHTML=
    '<div class="sc"><div class="sl">Total Apostado</div><div class="sv">'+fmtC(totS)+'</div></div>'
   +'<div class="sc"><div class="sl">EV por sorteo</div><div class="sv" style="color:'+(totEV>=0?'#0F6E56':'#888')+'">'+fmtC(totEV)+'</div><div class="ss">costo esperado</div></div>'
   +'<div class="sc"><div class="sl">Riesgo Rev</div><div class="sv"><span class="bdg '+rc+'">'+risk+'</span></div><div class="ss">'+(rp*100).toFixed(0)+'% del total</div></div>'
   +'<div class="sc sc2"><div class="sl">Prob. de Ganar (MC)</div><div class="sv" style="color:'+(pG>7?'#0F6E56':'#E24B4A')+'">'+pG.toFixed(2)+'%</div><div class="ss">Mediana: '+fmtC(pMed)+' · Prom: '+fmtC(pAvg)+'</div></div>'
   +'<div class="sc sc2"><div class="sl">Upside P95</div><div class="sv" style="color:#0F6E56">'+fmtC(p95)+'</div><div class="ss">escenario mejor 5%</div></div>';
  document.getElementById('se').style.display='none';
  document.getElementById('sd').style.display='block';
}

// ── RENDER: ANOMALÍAS ──────────────────────────────────────────────────────────
function renderAnomalies(anom){
  if(!anom||anom.error)return;
  const chi2=anom.chi2||0,nSig=anom.n_sig_individual||0,rr=parseFloat(anom.rev_rate_global||33.33);
  document.getElementById('anom-sum').innerHTML=
    '<div class="asp">Sorteos: <span class="asp-v">'+(anom.total||'—')+'</span></div>'
   +'<div class="asp">Chi² (DF=99): <span class="'+(chi2>123.2?'asp-w':'asp-v')+'">'+chi2+' '+(chi2>123.2?'⚠':'✓')+'</span></div>'
   +'<div class="asp">Nums p&lt;0.01: <span class="'+(nSig>0?'asp-w':'asp-v')+'">'+nSig+'</span></div>'
   +'<div class="asp">Rev. global: <span class="'+(Math.abs(rr-33.33)>3?'asp-w':'asp-v')+'">'+rr+'% (esp.33.33%)</span></div>';
  function zB(z){const v=parseFloat(z||0);const c=v>=2.576?'ab-hi':v<=-2.576?'ab-lo':v>=1?'ab-w':v<=-1?'ab-w':'ab-ok';return '<span class="ab '+c+'">z='+(v>=0?'+':'')+v+'</span>';}
  function chipCls(z){return parseFloat(z||0)>=0.5?'ac-hi':parseFloat(z||0)<=-0.5?'ac-lo':'ac-neu';}
  const outs=anom.outliers||[];
  let h='<div class="anom-sec-title">🔢 Números fuera de rango (|z|≥1.5)</div>';
  if(!outs.length)h+='<span style="color:#aaa;font-size:11px">Ninguno detectado.</span>';
  else{
    h+='<div class="anom-chips">';
    outs.forEach(n=>{h+='<div class="ac-chip '+chipCls(n.z)+'">'+n.num_str+'<span class="ac-z">('+n.total+')</span><span class="ac-z">z='+(n.z>=0?'+':'')+n.z+'</span>'+(Math.abs(n.z)>=2.576?'<span class="ac-star">★★</span>':Math.abs(n.z)>=1.96?'<span class="ac-star">★</span>':'')+'</div>';});
    h+='</div><p style="font-size:10px;color:#888;margin-top:3px">Verde=alto · Rojo=bajo · ★★ p&lt;0.01 · ★ p&lt;0.05</p>';
  }
  document.getElementById('anom-ind').innerHTML=h;
  const pairs=anom.rev_pairs||[];
  let rh='<div class="anom-sec-title">↔ Pares Reverso — análisis espejo completo</div>';
  if(!pairs.length)rh+='<span style="color:#aaa;font-size:11px">Sin pares notables.</span>';
  else{
    rh+='<table class="anom-tbl"><thead><tr><th>Par</th><th>A freq</th><th>z(A)</th><th>B freq</th><th>z(B)</th><th>z comb.</th><th>Patrón</th></tr></thead><tbody>';
    pairs.forEach(p=>{
      const cz=parseFloat(p.combined_z),czc=cz>=1.5?'ab-hi':cz<=-1.5?'ab-lo':'ab-w';
      const dc=p.direction==='ambos altos'?'ab-hi':p.direction==='ambos bajos'?'ab-lo':'ab-w';
      rh+='<tr><td style="font-family:monospace;font-weight:800;color:#0F6E56">'+p.a+'↔'+p.b+'</td><td>'+p.a_total+'</td><td>'+zB(p.a_z)+'</td><td>'+p.b_total+'</td><td>'+zB(p.b_z)+'</td><td><span class="ab '+czc+'">'+(cz>=0?'+':'')+cz+'</span></td><td><span class="ab '+dc+'">'+p.direction+'</span></td></tr>';
    });
    rh+='</tbody></table>';
  }
  document.getElementById('anom-rev').innerHTML=rh;
  const ro=anom.rev_outliers||[];
  let rrh='<div class="anom-sec-title">🔴 Reventada por número (mín.5 sorteos, |z|≥1.5)</div>';
  if(!ro.length)rrh+='<span style="color:#aaa;font-size:11px">Sin anomalías en tasa de Reventada.</span>';
  else{
    rrh+='<table class="anom-tbl"><thead><tr><th>Núm</th><th>Sorteos</th><th>SI</th><th>Tasa obs.</th><th>Esperada</th><th>z</th></tr></thead><tbody>';
    ro.forEach(r=>{rrh+='<tr><td class="nm" style="font-family:monospace;font-weight:800;color:#0F6E56">'+r.num_str+'</td><td>'+r.total+'</td><td>'+r.si+'</td><td><strong>'+r.rate_pct+'%</strong></td><td style="color:#aaa">'+r.exp_pct+'%</td><td>'+zB(r.z)+'</td></tr>';});
    rrh+='</tbody></table>';
  }
  document.getElementById('anom-rr').innerHTML=rrh;
  const decs=anom.decade_bias||[];const maxDO=decs.length?Math.max(...decs.map(d=>d.observed)):1;
  let dh='<div class="anom-sec-title">📊 Sesgo por decena</div><div class="dec-wrap">';
  [...decs].sort((a,b)=>parseInt(a.label)-parseInt(b.label)).forEach(d=>{
    const w=Math.round(d.observed/maxDO*120);const col=parseFloat(d.z)>=1.5?'#1D9E75':parseFloat(d.z)<=-1.5?'#E24B4A':'#B5D4F4';
    dh+='<div class="dec-row"><span class="dec-lbl">'+d.label+'</span><div class="dec-bar" style="width:'+w+'px;background:'+col+'"></div><span class="dec-meta">'+d.observed+' '+zB(d.z)+'</span></div>';
  });
  dh+='</div>';document.getElementById('anom-dec').innerHTML=dh;
  const lds=anom.last_digit||[];const maxLD=lds.length?Math.max(...lds.map(d=>d.observed)):1;
  let ldh='<div class="anom-sec-title">🔢 Sesgo por dígito final</div><div class="dec-wrap">';
  [...lds].sort((a,b)=>a.digit-b.digit).forEach(d=>{
    const w=Math.round(d.observed/maxLD*120);const col=parseFloat(d.z)>=1.5?'#1D9E75':parseFloat(d.z)<=-1.5?'#E24B4A':'#B5D4F4';
    ldh+='<div class="dec-row"><span class="dec-lbl" style="width:30px">×'+d.digit+'</span><div class="dec-bar" style="width:'+w+'px;background:'+col+'"></div><span class="dec-meta">'+d.observed+' '+zB(d.z)+'</span></div>';
  });
  ldh+='</div>';document.getElementById('anom-ld').innerHTML=ldh;
  document.getElementById('anome').style.display='none';
  document.getElementById('anomc').style.display='block';
}

// ── RENDER: THE ARCHITECT ──────────────────────────────────────────────────────
function renderArchitect(top25,anom,output,budget,profile){
  if(!top25?.length||!output)return;
  const rratio={conservative:.25,balanced:.45,aggressive:.65}[profile]||.45;
  const profLbl=rratio<=0.3?'Conservador':rratio>=0.55?'Agresivo':'Balanceado';
  const nSim=(output.monte_carlo_result?.Simulaciones||0).toLocaleString();
  const rawPT=Math.max(200,Math.floor(budget/5/100)*100);
  const rawRev=Math.max(100,Math.min(Math.floor(rawPT*rratio/100)*100,rawPT-100));
  const rawBase=rawPT-rawRev;
  function ev(b,r){return Math.round((1/100)*(70*b+(1/3)*200*r)-(b+r));}
  function fC(n){return '₡'+Math.round(n).toLocaleString();}
  function zCls(z){const v=parseFloat(z||0);return v>=2?'ach-zhi':v<=-2?'ach-zlo':v>=1?'ach-zhi':v<=-1?'ach-zlo':'ach-zn';}
  function siCls(p){return p>=42?'ach-si':p<=24?'ach-si-lo':'ach-si-mid';}
  const t25m={};top25.forEach(n=>t25m[n.num_str]=n);
  const zm={};(anom?.outliers||[]).forEach(o=>zm[o.num_str]=o.z);
  function mkT(ns,extra={}){
    const d=t25m[ns]||{};const b=extra.base??rawBase,r=extra.rev??Math.min(rawRev,b);
    return{num_str:ns,base:b,rev:r,total:b+r,ev:ev(b,r),freq:d.total||0,si_pct:d.si_pct||0,
           weight:d.weight||0,z:zm[ns]??null,reverso:d.reverso,rev_in_top:d.rev_in_top||false,...extra};
  }
  const setA=(output.tickets||[]).slice(0,5).map(t=>{
    const ns=String(t.num_exacto??t.num??0).padStart(2,'0');
    return mkT(ns,{base:t.base||rawBase,rev:t.rev||rawRev});
  });
  const setAn=new Set(setA.map(t=>t.num_str));
  const dispB=[];const setB=[];
  [...top25].sort((a,b)=>b.weight!==a.weight?b.weight-a.weight:b.si_pct-a.si_pct).forEach(n=>{
    if(setAn.has(n.num_str)){if(dispB.length<8)dispB.push({...n,genieSource:'⬆ Top-B bloqueado por A'});}
    else if(setB.length<5)setB.push(mkT(n.num_str));
  });
  const setBn=new Set(setB.map(t=>t.num_str));
  const abn=new Set([...setAn,...setBn]);
  const dispC=[],cMap=new Map();
  [...setA,...setB].forEach(t=>{
    const ns=t.num_str,rev=ns[1]+ns[0];
    if(rev===ns||cMap.has(rev))return;
    const d=t25m[rev]||{};
    const c={num_str:rev,freq:d.total||0,si_pct:d.si_pct||0,weight:d.weight||0,z:zm[rev]??0,sourceOf:ns};
    if(abn.has(rev))dispC.push({...c,genieSource:'↔ Reverso ya en A/B'});
    else cMap.set(rev,c);
  });
  const cCands=[...cMap.values()].sort((a,b)=>Math.abs(b.z)-Math.abs(a.z)||b.weight-a.weight);
  const setC=cCands.slice(0,5).map(c=>mkT(c.num_str,{sourceOf:c.sourceOf}));
  const setCn=new Set(setC.map(t=>t.num_str));
  const cOvf=cCands.slice(5);
  const gAdd=new Set([...setAn,...setBn,...setCn]);const gPool=[];
  function addG(item){if(!gAdd.has(item.num_str)){gAdd.add(item.num_str);gPool.push(item);}}
  dispB.forEach(n=>addG(n));dispC.forEach(n=>addG(n));
  cOvf.forEach(c=>addG({...c,genieSource:'↔ Reverso desbordado de C'}));
  (anom?.outliers||[]).filter(o=>!gAdd.has(o.num_str)&&parseFloat(o.z)>1.5).sort((a,b)=>b.z-a.z).forEach(o=>addG({num_str:o.num_str,...(t25m[o.num_str]||{}),z:o.z,freq:t25m[o.num_str]?.total||0,genieSource:'🔬 Anomalía z>1.5'}));
  (anom?.rev_outliers||[]).filter(o=>!gAdd.has(o.num_str)&&o.z>1.0).forEach(o=>addG({num_str:o.num_str,...(t25m[o.num_str]||{}),z:zm[o.num_str]||0,freq:t25m[o.num_str]?.total||0,genieSource:'🔴 SI-Rev outlier'}));
  top25.filter(n=>!gAdd.has(n.num_str)).map(n=>({...n,gS:n.weight*(n.si_pct/100+0.3)*(1+Math.abs(zm[n.num_str]||0)/5)})).sort((a,b)=>b.gS-a.gS).forEach(n=>addG({...n,freq:n.total,z:zm[n.num_str]??null,genieSource:'💡 Score compuesto'}));
  const setD=gPool.slice(0,5).map(item=>mkT(item.num_str,{genieSource:item.genieSource}));
  function row(t,sk){
    const zv=t.z!=null?parseFloat(t.z):null;
    const zStr=zv!=null?(zv>=0?'+':'')+zv.toFixed(2):'—';
    const zBadge=zv!=null?'<span class="ach '+zCls(zv)+'">z '+zStr+'</span>':'';
    const siB='<span class="ach '+siCls(t.si_pct)+'">SI '+t.si_pct+'%</span>';
    const wB=t.weight?'<span class="ach ach-w">w '+parseFloat(t.weight).toFixed(2)+'x</span>':'';
    const fB=t.freq?'<span class="ach ach-freq">'+t.freq+' hist.</span>':'';
    const betB='<span class="ach ach-bet">'+fC(t.base)+'+'+fC(t.rev)+'</span>';
    const evB='<span class="ach '+(t.ev>=0?'ach-evp':'ach-evn')+'">EV '+fC(t.ev)+'</span>';
    const revB=t.rev_in_top?'<span class="ach ach-rev">↔'+t.reverso+'★</span>':'';
    const srcB=t.sourceOf?'<span class="ach ach-rev">↔de '+t.sourceOf+'</span>':'';
    const gnB=t.genieSource?'<span class="ach ach-genie">'+t.genieSource+'</span>':'';
    return '<li class="arch-row"><div class="arch-rl"><span class="arch-num-big arch-num-big-'+sk+'">'+t.num_str+'</span>'
      +'<div style="display:flex;flex-wrap:wrap;gap:3px">'+fB+siB+wB+zBadge+revB+srcB+'</div></div>'
      +'<div style="display:flex;flex-wrap:wrap;gap:3px">'+betB+evB+gnB+'</div></li>';
  }
  function foot(set,elId,fillCls,lbl,score,max){
    const tB=set.reduce((s,t)=>s+t.total,0),tE=set.reduce((s,t)=>s+t.ev,0);
    const pct=Math.min(100,Math.round(Math.abs(score)/max*100));
    document.getElementById(elId).innerHTML=
      '<div class="arch-fr"><span class="arch-fl">Total</span><span class="arch-fv">'+fC(tB)+'</span></div>'
     +'<div class="arch-fr"><span class="arch-fl">EV/sorteo</span><span class="arch-fv" style="color:'+(tE>=0?'#0F6E56':'#888')+'">'+fC(tE)+'</span></div>'
     +'<div class="arch-edge-bar"><span class="arch-edge-lbl">'+lbl+'</span><div class="arch-edge-track"><div class="'+fillCls+'" style="width:'+pct+'%"></div></div><span class="arch-edge-pct">'+pct+'%</span></div>';
  }
  const empty='<li class="arch-row" style="color:#aaa;font-size:11px;padding:12px">Sin candidatos.</li>';
  document.getElementById('alist-a').innerHTML=setA.map(t=>row(t,'a')).join('')||empty;
  document.getElementById('alist-b').innerHTML=setB.map(t=>row(t,'b')).join('')||empty;
  document.getElementById('alist-c').innerHTML=setC.map(t=>row(t,'c')).join('')||empty;
  document.getElementById('alist-d').innerHTML=setD.map(t=>row(t,'d')).join('')||empty;
  const mc=output.monte_carlo_result||{},pGan=(mc['Probabilidad Ganar']||0)*100;
  const avgWB=setB.length?setB.reduce((s,t)=>s+(t.weight||0),0)/setB.length:0;
  const avgWC=setC.length?setC.reduce((s,t)=>s+(t.weight||0),0)/setC.length:0;
  const avgWD=setD.length?setD.reduce((s,t)=>s+(t.weight||0),0)/setD.length:0;
  foot(setA,'afoot-a','arch-edge-fill-a','MC win%',pGan,100);
  foot(setB,'afoot-b','arch-edge-fill-b','Avg peso',avgWB,3);
  foot(setC,'afoot-c','arch-edge-fill-c','Avg peso',avgWC,3);
  foot(setD,'afoot-d','arch-edge-fill-d','Avg peso',avgWD,3);
  const numsA=setA.map(t=>t.num_str).join('·')||'—';
  const numsB=setB.map(t=>t.num_str).join('·')||'—';
  const numsC=setC.map(t=>t.num_str).join('·')||'—';
  const numsD=setD.map(t=>t.num_str).join('·')||'—';
  const avgSI=setB.length?setB.reduce((s,t)=>s+t.si_pct,0)/setB.length:0;
  document.getElementById('arch-cmp').innerHTML=
    '<div class="arch-cmp-card arch-cmp-a"><div class="arch-cmp-title arch-cmp-title-a">Set A — Estrategia</div><div class="arch-cmp-nums">'+numsA+'</div><div class="arch-cmp-meta">MC '+pGan.toFixed(1)+'%</div></div>'
   +'<div class="arch-cmp-card arch-cmp-b"><div class="arch-cmp-title arch-cmp-title-b">Set B — Freq Elite</div><div class="arch-cmp-nums">'+numsB+'</div><div class="arch-cmp-meta">SI '+avgSI.toFixed(1)+'% · sin A</div></div>'
   +'<div class="arch-cmp-card arch-cmp-c"><div class="arch-cmp-title arch-cmp-title-c">Set C — Reverso</div><div class="arch-cmp-nums">'+numsC+'</div><div class="arch-cmp-meta">Espejos de A+B</div></div>'
   +'<div class="arch-cmp-card arch-cmp-d"><div class="arch-cmp-title arch-cmp-title-d">🧞 Genie</div><div class="arch-cmp-nums">'+numsD+'</div><div class="arch-cmp-meta">Desplazados+iter.</div></div>';
  const totAll=[...setA,...setB,...setC,...setD].reduce((s,t)=>s+t.total,0);
  document.getElementById('arch-hdr').innerHTML=
    '<div>Budget/set: <strong style="color:#0F6E56">'+fC(budget)+'</strong></div>'
   +'<div>Perfil: <strong>'+profLbl+' ('+(rratio*100).toFixed(0)+'% Rev)</strong></div>'
   +'<div>20 tickets total: <strong style="color:#C86A00">'+fC(totAll)+'</strong></div>'
   +'<div>MC sims: <strong>'+nSim+'</strong></div>';
  document.getElementById('arche').style.display='none';
  document.getElementById('archc').style.display='block';
}
</script>
</body>
</html>

"""


# ─── MONITOR DASHBOARD HTML (Mission Control) ──────────────────────────────────
MONITOR_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JPS · Mission Control</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{
  --green:#0F6E56;--green-d:#0a5443;--bg:#0d1117;--card:#161b22;--card2:#1c2230;
  --bd:#283041;--txt:#e6edf3;--mut:#8b949e;--mut2:#6e7681;
  --pos:#2ea043;--posb:#3fb950;--warn:#d29922;--neg:#f85149;--blue:#2f81f7;--pur:#a371f7;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:radial-gradient(1400px 700px at 50% -15%,#11251c,#0d1117 60%);
  color:var(--txt);min-height:100vh;font-size:14px;line-height:1.45}
a{color:inherit;text-decoration:none}
.wrap{max-width:1480px;margin:0 auto;padding:18px 22px 60px}

/* Header */
header{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:16px;
  flex-wrap:wrap;padding:14px 22px;margin:-18px -22px 22px;
  background:rgba(13,17,23,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd)}
.brand{display:flex;align-items:center;gap:11px;margin-right:auto}
.brand .dot{width:12px;height:12px;border-radius:50%;background:var(--green);box-shadow:0 0 16px var(--green);animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.brand h1{font-size:16px;font-weight:800;letter-spacing:.3px}
.brand .sub{font-size:11px;color:var(--mut)}
.hpill{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;
  font-size:12px;font-weight:700;border:1px solid var(--bd);background:var(--card)}
.hpill .d{width:9px;height:9px;border-radius:50%}
.hdr-meta{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut)}
.hdr-meta b{color:var(--txt)}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:9px;
  border:1px solid var(--bd);background:var(--card);color:var(--txt);font-size:12.5px;
  font-weight:600;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--green);background:var(--card2)}
.btn.gh{background:linear-gradient(180deg,var(--green),var(--green-d));border-color:transparent}
.spin{animation:rot 1s linear infinite}@keyframes rot{to{transform:rotate(360deg)}}

/* Grid + cards */
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(6,1fr);margin-bottom:18px}
@media(max-width:1180px){.kpis{grid-template-columns:repeat(3,1fr)}}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px 17px;position:relative;overflow:hidden}
.kpi .lab{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);margin-bottom:7px}
.kpi .val{font-size:24px;font-weight:800;letter-spacing:.2px;line-height:1.1}
.kpi .hint{font-size:11px;color:var(--mut);margin-top:5px}
.kpi .edge{position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--green)}

section{margin-bottom:20px}
.sec-h{display:flex;align-items:center;gap:10px;margin:0 2px 11px}
.sec-h h2{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.8px}
.sec-h .tag{font-size:11px;color:var(--mut);font-weight:600}
.sec-h .ln{flex:1;height:1px;background:linear-gradient(90deg,var(--bd),transparent)}

.cols2{display:grid;grid-template-columns:1.25fr 1fr;gap:14px}
.cols2b{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:980px){.cols2,.cols2b{grid-template-columns:1fr}}

/* File status grid */
.files{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:11px}
.file{background:var(--card2);border:1px solid var(--bd);border-radius:11px;padding:12px 13px}
.file .top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.file .nm{font-size:12.5px;font-weight:700}
.file .meta{font-size:11px;color:var(--mut);line-height:1.5}
.file .meta b{color:var(--txt);font-weight:600}
.fdot{width:9px;height:9px;border-radius:50%;flex:none}
.miss{opacity:.5}

/* Tables */
.tbl-wrap{overflow:auto;border-radius:11px;border:1px solid var(--bd)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:8px 11px;text-align:left;white-space:nowrap}
thead th{background:var(--card2);color:var(--mut);font-size:10.5px;text-transform:uppercase;
  letter-spacing:.5px;font-weight:700;position:sticky;top:0;cursor:pointer;user-select:none}
tbody tr{border-top:1px solid var(--bd)}
tbody tr:hover{background:rgba(255,255,255,.025)}
td.num{font-variant-numeric:tabular-nums;text-align:right}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10.5px;font-weight:700;letter-spacing:.3px}
.b-pend{background:rgba(210,153,34,.16);color:#e3b341;border:1px solid rgba(210,153,34,.35)}
.b-rec{background:rgba(46,160,67,.16);color:var(--posb);border:1px solid rgba(46,160,67,.35)}
.b-com{background:rgba(47,129,247,.16);color:#6cb0ff;border:1px solid rgba(47,129,247,.35)}
.b-skip{background:rgba(139,148,158,.16);color:var(--mut);border:1px solid var(--bd)}
.b-hit{background:rgba(46,160,67,.22);color:var(--posb)}
.b-miss{background:rgba(248,81,73,.16);color:#ff7b72}
.pos{color:var(--posb)}.neg{color:#ff7b72}.warnc{color:var(--warn)}.muted{color:var(--mut)}
.win-row{box-shadow:inset 3px 0 0 var(--posb)}
.best-row{background:rgba(46,160,67,.07)}
.base-row td{color:var(--mut);font-style:italic}
.chip{font-size:10px;padding:1px 6px;border-radius:6px;background:var(--card2);border:1px solid var(--bd);color:var(--mut)}
.mono{font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace}

/* Gauge */
.gauge{margin-top:4px}
.gbar{position:relative;height:26px;border-radius:8px;background:var(--card2);border:1px solid var(--bd);overflow:hidden}
.gfill{position:absolute;left:0;top:0;bottom:0;border-radius:8px 0 0 8px;transition:width .5s}
.gmark{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--txt)}
.gmark::after{content:'33.3%';position:absolute;top:-15px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--mut);white-space:nowrap}
.glabels{display:flex;justify-content:space-between;font-size:10px;color:var(--mut);margin-top:4px}

.chart-box{position:relative;height:280px}
.chart-box.sm{height:230px}
.empty{display:flex;align-items:center;justify-content:center;height:100%;min-height:120px;
  color:var(--mut);font-size:12.5px;text-align:center;padding:20px}
.disc{margin-top:24px;padding:13px 16px;border:1px solid var(--bd);border-radius:11px;
  background:var(--card);font-size:11.5px;color:var(--mut);line-height:1.6}
.disc b{color:var(--warn)}
.toast{position:fixed;bottom:18px;right:18px;z-index:99;padding:11px 15px;border-radius:10px;
  background:var(--card);border:1px solid var(--neg);color:#ff9a9a;font-size:12.5px;
  box-shadow:0 10px 30px rgba(0,0,0,.4);opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.statline{font-size:11px;color:var(--mut2);margin-top:3px}
.gridstat{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
@media(max-width:640px){.gridstat{grid-template-columns:repeat(2,1fr)}}
.mini{background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:10px 12px}
.mini .l{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut)}
.mini .v{font-size:18px;font-weight:800;margin-top:3px}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="brand">
      <span class="dot"></span>
      <div>
        <h1>JPS TIEMPOS · MISSION CONTROL</h1>
        <div class="sub">Observabilidad del Architect system · datos locales</div>
      </div>
    </div>
    <span id="health" class="hpill"><span class="d" style="background:var(--mut)"></span><span id="healthtx">—</span></span>
    <div class="hdr-meta">
      <span>Datos: <b id="dataage">—</b></span>
      <span>Uptime: <b id="uptime">—</b></span>
      <span id="clock" class="mono">--:--:--</span>
    </div>
    <button class="btn" id="refresh"><span id="ricon">↻</span> <span id="rtext">Refrescar</span></button>
    <a class="btn" href="/" title="Dashboard principal">← Dashboard</a>
    <a class="btn" href="/logout" title="Cerrar sesión">Salir</a>
  </header>

  <!-- KPI strip -->
  <div class="grid kpis" id="kpis"></div>

  <!-- Pipeline status -->
  <section>
    <div class="sec-h"><h2>Pipeline</h2><span class="ln"></span><span class="tag" id="pipe-tag"></span></div>
    <div class="files" id="files"></div>
  </section>

  <!-- Predictions + P&L -->
  <section class="cols2">
    <div>
      <div class="sec-h"><h2>Predicciones</h2><span class="ln"></span><span class="tag" id="pred-tag"></span></div>
      <div class="tbl-wrap" style="max-height:380px"><table id="pred-tbl">
        <thead><tr><th>Fecha</th><th>Sesión</th><th>Estrategia</th><th>Estado</th><th>Exacto</th><th class="num">Neto</th></tr></thead>
        <tbody></tbody></table></div>
    </div>
    <div>
      <div class="sec-h"><h2>P&amp;L acumulado</h2><span class="ln"></span><span class="tag" id="pnl-tag"></span></div>
      <div class="card"><div class="chart-box" id="pnlbox"><canvas id="pnlChart"></canvas></div></div>
    </div>
  </section>

  <!-- Bandit + Frequency -->
  <section class="cols2b">
    <div>
      <div class="sec-h"><h2>Bandit · Thompson</h2><span class="ln"></span><span class="tag" id="bandit-tag"></span></div>
      <div class="card">
        <div class="chart-box sm" id="banditbox"><canvas id="banditChart"></canvas></div>
      </div>
    </div>
    <div>
      <div class="sec-h"><h2>Análisis de frecuencias</h2><span class="ln"></span><span class="tag" id="an-tag"></span></div>
      <div class="card">
        <div id="rev-gauge"></div>
        <div class="chart-box sm" id="freqbox" style="margin-top:14px"><canvas id="freqChart"></canvas></div>
      </div>
    </div>
  </section>

  <!-- Backtest leaderboard -->
  <section>
    <div class="sec-h"><h2>Backtest · Leaderboard</h2><span class="ln"></span>
      <button class="btn" id="bt-run" title="Corre el walk-forward 80/20 y regenera backtest_report.json">▶ Correr backtest</button>
      <span class="tag" id="bt-tag"></span></div>
    <div class="gridstat" id="bt-stats"></div>
    <div class="tbl-wrap"><table id="bt-tbl">
      <thead><tr>
        <th>#</th><th>Estrategia</th><th>Perfil</th><th class="num">Hit %</th><th class="num">ROI</th>
        <th class="num">Media/ses</th><th class="num">Mediana</th><th class="num">P95</th>
        <th class="num">Max DD</th><th class="num">z vs base</th><th class="num">p-val</th>
      </tr></thead><tbody></tbody></table></div>
  </section>

  <!-- Strategy P&L -->
  <section>
    <div class="sec-h"><h2>P&amp;L por estrategia (real)</h2><span class="ln"></span><span class="tag" id="sp-tag"></span></div>
    <div class="card"><div class="chart-box" id="spbox"><canvas id="spChart"></canvas></div></div>
  </section>

  <div class="disc">
    <b>Disclaimer:</b> Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio.
    No se garantiza ningún resultado. Las diferencias entre estrategias sobre muestras pequeñas están
    dominadas por ruido; el backtest valida calibración, no predice el futuro.
    <span id="lastupd" class="statline"></span>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API={pipeline:'/api/monitor/pipeline',bandit:'/api/monitor/bandit',predictions:'/api/monitor/predictions',analysis:'/api/monitor/analysis',backtest:'/api/monitor/backtest',pnl:'/api/monitor/pnl'};
const REFRESH_MS=30000;
let charts={};let countdown=REFRESH_MS/1000;let busy=false;

// ── helpers ───────────────────────────────────────────────────────────────────
const $=s=>document.querySelector(s);
const el=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;};
function fmtC(n){if(n==null||isNaN(n))return '—';const s=n<0?'-':(n>0?'+':'');return s+'₡'+Math.abs(Math.round(n)).toLocaleString('en-US');}
function fmtCp(n){if(n==null||isNaN(n))return '—';return '₡'+Math.round(n).toLocaleString('en-US');}
function pct(n,d=1){if(n==null||isNaN(n))return '—';return (n>0?'+':'')+Number(n).toFixed(d)+'%';}
function pctp(n,d=1){if(n==null||isNaN(n))return '—';return Number(n).toFixed(d)+'%';}
function ageStr(m){if(m==null)return 'n/d';if(m<1)return 'ahora';if(m<60)return Math.round(m)+'m';if(m<1440)return (m/60).toFixed(1)+'h';return (m/1440).toFixed(1)+'d';}
function signClass(n){return n>0?'pos':(n<0?'neg':'muted');}
const HCOL={green:'var(--pos)',yellow:'var(--warn)',red:'var(--neg)'};
function freshColor(m){if(m==null)return 'var(--mut2)';if(m<240)return 'var(--pos)';if(m<1440)return 'var(--warn)';return 'var(--neg)';}
async function fJSON(u){const r=await fetch(u,{headers:{Accept:'application/json'},cache:'no-store'});if(!r.ok)throw new Error(u.split('/').pop()+' HTTP '+r.status);return r.json();}
function toast(msg){const t=$('#toast');t.textContent='⚠ '+msg;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),4200);}
function destroy(k){if(charts[k]){charts[k].destroy();delete charts[k];}}
const HAS_CHART=typeof Chart!=='undefined';
if(HAS_CHART){Chart.defaults.color='#8b949e';Chart.defaults.borderColor='rgba(255,255,255,.06)';Chart.defaults.font.family="-apple-system,Segoe UI,sans-serif";Chart.defaults.font.size=11;Chart.defaults.animation.duration=500;}

// ── KPIs ──────────────────────────────────────────────────────────────────────
function renderKPIs(pipe,preds,pnl,bt,an){
  const k=$('#kpis');k.innerHTML='';
  const health=pipe?pipe.pipeline_health:null;
  const cards=[];
  cards.push({lab:'Pipeline',val:health?health.toUpperCase():'—',hint:pipe?('uptime '+ageStr(pipe.server_uptime_minutes)):'',col:health?HCOL[health]:'var(--mut)'});
  cards.push({lab:'Frescura datos',val:pipe?ageStr(pipe.data_age_minutes):'—',hint:pipe&&pipe.files.historical_accumulated.last_draw_date?('último '+pipe.files.historical_accumulated.last_draw_date):'',col:freshColor(pipe?pipe.data_age_minutes:null)});
  if(preds){const r=preds.reconciled,p=preds.pending+preds.committed;
    cards.push({lab:'Predicciones',val:preds.total,hint:p+' pend · '+r+' recon',col:'var(--blue)'});}
  else cards.push({lab:'Predicciones',val:'—',hint:'',col:'var(--mut)'});
  if(pnl){cards.push({lab:'P&L acumulado',val:fmtC(pnl.net),hint:pnl.n_sessions?(pct(pnl.roi_pct)+' ROI · '+pnl.n_sessions+' ses'):'sin sesiones',col:pnl.net>0?'var(--pos)':(pnl.net<0?'var(--neg)':'var(--mut)')});}
  else cards.push({lab:'P&L acumulado',val:'—',hint:'',col:'var(--mut)'});
  if(preds){cards.push({lab:'Hit rate (real)',val:preds.reconciled?pctp(preds.hit_rate_pct):'—',hint:preds.n_hits+' de '+preds.reconciled+' aciertos',col:'var(--pur)'});}
  else cards.push({lab:'Hit rate',val:'—',hint:'',col:'var(--mut)'});
  if(bt&&bt.exists){const best=bt.strategies[0];cards.push({lab:'Backtest líder',val:best?pct(best.roi_total_pct):'—',hint:best?best.name:'',col:best&&best.roi_total_pct>0?'var(--pos)':'var(--warn)'});}
  else cards.push({lab:'Backtest',val:'—',hint:'',col:'var(--mut)'});
  cards.forEach(c=>{const d=el('div','card kpi');d.innerHTML=`<div class="edge" style="background:${c.col}"></div><div class="lab">${c.lab}</div><div class="val" style="color:${c.col}">${c.val}</div><div class="hint">${c.hint||''}</div>`;k.appendChild(d);});
}

// ── Pipeline files ────────────────────────────────────────────────────────────
const FILE_LABELS={historical_accumulated:'Histórico acumulado',historical_data:'Histórico (fetch)',analysis_report:'Análisis estadístico',bandit_state:'Bandit state',predictions_log:'Predictions log',last_result:'Último sorteo',backtest_report:'Backtest report',output:'Monte Carlo output',audit_result:'Auditoría'};
function fileMeta(key,f){
  if(!f.exists)return '<span class="muted">no existe</span>';
  const a=`hace <b>${ageStr(f.age_minutes)}</b>`;let extra='';
  if(key==='historical_accumulated'||key==='historical_data')extra=`<b>${f.n_draws||0}</b> sorteos${f.last_draw_date?' · '+f.last_draw_date:''}`;
  else if(key==='analysis_report')extra=`<b>${f.n_valid||0}</b> válidos`;
  else if(key==='bandit_state')extra=`<b>${f.n_strategies||0}</b> estrategias · ${f.total_observations||0} obs`;
  else if(key==='predictions_log')extra=`<b>${f.n_ids||0}</b> preds · ${f.pending||0}P/${f.reconciled||0}R`;
  else if(key==='last_result')extra=f.exacto?`exacto <b>${f.exacto}</b>${f.reventada?' · REV':''} (${f.session||''})`:`${f.date||''}`;
  else if(key==='backtest_report')extra=`<b>${f.n_strategies||0}</b> estrategias`;
  else if(key==='audit_result'&&f.estado)extra=`<b>${f.estado}</b>${f.neto!=null?' · '+fmtC(f.neto):''}${f.draw_date?' · '+f.draw_date:''}`;
  return a+(extra?'<br>'+extra:'');
}
function renderPipeline(pipe){
  const box=$('#files');box.innerHTML='';
  if(!pipe){box.innerHTML='<div class="empty">No se pudo leer el pipeline</div>';return;}
  $('#pipe-tag').textContent='salud: '+pipe.pipeline_health+' · auto: '+(pipe.auto_status||'—');
  const order=['historical_accumulated','historical_data','analysis_report','last_result','predictions_log','bandit_state','backtest_report','output','audit_result'];
  order.forEach(key=>{const f=pipe.files[key];if(!f)return;
    const d=el('div','file'+(f.exists?'':' miss'));
    d.innerHTML=`<div class="top"><span class="fdot" style="background:${f.exists?freshColor(f.age_minutes):'var(--mut2)'}"></span><span class="nm">${FILE_LABELS[key]||key}</span></div><div class="meta">${fileMeta(key,f)}</div>`;
    box.appendChild(d);});
}

// ── Predictions table ─────────────────────────────────────────────────────────
const BADGE={pending:'b-pend',reconciled:'b-rec',committed:'b-com',skipped:'b-skip'};
function renderPredictions(p){
  const tb=$('#pred-tbl').querySelector('tbody');tb.innerHTML='';
  if(!p){tb.innerHTML='<tr><td colspan="6" class="empty">sin datos</td></tr>';return;}
  $('#pred-tag').textContent=`${p.total} preds · ${p.reconciled} reconciliadas · hit ${pctp(p.hit_rate_pct)}`;
  if(!p.recent.length){tb.innerHTML='<tr><td colspan="6"><div class="empty">No hay predicciones registradas todavía</div></td></tr>';return;}
  p.recent.forEach(r=>{
    const res=r.result;const win=res&&res.hit;
    const tr=el('tr',win?'win-row':'');
    const exa=res?`<span class="badge ${win?'b-hit':'b-miss'}">${res.exacto}${res.reventada?' R':''}</span>`:'<span class="muted">—</span>';
    const net=res?`<span class="${signClass(res.net)}">${fmtC(res.net)}</span>`:'<span class="chip">pend</span>';
    tr.innerHTML=`<td class="mono">${r.draw_date||'—'}</td><td>${r.session||'—'}</td><td>${r.strategy||'—'}</td><td><span class="badge ${BADGE[r.status]||'b-skip'}">${r.status||'—'}</span></td><td>${exa}</td><td class="num">${net}</td>`;
    tb.appendChild(tr);
  });
}

// ── P&L chart ─────────────────────────────────────────────────────────────────
function renderPnl(pnl){
  $('#pnl-tag').textContent=pnl?(fmtC(pnl.net)+' · '+pct(pnl.roi_pct)+' ROI'):'';
  destroy('pnl');const box=$('#pnlbox');
  if(!pnl||!pnl.series||!pnl.series.length){box.innerHTML='<div class="empty">Sin sesiones reconciliadas todavía.<br>El P&amp;L aparece cuando hay resultados cruzados.</div>';return;}
  box.innerHTML='<canvas id="pnlChart"></canvas>';if(!HAS_CHART){box.innerHTML='<div class="empty">Chart.js no disponible (offline)</div>';return;}
  const labels=pnl.series.map((s,i)=>(s.date||('#'+(i+1)))+(s.session?(' '+s.session[0].toUpperCase()):''));
  const data=pnl.series.map(s=>s.cumulative_net);
  const up=pnl.net>=0;
  charts.pnl=new Chart($('#pnlChart'),{type:'line',data:{labels,datasets:[{data,borderColor:up?'#3fb950':'#f85149',backgroundColor:up?'rgba(63,185,80,.12)':'rgba(248,81,73,.12)',fill:true,tension:.25,pointRadius:2,pointHoverRadius:5,borderWidth:2}]},
    options:{maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'Acum: '+fmtC(c.parsed.y),afterLabel:c=>{const s=pnl.series[c.dataIndex];return [s.strategy||'',(s.hit?'★ HIT ':'')+'sesión '+fmtC(s.net_session)];}}}},
    scales:{y:{ticks:{callback:v=>fmtCp(v)},grid:{color:'rgba(255,255,255,.05)'}},x:{ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:8}}}}});
}

// ── Bandit chart ──────────────────────────────────────────────────────────────
function renderBandit(b){
  destroy('bandit');const box=$('#banditbox');
  if(!b){box.innerHTML='<div class="empty">sin datos de bandit</div>';return;}
  $('#bandit-tag').textContent=`${b.total_strategies} brazos · ${b.total_observations} obs`;
  if(!b.has_data){box.innerHTML=`<div class="empty">Bandit inicializado (${b.total_strategies} estrategias) — sin outcomes todavía.<br>Posteriores en 0.50 hasta reconciliar predicciones.</div>`;return;}
  box.innerHTML='<canvas id="banditChart"></canvas>';if(!HAS_CHART){box.innerHTML='<div class="empty">Chart.js offline</div>';return;}
  const arms=b.arms.slice(0,12);
  charts.bandit=new Chart($('#banditChart'),{type:'bar',data:{labels:arms.map(a=>a.strategy),datasets:[{label:'Posterior',data:arms.map(a=>+(a.posterior_mean*100).toFixed(1)),backgroundColor:arms.map((a,i)=>i===0?'#3fb950':'rgba(47,129,247,.6)'),borderRadius:5}]},
    options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'media '+c.parsed.x+'%',afterLabel:c=>{const a=arms[c.dataIndex];return `obs ${a.n_observations} · hits ${a.n_hits} · CI[${(a.ci_low*100).toFixed(0)}-${(a.ci_high*100).toFixed(0)}]`;}}}},
    scales:{x:{min:0,suggestedMax:Math.max(60,...arms.map(a=>a.posterior_mean*100+5)),ticks:{callback:v=>v+'%'}},y:{ticks:{font:{size:10}}}}}});
}

// ── Frequency: rev gauge + top15 bar ──────────────────────────────────────────
function renderAnalysis(an){
  const g=$('#rev-gauge');
  destroy('freq');const box=$('#freqbox');
  if(!an||!an.exists){g.innerHTML='';box.innerHTML='<div class="empty">Sin análisis. Corré el pipeline.</div>';$('#an-tag').textContent='';return;}
  $('#an-tag').textContent=`${an.n_valid} sorteos · χ²=${an.chi2!=null?an.chi2:'?'}`;
  const obs=an.rev_rate_pct,exp=33.33;const w=Math.min(100,obs);const zc=Math.abs(an.rev_z)>2.576?'neg':(Math.abs(an.rev_z)>1.96?'warnc':'pos');
  g.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px"><span style="font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px">Reventada</span><span><b style="font-size:16px">${pctp(obs)}</b> <span class="${zc}">z=${an.rev_z}</span></span></div>
    <div class="gauge"><div class="gbar"><div class="gfill" style="width:${w}%;background:${an.rev_anomaly?'var(--neg)':'linear-gradient(90deg,var(--green),#3fb950)'}"></div><div class="gmark" style="left:${exp}%"></div></div>
    <div class="glabels"><span>0%</span><span>esperado 33.3%</span><span>100%</span></div></div>
    ${an.rev_anomaly?'<div class="statline neg">⚠ Desviación significativa (|z|&gt;2.58)</div>':'<div class="statline">Dentro de variabilidad normal</div>'}`;
  const top=(an.top15||[]).slice(0,15);
  if(!top.length){box.innerHTML='<div class="empty">sin top números</div>';return;}
  box.innerHTML='<canvas id="freqChart"></canvas>';if(!HAS_CHART){box.innerHTML='<div class="empty">Chart.js offline</div>';return;}
  charts.freq=new Chart($('#freqChart'),{type:'bar',data:{labels:top.map(t=>t.num),datasets:[{label:'Frecuencia',data:top.map(t=>t.freq),backgroundColor:top.map(t=>t.weight>=2?'#3fb950':(t.weight>=1.5?'#2f81f7':'rgba(139,148,158,.55)')),borderRadius:4}]},
    options:{maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'freq '+c.parsed.y,afterLabel:c=>{const t=top[c.dataIndex];return `peso ${t.weight} · rev ${t.si_pct}%`;}}}},
    scales:{y:{beginAtZero:true,ticks:{precision:0},grid:{color:'rgba(255,255,255,.05)'}},x:{ticks:{font:{size:10}}}}}});
}

// ── Backtest leaderboard ──────────────────────────────────────────────────────
let btSort={key:'roi_total_pct',dir:-1};let btCache=null;
function renderBacktest(bt){
  btCache=bt;
  const stats=$('#bt-stats');const tb=$('#bt-tbl').querySelector('tbody');stats.innerHTML='';tb.innerHTML='';
  if(!bt||!bt.exists){$('#bt-tag').textContent='';tb.innerHTML='<tr><td colspan="11"><div class="empty">Sin backtest_report.json</div></td></tr>';return;}
  const c=bt.config||{};$('#bt-tag').textContent=`${c.n_test||'?'} test · ₡${(c.budget||0).toLocaleString('en-US')} · ${c.n_tickets||'?'} tickets`;
  const minis=[['Mejor ROI',bt.best_strategy,'var(--pos)'],['Baseline (random)',pct(bt.baseline_roi),'var(--mut)'],['EV teórico/ses',fmtC(bt.ev_teorico),'var(--neg)'],['Estrategias',bt.strategies.length,'var(--blue)']];
  minis.forEach(m=>{const d=el('div','mini');d.innerHTML=`<div class="l">${m[0]}</div><div class="v" style="color:${m[2]}">${m[1]==null?'—':m[1]}</div>`;stats.appendChild(d);});
  const rows=bt.strategies.slice().sort((a,b)=>{const x=a[btSort.key],y=b[btSort.key];return ((x==null?-9e9:x)-(y==null?-9e9:y))*btSort.dir;});
  const base=bt.baseline_roi;
  rows.forEach((s,i)=>{
    const tr=el('tr',(s.name===bt.best_strategy?'best-row ':'')+(s.is_baseline?'base-row':''));
    const roiC=s.roi_total_pct>0?'pos':(s.roi_total_pct<0?'neg':'muted');
    const beat=base!=null&&!s.is_baseline?(s.roi_total_pct>base?' ▲':''):'';
    tr.innerHTML=`<td class="muted">${i+1}</td><td><b>${s.name}</b>${s.is_baseline?' <span class="chip">base</span>':''}</td><td class="muted">${s.profile||''}</td>
      <td class="num">${pctp(s.hit_rate_pct)}</td><td class="num ${roiC}">${pct(s.roi_total_pct)}${beat}</td>
      <td class="num ${signClass(s.mean_per_session)}">${fmtC(s.mean_per_session)}</td>
      <td class="num muted">${fmtCp(s.median)}</td><td class="num">${fmtCp(s.p95)}</td>
      <td class="num neg">${fmtCp(s.max_drawdown)}</td>
      <td class="num">${s.z_vs_baseline==null?'—':s.z_vs_baseline}</td>
      <td class="num ${s.p_value!=null&&s.p_value<0.05?'warnc':'muted'}">${s.p_value==null?'—':s.p_value}</td>`;
    tb.appendChild(tr);
  });
}
$('#bt-tbl').querySelectorAll('thead th').forEach((th,idx)=>{const keys=[null,'name','profile','hit_rate_pct','roi_total_pct','mean_per_session','median','p95','max_drawdown','z_vs_baseline','p_value'];const k=keys[idx];if(!k)return;th.addEventListener('click',()=>{btSort.dir=(btSort.key===k?-btSort.dir:-1);btSort.key=k;if(btCache)renderBacktest(btCache);});});

// ── Backtest run trigger (on-demand, background + polling) ─────────────────────
let btRunPoll=null;
async function runBacktest(){
  const btn=$('#bt-run');if(!btn||btn.disabled)return;
  const orig=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class="spin">↻</span> Corriendo…';
  const restore=()=>{btn.disabled=false;btn.innerHTML=orig;};
  try{
    const r=await fetch('/api/backtest/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
  }catch(e){toast('No se pudo iniciar el backtest: '+e.message);restore();return;}
  if(btRunPoll)clearInterval(btRunPoll);
  btRunPoll=setInterval(async()=>{
    let s;try{s=await fJSON('/api/backtest/status');}catch(e){return;}
    if(s.status==='running')return;
    clearInterval(btRunPoll);btRunPoll=null;restore();
    if(s.status==='done'){$('#bt-tag').textContent='✓ backtest regenerado';loadAll();}
    else toast('Backtest '+(s.status||'?')+': '+((s.log||[]).slice(-1)[0]||''));
  },2500);
}
{const b=$('#bt-run');if(b)b.addEventListener('click',runBacktest);}

// ── Strategy P&L bar ──────────────────────────────────────────────────────────
function renderStratPnl(pnl){
  destroy('sp');const box=$('#spbox');
  const bs=pnl&&pnl.by_strategy?Object.entries(pnl.by_strategy):[];
  $('#sp-tag').textContent=bs.length?`${bs.length} estrategias jugadas`:'';
  if(!bs.length){box.innerHTML='<div class="empty">Sin P&amp;L por estrategia todavía (no hay sesiones reconciliadas).</div>';return;}
  box.innerHTML='<canvas id="spChart"></canvas>';if(!HAS_CHART){box.innerHTML='<div class="empty">Chart.js offline</div>';return;}
  bs.sort((a,b)=>b[1].net-a[1].net);
  charts.sp=new Chart($('#spChart'),{type:'bar',data:{labels:bs.map(x=>x[0]),datasets:[{data:bs.map(x=>x[1].net),backgroundColor:bs.map(x=>x[1].net>=0?'rgba(63,185,80,.7)':'rgba(248,81,73,.7)'),borderRadius:5}]},
    options:{maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>fmtC(c.parsed.y),afterLabel:c=>{const d=bs[c.dataIndex][1];return `n=${d.n} · hits ${d.hits} · ROI ${pct(d.roi)}`;}}}},
    scales:{y:{ticks:{callback:v=>fmtCp(v)},grid:{color:'rgba(255,255,255,.05)'}},x:{ticks:{font:{size:10},maxRotation:35,minRotation:0}}}}});
}

// ── header health + clock ─────────────────────────────────────────────────────
function renderHeader(pipe){
  const h=$('#health'),tx=$('#healthtx');
  if(!pipe){h.querySelector('.d').style.background='var(--mut)';tx.textContent='sin conexión';return;}
  const c=HCOL[pipe.pipeline_health]||'var(--mut)';
  h.querySelector('.d').style.background=c;h.querySelector('.d').style.boxShadow='0 0 10px '+c;
  tx.textContent={green:'Saludable',yellow:'Atención',red:'Crítico'}[pipe.pipeline_health]||'—';
  $('#dataage').textContent=ageStr(pipe.data_age_minutes);
  $('#uptime').textContent=ageStr(pipe.server_uptime_minutes);
}
function tickClock(){const d=new Date();$('#clock').textContent=d.toLocaleTimeString('es-CR',{hour12:false});}
setInterval(tickClock,1000);tickClock();

// ── main load ─────────────────────────────────────────────────────────────────
async function loadAll(){
  if(busy)return;busy=true;
  $('#ricon').classList.add('spin');$('#rtext').textContent='…';
  const res=await Promise.allSettled([fJSON(API.pipeline),fJSON(API.bandit),fJSON(API.predictions),fJSON(API.analysis),fJSON(API.backtest),fJSON(API.pnl)]);
  const [pipe,bandit,preds,an,bt,pnl]=res.map(r=>r.status==='fulfilled'?r.value:null);
  const failed=res.filter(r=>r.status==='rejected');
  if(failed.length)toast(failed.length+' endpoint(s) fallaron: '+failed.map(f=>f.reason.message).join(', '));
  try{renderHeader(pipe);}catch(e){}
  try{renderKPIs(pipe,preds,pnl,bt,an);}catch(e){console.error(e);}
  try{renderPipeline(pipe);}catch(e){console.error(e);}
  try{renderPredictions(preds);}catch(e){console.error(e);}
  try{renderPnl(pnl);}catch(e){console.error(e);}
  try{renderBandit(bandit);}catch(e){console.error(e);}
  try{renderAnalysis(an);}catch(e){console.error(e);}
  try{renderBacktest(bt);}catch(e){console.error(e);}
  try{renderStratPnl(pnl);}catch(e){console.error(e);}
  $('#lastupd').textContent=' · Actualizado '+new Date().toLocaleTimeString('es-CR',{hour12:false});
  $('#ricon').classList.remove('spin');$('#rtext').textContent='Refrescar';
  countdown=REFRESH_MS/1000;busy=false;
}
$('#refresh').addEventListener('click',loadAll);
setInterval(()=>{countdown--;if(countdown<=0){loadAll();}$('#rtext').textContent=busy?'…':('Refrescar ('+Math.max(0,countdown)+'s)');},1000);
loadAll();
</script>
</body>
</html>
"""


# ─── AUTO SCHEDULER ────────────────────────────────────────────────────────────
# Schedule diario: predict adaptive 50min antes de cada sorteo, fetch+reconcile
# 30-60min después. Horarios JPS oficiales: 12:55 / 16:30 / 19:30.
SCHEDULE = [
    # (hour, minute, kind, args)
    (12,  5, "predict",         ["--session", "manana",     "--strategy", "adaptive", "--force"]),
    (13, 30, "fetch_reconcile", None),
    (15, 40, "predict",         ["--session", "mediaTarde", "--strategy", "adaptive", "--force"]),
    (17,  0, "fetch_reconcile", None),
    (18, 40, "predict",         ["--session", "tarde",      "--strategy", "adaptive", "--force"]),
    (20,  0, "fetch_reconcile", None),
]
_last_run_per_slot = {}  # {(h, m, kind): date} → evita ejecutar 2 veces el mismo slot el mismo día


def _sched_log(msg):
    """Log con timestamp para debugging del scheduler."""
    print(f"[SCHED {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _run_predict_sched(args):
    cmd = [sys.executable, os.path.join(HERE, "jps_predict.py")] + args
    _sched_log(f"→ predict: {' '.join(args)}")
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, env=_CHILD_ENV)
        if r.returncode == 0:
            _sched_log(f"✓ predict ok")
        else:
            # Capturar AMBOS stdout y stderr — el predict imprime errores
            # a stdout también (ej. "ya existe predicción pending para X")
            err_combined = (r.stderr or "") + "\n" + (r.stdout or "")
            _sched_log(f"✗ predict failed (rc={r.returncode})")
            for line in err_combined.strip().split("\n")[-10:]:
                if line.strip():
                    _sched_log(f"    {line}")
    except subprocess.TimeoutExpired:
        _sched_log("✗ predict timeout 120s")
    except Exception as e:
        _sched_log(f"✗ predict error: {e}")


def _run_fetch_reconcile_sched():
    # CRÍTICO: usar --days 180 porque fetch SOBREESCRIBE historical_data.json
    # (no es acumulativo). Si usamos --days 7, perdemos 173 días de histórico
    # cada vez. Eso rompe estrategias que necesitan mucha data (weekday_specific
    # necesita ≥30 sorteos por weekday = ≥210 sorteos total).
    _sched_log("→ fetch --mode history --days 180 + reconcile")
    try:
        r1 = subprocess.run(
            [sys.executable, os.path.join(HERE, "jps_edge_tool.py"), "fetch", "--mode", "history", "--days", "180"],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_CHILD_ENV,
        )
        if r1.returncode != 0:
            _sched_log(f"✗ fetch failed: {r1.stderr[-200:]}")
            return
        # Acumular el histórico recién bajado en historical_accumulated.json —
        # mismo paso que el auto-pipeline, para que el volumen quede al día
        # aunque nadie abra el dashboard entre sorteos.
        acc = accumulate_history()
        if "error" in acc:
            _sched_log(f"⚠ accumulate: {acc['error']}")
        else:
            _sched_log(f"✓ accumulate: {acc['total']} días (+{acc['added']} nuevos)")
        r2 = subprocess.run(
            [sys.executable, os.path.join(HERE, "jps_reconcile.py")],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_CHILD_ENV,
        )
        if r2.returncode == 0:
            # extract última línea útil del stdout
            lines = [l for l in r2.stdout.split("\n") if l.strip()]
            tail = lines[-3:] if len(lines) >= 3 else lines
            _sched_log("✓ reconcile ok")
            for l in tail:
                _sched_log(f"    {l}")
            # Reconcile de apuestas por usuario (separado del bandit del sistema).
            try:
                from jps_reconcile_user import reconcile_users
                n_user = reconcile_users()
                _sched_log(f"✓ user-bets reconciliadas: {n_user}")
            except Exception as e:
                _sched_log(f"⚠ user-reconcile error: {e}")
        else:
            _sched_log(f"✗ reconcile failed: {r2.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        _sched_log("✗ fetch+reconcile timeout")
    except Exception as e:
        _sched_log(f"✗ fetch+reconcile error: {e}")


def _scheduler_loop():
    import time as _time
    _sched_log(f"Scheduler iniciado · {len(SCHEDULE)} slots/día")
    for h, m, kind, _args in SCHEDULE:
        _sched_log(f"  {h:02d}:{m:02d}  →  {kind}")
    while True:
        try:
            now = datetime.now()
            today = now.date()
            for h, m, kind, args in SCHEDULE:
                if now.hour == h and now.minute == m:
                    key = (h, m, kind)
                    if _last_run_per_slot.get(key) == today:
                        continue
                    _last_run_per_slot[key] = today
                    if kind == "predict":
                        _run_predict_sched(args)
                    elif kind == "fetch_reconcile":
                        _run_fetch_reconcile_sched()
        except Exception as e:
            _sched_log(f"loop error: {e}")
        _time.sleep(45)  # check ~cada 45s (no nos saltamos un minuto)


def start_scheduler():
    import threading
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") not in ("utf8", "utf16"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Bind a 0.0.0.0 por defecto para que el healthcheck del contenedor (Railway)
    # y el tráfico externo lleguen. Local: igual sirve en http://localhost:PORT.
    # Override con JPS_HOST=127.0.0.1 si se quiere restringir a loopback.
    host = os.environ.get("JPS_HOST", "0.0.0.0")
    # ThreadingHTTPServer: el monitor dispara 6 fetches en paralelo cada 30s y el
    # console corre el pipeline (fetch bloqueante al API JPS). Con el server
    # single-thread esas requests se serializaban y el dashboard se quedaba
    # colgado mientras el pipeline corría. Threading = cada request en su hilo.
    server = ThreadingHTTPServer((host, PORT), Handler)
    start_scheduler()
    print(f"""
+--------------------------------------------------------------+
|       JPS TIEMPOS LAB -- Dashboard activo  v1.0             |
+--------------------------------------------------------------+
|  URL : http://localhost:{PORT:<36}|
|  Stop: Ctrl+C                                                |
+--------------------------------------------------------------+
""")
    def _open():
        import time
        time.sleep(0.8)
        import webbrowser
        webbrowser.open(f"http://localhost:{PORT}")
    import threading
    threading.Thread(target=_open, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServidor detenido.")


if __name__ == "__main__":
    main()
