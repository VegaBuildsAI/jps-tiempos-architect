# JPS Tiempos Lab

> **"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."**

Statistical analysis console for **Nuevos Tiempos Reventados** — JPS Costa Rica.

Converts historical draw data into auditable betting decisions using classical statistics, Monte Carlo simulation, and a rigorous expected-value model that never hides the house edge.

---

## What this is

A CLI + local dashboard that turns raw JPS API data into:

- **Frequency analysis** with z-scores and chi-squared tests
- **Bet engine** that builds optimal tickets under budget constraints (conservative / balanced / aggressive profiles)
- **Monte Carlo simulation** (20,000 trials) to quantify risk before placing a bet
- **Post-draw auditor** that reconciles every ticket against the official result

What it is **not**: a predictor. EV is always negative. That is shown without softening.

---

## Game mechanics

| Parameter | Value |
|---|---|
| Number range | 00–99 (100 numbers) |
| P(Exacto) | 1/100 = 1% |
| Exacto payout | 70× base stake |
| Reventada | 1 of 3 balls → P = 1/3 |
| Reventados payout | 200× rev stake (only if Exacto hits AND Reventada drawn) |
| P(Exacto + Reventada) | 1/100 × 1/3 = 0.3333% |
| Min stake | ₡100 per mode · multiples of ₡100 |
| Constraint | rev ≤ base always |
| Daily draws | Mañana ~10:55 · Media tarde ~14:00 · Tarde ~18:00 |

**EV formula per ticket (Exacto + Reventados):**
```
EV = (1/100) × [70 × base + (1/3) × 200 × rev] − (base + rev)
```
Example — base ₡200, rev ₡200: `EV = −₡126.67 / draw`. Setting `rev = 0` (Exacto-only) reduces house edge to **-30%** (best Exacto play).

JPS Tiempos offers 6 betting modalities — each is an independent bet (₡100 to ₡50,000):

| Modality | Pays | Hit probability | EV per ₡100 stake | House edge |
|---|---|---|---|---|
| Exacto | 70× | 1/100 | -₡30 | 30% |
| Reversible | 35× | 2/100 (palindrome: 1/100) | -₡30 | 30% |
| Primer número | 7× | 1/10 | -₡30 | 30% |
| Terminación | 7× | 1/10 | -₡30 | 30% |
| Reventados | 200× | 1/100 × 1/3 = 1/300 | -₡33.33 | 33% |
| Mega Reventados | 10× to 4000× | varies (6 cases) | ~-₡44 | 44% |

All modalities have negative EV by design. Lower-multiplier modalities (Primer, Terminación) trade variance for hit-rate at the same expected loss.

---

## Requirements

- Python 3.10+
- No external dependencies — stdlib only (`json`, `math`, `urllib`, `argparse`, `statistics`)

---

## Usage

### 1. Fetch data (requires internet — run locally)

```bash
# Last result
python jps_edge_tool.py fetch --mode last

# Historical range
python jps_edge_tool.py fetch --mode history --days 60
```

### 2. Analyze (Claude Code / local)

```bash
python jps_edge_tool.py analyze
```
Outputs `analysis_report.json` with z-scores, chi-squared, Reventada rate, and weighted number ranking.

### 3. Build a bet

```bash
python jps_edge_tool.py bet --budget 5000 --n 5 --profile balanced
```

| Profile | Rev ratio | Use when |
|---|---|---|
| `conservative` | 25% | Lower variance, more base |
| `balanced` | 45% | Standard play |
| `aggressive` | 65% | Maximum upside, high variance |

### 4. Simulate risk

```bash
python jps_edge_tool.py simulate
```
Runs 20,000 Monte Carlo trials on the current `input.json`. Median = typical outcome. P95 = optimistic ceiling.

### 5. Audit after the draw

```bash
python jps_edge_tool.py audit --exacto 47 --reventada SI
```

### 6. Full pipeline

```bash
python jps_edge_tool.py run --budget 5000 --n 5 --profile balanced
```

---

## Backtesting & Live Prediction

Three additional scripts that turn the bet engine into a measurable, auditable system:

### Backtest (walk-forward 80/20)

Validates the calibration of every selection strategy against historical results, without data leakage. Compares 11 strategies — `architect_balanced/conservative/aggressive`, the Architect Sets A–D (ported from the JS dashboard to Python), `freq_only`, `cold_numbers`, `top_mega_only`, and `random_uniform` as the baseline.

```bash
python jps_edge_tool.py fetch --mode history --days 180   # ≥150 days recommended
python jps_backtest.py --budget 5000 --n 5                # ~1 min for 100 test sessions
```

