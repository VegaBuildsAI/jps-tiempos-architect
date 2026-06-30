# JPS Tiempos Lab — Technical Specification

**Versión:** 3.0
**Juego:** Nuevos Tiempos Reventados — JPS Costa Rica
**Moneda:** Colones Costarricenses (₡)
**Plataforma:** Python 3.10+ · Windows/macOS/Linux · Navegador moderno · Docker/Railway (producción)
**Estado:** Desplegado en vivo (Railway) — ver §18

---

## 1. Mecánica del juego (base para todos los cálculos)

| Parámetro | Valor |
|---|---|
| Rango Exacto | 00–99 (100 números) |
| P(Exacto) | 1/100 = 1% |
| Pago Exacto | 70× la apuesta base |
| Reventada | 1 de 3 bolas → P = 1/3 |
| Pago Reventados | 200× la apuesta rev (solo si Exacto acierta Y sale Reventada) |
| P(Exacto + Reventada) | 1/100 × 1/3 = 0.3333% |
| Apuesta mínima | ₡100 por modalidad |
| Incrementos | Múltiplos de ₡100 |
| Restricción rev | rev ≤ base siempre |
| meganNumero | Número Mega Reventados, 00–99, independiente del Exacto |
| Sorteos diarios | Mañana (12:55pm) · Media tarde (4:30pm) · Tarde (7:30pm) |

### Fórmula EV por ticket

```
EV = (1/100) × [70 × base + (1/3) × 200 × rev] − (base + rev)
```

**Ejemplo:** base=₡200, rev=₡200
```
EV = 0.01 × [14,000 + 13,333] − 400 = 273.33 − 400 = −₡126.67/sorteo
```

El EV es siempre negativo. Esto es correcto y se muestra sin suavizar.

**Apuesta óptima dentro de Exacto+Reventados**: con rev=0 (Exacto puro), el house edge es `−0.30 × base` → ROI = −30%. Es la jugada menos mala porque el Reventados tiene house edge 33% (peor por colón).

### Modalidades completas (per reglas oficiales JPS)

Cada modalidad es una jugada independiente. Inversión: ₡100 a ₡50,000 por jugada.

| Modalidad | Pago | Prob | EV teórica por ₡100 |
|---|---|---|---|
| **Exacto** | 70× base | 1/100 | -₡30 |
| **Reversible** | 35× | 2/100 (1/100 si palíndromo) | -₡30 |
| **Primer número** | 7× | 1/10 | -₡30 |
| **Terminación** | 7× | 1/10 | -₡30 |
| **Reventados** | 200× rev | 1/100 × 1/3 = 1/300 (cond. Exacto + bola) | -₡33.33 |
| **Mega Reventados** | 10× a 4000× | 6 casos (ver detalle abajo) | -₡43.7 |

#### Detalle Mega Reventados (6 casos, cada uno con stake independiente Mega)

| Caso | Exacto | Reventada | Mega | Pago Mega |
|---|---|---|---|---|
| 1 | ✓ acierta | ✓ sale | ✓ acierta | **4000×** + 70×Exacto + 200×Rev |
| 2 | ✓ acierta | ✗ no sale | ✓ acierta | **1000×** + 70×Exacto |
| 3 | ✓ acierta | ✓ sale | ✗ no acierta | **50×** + 70×Exacto + 200×Rev |
| 4 | ✗ no acierta | ✓ sale | ✓ acierta | **20×** (solo Mega, sin Exacto) |
| 5 | ✓ acierta | ✗ no sale | ✗ no acierta | **10×** + 70×Exacto |
| 6 | ✗ no acierta | ✗ no sale | ✓ acierta | **10×** (solo Mega) |

Probabilidades:
- Caso 1: 1/100 × 1/3 × 1/100 = 1/30,000
- Caso 2: 1/100 × 2/3 × 1/100 = 2/30,000
- Caso 3: 1/100 × 1/3 × 99/100 = 99/30,000
- Caso 4: 99/100 × 1/3 × 1/100 = 99/30,000
- Caso 5: 1/100 × 2/3 × 99/100 = 198/30,000
- Caso 6: 99/100 × 2/3 × 1/100 = 198/30,000

EV Mega standalone por ₡1: `(4000+2000+4950+1980+1980+1980)/30000 = 0.563 → -₡43.7 / ₡100`.

---

## 2. Arquitectura del sistema

