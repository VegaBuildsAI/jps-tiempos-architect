#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JPS TIEMPOS LAB — RECONCILE DE APUESTAS POR USUARIO (Workstream D)

Cruza las apuestas manuales de cada usuario (tabla `user_bets` en SQLite) contra
los resultados reales (`historical_data.json`) y produce una auditoría por usuario
(tabla `user_audits`, mismo esquema que `audit_result.json`).

CLAVE: es TOTALMENTE SEPARADO del reconcile del sistema (`jps_reconcile.py`). NO
toca `predictions_log.jsonl` ni el bandit — las elecciones manuales del usuario no
deben contaminar el aprendizaje de las estrategias automáticas.

Reusa `payout_ticket()` de jps_edge_tool y el índice de resultados de
`jps_reconcile._build_results_index()` (misma fuente de verdad que el sistema).

USO:
  python3 jps_reconcile_user.py            # reconcilia todas las apuestas pending
  python3 jps_reconcile_user.py --dry-run  # muestra sin escribir
"""

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jps_db as db
from jps_edge_tool import payout_ticket
from jps_reconcile import _build_results_index


def _safe_log(component, event, detail=None):
    try:
        from jps_logging import log_event
        log_event(component, event, detail)
    except Exception:
        pass


def _audit_for_bet(bet: dict, result: dict) -> dict:
    """Construye el audit (esquema audit_result.json) para una apuesta de usuario."""
    drawn_exacto = int(result.get("numero"))
    drawn_rev = "SI" if int(result.get("in_reventado", 0) or 0) == 1 else "NO"

    rows = []
    total_cost = total_recuperado = total_neto = 0
    any_hit = False
    for i, t in enumerate(bet.get("tickets") or [], 1):
        try:
            num = int(t["num"]); base = int(t["base"]); rev = int(t["rev"])
        except (KeyError, ValueError, TypeError):
            continue
        out = payout_ticket(num, base, rev, drawn_exacto, drawn_rev)
        total_cost += out["cost"]; total_recuperado += out["recuperado"]; total_neto += out["neto"]
        if out["hit_exacto"]:
            any_hit = True
        rows.append({
            "ticket": i, "num_exacto": str(num).zfill(2), "base": base, "rev": rev,
            "ticket_total": out["cost"], "hit_exacto": out["hit_exacto"],
            "recuperado": out["recuperado"], "neto": out["neto"],
            "roi": round(out["roi"], 4),
        })

    roi_total = total_neto / total_cost if total_cost else 0
    estado = "WIN ✓" if total_neto > 0 else ("EMPATE" if total_neto == 0 else "LOSS ✗")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "user_reconcile",
        "username": bet.get("username"),
        "draw_date": bet.get("draw_date"),
        "session": bet.get("session"),
        "resultado_exacto": str(drawn_exacto).zfill(2),
        "resultado_reventada": drawn_rev,
        "resultado_mega": result.get("meganNumero"),
        "any_hit": any_hit,
        "tickets": rows,
        "resumen": {
            "total_apostado": total_cost,
            "total_recuperado": total_recuperado,
            "neto_total": total_neto,
            "roi_total": round(roi_total, 4),
            "estado": estado,
        },
    }


def reconcile_users(dry_run: bool = False) -> int:
    """Reconcilia todas las apuestas pending contra resultados reales. Devuelve el
    número de apuestas reconciliadas."""
    pending = db.pending_user_bets()
    if not pending:
        print("  No hay apuestas de usuario pending.")
        return 0
    idx = _build_results_index()
    if not idx:
        print("  ⚠ historical_data.json sin resultados — corre el fetch primero.")
        return 0

    n = 0
    for bet in pending:
        key = f"{bet.get('draw_date')}-{bet.get('session')}"
        result = idx.get(key)
        if not result or result.get("numero") is None:
            continue
        audit = _audit_for_bet(bet, result)
        rs = audit["resumen"]
        if not dry_run:
            db.upsert_user_audit(bet["username"], bet["draw_date"], bet["session"],
                                 audit, bet_id=bet["id"])
            db.mark_bet_reconciled(bet["id"])
        n += 1
        hit = " ★ HIT" if audit["any_hit"] else ""
        print(f"  ✓ {bet['username']} {bet['draw_date']} {bet['session']}: "
              f"{rs['estado']} neto ₡{rs['neto_total']:+,}{hit}")
        _safe_log("user_bet", "reconciled", {
            "user": bet["username"], "draw_date": bet["draw_date"], "session": bet["session"],
            "estado": rs["estado"], "neto": rs["neto_total"], "hit": audit["any_hit"],
        })

    print(f"\n  {'(dry-run) ' if dry_run else ''}Apuestas reconciliadas: {n}")
    return n


def main():
    ap = argparse.ArgumentParser(description="JPS Tiempos Lab — reconcile de apuestas por usuario")
    ap.add_argument("--dry-run", action="store_true", help="No escribe; solo muestra")
    args = ap.parse_args()
    print("\n═══ RECONCILE — APUESTAS POR USUARIO ═══")
    reconcile_users(dry_run=args.dry_run)
    print("\n  Disclaimer: Todos los números tienen la misma probabilidad. No se garantiza ningún resultado.\n")


if __name__ == "__main__":
    main()
