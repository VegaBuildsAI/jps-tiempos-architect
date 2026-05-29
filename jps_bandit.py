#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║         JPS TIEMPOS LAB — ADAPTIVE BANDIT (Phase 9)              ║
║  Multi-armed bandit con Thompson Sampling sobre las estrategias  ║
║  del backtester. Aprende online cuál estrategia rinde mejor.     ║
╚══════════════════════════════════════════════════════════════════╝

USO:
  python3 jps_bandit.py status                  # ver posteriors actuales
  python3 jps_bandit.py bootstrap               # inicializar desde JSONL + histórico
  python3 jps_bandit.py pick                    # elegir estrategia para próximo sorteo
  python3 jps_bandit.py reset                   # borrar estado y empezar de cero

MODELO MATEMÁTICO:
  Cada estrategia tiene un Beta(α, β) — α=1+hits, β=1+misses.
  - Posterior mean   = α / (α+β)  → estimación de hit rate
  - Posterior var    = (α·β) / ((α+β)²·(α+β+1))  → incertidumbre
  - Thompson sample  = p_i ~ Beta(α_i, β_i)  → exploration natural

LIMITACIONES HONESTAS:
  El bandit puede elegir la mejor entre las 22 estrategias disponibles,
  pero NO puede crear una estrategia con +EV en un juego de EV negativo.
  Si el RNG es uniforme, todas las estrategias convergen al 4.9% teórico
  y el bandit lo reflejará.
