# JPS Tiempos Architect — Monitor Dashboard Plan
## Target: Claude Code Opus 4.8 (token-efficient execution)

---

## 1. OBJETIVO

Construir un dashboard de monitoreo (`monitor.html` + endpoints `/api/monitor/*` en
`jps_server.py`) que muestre en tiempo real el estado de **todos** los componentes del
Architect system:

- Estado del pipeline de datos (freshness, última corrida, errores)
- Predictions en vuelo y outcomes reconciliados
- Bandit state (Thompson Sampling por estrategia)
- Análisis estadístico activo (anomalías, frecuencias, chi-square)
- P&L tracker (running P&L desde predictions_log.jsonl)
- Backtest leaderboard (rankings por estrategia)

**No** reemplaza el dashboard existente (`dashboard.html`). Es una vista de "Mission
Control" para el operador que quiere observar y mejorar el sistema.

---

## 2. ARQUITECTURA DE IMPLEMENTACIÓN

### 2.1 Approach: Extender `jps_server.py` (no servidor nuevo)

Agregar 6 endpoints GET a la clase `Handler.do_GET()` existente:

```
GET /monitor          → sirve monitor.html (HTML estático embebido)
GET /api/monitor/pipeline   → estado de archivos y pipeline
GET /api/monitor/bandit     → bandit_state.json procesado
GET /api/monitor/predictions → predictions_log.jsonl procesado
GET /api/monitor/analysis   → analysis_report.json procesado
GET /api/monitor/backtest   → backtest_report.json + backtest_sessions.json
GET /api/monitor/pnl        → P&L acumulado desde predictions reconciliadas
```

Ningún endpoint requiere internet. Todo lee archivos locales en `DATA_DIR`.

### 2.2 Frontend: `monitor.html` embebido en jps_server.py

Mismo patrón que el HTML actual (string literal en Python). Usa:
- **Chart.js 4.x** (CDN: `https://cdn.jsdelivr.net/npm/chart.js`)
- CSS puro (no frameworks externos)
- JS vanilla con `fetch()` polling cada 30 segundos

---

## 3. ARCHIVOS FUENTE QUE LEE EL DASHBOARD

| Archivo | Endpoint que lo lee | Notas |
|---|---|---|
| `analysis_report.json` | `/api/monitor/analysis` | Keys: `generated_at`, `n_draws`, `rev_rate_pct`, `top25`, `weights`, `anomalies` |
| `bandit_state.json` | `/api/monitor/bandit` | Keys: `arms` (dict strategy→{alpha,beta}), `total_plays` |
| `predictions_log.jsonl` | `/api/monitor/predictions`, `/api/monitor/pnl` | Append-only, status: `pending`\|`reconciled` |
| `backtest_report.json` | `/api/monitor/backtest` | Dict de estrategia→métricas |
| `backtest_sessions.json` | `/api/monitor/backtest` | Sesiones individuales |
| `backtest_summary.md` | (solo referencia) | Parsear como texto si `backtest_report.json` no existe |
| `historical_accumulated.json` | `/api/monitor/pipeline` | Para calcular freshness |
| `last_result.json` | `/api/monitor/pipeline` | Último sorteo real |
| `audit_result.json` | `/api/monitor/pipeline` | Última auditoría |
| `output.json` | `/api/monitor/pipeline` | Último Monte Carlo output |

---

## 4. ESPECIFICACIÓN DE ENDPOINTS

### 4.1 `GET /api/monitor/pipeline`

```python
# Respuesta JSON
{
  "files": {
    "historical_accumulated": {
      "exists": bool,
      "age_minutes": float,          # mtime relativo a ahora
      "n_draws": int,
      "last_draw_date": "YYYY-MM-DD"
    },
    "analysis_report": {
      "exists": bool, "age_minutes": float,
      "generated_at": str, "n_draws": int
    },
    "bandit_state": {
      "exists": bool, "age_minutes": float,
      "n_strategies": int, "total_plays": int
    },
    "predictions_log": {
      "exists": bool, "age_minutes": float,
      "total_lines": int, "pending": int, "reconciled": int
    },
    "last_result": {
      "exists": bool, "age_minutes": float,
      "date": str, "exacto": str, "session": str
    },
    "backtest_report": {
      "exists": bool, "age_minutes": float, "n_strategies": int
    }
  },
  "pipeline_health": "green" | "yellow" | "red",
  "server_uptime_minutes": float,
  "auto_status": str,                # del STATE global de jps_server
  "auto_started_at": str | null
}
```

