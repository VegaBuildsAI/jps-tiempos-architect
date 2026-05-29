#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          JPS TIEMPOS LAB — PREDICT (pre-sorteo)                  ║
║  Genera tickets para un sorteo futuro y los registra en          ║
║  predictions_log.jsonl (append-only) para reconciliación posterior. ║
╚══════════════════════════════════════════════════════════════════╝

USO:
  python3 jps_predict.py --session manana
  python3 jps_predict.py --session mediaTarde --budget 10000 --n 5
  python3 jps_predict.py --session tarde --strategy set_a

Workflow esperado:
  1. python3 jps_edge_tool.py fetch --mode history --days 60   (idealmente reciente)
  2. python3 jps_predict.py --session <next>                   (1h antes del sorteo)
  3. (ocurre el sorteo)
  4. python3 jps_edge_tool.py fetch --mode last                (trae el resultado)
  5. python3 jps_reconcile.py                                  (cruza pending con real)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jps_edge_tool import (
    _extract_draws,
    _build_tickets,
    _profile_to_ratio,
    cmd_analyze,
    load_json,
    path as repo_path,
)
from jps_backtest import (
    select_architect,
    select_set_a,
    select_set_b,
    select_set_c,
    select_set_d,
    select_freq_only,
    select_top_mega,
    select_ensemble,
    select_exacto_mega_agree,
)

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = "predictions_log.jsonl"
VALID_SESSIONS = ("manana", "mediaTarde", "tarde")
# Horarios oficiales JPS: 12:55pm, 4:30pm, 7:30pm. SESSION_HOUR es el cutoff
# después del cual la sesión "ya pasó" para efectos de calcular el próximo sorteo.
SESSION_HOUR = {"manana": 13, "mediaTarde": 17, "tarde": 20}

# Selectores simples (consumen el report ya calculado)
STRATEGY_SELECTORS = {
    "architect_balanced":      select_architect,
    "architect_conservative":  select_architect,
    "architect_aggressive":    select_architect,
    "architect_exacto_only":   select_architect,
    "set_a":                   select_set_a,
    "set_b_freq_elite":        select_set_b,
    "set_c_reverso_edge":      select_set_c,
    "set_d_genie":             select_set_d,
    "freq_only":               select_freq_only,
    "top_mega_only":           select_top_mega,
    "multi_strategy_ensemble": select_ensemble,
    "exacto_mega_agree":       select_exacto_mega_agree,
}

# Estrategias que filtran el corpus antes de analizar (manejadas con special-case)
DATA_FILTER_STRATEGIES = {
    "weekday_specific",      # solo sorteos del mismo día de la semana
    "weekday_recent30",      # ★ CAMPEÓN: weekday + últimos 30 del mismo weekday (decay + clustering)
    "session_specific",      # solo sorteos de la misma sesión
    "decay_recent",          # últimos 100 sorteos
    "inverse_recent",        # números que NO han salido en últimos 30
    "signal_only_play",      # apuesta solo si top-1 z>2.5
    "concentrated_top1",     # 1 ticket con todo el budget al top-1
}

ALL_STRATEGIES = list(STRATEGY_SELECTORS.keys()) + list(DATA_FILTER_STRATEGIES) + ["adaptive"]

STRATEGY_PROFILE_OVERRIDE = {
    "architect_conservative":  "conservative",
    "architect_aggressive":    "aggressive",
    "architect_exacto_only":   "exacto_only",
    "concentrated_top1":       "concentrated",
    # Estrategias nuevas usan exacto_only por defecto (mejor EV per colón)
    "weekday_specific":        "exacto_only",
    "weekday_recent30":        "exacto_only",
    "session_specific":        "exacto_only",
    "decay_recent":            "exacto_only",
    "inverse_recent":          "exacto_only",
    "signal_only_play":        "exacto_only",
    "multi_strategy_ensemble": "exacto_only",
    "exacto_mega_agree":       "exacto_only",
}