"""

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
if DATA_DIR != HERE and not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = "bandit_state.json"
PREDICTIONS_LOG = "predictions_log.jsonl"
HISTORICAL = "historical_data.json"

# Las estrategias disponibles — debe coincidir con jps_backtest.py STRATEGIES
ALL_STRATEGIES = [
    "architect_exacto_only",
    "architect_balanced",
    "architect_conservative",
    "architect_aggressive",
    "set_a",
    "set_b_freq_elite",
    "set_c_reverso_edge",
    "set_d_genie",
    "freq_only",
    "cold_numbers",
    "top_mega_only",
    "exacto_mega_agree",
    "concentrated_top1",
    "decay_recent",
    "session_specific",
    "signal_only_play",
    "multi_strategy_ensemble",
    "weekday_specific",
    "proportional_weight",
    "inverse_recent",
    "weekday_recent30",
    # random_uniform NO es candidato — es el baseline, no algo a "elegir"
]


def _state_path() -> str:
    return os.path.join(DATA_DIR, STATE_FILE)


# ────────────────────────────────────────────────────────────────────
# Bandit core
# ────────────────────────────────────────────────────────────────────

class BanditState:
    """Estado persistente del Multi-Armed Bandit con Thompson Sampling."""

    def __init__(self, data: Optional[dict] = None):
        if data is None:
            data = self._fresh()
        self.data = data

    @staticmethod
    def _fresh() -> dict:
        now = datetime.now().isoformat()
        return {
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "strategies": {
                name: {"alpha": 1, "beta": 1, "n_picks": 0, "n_observations": 0,
                       "last_hit_dia": None, "last_miss_dia": None}
                for name in ALL_STRATEGIES
            },
            "history": [],   # lista de eventos {ts, strategy, hit, dia, session}
        }

    # ── Persistencia ──
    @classmethod
    def load(cls) -> "BanditState":
        p = _state_path()
        if not os.path.exists(p):
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def save(self):
        self.data["updated_at"] = datetime.now().isoformat()
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    # ── Operaciones ──
    def update(self, strategy: str, hit: bool, dia: str = "", session: str = ""):
        """Registra un outcome (hit o miss) en la estrategia."""
        if strategy not in self.data["strategies"]:
            self.data["strategies"][strategy] = {
                "alpha": 1, "beta": 1, "n_picks": 0, "n_observations": 0,
                "last_hit_dia": None, "last_miss_dia": None,
            }
        s = self.data["strategies"][strategy]
        if hit:
            s["alpha"] += 1
            s["last_hit_dia"] = dia or s.get("last_hit_dia")
        else:
            s["beta"] += 1
            s["last_miss_dia"] = dia or s.get("last_miss_dia")
        s["n_observations"] += 1
        self.data["history"].append({
            "ts": datetime.now().isoformat(),
            "strategy": strategy,
            "hit": hit,
            "dia": dia,
            "session": session,
        })

    def pick(self, rng: Optional[random.Random] = None) -> str:
        """Thompson sampling: muestrea Beta(α, β) por estrategia, retorna la de mayor sample."""
        rng = rng or random
        samples = {}
        for name, s in self.data["strategies"].items():
            samples[name] = rng.betavariate(s["alpha"], s["beta"])
        chosen = max(samples.items(), key=lambda x: x[1])[0]
        self.data["strategies"][chosen]["n_picks"] += 1
        return chosen

    def posterior_means(self) -> Dict[str, float]:
        out = {}
        for name, s in self.data["strategies"].items():
            out[name] = s["alpha"] / (s["alpha"] + s["beta"])
        return out

    def posterior_variances(self) -> Dict[str, float]:
        out = {}
        for name, s in self.data["strategies"].items():
            a, b = s["alpha"], s["beta"]
            out[name] = (a * b) / ((a + b) ** 2 * (a + b + 1))
        return out

    def summary(self) -> List[dict]:
        """Lista ordenada por posterior mean desc."""
        means = self.posterior_means()
        vars_ = self.posterior_variances()
        rows = []
        for name, s in self.data["strategies"].items():
            rows.append({
                "strategy": name,
                "alpha": s["alpha"],
                "beta": s["beta"],
                "hits": s["alpha"] - 1,
                "misses": s["beta"] - 1,
                "n_observations": s["n_observations"],
                "n_picks": s["n_picks"],
                "posterior_mean": round(means[name], 6),
                "posterior_std": round(math.sqrt(vars_[name]), 6),
                "last_hit_dia": s.get("last_hit_dia"),
                "last_miss_dia": s.get("last_miss_dia"),
            })
        # Sort: primero por evidencia (n_observations), luego por posterior mean.
        # Esto pone las estrategias observadas arriba; las no observadas (prior=50%)
        # quedan al final como "still exploring".
        rows.sort(key=lambda r: (r["n_observations"] > 0, r["n_observations"], r["posterior_mean"]), reverse=True)
        return rows


# ────────────────────────────────────────────────────────────────────
# Bootstrap desde JSONL + historical_data.json
# ────────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _build_winners_index() -> Dict[str, dict]:
    p = os.path.join(DATA_DIR, HISTORICAL)
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    idx = {}
    for day in data:
        dia = (day.get("dia") or "").split("T")[0]
        for sess in ("manana", "mediaTarde", "tarde"):
            d = day.get(sess)
            if d and d.get("numero") is not None:
                idx[f"{dia}-{sess}"] = {
                    "numero": int(d["numero"]),
                    "reventada": "SI" if int(d.get("in_reventado", 0)) == 1 else "NO",
                }
    return idx


def _merge_latest(records: List[dict]) -> Dict[str, dict]:
    latest = {}
    for r in records:
        rid = r.get("id")
        if not rid:
            continue
        if rid not in latest:
            latest[rid] = dict(r)
        else:
            merged = dict(latest[rid])
            merged.update(r)
            latest[rid] = merged
    return latest


def bootstrap_from_backtest(state: BanditState, verbose: bool = True) -> dict:
    """Alimenta el bandit con las observaciones de TODAS las estrategias del
    backtester (counterfactual evaluation).

    Lee backtest_sessions.json — que tiene per-strategy, per-session, hit/miss
    info — y actualiza el bandit con cada observación. Esto evita el sesgo de
    que el JSONL solo tiene predicciones de weekday_recent30 (porque solo esa
    se "ejecutó" en el paper trading). Ahora todas las estrategias tienen
    historia.

    Reseta los contadores antes de bootstrappear.
    Returns: {n_strategies_with_data, n_total_observations, n_total_hits}
    """
    # Reset
    for name in state.data["strategies"]:
        state.data["strategies"][name] = {
            "alpha": 1, "beta": 1, "n_picks": 0, "n_observations": 0,
            "last_hit_dia": None, "last_miss_dia": None,
        }
    state.data["history"] = []

    sessions_path = os.path.join(DATA_DIR, "backtest_sessions.json")
    if not os.path.exists(sessions_path):
        if verbose:
            print(f"  ⚠ No existe backtest_sessions.json — corré primero `python3 jps_backtest.py`")
        return {"n_strategies_with_data": 0, "n_total_observations": 0, "n_total_hits": 0}

    with open(sessions_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_strategy = data.get("sessions_by_strategy", {})
    total_obs = 0
    total_hits = 0
    strategies_with_data = 0

    for strat_name, sessions in by_strategy.items():
        if strat_name not in state.data["strategies"]:
            continue  # skip random_uniform u otras no candidatas
        n_for_strat = 0
        for sess in sessions:
            if sess.get("skipped"):
                continue
            hit = bool(sess.get("hit", False))
            state.update(strat_name, hit,
                         dia=sess.get("dia", ""),
                         session=sess.get("session", ""))
            n_for_strat += 1
            total_obs += 1
            if hit:
                total_hits += 1
        if n_for_strat > 0:
            strategies_with_data += 1

    if verbose:
        print(f"  Procesadas {total_obs} observaciones de {strategies_with_data} estrategias")
        print(f"  Total hits: {total_hits}  ·  hit rate global: {total_hits/total_obs*100:.2f}%" if total_obs else "")
    return {
        "n_strategies_with_data": strategies_with_data,
        "n_total_observations": total_obs,
        "n_total_hits": total_hits,
    }


def bootstrap_from_history(state: BanditState, verbose: bool = True) -> int:
    """Replay todas las predicciones del JSONL contra los winners reales.

    Reseta los contadores antes de bootstrappear para evitar doble conteo.
    Devuelve número de predicciones procesadas.
    """
    # Reset
    for name in state.data["strategies"]:
        state.data["strategies"][name] = {
            "alpha": 1, "beta": 1, "n_picks": 0, "n_observations": 0,
            "last_hit_dia": None, "last_miss_dia": None,
        }
    state.data["history"] = []

    records = _load_jsonl(os.path.join(DATA_DIR, PREDICTIONS_LOG))
    latest = _merge_latest(records)
    winners = _build_winners_index()

    processed = 0
    hits = 0
    for pid, pred in latest.items():
        strategy = pred.get("strategy")
        if not strategy or strategy not in state.data["strategies"]:
            continue
        if pred.get("status") == "skipped":
            continue
        tickets = pred.get("tickets") or []
        if not tickets:
            continue
        drawn = winners.get(f"{pred.get('draw_date')}-{pred.get('session')}")
        if not drawn:
            continue  # sin resultado todavía
        drawn_num = drawn["numero"]
        hit = any(int(t.get("num", "-1")) == drawn_num for t in tickets)
        state.update(strategy, hit, dia=pred.get("draw_date", ""), session=pred.get("session", ""))
        processed += 1
        if hit:
            hits += 1

    if verbose:
        print(f"  Procesadas {processed} predicciones · {hits} hits · "
              f"hit rate global {hits/processed*100:.2f}%" if processed else "  Sin predicciones para procesar.")
    return processed


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def cmd_status():
    state = BanditState.load()
    rows = state.summary()
    print("\n╔══════════════════════════════════════════════════════════════════╗")
    print("║   ADAPTIVE BANDIT · estado actual                                ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"  Estrategias activas: {len(rows)}")
    total_obs = sum(r["n_observations"] for r in rows)
    print(f"  Total observaciones registradas: {total_obs}")
    print()
    print(f"  {'Estrategia':<28} {'α':>4} {'β':>4} {'hits':>5} {'miss':>5} {'mean':>8} {'std':>7} {'picks':>6}")
    print(f"  {'─'*28} {'─'*4} {'─'*4} {'─'*5} {'─'*5} {'─'*8} {'─'*7} {'─'*6}")
    for r in rows[:15]:
        print(f"  {r['strategy']:<28} {r['alpha']:>4} {r['beta']:>4} {r['hits']:>5} {r['misses']:>5} "
              f"{r['posterior_mean']*100:>7.2f}% {r['posterior_std']*100:>6.2f}% {r['n_picks']:>6}")

    print()
    leader = rows[0] if rows else None
    if leader:
        print(f"  🏆 Líder por posterior mean: {leader['strategy']} "
              f"({leader['posterior_mean']*100:.2f}% hit rate estimado)")


def cmd_bootstrap():
    state = BanditState.load()
    print("\n  Bootstrapping bandit desde predictions_log.jsonl + historical_data.json…")
    n = bootstrap_from_history(state, verbose=True)
    state.save()
    print(f"  ✓ Guardado en {STATE_FILE}")
    cmd_status()


def cmd_bootstrap_full():
    """Bootstrap inteligente: usa backtest_sessions.json para alimentar
    el bandit con observaciones de TODAS las estrategias (counterfactual)."""
    state = BanditState.load()
    print("\n  Bootstrap-full: usando backtest_sessions.json (counterfactual eval)")
    print("  Esto alimenta el bandit con outcomes de TODAS las estrategias,")
    print("  no solo de las que se 'jugaron' en el paper trading.")
    print()
    result = bootstrap_from_backtest(state, verbose=True)
    if result["n_total_observations"] == 0:
        print("  ⚠ Necesitás correr `python3 jps_backtest.py` primero.")
        return
    state.save()
    print(f"  ✓ Guardado en {STATE_FILE}")
    cmd_status()


def cmd_pick():
    state = BanditState.load()
    rng = random.Random()
    chosen = state.pick(rng)
    state.save()
    means = state.posterior_means()
    print(f"\n  Thompson sampling eligió: {chosen}")
    print(f"  Posterior mean: {means[chosen]*100:.2f}% hit rate")


def cmd_reset():
    if os.path.exists(_state_path()):
        os.remove(_state_path())
        print(f"  ✓ {STATE_FILE} borrado. Próximo `bootstrap` o `pick` lo regenera.")
    else:
        print(f"  No había {STATE_FILE} para borrar.")


def main():
    parser = argparse.ArgumentParser(description="JPS Tiempos Lab — Adaptive Bandit")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status",         help="Ver posteriors actuales")
    sub.add_parser("bootstrap",      help="Inicializar desde JSONL + histórico (solo estrategias usadas)")
    sub.add_parser("bootstrap-full", help="Inicializar desde backtest_sessions.json (TODAS las estrategias, counterfactual)")
    sub.add_parser("pick",           help="Elegir estrategia para próximo sorteo")
    sub.add_parser("reset",          help="Borrar estado")
    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "bootstrap":
        cmd_bootstrap()
    elif args.cmd == "bootstrap-full":
        cmd_bootstrap_full()
    elif args.cmd == "pick":
        cmd_pick()
    elif args.cmd == "reset":
        cmd_reset()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