```
JPS Tiempos Architect/
├── jps_edge_tool.py          ← CLI principal (7 comandos)
├── jps_accumulate.py         ← Acumulador diario de datos
├── simulador.py              ← Motor Monte Carlo (standalone)
├── jps_server.py             ← Servidor HTTP (login + auto-pipeline + scheduler)
├── jps_console_v2.html       ← Dashboard principal (servido en "/")
├── dashboard.html             ← Dashboard editorial alternativo (fallback)
├── jps_backtest.py           ← Backtester walk-forward 80/20 (22 estrategias)
├── jps_predict.py            ← Predicción pre-sorteo → predictions_log.jsonl
├── jps_reconcile.py          ← Reconciliación post-sorteo + bandit update
├── jps_bandit.py             ← Multi-armed bandit (Thompson Sampling)
├── jps_randomness_tests.py   ← 8 tests clásicos de aleatoriedad
├── jps_nist_sts.py           ← 8 tests NIST SP 800-22
├── jps_diehard.py            ← 9 tests DIEHARD (6 válidos, 3 marcados _broken)
│
├── Dockerfile                 ← Imagen de producción (python:3.12-slim)
├── railway.json               ← Config de build/deploy para Railway
├── assets/brand/               ← CSS + logo del dashboard editorial
│
├── historical_accumulated.json   ← Archivo maestro (dict por YYYY-MM-DD)
├── historical_data.json          ← Dataset de trabajo (lista de día-objetos)
├── last_result.json              ← Último sorteo del API
├── analysis_report.json          ← Reporte estadístico completo
├── analysis_session_{s}.json     ← Reporte filtrado por sesión
├── input.json / output.json      ← Configuración de apuesta + Monte Carlo
├── audit_result.json             ← Auditoría post-sorteo
├── predictions_log.jsonl         ← Track record de predicciones (append-only)
├── backtest_report.json / backtest_sessions.json / backtest_summary.md
├── bandit_state.json              ← Estado persistente del bandit (α, β por estrategia)
└── accumulate_log.txt            ← Log del acumulador
```

### Capas

```
┌──────────────────────────────────────────────────────────────┐
│                    ACCESO Y SESIÓN                            │
│   /login (pantalla)  ·  cookie de sesión  ·  Basic Auth compat │
├──────────────────────────────────────────────────────────────┤
│                    INTERFAZ DE USUARIO                        │
│   jps_console_v2.html (default en "/")  ·  dashboard.html     │
│   Auto-run on entry: /api/auto/run dispara el pipeline        │
├──────────────────────────────────────────────────────────────┤
│                    MOTOR DE ANÁLISIS                          │
│   jps_edge_tool.py  (analyze · bet · simulate · audit · run)  │
├──────────────────────────────────────────────────────────────┤
│           MOTORES DE VALIDACIÓN Y APRENDIZAJE                 │
│   jps_backtest.py  ·  jps_predict.py / jps_reconcile.py       │
│   jps_bandit.py  ·  jps_randomness_tests / nist_sts / diehard │
├──────────────────────────────────────────────────────────────┤
│                    MOTOR DE SIMULACIÓN                        │
│   simulador.py   (Monte Carlo portfolio)                      │
├──────────────────────────────────────────────────────────────┤
│                    CAPA DE DATOS                              │
│   jps_accumulate.py  +  JSON files  +  volumen persistente    │
├──────────────────────────────────────────────────────────────┤
│                    FUENTE EXTERNA                              │
│   JPS API  https://integration.jps.go.cr                      │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Formato de datos

### 3.1 `historical_data.json` — Lista de día-objetos

```json
[
  {
    "dia": "2026-05-17",
    "manana": {
      "numero": 47,
      "meganNumero": 23,
      "in_reventado": 1,
      "colorBolita": "ROJA",
      "hora": "10:55:00"
    },
    "mediaTarde": {
      "numero": 8,
      "meganNumero": 61,
      "in_reventado": 0,
      "colorBolita": "AZUL",
      "hora": "14:00:00"
    },
    "tarde": null
  }
]
```

### 3.2 `historical_accumulated.json` — Diccionario por fecha

```json
{
  "2026-05-17": { /* mismo objeto día */ },
  "2026-05-16": { ... }
}
```

### 3.3 `analysis_report.json` — Reporte estadístico

```json
{
  "generated_at": "2026-05-18T10:30:00",
  "n_draws": 171,
  "n_valid": 171,
  "rev_rate_pct": 33.91,
  "chi2": 98.42,
  "chi2_p_approx": 0.51,
  "top25": [ { "num_str": "47", "total": 5, "si": 2, "weight": 1.34, ... } ],
  "weights": { "47": 1.34, "23": 1.21, ... },
  "anomalies": { "outliers": [...], "rev_pairs": [...], ... }
}
```

### 3.4 `input.json` — Configuración de apuesta

```json
{
  "budget_total": 5000,
  "n_apuestas": 5,
  "rev_ratio": 0.45,
  "numeros_exacto": ["47", "23", "69", "11", "34"],
  "weights": { "47": 1.34, ... },
  "n_simulaciones": 20000,
  "seed": 42
}
```

### 3.5 `output.json` — Resultados Monte Carlo

```json
{
  "budget_total": 5000,
  "total_apostado": 4500,
  "remanente": 500,
  "tickets": [
    { "num_exacto": "47", "base": 500, "rev": 400, "tipo": "balanced" }
  ],
  "monte_carlo_result": {
    "Simulaciones": 20000,
    "Promedio Neto": -312,
    "Desviacion Std": 8940,
    "Probabilidad Ganar": 0.0612,
    "P5 Neto": -4500,
    "Mediana Neto": -4500,
    "P95 Neto": 24500
  }
}
```

### 3.6 `predictions_log.jsonl` — Track record (append-only, una línea por evento)

```jsonl
{"id":"2026-06-26-manana-architect_balanced","draw_date":"2026-06-26","session":"manana","strategy":"architect_balanced","tickets":[...],"status":"pending","predicted_at":"2026-06-26T11:15:00"}
{"id":"2026-06-26-manana-architect_balanced","status":"reconciled","hit_exacto":false,"neto":-2000,"roi":-1.0,"reconciled_at":"2026-06-26T13:30:00"}
```

Cada predicción se identifica por `id` (derivado de fecha+sesión+estrategia). El reconciliador busca el resultado oficial por `draw_date + session` y **agrega** una nueva línea con `status: "reconciled"` — nunca sobrescribe ni borra la línea `pending` original. El estado "actual" de una predicción es siempre la última línea con ese `id`.

---

## 4. `jps_edge_tool.py` — CLI principal

### 4.1 Comandos

| Comando | Función | Inputs | Outputs |
|---|---|---|---|
| `fetch` | Llama al API JPS | `--mode last\|history --days N` | `last_result.json` / `historical_data.json` |
| `analyze` | Análisis estadístico | `historical_data.json` | `analysis_report.json` |
| `session_analyze` | Análisis por sesión | `--session manana\|mediaTarde\|tarde` | `analysis_session_{s}.json` |
| `bet` | Construye apuesta | `--budget N --n N --profile P` | `input.json` + `output.json` |
| `simulate` | Re-corre Monte Carlo | `input.json` existente | `output.json` |
| `audit` | Audita resultado | `--exacto NN --reventada SI\|NO` | `audit_result.json` |
| `run` | Pipeline completo | `--budget N --n N --profile P` | Todos los anteriores |

### 4.2 Perfiles de riesgo

| Perfil | Rev ratio solicitado | Rev ratio efectivo | Descripción |
|---|---|---|---|
| `conservative` | 25% | ≤33% | Más base, menor varianza |
| `balanced` | 45% | ≤45% | Equilibrio estándar |
| `aggressive` | 65% | ≤50% | Limitado por `rev ≤ base` |

**Nota:** El perfil `aggressive` nunca puede superar 50% de rev porque la regla `rev ≤ base` lo impide. El sistema advierte la diferencia entre ratio solicitado y ratio efectivo.

### 4.3 `_build_tickets()` — Algoritmo de construcción

```
1. ticket_amount = floor(budget / n_tickets / 100) × 100
2. rev = round(ticket_amount × rev_ratio / 100) × 100
3. base = ticket_amount − rev
4. while rev > base: rev −= 100; base += 100
5. if base < 100: base = 100; rev = ticket_amount − 100
6. if rev < 100: rev = 0 (no reventada)
7. Garantías: base + rev == ticket_amount; rev ≤ base; base ≥ 100
8. remanente = budget − (ticket_amount × n_tickets)
```

### 4.4 `data_path()` — Resolución de directorio de datos

```python
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)
def data_path(filename): return os.path.join(DATA_DIR, filename)
```

En local, `JPS_DATA_DIR` no está seteada → todo vive junto al código (`HERE`). En Railway, `JPS_DATA_DIR=/data` apunta al volumen persistente — independiente del directorio de la app, que se reconstruye en cada deploy.

---

## 5. `jps_accumulate.py` — Acumulador

### Lógica de fusión

```python
for rec in new_records:
    key = date_key(rec["dia"])
    if key not in accumulated:
        accumulated[key] = rec          # nuevo día
    else:
        for slot in ("manana", "mediaTarde", "tarde"):
            new_slot = rec.get(slot)
            if new_slot and new_slot.get("numero") is not None:
                if not existing.get(slot):
                    existing[slot] = new_slot   # slot nuevo (tarde cerró después)
                elif existing[slot] != new_slot:
                    existing[slot] = new_slot   # corrección del API
