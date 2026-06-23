#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          JPS TIEMPOS LAB — RECONCILE                             ║
║  Cruza predicciones pending (de jps_predict.py) contra los       ║
║  resultados reales del histórico y registra el outcome.          ║
╚══════════════════════════════════════════════════════════════════╝

USO:
  python3 jps_reconcile.py                    # cruza todas las pending
  python3 jps_reconcile.py --since 2026-05-01 # solo desde esa fecha
  python3 jps_reconcile.py --dry-run          # muestra sin escribir

Diseño append-only:
  Las líneas pending nunca se modifican. La reconciliación añade una nueva línea
  con el mismo id y status="reconciled". Una nota convencional: la "última línea
  por id" es la fuente de verdad. Eso permite reconstruir la historia completa
  y recuperarse de errores sin perder data.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jps_edge_tool import (
    _extract_draws,
    payout_ticket,
    load_json,
    path as repo_path,
)

LOG_FILE = "predictions_log.jsonl"


def _load_log() -> List[dict]:
    p = repo_path(LOG_FILE)
    if not os.path.exists(p):
        return []
    records = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  ⚠ Línea inválida en {LOG_FILE}: {e}")
    return records


def _build_results_index() -> Dict[str, dict]:
    """Construye index {YYYY-MM-DD-session: {numero, in_reventado, ...}} desde historical_data.json.

    Acepta también historical_accumulated.json si existe (mismo contenido en otro formato).
    """
    try:
        data = load_json("historical_data.json")
    except FileNotFoundError:
        return {}
    draws = _extract_draws(data)
    index = {}
    for d in draws:
        dia_raw = d.get("dia", "")
        # Normalizar a YYYY-MM-DD
        dia = dia_raw.split("T")[0] if dia_raw else ""
        sess = d.get("session", "")
        if not dia or not sess:
            continue
        if d.get("numero") is None:
            continue
        index[f"{dia}-{sess}"] = d
    return index


def _latest_status_by_id(records: List[dict]) -> Dict[str, dict]:
    """Devuelve, para cada id, un dict MERGED con todas las líneas del id.

    Como el JSONL es append-only, una predicción puede tener varias líneas:
    pending → committed (con bet_per_ticket) → reconciled (con result).
    El merge preserva campos como `tickets` del registro original mientras
    sobrescribe `status`, `actual_bet_per_ticket`, `result`, etc. desde las
    actualizaciones posteriores.
    """
    latest: Dict[str, dict] = {}
    for rec in records:
        rid = rec.get("id")
        if not rid:
            continue
        if rid not in latest:
            latest[rid] = dict(rec)
        else:
            # Merge: nuevos campos sobrescriben los viejos, pero los viejos
            # que no aparezcan en rec se mantienen (preservamos tickets, etc.)
            merged = dict(latest[rid])
            merged.update(rec)
            latest[rid] = merged
    return latest


def _reconcile_one(prediction: dict, result: dict) -> dict:
    """Calcula el outcome de una predicción contra el resultado real."""
    try:
        drawn_exacto = int(result.get("numero"))
    except (TypeError, ValueError):
        return {"error": "resultado inválido (numero no parseable)"}
    try:
        drawn_rev_int = int(result.get("in_reventado", 0))
    except (TypeError, ValueError):
        drawn_rev_int = 0
    drawn_rev = "SI" if drawn_rev_int == 1 else "NO"

    total_cost = 0
    total_neto = 0
    total_recuperado = 0
    any_hit = False
    ticket_details = []
    # Si el usuario confirmó una apuesta diferente (vía dashboard /api/commit),
    # usamos `actual_bet_per_ticket` en lugar del `base` original. Eso refleja
    # el dinero REAL apostado, no el budget hipotético del predict.
    actual_bet = prediction.get("actual_bet_per_ticket")
    for t in prediction.get("tickets", []):
        try:
            num = int(t["num"])
            base = int(t["base"])
            rev = int(t["rev"])
        except (KeyError, ValueError, TypeError):
            continue
        if actual_bet is not None and actual_bet > 0:
            # Override con la inversión real del usuario, rev=0 (exacto-only)
            base = int(actual_bet)
            rev = 0
        out = payout_ticket(num, base, rev, drawn_exacto, drawn_rev)
        total_cost += out["cost"]
        total_neto += out["neto"]
        total_recuperado += out["recuperado"]
        if out["hit_exacto"]:
            any_hit = True
        ticket_details.append({
            "num": str(num).zfill(2),
            "base": base,
            "rev": rev,
            "hit": out["hit_exacto"],
            "recuperado": out["recuperado"],
            "neto": out["neto"],
        })

    roi = total_neto / total_cost if total_cost else 0
    return {
        "drawn_exacto": str(drawn_exacto).zfill(2),
        "drawn_reventada": drawn_rev,
        "drawn_mega": result.get("meganNumero"),
        "any_hit": any_hit,
        "total_cost": total_cost,
        "total_recuperado": total_recuperado,
        "total_neto": total_neto,
        "roi": round(roi, 4),
        "tickets_detail": ticket_details,
    }