**Health logic:**
- `green`: historical_accumulated < 4h de antigüedad, analysis_report < 8h
- `yellow`: historical_accumulated < 24h
- `red`: historical_accumulated > 24h o no existe

### 4.2 `GET /api/monitor/bandit`

```python
# Lee bandit_state.json
{
  "total_strategies": int,
  "total_plays": int,
  "created_at": str,
  "arms": [
    {
      "strategy": str,
      "alpha": float,
      "beta": float,
      "posterior_mean": float,       # alpha/(alpha+beta)
      "posterior_std": float,        # sqrt(ab/((a+b)^2*(a+b+1)))
      "ci_low": float,               # percentil 2.5 de Beta(α,β) via aprox
      "ci_high": float,              # percentil 97.5
      "n_trials": int,               # alpha+beta-2 (prior starts at 1,1)
      "n_hits": int                  # alpha-1
    }
  ],
  # ordenado por posterior_mean desc
  "top_strategy": str | null,
  "last_pick": str | null            # última estrategia elegida (del log)
}
```

**Nota**: Si `bandit_state.json` tiene `"arms": {}` (vacío), devolver `total_plays: 0`
y lista vacía. El dashboard mostrará "Bandit sin datos todavía".

### 4.3 `GET /api/monitor/predictions`

```python
# Lee predictions_log.jsonl (última versión por id)
{
  "total": int,
  "pending": int,
  "reconciled": int,
  "hit_rate_pct": float,             # reconciliadas que tuvieron hit_exacto
  "recent": [                        # últimas 20, más reciente primero
    {
      "id": str,                     # "YYYY-MM-DD-session"
      "draw_date": str,
      "session": str,
      "strategy": str,
      "profile": str,
      "budget": int,
      "n_tickets": int,
      "tickets": [...],
      "status": "pending" | "reconciled",
      "result": {
        "exacto": str,
        "reventada": bool,
        "hit_exacto": bool,
        "hit_tickets": [...],
        "payout_total": int,
        "net": int
      } | null,
      "predicted_at": str,
      "reconciled_at": str | null
    }
  ],
  "by_strategy": {                   # stats agrupadas por estrategia
    "weekday_recent30": {
      "n": int, "hits": int, "hit_rate": float, "net_total": int
    }
  }
}
```

### 4.4 `GET /api/monitor/analysis`

```python
# Lee analysis_report.json
{
  "generated_at": str,
  "n_draws": int,
  "n_valid": int,
  "rev_rate_pct": float,
  "rev_expected_pct": 33.33,
  "rev_z_score": float,              # calcular si no viene en el json
  "rev_anomaly": bool,               # |z| > 2.576
  "chi_square_p": float | null,
  "anomalies": [                     # números con |z| > 2.576
    {"num": str, "freq": int, "z": float, "weight": float}
  ],
  "top15": [                         # top 15 por combined_weight
    {"num": str, "freq": int, "z": float, "weight": float}
  ],
  "bottom10": [                      # 10 menos frecuentes
    {"num": str, "freq": int, "z": float}
  ]
}
```

### 4.5 `GET /api/monitor/backtest`

```python
# Lee backtest_report.json (o parsea backtest_summary.md como fallback)
{
  "config": {
    "budget": int, "n_tickets": int,
    "train_draws": int, "test_draws": int,
    "generated_at": str
  },
  "strategies": [
    {
      "name": str,
      "hit_rate_pct": float,
      "roi_total_pct": float,
      "mean_per_session": float,
      "std": float,
      "median": float,
      "p95": float,
      "max_drawdown": float,
      "z_vs_baseline": float,
      "p_value": float
    }
  ],
  # ordenado por roi_total_pct desc
  "baseline_roi": float,             # random_uniform ROI
  "best_strategy": str,
  "ev_teorico": float                # EV teórico esperado/sesión
}
```

### 4.6 `GET /api/monitor/pnl`