```

- Preserva la historia sin destruirla
- Acepta correcciones del API si el dato cambió
- Escribe `historical_accumulated.json` (dict) y `historical_data.json` (lista)
- Hace fetch de los últimos 180 días para capturar correcciones recientes
- **Bug conocido**: puede tener timeout en ventanas de 180 días bajo ciertas condiciones de red — el fetch directo (`jps_edge_tool.py fetch --mode history --days N`) es más confiable (ver `LEARNINGS.md`)

---

## 6. Motor estadístico

### 6.1 `_extract_draws()` — Normalización de datos

Soporta dos formatos de entrada:

| Formato | Detección | Acción |
|---|---|---|
| Día-objeto `[{dia, manana:{...}, mediaTarde:{...}, tarde:{...}}]` | `any(k in data[0] for k in SESSION_KEYS)` | Aplana con etiqueta de sesión |
| Plano `[{numero, in_reventado, hora, ...}]` | ninguna session key en data[0] | Infiere sesión desde `hora` |

**Inferencia de sesión desde `hora`:**
```
hora < 12:00 → "manana"
12:00 ≤ hora < 16:00 → "mediaTarde"
hora ≥ 16:00 → "tarde"
```
Acepta formatos: `"HH:MM"`, `"HH:MM:SS"`, `"YYYY-MM-DDTHH:MM:SS"`.

### 6.2 Z-score individual

```
z = (freq_obs / total − P_esperada) / SE
SE = sqrt(P_esperada × (1 − P_esperada) / total)