def _next_draw_date(session: str, now: Optional[datetime] = None) -> str:
    """Devuelve YYYY-MM-DD del próximo sorteo de esa sesión.

    Si la hora ya pasó hoy, devuelve mañana; si no, devuelve hoy.
    """
    now = now or datetime.now()
    hour_cutoff = SESSION_HOUR.get(session, 12)
    target = now.replace(hour=hour_cutoff, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return target.strftime("%Y-%m-%d")


def _load_existing_ids() -> set:
    """Lee predictions_log.jsonl y devuelve set de ids ya registrados."""
    p = repo_path(LOG_FILE)
    if not os.path.exists(p):
        return set()
    ids = set()
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("status") == "pending":
                    ids.add(rec.get("id"))
            except json.JSONDecodeError:
                pass
    return ids


def _append_jsonl(record: dict):
    p = repo_path(LOG_FILE)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="JPS Tiempos Lab — genera predicción pre-sorteo y la registra en predictions_log.jsonl",
    )
    parser.add_argument("--session", required=True, choices=VALID_SESSIONS,
                        help="Sesión del sorteo: manana | mediaTarde | tarde")
    parser.add_argument("--budget", type=int, default=5000, help="Budget total (₡)")
    parser.add_argument("--n", type=int, default=5, help="Número de tickets")
    parser.add_argument("--strategy", default="architect_exacto_only",
                        choices=ALL_STRATEGIES,
                        help="Estrategia de selección de números")
    parser.add_argument("--profile", default=None,
                        choices=["conservative", "balanced", "aggressive", "exacto_only", "concentrated"],
                        help="Override del perfil de riesgo (default: derivado de la estrategia)")
    parser.add_argument("--force", action="store_true",
                        help="Permite registrar otra predicción para la misma sesión-fecha si ya existe una pending")
    args = parser.parse_args()

    # Strategy "adaptive" → delega al bandit
    effective_strategy = args.strategy
    bandit_pick = None
    if args.strategy == "adaptive":
        try:
            from jps_bandit import BanditState
            state = BanditState.load()
            effective_strategy = state.pick()
            state.save()
            bandit_pick = effective_strategy
            print(f"\n  🧠 Bandit (Thompson sampling) eligió: {effective_strategy}")
        except Exception as e:
            print(f"\n  ⚠ Bandit no disponible ({e}). Fallback a architect_exacto_only.")
            effective_strategy = "architect_exacto_only"

    # Determinar profile efectivo
    profile = args.profile or STRATEGY_PROFILE_OVERRIDE.get(effective_strategy, "exacto_only")
    rev_ratio = _profile_to_ratio(profile) if profile in ("conservative", "balanced", "aggressive") else 0.0

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   JPS TIEMPOS LAB — PREDICT (pre-sorteo)             ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  Sesión        : {args.session}")
    strat_display = effective_strategy if args.strategy == "adaptive" else effective_strategy
    suffix = "  (vía bandit adaptive)" if bandit_pick else ""
    print(f"  Estrategia    : {strat_display}{suffix}  (profile={profile}, rev_ratio={rev_ratio:.0%})")
    print(f"  Budget        : ₡{args.budget:,}  ·  Tickets: {args.n}")

    # Cargar histórico y verificar frescura
    try:
        data = load_json("historical_data.json")
    except FileNotFoundError:
        print("\n  ERROR: historical_data.json no existe.")
        print("  Corre primero: python3 jps_edge_tool.py fetch --mode history --days 60")
        sys.exit(1)

    draws = _extract_draws(data)
    if not draws:
        print("\n  ERROR: historical_data.json no tiene sorteos válidos.")
        sys.exit(1)
    print(f"  Histórico     : {len(draws)} sorteos en historical_data.json")

    # Generar fecha del próximo sorteo
    draw_date = _next_draw_date(args.session)
    pred_id = f"{draw_date}-{args.session}"

    # Verificar duplicados
    existing = _load_existing_ids()
    if pred_id in existing and not args.force:
        print(f"\n  ERROR: ya existe una predicción pending para {pred_id}.")
        print(f"  Usa --force si querés registrar otra, o corre jps_reconcile.py.")
        sys.exit(1)

    # Análisis (lo silenciamos para no spammar)
    import contextlib
    import io

    def _silent_analyze(draws_subset):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            class _A: pass
            return cmd_analyze(_A(), _draws=draws_subset, _no_save=True, _mc_iterations=1)

    report = _silent_analyze(draws)
    if not report:
        print("\n  ERROR: análisis no produjo reporte.")
        sys.exit(1)

    # ── Seleccionar números (varias rutas según estrategia)
    skipped = False
    if effective_strategy in STRATEGY_SELECTORS:
        selector = STRATEGY_SELECTORS[effective_strategy]
        try:
            numbers = selector(report, args.n)
        except Exception as e:
            print(f"\n  ERROR: estrategia {effective_strategy} falló: {e}")
            sys.exit(1)

    elif effective_strategy in ("weekday_specific", "weekday_recent30"):
        target_date = datetime.fromisoformat(f"{draw_date}T00:00:00")
        target_wd = target_date.weekday()
        wd_draws = []
        for d in draws:
            try:
                d_date = datetime.fromisoformat(d.get("dia", "").split("T")[0])
                if d_date.weekday() == target_wd:
                    wd_draws.append(d)
            except (ValueError, TypeError):
                pass
        if len(wd_draws) < 30:
            print(f"\n  ERROR: solo {len(wd_draws)} sorteos para weekday {target_wd}. Mínimo 30.")
            sys.exit(1)
        if effective_strategy == "weekday_recent30":
            wd_draws = wd_draws[-30:]
            print(f"  Sub-corpus    : últimos 30 sorteos de {target_date.strftime('%A')} (campeón multi-window)")
        else:
            print(f"  Sub-corpus    : {len(wd_draws)} sorteos del mismo weekday ({target_date.strftime('%A')})")
        report_wd = _silent_analyze(wd_draws)
        numbers = select_architect(report_wd, args.n)

    elif effective_strategy == "session_specific":
        sess_draws = [d for d in draws if d.get("session") == args.session]
        print(f"  Sub-corpus    : {len(sess_draws)} sorteos de la misma sesión ({args.session})")
        if len(sess_draws) < 30:
            print(f"\n  ERROR: solo {len(sess_draws)} sorteos para sesión {args.session}. Mínimo 30.")
            sys.exit(1)
        report_sess = _silent_analyze(sess_draws)
        numbers = select_architect(report_sess, args.n)

    elif effective_strategy == "decay_recent":
        recent = draws[-100:] if len(draws) > 100 else draws
        print(f"  Sub-corpus    : últimos {len(recent)} sorteos (step-decay)")
        report_recent = _silent_analyze(recent)
        numbers = select_architect(report_recent, args.n)

    elif effective_strategy == "inverse_recent":
        recent_subset = draws[-30:] if len(draws) > 30 else draws
        seen = set()
        for d in recent_subset:
            try:
                seen.add(int(d.get("numero")))
            except (TypeError, ValueError):
                pass
        missing = [nn for nn in range(100) if nn not in seen]
        combined = report.get("combined_weights") or report.get("weights", {})
        missing.sort(key=lambda nn: -combined.get(str(nn).zfill(2), 0))
        numbers = missing[:args.n]
        print(f"  Sub-corpus    : {len(missing)} números NO vistos en últimos 30")

    elif effective_strategy == "signal_only_play":
        ranked = report.get("ranked_numbers", [])
        if not ranked:
            print("\n  ERROR: report no tiene ranked_numbers.")
            sys.exit(1)
        top1_z = abs(ranked[0].get("z_score", 0))
        print(f"  Top-1 z-score : {top1_z:.2f}  (umbral: 2.5)")
        if top1_z <= 2.5:
            print(f"\n  ⚠ SEÑAL DÉBIL — la estrategia signal_only_play recomienda NO APOSTAR esta sesión.")
            print(f"  No se registra predicción.\n")
            sys.exit(0)
        numbers = select_architect(report, args.n)

    elif effective_strategy == "concentrated_top1":
        combined = report.get("combined_weights") or report.get("weights", {})
        if not combined:
            print("\n  ERROR: report sin weights.")
            sys.exit(1)
        top1 = max(combined.items(), key=lambda x: x[1])[0]
        numbers = [int(top1)]

    else:
        print(f"\n  ERROR: estrategia {effective_strategy} no soportada todavía en predict.")
        sys.exit(1)

    # Validación (concentrated_top1 solo requiere 1; resto requieren args.n)
    min_needed = 1 if effective_strategy == "concentrated_top1" else args.n
    if not numbers or len(numbers) < min_needed:
        print(f"\n  ERROR: estrategia devolvió {len(numbers) if numbers else 0} números (esperados {min_needed}).")
        sys.exit(1)

    # ── Construir tickets según profile
    try:
        if profile == "concentrated":
            ticket_amount = (args.budget // 100) * 100
            tickets, remanente = _build_tickets(
                numbers, args.budget, base_fixed=ticket_amount, rev_fixed=0,
            )
        elif profile == "exacto_only":
            ticket_amount = (args.budget // args.n // 100) * 100
            tickets, remanente = _build_tickets(
                numbers, args.budget, base_fixed=ticket_amount, rev_fixed=0,
            )
        else:
            tickets, remanente = _build_tickets(numbers, args.budget, rev_ratio=rev_ratio)
    except ValueError as e:
        print(f"\n  ERROR al construir tickets: {e}")
        sys.exit(1)

    print(f"\n  Sorteo objetivo: {draw_date} sesión {args.session}")
    print(f"  Predicción ID  : {pred_id}")
    print(f"\n  ┌─ TICKETS ──────────────────────────────────────────────┐")
    print(f"  │ {'#':<4} {'Núm':<6} {'Base':>8} {'Rev':>8} {'Total':>8}    │")
    print(f"  │ {'─'*4} {'─'*6} {'─'*8} {'─'*8} {'─'*8}    │")
    for i, t in enumerate(tickets, 1):
        print(f"  │ {i:<4} {str(t.num_exacto).zfill(2):<6} ₡{t.base:>7,} ₡{t.rev:>7,} ₡{t.base+t.rev:>7,}    │")
    print(f"  └────────────────────────────────────────────────────────┘")
    print(f"  Remanente: ₡{remanente:,}")

    # Top-10 weights snapshot (para auditoría: si en el futuro queremos revisar
    # qué pesos había en ese momento, no tenemos que recomputar)
    combined = report.get("combined_weights") or report.get("weights", {})
    top10_weights = dict(sorted(combined.items(), key=lambda x: x[1], reverse=True)[:10])

    record = {
        "id": pred_id,
        "predicted_at": datetime.now(timezone.utc).isoformat(),
        "draw_date": draw_date,
        "session": args.session,
        "strategy": effective_strategy,
        "strategy_requested": args.strategy,
        "bandit_pick": bandit_pick,
        "profile": profile,
        "budget": args.budget,
        "n_tickets": args.n,
        "tickets": [
            {"num": str(t.num_exacto).zfill(2), "base": t.base, "rev": t.rev}
            for t in tickets
        ],
        "weights_top10": top10_weights,
        "n_draws_used_for_analysis": len(draws),
        "status": "pending",
        "result": None,
    }
    _append_jsonl(record)
    print(f"\n  ✓ Registrado en {LOG_FILE}")
    print(f"  → Después del sorteo, corre: python3 jps_reconcile.py")
    print()


if __name__ == "__main__":
    main()
