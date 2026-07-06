#!/usr/bin/env python3
"""
JPS Tiempos Lab — Acumulador diario de datos históricos
Corre diariamente para bajar los últimos 90 días y fusionar con el
archivo acumulado, deduplicando por fecha de día.
Guarda en: historical_accumulated.json
"""
import json, os, urllib.request, urllib.error
from datetime import datetime, timedelta

HERE     = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR: dónde viven los datos persistentes. En Railway = /data (volume).
# Antes estas rutas colgaban de HERE, así que en el contenedor el acumulado se
# escribía en /app (efímero) y nunca aterrizaba en el volumen — por eso el
# monitor lo marcaba "no existe". Ahora respeta JPS_DATA_DIR igual que
# jps_edge_tool y jps_server, para que el archivo persista.
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)
ACCUM    = os.path.join(DATA_DIR, "historical_accumulated.json")
JPS_BASE = "https://integration.jps.go.cr"
LOG_FILE = os.path.join(DATA_DIR, "accumulate_log.txt")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def jps_get(endpoint):
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def load_accumulated():
    if not os.path.exists(ACCUM):
        return {}
    with open(ACCUM, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Devuelve dict keyed por fecha "YYYY-MM-DD"
    return data

def save_accumulated(records: dict):
    with open(ACCUM, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2, default=str)

def date_key(dia_str: str) -> str:
    """Normaliza fecha a YYYY-MM-DD"""
    try:
        return dia_str[:10]
    except Exception:
        return dia_str

def merge_records(accumulated: dict, new_records: list) -> tuple:
    """Fusiona `new_records` (día-registros del API con slots manana/mediaTarde/
    tarde) dentro de `accumulated` (dict keyed por fecha YYYY-MM-DD), in-place.

    Deduplica por fecha: agrega días nuevos, rellena slots que aún no existían
    (ej. 'tarde' que cerró después) y aplica correcciones del API sobre slots ya
    guardados. Devuelve (added, corrected). Se comparte con el auto-pipeline del
    server para no duplicar la lógica de merge en dos lugares."""
    added = corrected = 0
    for rec in new_records:
        if not isinstance(rec, dict):
            continue
        key = date_key(rec.get("dia", ""))
        if not key:
            continue
        if key not in accumulated:
            accumulated[key] = rec
            added += 1
        else:
            # Agregar slots vacíos O aplicar correcciones del API
            existing = accumulated[key]
            for slot in ("manana", "mediaTarde", "tarde"):
                new_slot = rec.get(slot)
                if new_slot and isinstance(new_slot, dict) and new_slot.get("numero") is not None:
                    if not existing.get(slot):
                        existing[slot] = new_slot          # slot nuevo (ej: tarde ya cerró)
                    elif existing[slot] != new_slot:
                        existing[slot] = new_slot          # corrección del API
                        corrected += 1
    return added, corrected

def _parse_records(raw) -> list:
    """Normaliza la respuesta del API a lista de día-registros."""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for k in ("data", "results", "sorteos", "items", "historico"):
            v = raw.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        if raw.get("dia") or any(k in raw for k in ("manana", "mediaTarde", "tarde")):
            return [raw]
    return []


def _latest_date(accumulated: dict):
    dates = sorted(k for k in accumulated.keys() if isinstance(k, str) and k[:4].isdigit())
    return dates[-1] if dates else None


def _needs_bootstrap(accumulated: dict, gap_days: int = 3) -> bool:
    """True si el acumulado está vacío o su último día está a más de `gap_days`
    de hoy (hueco grande) → conviene un fetch COMPLETO en vez de solo 'last'."""
    if not accumulated:
        return True
    last = _latest_date(accumulated)
    if not last:
        return True
    try:
        gap = (datetime.now().date() - datetime.strptime(last, "%Y-%m-%d").date()).days
        return gap > gap_days
    except Exception:
        return True


def _safe_log(component, event, detail=None):
    try:
        from jps_logging import log_event
        log_event(component, event, detail)
    except Exception:
        pass


def main(force_full: bool = False):
    log("=== JPS Acumulador arrancando ===")

    # 1. Cargar acumulado existente
    accumulated = load_accumulated()
    before = len(accumulated)
    log(f"  Registros acumulados existentes: {before} días")

    # 2. Decidir cadencia: bootstrap COMPLETO (vacío / hueco grande) vs diario (solo 'last')
    bootstrap = force_full or _needs_bootstrap(accumulated)
    end = datetime.now()
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        if bootstrap:
            # El API rechaza rangos muy amplios (devuelve vacío) → bajar por ventanas.
            log("  Modo BOOTSTRAP (histórico completo, por ventanas)")
            from jps_edge_tool import _fetch_history_chunked
            new_records = _fetch_history_chunked()
        else:
            log("  Modo DIARIO (solo último resultado)")
            new_records = _parse_records(jps_get("/api/App/nuevostiempos/last"))
    except Exception as e:
        log(f"  ERROR al llamar API: {e}")
        _safe_log("accumulate", "error", {"bootstrap": bootstrap, "error": str(e)})
        return
    log(f"  Registros recibidos del API: {len(new_records)}")

    # 3. Fusionar — deduplicar por fecha (lógica compartida con el server)
    added, corrected = merge_records(accumulated, new_records)
    after = len(accumulated)
    log(f"  Días nuevos agregados: {added} | Correcciones aplicadas: {corrected} | Total acumulado: {after} días")

    # 4. Guardar acumulado + regenerar historical_data.json (working dataset)
    save_accumulated(accumulated)
    log(f"  ✓ Guardado en historical_accumulated.json")
    all_records = [accumulated[k] for k in sorted(accumulated.keys())]
    with open(os.path.join(DATA_DIR, "historical_data.json"), "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2, default=str)
    log(f"  ✓ historical_data.json actualizado ({len(all_records)} días)")

    _safe_log("accumulate", "bootstrap" if bootstrap else "daily",
              {"days_before": before, "days_added": added, "slots_corrected": corrected,
               "days_total": after, "last_date": _latest_date(accumulated)})
    log("=== Acumulación completa ===\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="JPS Tiempos Lab — acumulador (bootstrap completo o diario)")
    ap.add_argument("--full", action="store_true", help="Forzar fetch del histórico COMPLETO")
    args = ap.parse_args()
    main(force_full=args.full)
