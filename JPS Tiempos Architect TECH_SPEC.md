# JPS Tiempos Lab — Technical Specification

**Versión:** 2.0  
**Juego:** Nuevos Tiempos Reventados — JPS Costa Rica  
**Moneda:** Colones Costarricenses (₡)  
**Plataforma:** Python 3.10+ · Windows/macOS/Linux · Navegador moderno

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
├── jps_server.py             ← Servidor HTTP local :7788
├── jps_console_v2.html       ← Dashboard standalone (sin servidor)
│
├── historical_accumulated.json   ← Archivo maestro (dict por YYYY-MM-DD)
├── historical_data.json          ← Dataset de trabajo (lista de día-objetos)
├── last_result.json              ← Último sorteo del API
├── analysis_report.json          ← Reporte estadístico completo
├── analysis_session_{s}.json     ← Reporte filtrado por sesión
├── input.json                    ← Configuración de apuesta actual
├── output.json                   ← Resultados Monte Carlo
├── audit_result.json             ← Auditoría post-sorteo
└── accumulate_log.txt            ← Log del acumulador
```

### Capas

```
┌─────────────────────────────────────────────────────┐
│              INTERFAZ DE USUARIO                    │
│   jps_console_v2.html          Embedded Dashboard  │
│   (standalone · file upload)   (via jps_server.py) │
├─────────────────────────────────────────────────────┤
│              MOTOR DE ANÁLISIS                      │
│   jps_edge_tool.py  (analyze · bet · simulate · …) │
├─────────────────────────────────────────────────────┤
│              MOTOR DE SIMULACIÓN                    │
│   simulador.py   (Monte Carlo portfolio)            │
├─────────────────────────────────────────────────────┤
│              CAPA DE DATOS                          │
│   jps_accumulate.py  +  JSON files                 │
├─────────────────────────────────────────────────────┤
│              FUENTE EXTERNA                         │
│   JPS API  https://integration.jps.go.cr           │
└─────────────────────────────────────────────────────┘
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

### 6.5 Peso combinado Exacto + Mega (75/25)

```
weight_exacto = smooth(freq_exacto / expected)
weight_mega   = smooth(freq_mega / expected_mega)     # fallback=1.0 si sin datos
weight_combined = 0.75 × weight_exacto + 0.25 × weight_mega
```

Rango resultante: [0.5, 2.0].

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

## 8. `jps_server.py` — Servidor HTTP local

### Endpoints

| Path | Método | Descripción |
|---|---|---|
| `GET /` | HTML | Sirve el dashboard embebido |
| `GET /api/status` | JSON | Estado: sorteos en memoria, has_output, has_last |
| `GET /api/last` | JSON | Llama API JPS → guarda → retorna |
| `GET /api/pipeline?budget=N&n=N&profile=P&days=D&nsim=N` | JSON | Pipeline completo: fetch → analyze → build → simulate |
| `GET /api/state` | JSON | Estado actual: top25, output, last |
| `GET /api/auto` | JSON | Estado del pipeline asíncrono |

### Pipeline del servidor (5 pasos)

1. **Fetch histórico** — API JPS → `historical_data.json`
2. **Fetch último** — API JPS → `last_result.json`
3. **Análisis** — `compute_top25()` + `compute_anomalies()` → `analysis_report.json`
4. **Build + simulate** — `build_input_json()` → `input.json` → `simulador.py` → `output.json`
5. **Respuesta** — JSON completo con top25, anomalías, MC, último resultado

---

## 9. `jps_console_v2.html` — Dashboard standalone

### Características

- **Sin servidor**: toda la computación ocurre en el navegador (JavaScript puro)
- **Carga de archivos**: `historical_data.json` + `last_result.json` via file picker
- **Datos demo**: 540 sorteos sintéticos con sesgos por sesión para testing

### Pipeline en JavaScript (6 pasos)

| Paso | Función | Descripción |
|---|---|---|
| 1 | `computeTop25()` | Frecuencias + pesos + mega (espeja Python) |
| 2 | `rankMC()` | MC ranking de top 25 (hasta 15,000 sims) |
| 3 | `simulatePortfolio()` | MC de portafolio completo |
| 4 | `renderStrat()` | Tabla de tickets + métricas |
| 5 | `computeAnomaliesJS()` | Motor de anomalías (espeja Python) |
| 6 | `renderArchitect()` | The Architect Sets A–D |

### Live Sesión (tab exclusivo del standalone)

Permite análisis aislado por sesión con control de fecha:
- **Global**: corpus completo antes de la fecha objetivo
- **Aislado**: solo sorteos de esa sesión (mañana / media tarde / tarde)
- Muestra The Architect Sets para ambos corpus en paralelo
- Usa `buildArchitectSets()` como función separada (sin DOM) para reutilización

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

## 13. Requerimientos técnicos

### Python
- **Versión mínima:** Python 3.10
- **Dependencias estándar únicamente:** `json`, `math`, `random`, `subprocess`, `argparse`, `urllib`, `http.server`, `collections`, `datetime`, `typing`
- **Sin pip install necesario**

### Navegador (standalone)
- Chart.js 4.5.0 (CDN, con SRI hash)
- Cualquier navegador moderno con ES2020 support

### API JPS
- Base URL: `https://integration.jps.go.cr`
- Endpoint histórico: `/api/App/nuevostiempos/historical?fechaInicio=...&fechaFin=...`
- Endpoint último: `/api/App/nuevostiempos/last`
- Requiere headers específicos (Origin, Referer, User-Agent)
- No está en la allowlist del sandbox de Claude → solo ejecutable localmente

---

## 14. Disclaimer (obligatorio en todo output)

> **"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."**

Este disclaimer aparece en:
- Toda salida del CLI
- Cada tab de resultados en ambas interfaces
- Este documento
- El `MANIFESTO.md`
- Cualquier archivo generado por el sistema