```python
# Calcula desde predictions_log.jsonl solo registros reconciliados
{
  "total_bet": int,                  # suma de (budget) en reconciliadas
  "total_payout": int,
  "net": int,                        # total_payout - total_bet
  "roi_pct": float,
  "n_sessions": int,
  "n_hits": int,
  "hit_rate_pct": float,
  "series": [                        # P&L acumulado en orden cronológico
    {
      "date": str,
      "session": str,
      "strategy": str,
      "bet": int,
      "payout": int,
      "net_session": int,
      "cumulative_net": int
    }
  ],
  "by_strategy": {
    "weekday_recent30": {"n": int, "net": int, "roi": float}
  }
}
```

---

## 5. LAYOUT DEL DASHBOARD (`monitor.html`)

### Estructura visual (6 secciones)

```
┌─────────────────────────────────────────────────────────────────┐
│  🏛️  JPS TIEMPOS — MISSION CONTROL          [🔄 Auto-refresh 30s]│
├───────────────┬───────────────┬────────────┬────────────────────┤
│ 🟢 Pipeline   │ 📊 Predictions│ 🎯 Bandit  │ 📈 P&L Tracker    │
│  HEALTH       │  TRACKER      │  STATE     │                    │
├───────────────┴───────────────┴────────────┴────────────────────┤
│                                                                 │
│  SECTION 1: PIPELINE STATUS CARDS (horizontal row)             │
│  [Data Age] [Last Sorteo] [Pipeline] [Analysis] [Predictions]  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  SECTION 2: PREDICTIONS TRACKER (tabla + mini P&L chart)      │
│  Left: últimas 10 predicciones con status badges               │
│  Right: P&L acumulado line chart                               │
│                                                                 │
├──────────────────────────┬──────────────────────────────────────┤
│  SECTION 3: BANDIT STATE │  SECTION 4: FREQUENCY ANALYSIS      │
│  Bar chart: posterior    │  Top 15 números + anomalías          │
│  mean per strategy       │  Rev rate gauge                      │
│  (sorted desc)           │  Chi-square p-value                  │
├──────────────────────────┴──────────────────────────────────────┤
│                                                                 │
│  SECTION 5: BACKTEST LEADERBOARD (tabla completa)              │
│  Hit% | ROI | Mean/sess | Std | P95 | z vs base | p-val       │
│  Highlight best strategy; color-code vs random baseline        │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  SECTION 6: STRATEGY-LEVEL P&L (bar chart por estrategia)     │
│  Net P&L desde predictions reconciliadas, agrupado             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Color coding
- 🟢 Verde: saludable / hit / positivo
- 🟡 Amarillo: warning / dato antiguo / neutro
- 🔴 Rojo: error / miss / negativo / dato muy antiguo

---

## 6. PLAN DE EJECUCIÓN PARA CLAUDE CODE OPUS 4.8

### Instrucciones de contexto (pegar al inicio de la sesión Claude Code)

```
Estás construyendo el monitor dashboard del JPS Tiempos Architect system.
Lee primero MONITOR_DASHBOARD_PLAN.md y CLAUDE.md para contexto completo.
Los archivos viven en el directorio actual. Ejecuta los scripts con python3.
No necesitas internet. Todos los datos vienen de archivos JSON locales.
```

---

### TASK 1 — Agregar helper functions en `jps_server.py`

**Dónde**: Inmediatamente después del bloque `# ─── HELPERS ───` (~línea 65)

**Qué agregar**:

```python
# ─── MONITOR HELPERS ──────────────────────────────────────────────────────────
import time as _time_module
_SERVER_START = _time_module.time()

def _file_info(filename: str) -> dict:
    """Retorna metadata de un archivo en DATA_DIR."""
    p = os.path.join(DATA_DIR, filename)
    if not os.path.exists(p):
        return {"exists": False, "age_minutes": None}
    mtime = os.path.getmtime(p)
    age_min = (time.time() - mtime) / 60
    return {"exists": True, "age_minutes": round(age_min, 1)}

def _load_jsonl_last_by_id(filename: str) -> dict:
    """Lee un .jsonl y retorna {id: último_registro} (append-only pattern)."""
    p = os.path.join(DATA_DIR, filename)
    result = {}
    if not os.path.exists(p):
        return result
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                result[rec["id"]] = rec
            except Exception:
                pass
    return result

def _beta_ci(alpha: float, beta: float) -> tuple:
    """Aproximación normal de intervalos de confianza 95% para Beta(α,β)."""
    mean = alpha / (alpha + beta)
    var  = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
    std  = math.sqrt(var)
    return (max(0, mean - 1.96 * std), min(1, mean + 1.96 * std))

def _pipeline_health(files: dict) -> str:
    ha = files.get("historical_accumulated", {})
    if not ha.get("exists"):
        return "red"
    age = ha.get("age_minutes", 9999)
    if age < 240:    # < 4 horas
        return "green"
    if age < 1440:   # < 24 horas
        return "yellow"
    return "red"
```