def main():
    parser = argparse.ArgumentParser(
        description="JPS Tiempos Lab — reconcilia predicciones pending contra resultados reales",
    )
    parser.add_argument("--since", default=None, help="Filtra predicciones con draw_date >= YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="No escribe nada, solo muestra")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   JPS TIEMPOS LAB — RECONCILE                        ║")
    print("╚══════════════════════════════════════════════════════╝")

    records = _load_log()
    if not records:
        print(f"\n  No hay registros en {LOG_FILE}.")
        print(f"  Corre primero: python3 jps_predict.py --session <session>")
        return

    latest = _latest_status_by_id(records)
    # Reconciliar tanto predicciones pending (registro automático sin apostar)
    # como committed (el usuario confirmó una apuesta real con monto). Ambas
    # tienen tickets[] y necesitan saber si acertaron contra el ganador.
    pending = [r for r in latest.values() if r.get("status") in ("pending", "committed")]
    if args.since:
        pending = [r for r in pending if r.get("draw_date", "") >= args.since]

    if not pending:
        print(f"\n  No hay predicciones pending para reconciliar.")
        # Mostrar resumen acumulado igual
    else:
        print(f"\n  Predicciones pending : {len(pending)}")
        if args.since:
            print(f"  Filtro --since       : {args.since}")

    results_idx = _build_results_index()
    if not results_idx and pending:
        print(f"\n  ⚠ historical_data.json no tiene resultados. Corre:")
        print(f"    python3 jps_edge_tool.py fetch --mode history --days 30")

    newly_reconciled = []
    still_pending = []

    for pred in pending:
        pid = pred.get("id", "?")
        key = f"{pred.get('draw_date')}-{pred.get('session')}"
        if key in results_idx:
            outcome = _reconcile_one(pred, results_idx[key])
            if "error" in outcome:
                print(f"  ⚠ {pid}: {outcome['error']}")
                still_pending.append(pred)
                continue
            new_record = {
                "id": pid,
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
                "supersedes_status": pred.get("status", "pending"),
                "draw_date": pred.get("draw_date"),
                "session": pred.get("session"),
                "strategy": pred.get("strategy"),
                "profile": pred.get("profile"),
                "user_committed": pred.get("status") == "committed",
                "actual_bet_per_ticket": pred.get("actual_bet_per_ticket"),
                "status": "reconciled",
                "result": outcome,
            }
            newly_reconciled.append(new_record)
            hit_mark = " ★ HIT" if outcome["any_hit"] else ""
            print(f"  ✓ {pid}: drawn={outcome['drawn_exacto']} rev={outcome['drawn_reventada']} → ROI={outcome['roi']*100:+.2f}%{hit_mark}")
        else:
            still_pending.append(pred)
            print(f"  ⏳ {pid}: aún no hay resultado en historical_data.json")

    # Escribir las nuevas líneas reconciled (append-only)
    if newly_reconciled and not args.dry_run:
        p = repo_path(LOG_FILE)
        with open(p, "a", encoding="utf-8") as f:
            for rec in newly_reconciled:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"\n  ✓ Apendeadas {len(newly_reconciled)} líneas a {LOG_FILE}")

        # ── Actualizar bandit con outcomes recién reconciliados (Phase 9)
        try:
            from jps_bandit import BanditState
            bandit = BanditState.load()
            for rec in newly_reconciled:
                strategy = rec.get("strategy")
                if not strategy:
                    continue
                hit = rec.get("result", {}).get("any_hit", False)
                bandit.update(strategy, bool(hit),
                              dia=rec.get("draw_date", ""),
                              session=rec.get("session", ""))
            bandit.save()
            print(f"  ✓ Bandit actualizado con {len(newly_reconciled)} outcomes")
        except Exception as e:
            print(f"  ⚠ No se pudo actualizar el bandit: {e}")
    elif args.dry_run and newly_reconciled:
        print(f"\n  --dry-run: se habrían apendeado {len(newly_reconciled)} líneas.")

    # ── Resumen acumulado del track record (todas las reconciled, no solo las nuevas)
    all_records = _load_log() + newly_reconciled if not args.dry_run else _load_log()
    latest_all = _latest_status_by_id(all_records)
    reconciled_all = [r for r in latest_all.values() if r.get("status") == "reconciled"]

    if reconciled_all:
        n = len(reconciled_all)
        total_cost = sum(r.get("result", {}).get("total_cost", 0) for r in reconciled_all)
        total_neto = sum(r.get("result", {}).get("total_neto", 0) for r in reconciled_all)
        hits = sum(1 for r in reconciled_all if r.get("result", {}).get("any_hit"))
        roi = total_neto / total_cost if total_cost else 0

        # Por estrategia
        by_strat: Dict[str, dict] = {}
        for r in reconciled_all:
            s = r.get("strategy", "?")
            by_strat.setdefault(s, {"n": 0, "cost": 0, "neto": 0, "hits": 0})
            res = r.get("result", {})
            by_strat[s]["n"] += 1
            by_strat[s]["cost"] += res.get("total_cost", 0)
            by_strat[s]["neto"] += res.get("total_neto", 0)
            if res.get("any_hit"):
                by_strat[s]["hits"] += 1

        print(f"\n  ┌─ TRACK RECORD ACUMULADO ────────────────────────┐")
        print(f"  │ Sesiones reconciled  : {n:>6}                   │")
        print(f"  │ Hits totales         : {hits:>6} ({hits/n*100:5.2f}%)         │")
        print(f"  │ Total apostado       : ₡{total_cost:>9,}              │")
        print(f"  │ Neto acumulado       : ₡{total_neto:>+9,}              │")
        print(f"  │ ROI acumulado        : {roi*100:>+9.2f}%               │")
        print(f"  └──────────────────────────────────────────────────┘")

        print(f"\n  Por estrategia:")
        for s, info in sorted(by_strat.items(), key=lambda x: x[1]["neto"], reverse=True):
            r = info["neto"] / info["cost"] if info["cost"] else 0
            print(f"    {s:<28} n={info['n']:>3}  hits={info['hits']:>3} ({info['hits']/info['n']*100:5.2f}%)  ROI={r*100:+7.2f}%")

    print(f"\n  Aún pending: {len(still_pending)}")
    print()


if __name__ == "__main__":
    main()