Outputs:
- `backtest_report.json` — per-strategy aggregate metrics (ROI, hit rate, std, percentiles, drawdowns, permutation-test p-values vs baseline)
- `backtest_sessions.json` — per-session log for every strategy (useful for deep dives)
- `backtest_summary.md` — human-readable report with verdict, profile comparison, sanity checks, and tuning suggestions

**Important — what the backtest can and cannot tell you**: an honest lottery has fixed negative EV (`−₡126.67/draw` for `base=rev=₡200`). No selection strategy changes that in expectation. The backtest is useful for: (1) validating the implementation has no bugs (the system should statistically tie with random), (2) measuring variance across profiles, (3) detecting RNG anomalies if any, (4) building discipline. It is **not** a tool for predicting which numbers will come out.

### Live prediction logging

Generate a prediction for the next draw and record it in an append-only JSONL log:

```bash
python jps_predict.py --session manana                  # default strategy: architect_balanced
python jps_predict.py --session tarde --strategy set_a  # use Architect Set A
python jps_predict.py --session mediaTarde --strategy set_c_reverso_edge --budget 10000
```

Strategies available: `architect_balanced`, `architect_conservative`, `architect_aggressive`, `set_a`, `set_b_freq_elite`, `set_c_reverso_edge`, `set_d_genie`, `freq_only`, `top_mega_only`.

Predictions are stored in `predictions_log.jsonl` with full metadata (timestamp, tickets, weights snapshot, status=pending). One line per prediction. Never overwritten — reconciliation appends new lines.

### Reconciliation

After the draw, fetch the result and cross-reference pending predictions:

```bash
python jps_edge_tool.py fetch --mode history --days 7   # bring fresh results
python jps_reconcile.py                                 # process all pending
python jps_reconcile.py --since 2026-05-01              # filter by date
python jps_reconcile.py --dry-run                       # preview without writing
```

The reconciler matches each pending prediction against `historical_data.json` by `draw_date + session`, computes the outcome using the same `payout_ticket()` logic as `audit`, and appends a `status: reconciled` line. The accumulated track record (total hit rate, ROI, breakdown by strategy) is printed each run.

Operational flow:

```
                      ┌────────────────┐
                      │ jps_predict.py │  ←─ 1h before draw
                      └────────┬───────┘
                               │ append pending
                               ▼
                      predictions_log.jsonl
                               ▲
                               │ append reconciled
       ┌──────────────────┐    │
       │ jps_reconcile.py │────┘  ←─ after draw + fetch
       └──────────────────┘
```

---

## File structure

```
jps_edge_tool.py          ← Main CLI (fetch · analyze · bet · simulate · audit · run)
jps_accumulate.py         ← Daily data accumulator
simulador.py              ← Standalone Monte Carlo engine
jps_server.py             ← Local HTTP server :7788
jps_console_v2.html       ← Standalone dashboard (no server required)
jps_backtest.py           ← Walk-forward 80/20 backtester (11 strategies)
jps_predict.py            ← Pre-draw prediction → predictions_log.jsonl
jps_reconcile.py          ← Post-draw reconciliation + track record

# Runtime files — not tracked in git, generated by the tools:
historical_data.json      ← Working dataset (fetch --mode history)
historical_accumulated.json ← Master accumulator (dict by YYYY-MM-DD)
last_result.json          ← Latest draw result
analysis_report.json      ← Full statistical report
input.json / output.json  ← Current bet config + Monte Carlo results
audit_result.json         ← Post-draw audit
backtest_report.json      ← Backtest metrics per strategy
backtest_sessions.json    ← Per-session backtest log
backtest_summary.md       ← Human-readable backtest report
predictions_log.jsonl     ← Live prediction track record (append-only)
```

---

## Dashboard

Open `jps_console_v2.html` in any modern browser. Upload a `historical_data.json` file to run analysis client-side, or connect to `jps_server.py` for live data.

---

## API

Data source: `https://integration.jps.go.cr`

| Endpoint | Method | Description |
|---|---|---|
| `/api/App/nuevostiempos/last` | GET | Last draw (mañana, mediaTarde, tarde) |
| `/api/App/nuevostiempos/historical` | GET | Historical by date range (`fechaInicio`, `fechaFin`) |

---

## Disclaimer

This tool is for statistical analysis and entertainment. Lottery outcomes are random. No analysis of historical data changes the probability of future draws. Play only what you can afford to lose.

---

*JPS Tiempos Lab — built with Python, classical statistics, and respect for randomness.*
