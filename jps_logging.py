#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPS TIEMPOS LAB — Logging centralizado de observabilidad (Workstream B)

Un logger simple y a prueba de fallos: cada acción del sistema deja una línea
JSONL en `logs/YYYY-MM-DD.jsonl` (bajo `JPS_DATA_DIR`, igual que el resto de los
datos). Todos los módulos importan `log_event(...)` y lo llaman en sus puntos
clave, además de sus `print` actuales.

Formato de línea:
    {"ts": "<ISO-UTC>", "component": "<str>", "event": "<str>", "detail": {...}}

Componentes convencionales: fetch, accumulate, analyze, predict, commit, skip,
reconcile, bandit, backtest, user_bet, server.

`log_event` NUNCA lanza: la observabilidad no debe tumbar al que la llama.
"""

import json
import os
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
LOG_DIR = os.path.join(DATA_DIR, "logs")

_LOCK = threading.Lock()


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path(date_str):
    return os.path.join(LOG_DIR, f"{date_str}.jsonl")


def log_event(component, event, detail=None):
    """Escribe una línea de evento en el log del día. Tolerante a fallos."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "component": str(component),
            "event": str(event),
            "detail": detail if isinstance(detail, dict) else ({"value": detail} if detail is not None else {}),
        }
        with _LOCK:
            with open(_log_path(_today_str()), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Nunca propagar: si el log falla, la operación de negocio sigue.
        pass


def read_day(date_str):
    """Devuelve la lista de eventos (dicts) del día indicado, o [] si no hay log."""
    p = _log_path(date_str)
    if not os.path.exists(p):
        return []
    events = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return events
    return events


def summarize_day(date_str=None):
    """Resumen del día a partir del log: qué componentes corrieron, cuántos
    eventos, y desgloses útiles (predicciones, commits/skips por usuario,
    reconciles, apuestas de usuario, y ROI/net si el detalle lo trae).
    Reconstruye 'qué pasó ese día' sin leer código."""
    date_str = date_str or _today_str()
    events = read_day(date_str)

    by_component = {}
    for e in events:
        c = e.get("component", "?")
        by_component[c] = by_component.get(c, 0) + 1

    def _pick(component):
        return [e for e in events if e.get("component") == component]

    def _net_of(e):
        d = e.get("detail", {}) or {}
        for k in ("net", "neto", "neto_total", "total_neto"):
            if isinstance(d.get(k), (int, float)):
                return d[k]
        return 0

    reconciles = _pick("reconcile")
    user_bets = _pick("user_bet")
    roi_net = sum(_net_of(e) for e in reconciles)

    def _slim(e):
        return {"ts": e.get("ts"), "event": e.get("event"), "detail": e.get("detail", {})}

    return {
        "date": date_str,
        "n_events": len(events),
        "by_component": by_component,
        "components_active": sorted(by_component.keys()),
        "fetches": [_slim(e) for e in _pick("fetch")],
        "predictions": [_slim(e) for e in _pick("predict")],
        "commits": [_slim(e) for e in _pick("commit")],
        "skips": [_slim(e) for e in _pick("skip")],
        "reconciles": [_slim(e) for e in reconciles],
        "user_bets": [_slim(e) for e in user_bets],
        "backtests": [_slim(e) for e in _pick("backtest")],
        "errors": [_slim(e) for e in events if e.get("event", "").lower().startswith("error")
                   or (isinstance(e.get("detail"), dict) and e["detail"].get("error"))],
        "net_reconciled_total": roi_net,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="JPS Tiempos Lab — daily-report (resumen del log diario)")
    ap.add_argument("--date", default=None, help="Fecha YYYY-MM-DD (default: hoy UTC)")
    ap.add_argument("--json", action="store_true", help="Imprime el resumen crudo en JSON")
    args = ap.parse_args()
    summ = summarize_day(args.date)
    if args.json:
        print(json.dumps(summ, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n═══ DAILY REPORT — {summ['date']} ═══")
        print(f"  Eventos totales : {summ['n_events']}")
        print(f"  Componentes     : {', '.join(summ['components_active']) or '(ninguno)'}")
        for c, n in sorted(summ["by_component"].items()):
            print(f"    {c:<12} {n}")
        print(f"  Predicciones    : {len(summ['predictions'])}")
        print(f"  Commits/Skips   : {len(summ['commits'])}/{len(summ['skips'])}")
        print(f"  Reconciles      : {len(summ['reconciles'])}  (net acumulado ₡{summ['net_reconciled_total']:+,})")
        print(f"  Apuestas usuario: {len(summ['user_bets'])}")
        print(f"  Errores         : {len(summ['errors'])}")
        print("\n  Disclaimer: Todos los números tienen la misma probabilidad. No se garantiza ningún resultado.\n")
