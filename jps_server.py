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

import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─── CONFIG ────────────────────────────────────────────────────────────────────
PORT     = int(sys.argv[1]) if len(sys.argv) > 1 else 7788
JPS_BASE = "https://integration.jps.go.cr"
HERE     = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR: dónde viven los datos persistentes. En Railway será /data (volume).
# Default a HERE para correr local sin cambios.
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# In-memory state (persists while server runs)
STATE = {
    "draws": [], "top25": [], "last": None, "output": None,
    "auto_status": "idle",   # idle | running | done | error
    "auto_log":    [],
    "auto_result": None,
}

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
        capture_output=True, text=True, cwd=HERE, timeout=90
    )
    if result.returncode != 0:
        raise RuntimeError("simulador.py salió con error:\n" + result.stderr[-800:])
    return result.stdout.strip()


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
    }


# ─── HTTP SERVER ───────────────────────────────────────────────────────────────
CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

# Basic Auth opcional via env vars. Si BOTH están seteadas, se requiere auth.
# Si AMBAS están vacías, el server queda abierto (modo local).
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "").strip()
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "").strip()
BASIC_AUTH_ENABLED = bool(BASIC_AUTH_USER and BASIC_AUTH_PASS)


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
        return user == BASIC_AUTH_USER and pw == BASIC_AUTH_PASS
    except Exception:
        return False


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
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
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
                self.send_json({"ok": True, "record": record})

            else:
                self.send_json({"error": "Not found"}, 404)

        except Exception as e:
            import traceback
            self.send_json({"error": str(e), "trace": traceback.format_exc()[-1500:]}, 500)

    def _require_auth(self) -> bool:
        """Si auth está enabled y falla, manda 401 y devuelve False. True si OK."""
        if _check_basic_auth(self.headers):
            return True
        body = b'{"error": "authentication required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="JPS Tiempos Lab"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        # Permitir healthcheck SIN auth (necesario para Railway healthchecks)
        parsed_pre = urllib.parse.urlparse(self.path)
        if parsed_pre.path != "/api/status" and not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        try:
            if parsed.path == "/":
                # Nuevo dashboard editorial (Preset 2 Coreintel). El antiguo
                # console_v2.html sigue en el repo para uso analítico avanzado
                # pero el server sirve por default el nuevo.
                dash_path = os.path.join(HERE, "dashboard.html")
                if os.path.exists(dash_path):
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

            elif parsed.path == "/api/auto":
                # Browser polls this to track auto-pipeline progress
                self.send_json({
                    "status": STATE["auto_status"],
                    "log":    STATE["auto_log"],
                    "result": STATE["auto_result"],
                })

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
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=120)
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
            cwd=HERE, capture_output=True, text=True, timeout=120,
        )
        if r1.returncode != 0:
            _sched_log(f"✗ fetch failed: {r1.stderr[-200:]}")
            return
        r2 = subprocess.run(
            [sys.executable, os.path.join(HERE, "jps_reconcile.py")],
            cwd=HERE, capture_output=True, text=True, timeout=120,
        )
        if r2.returncode == 0:
            # extract última línea útil del stdout
            lines = [l for l in r2.stdout.split("\n") if l.strip()]
            tail = lines[-3:] if len(lines) >= 3 else lines
            _sched_log("✓ reconcile ok")
            for l in tail:
                _sched_log(f"    {l}")
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
    server = HTTPServer(("127.0.0.1", PORT), Handler)
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