Exacto: P_esperada = 1/100 = 0.01
Reventada: P_esperada = 1/3 ≈ 0.3333
```

**Umbrales:**
- |z| ≥ 1.5 → outlier leve
- |z| ≥ 1.96 → significativo p < 0.05
- |z| ≥ 2.576 → significativo p < 0.01

### 6.3 Chi-cuadrado global (df = 99)

```
χ² = Σ (obs_i − exp)² / exp    para i = 0..99
exp = total / 100

P-value: aproximación Wilson-Hilferty
z_WH = ((χ²/df)^(1/3) − (1 − 2/(9df))) / sqrt(2/(9df))
p ≈ Φ(-z_WH)    (donde Φ es la CDF normal estándar)
```

Umbral: χ² > 123.2 → distribución no uniforme (p < 0.05).

### 6.4 Suavizado bayesiano de pesos

```
raw_w = freq_num / expected_freq     # expected = total / 100
smoothed = 0.80 × raw_w + 0.20 × 1.0
weight = max(0.5, min(2.0, smoothed))
```

El prior de 1.0 es uniforme. El floor de 0.5 impide peso cero para números no vistos.

### 6.5 Mega como fenómeno independiente

```
weight_exacto = smooth(freq_exacto / expected)
weight_mega   = smooth(freq_mega / expected_mega)
```

`weight_exacto` es el único peso usado para selección de tickets, backtesting y predicción del Exacto.
`weight_mega` se conserva solo para reportar la distribución del Mega Reventados como serie separada.
No se calcula peso combinado porque `meganNumero` es independiente del número Exacto.

### 6.6 Análisis de pares reverso

Un par reverso es (A, B) donde los dígitos de A son el espejo de B (ej: 12 ↔ 21).
- Se excluyen palíndromos (00, 11, 22, ..., 99)
- Se calcula z combinado: `z_comb = (z_A + z_B) / √2`
- Patrones: "ambos altos" / "ambos bajos" / "opuestos"

---

## 7. `simulador.py` — Motor Monte Carlo

### Algoritmo

```python
# 1. Construir CDF de pesos
weights_vec = [weight[n] for n in range(100)]  # normalizado por random.choices
cum = cumulative(weights_vec)

# 2. Simular N sorteos
for _ in range(n_simulaciones):
    drawn_num = binary_search(cum, random())   # muestreo ponderado
    rev_hit = random() < rev_rate_observed     # Reventada independiente

    # 3. Calcular resultado de portafolio
    recovered = 0
    for ticket in tickets:
        if ticket.num == drawn_num:
            recovered += 70 × ticket.base
            if rev_hit:
                recovered += 200 × ticket.rev
    net = recovered − total_staked
    results.append(net)

# 4. Estadísticas del portafolio
p5   = percentile(results, 5)
med  = percentile(results, 50)
p95  = percentile(results, 95)
avg  = mean(results)
std  = stdev(results)
wins = count(r > 0 for r in results)
```

**Nota:** La semilla por defecto es 42 (reproducible). El seeding ocurre al inicio de cada llamada a `_run_monte_carlo()`.

---

## 8. `jps_server.py` — Servidor HTTP

### 8.1 Resolución de PORT y HOST

```python
def _resolve_port():
    # Prioridad: env PORT (Railway lo inyecta) → arg CLI numérico (local) → 7788.
    env_p = os.environ.get("PORT", "")
    if env_p.isdigit():
        return int(env_p)
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1])
    return 7788