**Estimado tokens**: ~400 tokens de escritura.

---

### TASK 2 — Agregar 6 endpoints en `Handler.do_GET()`

**Dónde**: En el bloque `elif parsed.path == ...` dentro de `do_GET()`, antes del `else` final.

**Patrón** (copiar 6 veces con la lógica correspondiente):

```python
elif parsed.path == "/api/monitor/pipeline":
    info = {}
    # historical_accumulated
    fi = _file_info("historical_accumulated.json")
    if fi["exists"]:
        try:
            ha = load_json("historical_accumulated.json")
            draws = expand_slots(parse_draws(ha)) if isinstance(ha, (list, dict)) else []
            fi["n_draws"] = len(draws)
            dates = [d.get("dia","")[:10] for d in draws if d.get("dia")]
            fi["last_draw_date"] = max(dates) if dates else None
        except Exception:
            fi["n_draws"] = 0
    info["historical_accumulated"] = fi
    # ... (repeat for each file, see spec above)
    info["pipeline_health"] = _pipeline_health(info)
    info["server_uptime_minutes"] = round((_time_module.time() - _SERVER_START) / 60, 1)
    info["auto_status"] = STATE["auto_status"]
    info["auto_started_at"] = STATE.get("auto_started_at")
    self._json(info)

elif parsed.path == "/api/monitor/bandit":
    # (ver spec 4.2)
    ...

elif parsed.path == "/api/monitor/predictions":
    # (ver spec 4.3)
    ...

elif parsed.path == "/api/monitor/analysis":
    # (ver spec 4.4)
    ...

elif parsed.path == "/api/monitor/backtest":
    # (ver spec 4.5)
    ...

elif parsed.path == "/api/monitor/pnl":
    # (ver spec 4.6)
    ...

elif parsed.path == "/monitor":
    self._send(200, "text/html; charset=utf-8", MONITOR_HTML.encode())
```

**Nota**: `self._json(data)` debe existir. Si no, agregar:
```python
def _json(self, data):
    body = json.dumps(data, ensure_ascii=False, default=str).encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    self.wfile.write(body)
```

**Estimado tokens**: ~2,000 tokens de escritura (lógica de los 6 endpoints).

---

### TASK 3 — Construir `MONITOR_HTML` string en `jps_server.py`

Agregar al final del archivo, antes de `if __name__ == "__main__":`:

```python
# ─── MONITOR DASHBOARD HTML ───────────────────────────────────────────────────
MONITOR_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>JPS Mission Control</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    /* ... CSS aquí ... */
  </style>
</head>
<body>
  <!-- Header con refresh countdown -->
  <!-- Section 1: Pipeline Status Cards -->
  <!-- Section 2: Predictions Tracker + P&L Chart -->
  <!-- Section 3: Bandit Bar Chart | Section 4: Frequency Analysis -->
  <!-- Section 5: Backtest Leaderboard -->
  <!-- Section 6: Strategy P&L Bar -->
  <script>
    const API = {
      pipeline:    '/api/monitor/pipeline',
      bandit:      '/api/monitor/bandit',
      predictions: '/api/monitor/predictions',
      analysis:    '/api/monitor/analysis',
      backtest:    '/api/monitor/backtest',
      pnl:         '/api/monitor/pnl'
    };
    // fetch all, render, repeat every 30s
  </script>
</body>
</html>"""
```

**Estimado tokens**: ~3,000 tokens de escritura (HTML + CSS + JS completo).

---

### TASK 4 — Verificar y testear

```bash
# 1. Verificar que el server arranca sin errores de sintaxis
python3 jps_server.py &
sleep 2

# 2. Probar cada endpoint
curl -s http://localhost:7788/api/monitor/pipeline | python3 -m json.tool | head -30
curl -s http://localhost:7788/api/monitor/bandit | python3 -m json.tool | head -20
curl -s http://localhost:7788/api/monitor/predictions | python3 -m json.tool | head -20
curl -s http://localhost:7788/api/monitor/analysis | python3 -m json.tool | head -20
curl -s http://localhost:7788/api/monitor/backtest | python3 -m json.tool | head -20
curl -s http://localhost:7788/api/monitor/pnl | python3 -m json.tool | head -20

# 3. Verificar que el HTML se sirve
curl -s http://localhost:7788/monitor | head -5

# 4. Matar el server de prueba
kill %1
```

