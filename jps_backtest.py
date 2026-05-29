#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          JPS TIEMPOS LAB — BACKTESTER (walk-forward 80/20)       ║
║  Valida la calidad estadística de las estrategias de selección   ║
║  contra el histórico real, sin data leakage.                     ║
╚══════════════════════════════════════════════════════════════════╝

USO:
  python3 jps_backtest.py                          # config default
  python3 jps_backtest.py --budget 10000 --n 5
  python3 jps_backtest.py --train-pct 0.70 --seed 7

Genera:
  backtest_report.json    — métricas crudas por estrategia
  backtest_summary.md     — reporte legible con veredicto y caveats

Caveat fundamental:
  Una lotería honesta tiene EV negativo fijo. Ninguna estrategia puede cambiarlo
  en expectativa. Este backtest sirve para validar calibración del código,
  medir varianza, detectar posibles anomalías del RNG y construir disciplina —
  NO para predecir el futuro.
"""

import argparse
import contextlib
import io
import math
import os
import random
import sys
from datetime import datetime
from typing import List, Dict, Optional

# Importar funciones del módulo principal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jps_edge_tool import (
    _extract_draws,
    _build_tickets,
    _profile_to_ratio,
    cmd_analyze,
    payout_ticket,
    load_json,
    save_json,
    EXACTO_MULT,
    REV_MULT,
    P_EXACTO,
    P_REV,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_ORDER = {"manana": 1, "mediaTarde": 2, "tarde": 3}


def _parse_dia(s: str) -> str:
    if not s:
        return ""
    return s.split("T")[0]


def _flatten_chrono(data) -> List[dict]:
    """Aplana el histórico y lo ordena cronológicamente (mañana → mediaTarde → tarde)."""
    draws = _extract_draws(data)
    return sorted(
        draws,
        key=lambda d: (_parse_dia(d.get("dia", "")), SESSION_ORDER.get(d.get("session", ""), 9)),
    )


def _silent_analyze(draws: List[dict]) -> Optional[dict]:
    """Llama cmd_analyze con todas las salidas suprimidas y MC interno mínimo."""
    class _A:
        pass
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report = cmd_analyze(_A(), _draws=draws, _no_save=True, _mc_iterations=1)
    return report


# ─────────────────────────────────────────────
# STRATEGY SELECTORS
# ─────────────────────────────────────────────

def _mirror(n: int) -> int:
    """Espejo de dígitos: 73 → 37, 5 → 50, 0 → 0."""
    return (n % 10) * 10 + (n // 10)


def _is_palindrome(n: int) -> bool:
    return (n % 10) == (n // 10)


def select_architect(report: dict, n: int = 5) -> List[int]:
    """Top-N por combined_weights (exacto 75% + mega 25%)."""
    weights = report.get("combined_weights") or report.get("weights", {})
    ranked = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    return [int(k) for k, _ in ranked[:n]]


def select_freq_only(report: dict, n: int = 5) -> List[int]:
    """Top-N por frecuencia cruda observada (sin bayesian smoothing)."""
    ranked = report.get("ranked_numbers", [])
    return [int(r["numero"]) for r in ranked[:n]]


def select_cold(report: dict, n: int = 5) -> List[int]:
    """Bottom-N por frecuencia (anti-strategy)."""
    ranked = report.get("ranked_numbers", [])
    return [int(r["numero"]) for r in ranked[-n:]]


def select_top_mega(report: dict, n: int = 5) -> List[int]:
    """Top-N por mega weight (ignora exacto)."""
    weights = report.get("mega_weights", {})
    ranked = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    return [int(k) for k, _ in ranked[:n]]


def _architect_sets(report: dict, n_per_set: int = 5) -> Dict[str, List[int]]:
    """Port a Python de The Architect Sets A–D (TECH_SPEC.md §10).

    Diferencias justificadas vs el JS original:
    - Set A usa ranking por combined_weights en lugar de MC ranking. El MC ranking
      del JS añade ruido aleatorio (sample finito) sin alterar el orden medio —
      determinístico es más correcto para backtesting reproducible.
    - Set B usa combined_weights desc como tiebreaker en lugar de si_pct, porque
      analysis_report no expone si_pct por número.
    """
    combined = report.get("combined_weights") or report.get("weights", {})
    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    ranked_nums = [int(k) for k, _ in ranked]

    set_a = ranked_nums[:n_per_set]
    set_b = [x for x in ranked_nums if x not in set_a][:n_per_set]

    # Set C: espejo de top2 de A + top2 de B
    sources = set_a[:2] + set_b[:2]
    set_c: List[int] = []
    overflow_to_d: List[int] = []
    for src in sources:
        if _is_palindrome(src):
            continue
        m = _mirror(src)
        if m in set_a or m in set_b:
            overflow_to_d.append(m)
        elif m not in set_c:
            set_c.append(m)
    # Completar set_c con más espejos de A+B si hace falta
    if len(set_c) < n_per_set:
        for src in set_a + set_b:
            if len(set_c) >= n_per_set:
                break
            if _is_palindrome(src):
                continue
            m = _mirror(src)
            if m not in set_a and m not in set_b and m not in set_c:
                set_c.append(m)
    set_c = set_c[:n_per_set]

    # Set D: pool prioritizado, primeros N no incluidos en A∪B∪C
    used = set(set_a) | set(set_b) | set(set_c)
    pool: List[int] = list(overflow_to_d)
    # Outliers (z > 1.5)
    for r in report.get("ranked_numbers", []):
        if abs(r.get("z_score", 0)) > 1.5:
            n_o = int(r["numero"])
            if n_o not in used and n_o not in pool:
                pool.append(n_o)
    # Resto del top por combined_weight
    for n_r in ranked_nums:
        if n_r not in used and n_r not in pool:
            pool.append(n_r)

    set_d = [x for x in pool if x not in used][:n_per_set]
    return {"A": set_a, "B": set_b, "C": set_c, "D": set_d}


def select_set_a(report, n=5): return _architect_sets(report, n)["A"]
def select_set_b(report, n=5): return _architect_sets(report, n)["B"]
def select_set_c(report, n=5): return _architect_sets(report, n)["C"]
def select_set_d(report, n=5): return _architect_sets(report, n)["D"]


def select_ensemble(report: dict, n: int = 5) -> List[int]:
    """Ensemble por votos: candidatos de Architect top-10 + Sets A/B/C/D.

    Cada número en cualquiera de esas listas suma un voto. Top-N por votos,
    tiebreaker por combined_weight. Hipótesis: consenso entre estrategias
    refleja una señal más robusta que cualquier individual.
    """
    candidates: List[int] = []
    candidates.extend(select_architect(report, 10))
    candidates.extend(select_set_a(report, 5))
    candidates.extend(select_set_b(report, 5))
    candidates.extend(select_set_c(report, 5))
    candidates.extend(select_set_d(report, 5))
    combined = report.get("combined_weights") or report.get("weights", {})

    votes: Dict[int, int] = {}
    for num in candidates:
        votes[num] = votes.get(num, 0) + 1
    if not votes:
        return []
    ranked = sorted(
        votes.items(),
        key=lambda x: (-x[1], -combined.get(str(x[0]).zfill(2), 0)),
    )
    return [num for num, _ in ranked[:n]]


def select_exacto_mega_agree(report: dict, n: int = 5) -> List[int]:
    """Números que están en top-15 de exacto Y en top-15 de mega simultáneamente.

    Hipótesis: dos señales independientes coincidiendo en un número ofrecen
    más evidencia que una sola. Si la lista de intersección tiene <n, completa
    con top exacto.
    """
    weights_e = report.get("weights", {})
    weights_m = report.get("mega_weights", {})
    if not weights_e or not weights_m:
        return []
    top_e = [k for k, _ in sorted(weights_e.items(), key=lambda x: -x[1])[:15]]
    top_m = set(k for k, _ in sorted(weights_m.items(), key=lambda x: -x[1])[:15])
    intersection = [k for k in top_e if k in top_m]
    combined = report.get("combined_weights", weights_e)
    intersection.sort(key=lambda k: -combined.get(k, 0))
    # Pad con top exacto si faltan
    if len(intersection) < n:
        for k in top_e:
            if k not in intersection:
                intersection.append(k)
                if len(intersection) >= n:
                    break
    return [int(k) for k in intersection[:n]]


# Definición: (name, selector, profile). Algunas estrategias son "especiales" — su
# nombre es la marca para que el loop principal aplique lógica distinta (random_uniform,
# concentrated_top1, decay_recent, session_specific, signal_only_play).
# Profile "exacto_only" → rev=0 (apuesta Exacto puro, sin Reventados). Matemáticamente
# óptima bajo pago 90× porque elimina la exposición a la apuesta Rev (que tiene EV peor).
# Profile "concentrated" → 1 solo ticket con todo el budget al top-1 number.
STRATEGIES = [
    ("architect_exacto_only",  select_architect,            "exacto_only"),
    ("architect_balanced",     select_architect,            "balanced"),
    ("architect_conservative", select_architect,            "conservative"),
    ("architect_aggressive",   select_architect,            "aggressive"),
    ("set_a",                  select_set_a,                "balanced"),
    ("set_b_freq_elite",       select_set_b,                "balanced"),
    ("set_c_reverso_edge",     select_set_c,                "balanced"),
    ("set_d_genie",            select_set_d,                "balanced"),
    ("freq_only",              select_freq_only,            "balanced"),
    ("cold_numbers",           select_cold,                 "balanced"),
    ("top_mega_only",          select_top_mega,             "balanced"),
    # ── Tier 1
    ("exacto_mega_agree",      select_exacto_mega_agree,    "exacto_only"),
    ("concentrated_top1",      None,                        "concentrated"),
    ("decay_recent",           None,                        "exacto_only"),
    ("session_specific",       None,                        "exacto_only"),
    ("signal_only_play",       None,                        "exacto_only"),
    # ── Tier 2
    ("multi_strategy_ensemble", select_ensemble,            "exacto_only"),
    ("weekday_specific",       None,                        "exacto_only"),
    ("proportional_weight",    select_architect,            "proportional"),
    ("inverse_recent",         None,                        "exacto_only"),
    # ── Champion: combina weekday + decay (mejor desempeño en multi-window walk-forward,
    # 0 derrotas en 6 ventanas vs architect_exacto_only). Promoted from exploration 2026-05-24.
    ("weekday_recent30",       None,                        "exacto_only"),
    # ── Baseline (siempre al final para que sirva de referencia)
    ("random_uniform",         None,                        "balanced"),
]


# ─────────────────────────────────────────────
# CORE LOOP
# ─────────────────────────────────────────────

def run_backtest(
    historical_path: str = "historical_data.json",
    budget: int = 5000,
    n_tickets: int = 5,
    train_pct: float = 0.80,
    rng_seed: int = 42,
    verbose: bool = True,
) -> dict:
    data = load_json(historical_path)
    chronological = _flatten_chrono(data)
    n_total = len(chronological)
    if n_total < 30:
        raise ValueError(f"Necesitas al menos 30 sorteos; tienes {n_total}.")

    cutoff = max(20, int(train_pct * n_total))
    train = chronological[:cutoff]
    test  = chronological[cutoff:]

    if verbose:
        print(f"\n  Sorteos totales : {n_total}")
        print(f"  Train ({train_pct*100:.0f}%)  : {len(train)} sorteos · hasta {train[-1].get('dia','?')[:10]} {train[-1].get('session','?')}")
        print(f"  Test            : {len(test)} sorteos · desde {test[0].get('dia','?')[:10]} {test[0].get('session','?')}")
        print(f"  Budget/sesión   : ₡{budget:,}  ·  Tickets/sesión: {n_tickets}\n")

    rng = random.Random(rng_seed)
    strategy_sessions = {name: [] for name, _, _ in STRATEGIES}
    strategy_profile  = {name: profile for name, _, profile in STRATEGIES}

    for i, target in enumerate(test, 1):
        # Subset = todo lo anterior al target (walk-forward sin leakage)
        subset = train + test[: i - 1]
        report = _silent_analyze(subset)
        if not report:
            continue

        try:
            target_num = int(target.get("numero"))
        except (TypeError, ValueError):
            continue
        try:
            target_rev = "SI" if int(target.get("in_reventado", 0)) == 1 else "NO"
        except (TypeError, ValueError):
            target_rev = "NO"

        target_session = target.get("session", "?")
        target_dia = _parse_dia(target.get("dia", ""))

        for name, selector, profile in STRATEGIES:
            skipped = False

            # ─── Selección de números (varias rutas según estrategia) ───
            if name == "random_uniform":
                numbers = rng.sample(range(100), n_tickets)

            elif name == "concentrated_top1":
                combined = report.get("combined_weights") or report.get("weights", {})
                if not combined:
                    continue
                top1_key = max(combined.items(), key=lambda x: x[1])[0]
                numbers = [int(top1_key)]  # 1 solo número

            elif name == "decay_recent":
                # Step-function decay: usar solo últimos 100 sorteos
                recent_n = 100
                recent_subset = subset[-recent_n:] if len(subset) > recent_n else subset
                if len(recent_subset) < 30:
                    continue
                report_recent = _silent_analyze(recent_subset)
                if not report_recent:
                    continue
                numbers = select_architect(report_recent, n_tickets)

            elif name == "session_specific":
                session_draws = [d for d in subset if d.get("session") == target_session]
                if len(session_draws) < 30:
                    continue
                report_session = _silent_analyze(session_draws)
                if not report_session:
                    continue
                numbers = select_architect(report_session, n_tickets)

            elif name == "signal_only_play":
                # Apostar solo cuando el top-1 z-score > 2.5; saltar si uniforme.
                ranked = report.get("ranked_numbers", [])
                if not ranked:
                    continue
                top1_z = abs(ranked[0].get("z_score", 0))
                if top1_z <= 2.5:
                    skipped = True
                    numbers = []
                else:
                    numbers = select_architect(report, n_tickets)

            elif name in ("weekday_specific", "weekday_recent30"):
                # Análisis filtrado por día de la semana del target.
                # weekday_recent30 además usa solo los últimos 30 sorteos del mismo weekday
                # (combina decay temporal + weekday clustering — campeón en multi-window).
                try:
                    target_date = datetime.fromisoformat(target.get("dia", "").split("T")[0])
                    target_wd = target_date.weekday()
                except (ValueError, TypeError):
                    continue
                wd_draws = []
                for d in subset:
                    try:
                        d_date = datetime.fromisoformat(d.get("dia", "").split("T")[0])
                        if d_date.weekday() == target_wd:
                            wd_draws.append(d)
                    except (ValueError, TypeError):
                        pass
                if len(wd_draws) < 30:
                    continue
                if name == "weekday_recent30":
                    wd_draws = wd_draws[-30:]  # últimos 30 del mismo weekday
                report_wd = _silent_analyze(wd_draws)
                if not report_wd:
                    continue
                numbers = select_architect(report_wd, n_tickets)

            elif name == "inverse_recent":
                # Anti-gambler's fallacy: números que NO salieron en últimos 30 sorteos,
                # rankeados por combined_weight entre los disponibles.
                recent_n = 30
                recent_subset = subset[-recent_n:] if len(subset) > recent_n else subset
                seen = set()
                for d in recent_subset:
                    try:
                        seen.add(int(d.get("numero")))
                    except (TypeError, ValueError):
                        pass
                missing = [n for n in range(100) if n not in seen]
                if len(missing) < n_tickets:
                    continue
                combined = report.get("combined_weights") or report.get("weights", {})
                missing.sort(key=lambda nn: -combined.get(str(nn).zfill(2), 0))
                numbers = missing[:n_tickets]

            else:
                try:
                    numbers = selector(report, n_tickets)
                except Exception:
                    continue

            # ─── Caso skip (signal_only_play sin señal) ───
            if skipped:
                strategy_sessions[name].append({
                    "dia": target_dia,
                    "session": target_session,
                    "drawn_exacto": target_num,
                    "drawn_reventada": target_rev,
                    "numbers_chosen": [],
                    "total_cost": 0,
                    "total_neto": 0,
                    "roi": 0,
                    "hit": False,
                    "skipped": True,
                })
                continue

            # ─── Validación de selección ───
            min_needed = 1 if name == "concentrated_top1" else n_tickets
            if not numbers or len(numbers) < min_needed:
                continue

            # ─── Construcción de tickets ───
            try:
                if profile == "concentrated":
                    # 1 ticket con todo el budget al top-1, rev=0
                    ticket_amount = (budget // 100) * 100
                    tickets, _rem = _build_tickets(
                        numbers, budget,
                        base_fixed=ticket_amount, rev_fixed=0,
                    )
                elif profile == "exacto_only":
                    # Exacto puro: rev=0. Bajo pago 90×, house edge a -10%.
                    ticket_amount = (budget // n_tickets // 100) * 100
                    tickets, _rem = _build_tickets(
                        numbers, budget,
                        base_fixed=ticket_amount, rev_fixed=0,
                    )
                elif profile == "proportional":
                    # Bet sizing proporcional al peso del número (top-1 más, top-5 menos),
                    # rev=0. Suma = budget, cada bet redondeada a ₡100, mínimo ₡100.
                    combined_w = report.get("combined_weights") or report.get("weights", {})
                    raw = [max(0.5, combined_w.get(str(nn).zfill(2), 1.0)) for nn in numbers]
                    tot_w = sum(raw)
                    bets = []
                    used = 0
                    for j, w in enumerate(raw[:-1]):
                        amt = int(budget * w / tot_w / 100) * 100
                        amt = max(100, amt)
                        bets.append(amt)
                        used += amt
                    # Último: el resto, mínimo ₡100
                    last_amt = max(100, ((budget - used) // 100) * 100)
                    bets.append(last_amt)
                    from jps_edge_tool import Ticket as _Ticket
                    tickets = [
                        _Ticket(num_exacto=nn, base=b, rev=0)
                        for nn, b in zip(numbers, bets)
                    ]
                else:
                    rev_ratio = _profile_to_ratio(profile)
                    tickets, _rem = _build_tickets(numbers, budget, rev_ratio=rev_ratio)
            except ValueError:
                continue

            total_neto = 0
            total_cost = 0
            any_hit = False
            for t in tickets:
                outcome = payout_ticket(t.num_exacto, t.base, t.rev, target_num, target_rev)
                total_neto += outcome["neto"]
                total_cost += outcome["cost"]
                if outcome["hit_exacto"]:
                    any_hit = True

            roi = total_neto / total_cost if total_cost else 0
            strategy_sessions[name].append({
                "dia": target_dia,
                "session": target_session,
                "drawn_exacto": target_num,
                "drawn_reventada": target_rev,
                "numbers_chosen": numbers,
                "total_cost": total_cost,
                "total_neto": total_neto,
                "roi": roi,
                "hit": any_hit,
                "skipped": False,
            })

        if verbose and (i % 20 == 0 or i == len(test)):
            print(f"  ... {i}/{len(test)} sesiones procesadas")

    # ── Agregar métricas
    baseline = strategy_sessions["random_uniform"]
    baseline_nets = [s["total_neto"] for s in baseline]

    aggregated = {}
    for name in strategy_sessions:
        sessions = strategy_sessions[name]
        if not sessions:
            continue
        # n_sess incluye sesiones saltadas; n_played solo las jugadas.
        # Para la mayoría de estrategias son iguales; signal_only_play los separa.
        played = [s for s in sessions if not s.get("skipped")]
        n_sess = len(sessions)
        n_played = len(played)
        play_rate = n_played / n_sess if n_sess else 0

        nets = [s["total_neto"] for s in sessions]
        costs = [s["total_cost"] for s in sessions]
        hits = sum(1 for s in sessions if s["hit"])
        total_apostado = sum(costs)
        total_neto = sum(nets)
        roi_total = total_neto / total_apostado if total_apostado else 0
        # Mean/std calculadas sobre TODAS las sesiones (incluyendo skipped=0). Eso
        # refleja honestamente el resultado por sesión disponible, no por sesión jugada.
        mean_net = sum(nets) / n_sess
        var = sum((x - mean_net) ** 2 for x in nets) / max(1, n_sess - 1)
        std = math.sqrt(var)

        sn = sorted(nets)
        def pct(p):
            return sn[min(n_sess - 1, int(p * n_sess))]

        # Permutation test contra baseline
        p_value = None
        z_vs_baseline = None
        if name != "random_uniform" and len(baseline_nets) == n_sess and n_sess > 0:
            observed_diff = mean_net - sum(baseline_nets) / n_sess
            n_perm = 1000
            combined = nets + baseline_nets
            count_extreme = 0
            for _ in range(n_perm):
                rng.shuffle(combined)
                left_mean = sum(combined[:n_sess]) / n_sess
                right_mean = sum(combined[n_sess:]) / n_sess
                if abs(left_mean - right_mean) >= abs(observed_diff):
                    count_extreme += 1
            p_value = count_extreme / n_perm

            base_mean = sum(baseline_nets) / n_sess
            base_var = sum((x - base_mean) ** 2 for x in baseline_nets) / max(1, n_sess - 1)
            base_std = math.sqrt(base_var)
            if base_std > 0:
                z_vs_baseline = (mean_net - base_mean) / base_std

        # Max drawdown / runup (cumulativo)
        cum = peak = 0
        max_dd = 0
        max_ru = 0
        for net in nets:
            cum += net
            if cum > peak:
                peak = cum
                max_ru = max(max_ru, cum)
            max_dd = min(max_dd, cum - peak)

        # Streaks
        win_streak = loss_streak = 0
        cur_w = cur_l = 0
        for net in nets:
            if net > 0:
                cur_w += 1; cur_l = 0
                win_streak = max(win_streak, cur_w)
            elif net < 0:
                cur_l += 1; cur_w = 0
                loss_streak = max(loss_streak, cur_l)
            else:
                cur_w = cur_l = 0

        aggregated[name] = {
            "strategy": name,
            "profile": strategy_profile[name],
            "n_sessions": n_sess,
            "n_played": n_played,
            "play_rate": round(play_rate, 4),
            "n_hits": hits,
            "hit_rate": round(hits / n_sess, 6),
            "total_apostado": total_apostado,
            "total_neto": total_neto,
            "roi_total": round(roi_total, 6),
            "mean_net_per_session": round(mean_net, 2),
            "std_net": round(std, 2),
            "p5_net": pct(0.05),
            "median_net": pct(0.50),
            "p95_net": pct(0.95),
            "max_drawdown_cumulative": int(max_dd),
            "max_runup_cumulative": int(max_ru),
            "win_streak_max": win_streak,
            "loss_streak_max": loss_streak,
            "z_vs_baseline": round(z_vs_baseline, 4) if z_vs_baseline is not None else None,
            "p_value_vs_baseline_permtest": round(p_value, 4) if p_value is not None else None,
        }

    # EV teórico por sesión para perfil balanced (5 tickets × (base=600, rev=400))
    ev_per_ticket_balanced = P_EXACTO * (EXACTO_MULT * 600 + P_REV * REV_MULT * 400) - 1000
    ev_session_balanced = ev_per_ticket_balanced * n_tickets

    return {
        "generated_at": datetime.now().isoformat(),
        "config": {
            "budget": budget,
            "n_tickets": n_tickets,
            "train_pct": train_pct,
            "n_total_draws": n_total,
            "n_train": len(train),
            "n_test": len(test),
            "rng_seed": rng_seed,
        },
        "expected_ev_per_session_balanced": round(ev_session_balanced, 2),
        "strategies": aggregated,
        "per_session_log": strategy_sessions,
        "disclaimer": "Lotería honesta tiene EV negativo fijo. Diferencias entre estrategias sobre N<1000 sorteos son dominadas por ruido. Backtest valida calibración, no predice futuro.",
    }


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────

def render_summary_md(report: dict) -> str:
    cfg = report["config"]
    strats = report["strategies"]
    base = strats.get("random_uniform")

    s = []
    s.append("# Backtest Summary — JPS Tiempos Lab")
    s.append("")
    s.append(f"_Generated: {report['generated_at']}_")
    s.append("")
    s.append(f"**Config**: budget=₡{cfg['budget']:,} · n_tickets={cfg['n_tickets']} · train={cfg['n_train']}/{cfg['n_total_draws']} sorteos · test={cfg['n_test']} sorteos · seed={cfg['rng_seed']}")
    s.append("")
    s.append(f"**EV teórico esperado/sesión (balanced)**: ₡{report['expected_ev_per_session_balanced']:,.0f}")
    s.append("")
    s.append(f"> {report['disclaimer']}")
    s.append("")
    s.append("## Resultados por estrategia (ordenado por ROI total)")
    s.append("")
    s.append("| Estrategia | Hit % | ROI Total | Mean/sess | Std | Median | P95 | Max Drawdown | z vs base | p-val |")
    s.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    sorted_strats = sorted(strats.values(), key=lambda x: x["roi_total"], reverse=True)
    for st in sorted_strats:
        z = f"{st['z_vs_baseline']:+.2f}" if st["z_vs_baseline"] is not None else "—"
        p = f"{st['p_value_vs_baseline_permtest']:.3f}" if st["p_value_vs_baseline_permtest"] is not None else "baseline"
        play_note = f" ({st['play_rate']*100:.0f}% played)" if st.get('play_rate', 1.0) < 1.0 else ""
        s.append(
            f"| `{st['strategy']}`{play_note} | "
            f"{st['hit_rate']*100:.2f}% | "
            f"{st['roi_total']*100:+.2f}% | "
            f"₡{st['mean_net_per_session']:,.0f} | "
            f"₡{st['std_net']:,.0f} | "
            f"₡{st['median_net']:,} | "
            f"₡{st['p95_net']:,} | "
            f"₡{st['max_drawdown_cumulative']:,} | "
            f"{z} | "
            f"{p} |"
        )

    s.append("")
    s.append("## Interpretación")
    s.append("")
    if base:
        s.append(f"- **Baseline (`random_uniform`)**: ROI = {base['roi_total']*100:+.2f}%, hit rate = {base['hit_rate']*100:.2f}%. Es lo que se obtiene sin intentar predecir nada.")

    significant = [st for st in sorted_strats
                   if st['p_value_vs_baseline_permtest'] is not None
                   and st['p_value_vs_baseline_permtest'] < 0.05]
    n_tested = sum(1 for st in sorted_strats if st['p_value_vs_baseline_permtest'] is not None)
    expected_false_positives = round(0.05 * n_tested, 2)
    bonferroni_threshold = 0.05 / max(1, n_tested)
    if significant:
        s.append("")
        s.append("### Estrategias estadísticamente significativas (p < 0.05 en permutation test crudo):")
        s.append("")
        for st in significant:
            direction = "mejor" if st["roi_total"] > base["roi_total"] else "peor"
            survives_bonf = "✓ pasa Bonferroni" if st['p_value_vs_baseline_permtest'] < bonferroni_threshold else "✗ NO pasa Bonferroni"
            s.append(f"- `{st['strategy']}` es **{direction}** que random (ROI {st['roi_total']*100:+.2f}% vs {base['roi_total']*100:+.2f}%, p={st['p_value_vs_baseline_permtest']:.3f}) — {survives_bonf}")
        s.append("")
        s.append(f"> **Cuidado con multiple testing**: probamos {n_tested} estrategias contra el mismo baseline. Con α=0.05 se esperan **~{expected_false_positives} falsos positivos** por puro azar. El umbral Bonferroni-corregido es p<{bonferroni_threshold:.4f}. Si una estrategia pasa el crudo pero NO el corregido, hay que validarla con data nueva antes de creerle.")
    else:
        s.append("")
        s.append("### Veredicto: ninguna estrategia es significativamente distinta de random.")
        s.append("")
        s.append("Eso es lo esperado en una lotería honesta. **Confirma que el sistema está construido correctamente** — si alguna estrategia 'venciera' a random sobre este sample, sería sospechoso (bug o suerte).")

    s.append("")
    s.append("## Comparativa de perfiles (Architect, mismos números top-5)")
    s.append("")
    eo   = strats.get("architect_exacto_only")
    cons = strats.get("architect_conservative")
    bal  = strats.get("architect_balanced")
    agg  = strats.get("architect_aggressive")
    if cons and bal and agg:
        s.append(f"| Perfil | Mean/sess | Std | P95 | Max Drawdown |")
        s.append(f"|---|---:|---:|---:|---:|")
        if eo:
            s.append(f"| **exacto_only** (rev=0) | ₡{eo['mean_net_per_session']:,.0f} | ₡{eo['std_net']:,.0f} | ₡{eo['p95_net']:,} | ₡{eo['max_drawdown_cumulative']:,} |")
        s.append(f"| conservative (rev=25%)  | ₡{cons['mean_net_per_session']:,.0f} | ₡{cons['std_net']:,.0f} | ₡{cons['p95_net']:,} | ₡{cons['max_drawdown_cumulative']:,} |")
        s.append(f"| balanced (rev=45%)      | ₡{bal['mean_net_per_session']:,.0f} | ₡{bal['std_net']:,.0f} | ₡{bal['p95_net']:,} | ₡{bal['max_drawdown_cumulative']:,} |")
        s.append(f"| aggressive (rev=65%)    | ₡{agg['mean_net_per_session']:,.0f} | ₡{agg['std_net']:,.0f} | ₡{agg['p95_net']:,} | ₡{agg['max_drawdown_cumulative']:,} |")
        s.append("")
        if cons['std_net'] < bal['std_net'] < agg['std_net']:
            s.append("Relación profile↔varianza es la **esperada**: más rev_ratio → más std sin afectar la media de forma significativa.")
        else:
            s.append("Relación profile↔varianza es **inesperada** — vale investigar `_build_tickets()` y la asignación rev/base por perfil.")
        if eo:
            s.append("")
            s.append(f"**Exacto-only** debería tener la **mejor mean/sess** (menor pérdida esperada) bajo pago 90× porque elimina la apuesta Rev (que es 3.3× peor en EV/colón). Si no lidera la media, sospechá ruido del sample.")

    cold = strats.get("cold_numbers")
    if cold and base:
        diff = (cold["roi_total"] - base["roi_total"]) * 100
        s.append("")
        s.append(f"## Sanity check — Cold Numbers")
        s.append("")
        s.append(f"ROI cold_numbers = {cold['roi_total']*100:+.2f}% vs baseline = {base['roi_total']*100:+.2f}% (diff = {diff:+.2f}pp). En una lotería honesta debería ser ≈ random. Si difiere mucho, hay sesgo o el sample es muy pequeño.")

    s.append("")
    s.append("## Sugerencias de tuning (post-backtest)")
    s.append("")
    s.append("Cualquier propuesta de cambio al motor debe:")
    s.append("")
    s.append("1. Plantear una hipótesis específica (ej: \"decay exponencial de frecuencias mejora hit rate en sesiones de tarde\")")
    s.append("2. Implementarla como una estrategia nueva en `STRATEGIES`")
    s.append("3. Correr este backtest")
    s.append("4. Mergear solo si supera baseline con p<0.05 **en un sample nuevo** (no en el mismo que la motivó — eso es overfit)")
    s.append("")
    s.append("## Caveats finales")
    s.append("")
    s.append(f"- **Sample size**: {cfg['n_test']} sesiones de test es bajo. Detectar edge de ±2pp en ROI requiere miles de sorteos.")
    s.append(f"- **Walk-forward parcial**: cada predicción usa toda la historia anterior (correcto). PERO los hiperparámetros del sistema (75/25 mega weight, 0.8 bayesian smoothing, profiles, etc.) fueron tuneados viendo data que ahora está en train. Eso es un grado leve de leakage. Mitigación: no cambiar esos hyperparams basándose en este backtest, solo evaluarlos.")
    s.append(f"- **EV teórico**: ₡{report['expected_ev_per_session_balanced']:,.0f}/sesión para perfil balanced. Toda estrategia con ROI total muy distinto a ese valor sobre N grande es sospechoso (bug, suerte extrema o anomalía real del RNG).")
    s.append("")
    return "\n".join(s)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="JPS Tiempos Lab — Backtester walk-forward 80/20",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--budget", type=int, default=5000, help="Budget por sesión (₡)")
    parser.add_argument("--n", type=int, default=5, help="Tickets por sesión")
    parser.add_argument("--train-pct", type=float, default=0.80, help="Fracción del histórico para train")
    parser.add_argument("--seed", type=int, default=42, help="Seed para random_uniform y permutation test")
    parser.add_argument("--quiet", action="store_true", help="Suprime progreso")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   JPS TIEMPOS LAB — BACKTESTER (walk-forward 80/20)  ║")
    print("╚══════════════════════════════════════════════════════╝")

    report = run_backtest(
        budget=args.budget,
        n_tickets=args.n,
        train_pct=args.train_pct,
        rng_seed=args.seed,
        verbose=not args.quiet,
    )

    # Drop per_session_log from the saved report to keep file size manageable;
    # save it separately if useful for deep dives.
    full_log = report.pop("per_session_log", None)
    save_json(report, "backtest_report.json")
    if full_log is not None:
        save_json({"sessions_by_strategy": full_log}, "backtest_sessions.json")

    md = render_summary_md(report)
    md_path = os.path.join(HERE, "backtest_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  ✓ Guardado: backtest_summary.md")

    print("\n  RESUMEN — Top 5 estrategias por ROI total:")
    sorted_strats = sorted(report["strategies"].values(), key=lambda x: x["roi_total"], reverse=True)
    for i, st in enumerate(sorted_strats[:5], 1):
        sig = ""
        if st.get("p_value_vs_baseline_permtest") is not None and st["p_value_vs_baseline_permtest"] < 0.05:
            sig = "  ★ p<0.05"
        print(f"    {i}. {st['strategy']:<28}  ROI={st['roi_total']*100:+7.2f}%   hit={st['hit_rate']*100:5.2f}%{sig}")

    print(f"\n  Disclaimer: {report['disclaimer']}\n")


if __name__ == "__main__":
    main()