HOST = os.environ.get("JPS_HOST", "0.0.0.0")   # 0.0.0.0 = alcanzable desde fuera del contenedor
```

`PORT` se lee directamente del entorno porque Railway invoca `deploy.startCommand` **sin shell** — un argumento como `${PORT:-7788}` llegaría como string literal y rompería `int(sys.argv[1])`. Resolver el puerto dentro de Python (en vez de depender de expansión de shell) hace el arranque robusto en cualquier plataforma.

### 8.2 Endpoints

| Path | Método | Auth | Descripción |
|---|---|---|---|
| `GET /` | HTML | Sí (redirige a `/login`) | Sirve `jps_console_v2.html` (fallback: `dashboard.html` → HTML embebido) |
| `GET /login` | HTML | No | Pantalla de login |
| `POST /login` | Form/JSON | No | Valida credenciales, setea cookie de sesión, redirige |
| `GET /logout` | — | No | Limpia la cookie de sesión |
| `GET /api/status` | JSON | No (healthcheck) | `{draws, top25, has_out, has_last}` |
| `GET /api/last` | JSON | Sí | Llama API JPS → guarda → retorna |
| `GET /api/pipeline?budget=N&n=N&profile=P&days=D&nsim=N` | JSON | Sí | Pipeline completo: fetch → analyze → build → simulate → reconcile |
| `GET /api/auto/run` | JSON | Sí | Dispara el pipeline en background (o reusa caché); no bloquea |
| `GET /api/auto` | JSON | Sí | Estado del pipeline asíncrono: `status, log, result, fresh, age` |
| `GET /api/state` | JSON | Sí | Estado actual: top25, output, last |
| `GET /assets/*` | estático | Sí | CSS/logo del dashboard editorial |
| `GET /historical_data.json` etc. | estático | Sí | Sirve los JSON de datos desde `DATA_DIR` |
| `POST /api/commit` / `POST /api/skip` | JSON | Sí | Acciones de paper-trading sobre una predicción pendiente |

Rutas públicas (`/login`, `/logout`, `/api/status`) existen para permitir el healthcheck de Railway y el flujo de login sin requerir sesión previa.

### 8.3 Pipeline (`pipeline()`, 5 pasos)

1. **Fetch histórico** — API JPS → `historical_data.json`
2. **Fetch último** — API JPS → `last_result.json`
3. **Análisis** — `compute_top25()` + `compute_anomalies()` → `analysis_report.json`
4. **Build + simulate** — `build_input_json()` → `input.json` → `simulador.py` → `output.json`
5. **Respuesta** — JSON completo con `top25`, `anomalies`, MC, último resultado, **y el histórico crudo (`historical`)**

El campo `historical` en la respuesta existe para que el dashboard pueda cargar los sorteos directamente del resultado del pipeline, sin depender de releer `historical_data.json` desde disco — relevante en Railway, donde `DATA_DIR` (`/data`) es distinto del directorio de la app.

### 8.4 Auto-pipeline (run-on-entry)

```python
STATE["auto_status"]      # idle | running | done | error
STATE["auto_log"]         # líneas de progreso, streamed al cliente
STATE["auto_result"]      # resultado completo de pipeline() + reconcile
STATE["auto_started_at"]  # epoch del último "done", para el TTL de caché

AUTO_CACHE_TTL = int(os.environ.get("JPS_AUTO_TTL", "600"))   # 10 min por defecto
```

`trigger_auto(params)`:
- Si hay un resultado `done` más reciente que `AUTO_CACHE_TTL` → lo reusa (`cached: true`), sin tocar el API JPS.
- Si ya hay una corrida `running` → no lanza una segunda (protegido con `threading.Lock`).
- Si no hay nada fresco → lanza `_run_auto_pipeline()` en un thread daemon: corre `pipeline()` y luego `jps_reconcile.py` como subprocess, y actualiza `STATE` al terminar.

El cliente (`jps_console_v2.html` / `dashboard.html`) llama `GET /api/auto/run` al entrar y, si no hay caché fresca, hace polling de `GET /api/auto` cada 1.5s mostrando el log en un overlay, hasta `status: done | error`.

### 8.5 Scheduler diario (background thread)

```python
SCHEDULE = [
    (12,  5, "predict",         ["--session", "manana",     "--strategy", "adaptive", "--force"]),
    (13, 30, "fetch_reconcile", None),
    (15, 40, "predict",         ["--session", "mediaTarde", "--strategy", "adaptive", "--force"]),
    (17,  0, "fetch_reconcile", None),
    (18, 40, "predict",         ["--session", "tarde",      "--strategy", "adaptive", "--force"]),
    (20,  0, "fetch_reconcile", None),
]
```

Corre en un thread daemon (`start_scheduler()`), chequea cada ~45s, y usa `_last_run_per_slot` para no ejecutar el mismo slot dos veces el mismo día. `fetch_reconcile` siempre pide `--days 180` (no incremental) porque `fetch` sobreescribe `historical_data.json`.

---

## 9. `jps_console_v2.html` — Dashboard principal

Servido por defecto en `GET /` (antes era `dashboard.html`; ver Changelog §21). Es la consola analítica densa: 6 pestañas, carga de archivos manual, y datos demo — todo corre client-side en JavaScript puro.

### 9.1 Auto-entry (carga automática al abrir)

```
1. GET /api/auto/run         → dispara pipeline (o reusa caché del server)
2. poll GET /api/auto        → overlay de progreso hasta done/error
3. S.draws  ← result.historical (fallback: GET /historical_data.json)
4. S.lastResult ← result.last_result
5. runPipeline()              → renderiza las 6 pestañas con datos reales
```

Si no hay servidor disponible (archivo abierto directo) o el fetch falla, `autoEntry()` no hace nada y deja los controles manuales intactos: **🎲 Datos Demo**, carga de archivo, y **▶ Pipeline Completo**.

### 9.2 Pipeline en JavaScript (6 pasos, `runPipeline()`)

| Paso | Función | Descripción |
|---|---|---|
| 1 | `computeTop25()` | Frecuencias + pesos + mega (espeja Python) |
| 2 | `rankMC()` | MC ranking de top 25 (hasta 15,000 sims) |
| 3 | `simulatePortfolio()` | MC de portafolio completo |
| 4 | `renderStrat()` | Tabla de tickets + métricas |
| 5 | `computeAnomaliesJS()` | Motor de anomalías (espeja Python) |
| 6 | `renderArchitect()` | The Architect Sets A–D |

### 9.3 Live Sesión (tab exclusivo)

Permite análisis aislado por sesión con control de fecha:
- **Global**: corpus completo antes de la fecha objetivo
- **Aislado**: solo sorteos de esa sesión (mañana / media tarde / tarde)
- Muestra The Architect Sets para ambos corpus en paralelo
- Usa `buildArchitectSets()` como función separada (sin DOM) para reutilización

### 9.4 `dashboard.html` — Fallback editorial

Mantiene su propio `startAuto()` con la misma lógica de auto-entry y caché. Se usa solo si `jps_console_v2.html` no existe en el deploy.

---

## 10. The Architect — Algoritmo de Sets A–D

### Set A — Estrategia (pipeline MC)
```
Fuente: simOut.tickets (output del pipeline MC)
Tamaño: 5 tickets (los primeros por MC score)
Lógica: resultado directo del motor bet + MC ranking
```

### Set B — Freq Elite
```
Fuente: top25 ordenado por (weight DESC, si_pct DESC)
Filtro: excluye todos los números en Set A
Tamaño: 5 tickets
```

### Set C — Reverso Edge
```
Algoritmo:
  1. Tomar top1 + top2 de Set A → calcular reverso (espejo de dígitos)
  2. Tomar top1 + top2 de Set B → calcular reverso
  3. Cada reverso: si está en A∪B → descartar a pool de Genie
  4. Si palindromo (AA) → skip, siguiente en la lista fuente
  5. Completar hasta 5 si faltan (fallback: todos los reversos de A+B)
Tamaño: 5 tickets
```

### Set D — The Genie
```
Pool acumulativo (en orden de prioridad):
  1. Números de B que fueron bloqueados por A (displacement)
  2. Reversos de C que cayeron en A∪B (colisión)
  3. Reversos de C que sobraron (overflow)
  4. Outliers individuales con z > 1.5 (anomalías frecuencia)
  5. Outliers de Reventada con z > 1.0
  6. Top25 restantes por score compuesto: weight × (si_pct/100 + 0.3) × (1 + |z|/5)
Tamaño: 5 tickets (primeros 5 del pool sin duplicados con A∪B∪C)
```

### Garantías
- Los 20 números de A+B+C+D son **únicos** (sin repeticiones entre sets)
- Cada set tiene como máximo 5 números
- Set D puede tener menos de 5 si el pool se agota

---

## 11. Auditoría post-sorteo

### Fórmula de auditoría por ticket

```
hit_exacto = (num_apostado == resultado_exacto)
exacto_win = base × 70   si hit_exacto, sino 0
rev_win    = rev × 200   si (hit_exacto AND reventada == SI), sino 0
recuperado = exacto_win + rev_win
neto       = recuperado − (base + rev)
roi        = neto / (base + rev)
```

### Casos especiales
- Múltiples tickets con el mismo número: cada ticket se audita independientemente
- `--exacto 00` → se parsea correctamente como 0 (`lstrip("0") or "0"`)
- Fallback: si `output.json` no existe, usa `input.json` (error descriptivo si tampoco existe)

---

## 12. Restricciones y validaciones

### Restricciones del juego (siempre forzadas)
- `base ≥ ₡100`
- `rev ≥ ₡0` (puede ser 0 si ticket_amount < ₡200)
- `rev ≤ base`
- `base + rev = ticket_amount` (sin fuga de presupuesto)
- `ticket_amount` múltiplo de ₡100
- `ticket_amount ≤ 25% del budget total`

### Validaciones de entrada
- `--budget`: entero positivo, mínimo ₡200 para producir al menos 1 ticket
- `--n`: entre 1 y 25 (limitado por top25 disponible)
- `--profile`: `conservative` | `balanced` | `aggressive`
- `--numbers "04,69,91"`: parsing con manejo de `ValueError` y rango 0–99

---

## 13. `jps_backtest.py` — Backtesting walk-forward

Valida la calibración de cada estrategia de selección contra resultados históricos reales, sin data leakage (split 80/20, walk-forward).

**Estrategias evaluadas (22):** `architect_balanced/conservative/aggressive`, los Architect Sets A–D portados de JS a Python, `freq_only`, `cold_numbers`, `random_uniform` (baseline), y la familia `weekday_*` / `session_specific` / `decay_recent` / `inverse_recent` / `signal_only_play` / `exacto_signal_gate` / `multi_strategy_ensemble` / `concentrated_top1` / `adaptive` (ver lista completa en `README.md`).

**Outputs:**
- `backtest_report.json` — métricas agregadas por estrategia (ROI, hit rate, std, percentiles, drawdowns, p-values de permutation test vs. baseline)
- `backtest_sessions.json` — log por sesión de cada estrategia
- `backtest_summary.md` — reporte humano con veredicto, comparación de perfiles y sanity checks

**Hallazgo central (ver `LEARNINGS.md`):** ningún edge sobreviviente a corrección por comparaciones múltiples (Bonferroni). El sistema es estadísticamente consistente con EV fijo de -30%. El backtest es útil para validar que la implementación no tiene bugs y para medir varianza — no para predecir qué número va a salir.

---

## 14. `jps_predict.py` + `jps_reconcile.py` — Predicción y reconciliación en vivo

### Predicción (`jps_predict.py`)

```bash
python jps_predict.py --session manana --strategy adaptive
```

Genera tickets para la próxima fecha+sesión usando la estrategia indicada, y los **agrega** (nunca sobrescribe) a `predictions_log.jsonl` con `status: "pending"`. `_load_existing_ids()` evita duplicar una predicción pendiente para la misma fecha+sesión+estrategia.

### Reconciliación (`jps_reconcile.py`)

```bash
python jps_reconcile.py            # procesa todas las pending
python jps_reconcile.py --dry-run  # preview sin escribir
```

`_reconcile_one()` busca el resultado oficial por `draw_date + session` en `historical_data.json`, calcula el resultado con la misma lógica de `audit` (§11), y agrega una línea `status: "reconciled"`. Imprime el track record acumulado (hit rate total, ROI, breakdown por estrategia) en cada corrida — y, si el bandit está activo, actualiza sus parámetros (§15).

### Flujo operativo

```
                      ┌────────────────┐
                      │ jps_predict.py │  ←─ 1h antes del sorteo (scheduler §8.5)
                      └────────┬───────┘
                               │ append pending
                               ▼
                      predictions_log.jsonl
                               ▲
                               │ append reconciled
       ┌──────────────────┐    │
       │ jps_reconcile.py │────┘  ←─ después del sorteo + fetch
       └──────────────────┘
```

---

## 15. `jps_bandit.py` — Multi-armed bandit adaptativo

Selecciona qué estrategia de predicción usar dinámicamente mediante **Thompson Sampling**, en vez de fijar una sola estrategia "campeona" del backtest.

### Modelo

```
Cada estrategia i tiene un estado Beta(α_i, β_i), inicializado en (1, 1) — prior uniforme.

Tras cada reconciliación:
    si hit_exacto:     α_i += 1
    si no hit_exacto:  β_i += 1

Selección (Thompson sample):
    sample_i ~ Beta(α_i, β_i)   para cada estrategia i
    elegida = argmax_i(sample_i)

Media posterior: E[p_i] = α_i / (α_i + β_i)
```

El sampling natural de la distribución Beta provee exploración (estrategias con poca data tienen samples más dispersos) sin necesitar un parámetro de exploración explícito (ε-greedy, UCB, etc.).

### Bootstrapping

- `bootstrap_from_backtest(state)` — inicializa α/β usando los resultados agregados de `backtest_sessions.json` (útil al arrancar el bandit con historia ya simulada).
- `bootstrap_from_history(state)` — inicializa desde el track record real de `predictions_log.jsonl`.

Estado persistido en `bandit_state.json`, servido también por el dashboard (`GET /bandit_state.json`) para mostrar la tabla de exploración en vivo (α, β, n_picks, mean ± std).

La estrategia `adaptive` usada por el scheduler (§8.5) delega en `BanditState` para elegir qué estrategia ejecutar en cada predicción.

---

## 16. Suite de validación de aleatoriedad

Tres módulos independientes que verifican si el RNG de JPS es indistinguible de aleatoriedad pura — el fundamento empírico de todo lo demás en este documento.

| Módulo | Batería | Tests | Resultado (última corrida, ver `LEARNINGS.md`) |
|---|---|---|---|
| `jps_randomness_tests.py` | Clásica + teoría de la información | 8 (chi², Ljung-Box, runs test, FFT espectral, compresión zlib/bz2/lzma, ApEn, etc.) | Acepta H0 en todos |
| `jps_nist_sts.py` | NIST SP 800-22 Rev 1a | 8 (frequency, block frequency, longest run, serial, approximate entropy, etc.) | Acepta H0 en todos |
| `jps_diehard.py` | DIEHARD (Marsaglia) | 9 implementados, 6 válidos (3 marcados `_broken` y excluidos del veredicto) | Acepta H0 en los 6 válidos |

**Total: 22 tests estadísticos independientes válidos. 0 rechazan H0** (uniforme + IID) sobre la última corrida documentada.

```bash
python jps_randomness_tests.py
python jps_nist_sts.py
python jps_diehard.py
```

**Implicancia:** no hay sesgo per-número, no hay autocorrelación temporal, no hay periodicidades, y la secuencia no es comprimible. El RNG de JPS pasa el estándar criptográfico NIST. Esto confirma — empíricamente, no solo por diseño — que ninguna estrategia de selección puede tener EV positivo. Ver `LEARNINGS.md` para el detalle hipótesis-por-hipótesis.

---

## 17. Autenticación y sesiones

### 17.1 Multi-usuario

```python
USERS = _parse_users()   # merge de BASIC_AUTH_USER/PASS (legacy, 1 usuario)
                          # + JPS_USERS="user1:pass1,user2:pass2" (N usuarios)
BASIC_AUTH_ENABLED = bool(USERS)
```

Si no hay ningún usuario configurado, el server queda abierto (modo local/desarrollo). Las credenciales se comparan en tiempo constante (`secrets.compare_digest`).

### 17.2 Login screen

`GET /login` sirve una página HTML standalone (sin dependencias del dashboard). `POST /login` acepta form-urlencoded o JSON (`user`/`username`, `pass`/`password`), valida contra `USERS`, y si es correcto:

```
Set-Cookie: jps_session=<token>; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200   # 12h
```

`SESSION_TOKEN` se genera con `secrets.token_urlsafe(32)` **una vez por arranque del proceso** — reiniciar el server invalida todas las sesiones activas. `GET /logout` limpia la cookie (`Max-Age=0`).

### 17.3 Reglas de acceso por tipo de ruta

| Tipo de ruta | Sin auth válida |
|---|---|
| Páginas HTML (`/`, `/assets/*`) | 302 → `/login` |
| Rutas de datos/API (`/api/*`, `*.json`) | 401 JSON (sin `WWW-Authenticate`, para no disparar el popup nativo del navegador) |
| `/login`, `/logout`, `/api/status` | Siempre públicas |

`_is_authed()` acepta **cookie de sesión válida** (flujo del login screen) **o** header `Authorization: Basic` válido (compat con `curl`/scripts/healthchecks que no soportan cookies).

---

## 18. Despliegue en Railway

### 18.1 Build

`Dockerfile` (imagen `python:3.12-slim`) copia todo el código + `assets/`, crea `/data` para el primer boot, y expone el puerto. `railway.json` apunta al `Dockerfile` como builder.

### 18.2 Configuración de despliegue (`railway.json`)

```json
{
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "python3 jps_server.py",
    "healthcheckPath": "/api/status",
    "healthcheckTimeout": 60,
    "restartPolicyType": "ALWAYS",
    "restartPolicyMaxRetries": 10
  }
}
```

`startCommand` se simplificó a un comando sin variables de shell (ver §8.1 sobre por qué `${PORT:-7788}` no funciona aquí).

### 18.3 Variables de entorno (nombres — los valores reales no se documentan en este archivo público)

| Variable | Propósito |
|---|---|
| `PORT` | Inyectada automáticamente por Railway |
| `JPS_DATA_DIR` | Directorio de datos persistentes — `/data` en producción |
| `JPS_HOST` | Override del bind host (default `0.0.0.0`) |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | Usuario único (legacy, compatible) |
| `JPS_USERS` | Lista de usuarios `user:pass,user:pass` (multi-usuario) |
| `JPS_AUTO_TTL` | TTL del caché del auto-pipeline en segundos (default 600) |

### 18.4 Almacenamiento persistente

Volumen montado en `/data` (independiente del filesystem de la app, que se reconstruye en cada deploy). Aquí viven `historical_data.json`, `predictions_log.jsonl`, `bandit_state.json`, `backtest_report.json`, etc. — todo lo que el sistema necesita recordar entre deploys.

### 18.5 Operación

- **Dominio:** servicio público vía subdominio `*.up.railway.app` (renombrable desde el dashboard de Railway o `railway domain update`).
- **Healthcheck:** `GET /api/status`, ventana de 60s, reinicia automáticamente si falla (`restartPolicyType: ALWAYS`, hasta 10 reintentos).
- **Despliegue:** `railway up` desde el CLI, o auto-deploy conectando el repo de GitHub desde el dashboard de Railway (no configurado por defecto).
- **Scheduler:** corre dentro del mismo proceso del servidor (§8.5) — no requiere un cron externo.

---

## 19. Requerimientos técnicos

### Python
- **Versión mínima:** Python 3.10 (imagen de producción: 3.12-slim)
- **Dependencias estándar únicamente:** `json`, `math`, `random`, `secrets`, `subprocess`, `argparse`, `urllib`, `http.server`, `collections`, `datetime`, `threading`, `typing`
- **Sin pip install necesario** — ni local ni en producción

### Navegador
- Chart.js 4.5.0 (CDN, con SRI hash) — usado por `dashboard.html`
- Cualquier navegador moderno con ES2020 support

### API JPS
- Base URL: `https://integration.jps.go.cr`
- Endpoint histórico: `/api/App/nuevostiempos/historical?fechaInicio=...&fechaFin=...`
- Endpoint último: `/api/App/nuevostiempos/last`
- Requiere headers específicos (Origin, Referer, User-Agent)
- No está en la allowlist del sandbox de Claude Code → solo ejecutable localmente o desde el server desplegado

### Producción
- Docker (imagen `python:3.12-slim`)
- Railway (o cualquier PaaS que soporte Dockerfile + volumen persistente)

---

## 20. Disclaimer (obligatorio en todo output)

> **"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."**

Este disclaimer aparece en:
- Toda salida del CLI
- Cada tab de resultados en ambas interfaces
- La pantalla de login y el dashboard
- Este documento, el `MANIFESTO.md`, y el `EXECUTIVE_SUMMARY.md`
- Cualquier archivo generado por el sistema

Está respaldado empíricamente por §16: 22 tests estadísticos independientes confirman que el sistema es indistinguible de aleatoriedad pura. Ver `LEARNINGS.md` para el detalle de cada hipótesis testeada y descartada.

---

## 21. Changelog v2.0 → v3.0

Lo que se agregó desde la última versión de este documento:

- **Motores de validación**: backtest walk-forward (§13), predicción + reconciliación en vivo (§14), bandit Thompson Sampling adaptativo (§15), suite de 22 tests de aleatoriedad (§16) — confirman empíricamente el modelo teórico del §1.
- **Despliegue en vivo**: Docker + Railway, volumen persistente, healthcheck, scheduler corriendo en el proceso del servidor (§18).
- **Autenticación**: pantalla de login, sesiones por cookie, soporte multi-usuario vía `JPS_USERS` (§17).
- **Auto-pipeline on entry**: el dashboard corre el pipeline completo al abrir, con caché de 10 min para no saturar el API JPS (§8.4).
- **`jps_console_v2.html` como dashboard por defecto** en `/`, con auto-entry; `dashboard.html` pasa a ser el fallback (§9).
- **Fix de robustez**: resolución de `PORT`/`HOST` independiente de expansión de shell, necesaria porque Railway invoca `startCommand` sin shell (§8.1).