---

## 7. DATOS DE CONTEXTO PARA CLAUDE CODE

### Estructura real de `bandit_state.json`
```json
{
  "version": 1,
  "created_at": "...",
  "arms": {
    "weekday_recent30": {"alpha": 1, "beta": 1},
    "set_a": {"alpha": 1, "beta": 1}
  },
  "total_plays": 0
}
```
_Nota_: Si `arms` está vacío `{}`, el dashboard muestra "Sin datos de bandit todavía".

### Estructura real de `predictions_log.jsonl` (una línea = un registro)
```json
{
  "id": "2026-06-03-manana",
  "predicted_at": "2026-06-03T05:24:10+00:00",
  "draw_date": "2026-06-03",
  "session": "manana",
  "strategy": "weekday_recent30",
  "profile": "exacto_only",
  "budget": 5000,
  "n_tickets": 5,
  "tickets": [{"num": "04", "base": 1000, "rev": 0}],
  "weights_top10": {"02": 1.89, "04": 1.74},
  "status": "pending",
  "result": null
}
```
Reconciliado agrega nueva línea con mismo `id` y `status: "reconciled"`.

### Estructura real de `analysis_report.json`
```json
{
  "generated_at": "...",
  "n_draws": 520,
  "n_valid": 520,
  "rev_rate_pct": 33.08,
  "rev_si": 172,
  "top25": [{"num": "47", "freq": 10, "z": 2.1, "weight": 1.85}],
  "weights": {"47": 1.85, "23": 1.72},
  "anomalies": []
}
```

### Backtest strategies ranking (desde `backtest_summary.md`)
Top performers:
1. `weekday_recent30` — Hit 11.54%, ROI +61.54%
2. `random_uniform` — Hit 7.69%, ROI +41.54% (baseline)
3. `concentrated_top1` — Hit 1.92%, ROI +34.62%
_Ninguna estrategia es estadísticamente distinta de random (esperado en lotería honesta)._

---

## 8. CONSIDERACIONES DE TOKEN EFFICIENCY

Para Opus 4.8, ejecutar en este orden para minimizar re-reads:

1. **Read once**: `jps_server.py` completo al inicio de la sesión
2. **Write once**: helpers + endpoints como un solo Edit (no múltiples edits parciales)
3. **Write once**: `MONITOR_HTML` como string literal completo (no construirlo en partes)
4. **Test once**: script bash de verificación, corregir si hay errores de sintaxis
5. **No leer archivos de datos** durante la implementación — los datos se leen en runtime

### Checkpoint de validación
Antes de terminar, verificar que:
- [ ] `python3 -c "import ast; ast.parse(open('jps_server.py').read())"` no da error
- [ ] Los 6 endpoints responden con JSON válido
- [ ] `/monitor` responde con HTML que incluye `<canvas>` para Chart.js
- [ ] El auto-refresh cada 30s funciona (testear en browser)
- [ ] Las cards de Pipeline Health muestran estado correcto basado en `historical_accumulated.json`

---

## 9. CÓMO ACCEDER AL DASHBOARD

Una vez implementado:
```
http://localhost:7788/monitor
```

O en Railway:
```
https://<tu-deployment>.railway.app/monitor
```

El link de Monitor puede agregarse como tab en el dashboard principal
(`dashboard.html`) con:
```html
<a href="/monitor" target="_blank">🏛️ Mission Control</a>
```

---

## 10. EXTENSIONES FUTURAS (post v1)

- **WebSocket push** en vez de polling (cuando predictions_log cambia → push inmediato)
- **Alertas** (email/webhook cuando bandit detecta hit o hay dato muy antiguo)
- **Timeline chart** de sorteos históricos con marcadores de prediction vs resultado
- **Heatmap 10×10** de frecuencias de números 00–99
- **Bootstrap automático** del bandit desde `predictions_log.jsonl` cuando `bandit_state.json` está vacío

---

*Plan generado: 2026-06-28 | JPS Tiempos Architect v3.0*
