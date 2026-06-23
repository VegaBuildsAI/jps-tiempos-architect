# JPS Tiempos Lab — Instrucciones para Codex (Cowork)

## QUÉ ES ESTE PROYECTO

Este proyecto es un laboratorio de análisis estadístico para **Nuevos Tiempos Reventados** (JPS Costa Rica).
El usuario juega este juego de lotería y quiere aprovechar datos históricos reales para:

1. Identificar desviaciones estadísticas en frecuencias de números y Reventada
2. Construir tickets óptimos bajo restricciones de presupuesto
3. Simular riesgo con Monte Carlo antes de apostar
4. Auditar resultados después de cada sorteo

**REGLA FUNDAMENTAL**: Siempre incluir el disclaimer:
> "Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."

---

## MECÁNICA DEL JUEGO (conocimiento base)

| Concepto | Detalle |
|---|---|
| Rango números | 00–99 |
| Exacto | Paga 70× la apuesta base |
| Reventada | 1 de 3 bolas → probabilidad 1/3 |
| Reventados | Paga 200× el rev stake (solo si Exacto acierta Y sale Reventada) |
| Mínimo apuesta | ₡100 por modalidad |
| Incrementos | Múltiplos de ₡100 |
| Restricción | Rev ≤ Base siempre |
| P(Exacto + Rev) | 1/100 × 1/3 = 0.3333% |
| meganNumero | Número Mega Reventados (bola adicional 00-99, independiente del exacto) |
| Peso combinado | Exacto 75% + Mega 25% → usado en selección de tickets y Monte Carlo |

---

## ARCHIVOS DEL PROYECTO

| Archivo | Descripción | ¿Quién lo crea? |
|---|---|---|
| `jps_edge_tool.py` | Herramienta principal (CLI) | Ya existe |
| `simulador.py` | Simulador Monte Carlo original | Ya existe |
| `historical_data.json` | Datos crudos del API histórico | Usuario corre `fetch` |
| `last_result.json` | Último resultado del API | Usuario corre `fetch` |
| `analysis_report.json` | Análisis estadístico completo | Codex corre `analyze` |
| `input.json` | Configuración de apuesta actual | Codex corre `bet` |
| `output.json` | Resultado de simulación Monte Carlo | Codex corre `bet` o `simulate` |
| `audit_result.json` | Auditoría post-sorteo | Codex corre `audit` |

---

## DIVISIÓN DE TRABAJO: Codex VS USUARIO

### El USUARIO ejecuta localmente (necesita internet):
```
python jps_edge_tool.py fetch --mode last
python jps_edge_tool.py fetch --mode history --days 60
```
Luego le pega los datos a Codex O Codex lee los archivos en el workspace.

### Codex ejecuta en sandbox (no necesita internet):
```bash
# Analyze
python "/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/jps_edge_tool.py" analyze

# Bet engine
python "/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/jps_edge_tool.py" bet \
  --budget 5000 --n 5 --profile balanced

# Audit
python "/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/jps_edge_tool.py" audit \
  --exacto 47 --reventada SI

# Simulate from existing input.json
python "/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/jps_edge_tool.py" simulate

# Full pipeline (requires historical_data.json to exist)
python "/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/jps_edge_tool.py" run \
  --budget 5000 --n 5 --profile balanced
```

---

## FLUJO DE TRABAJO POR TIPO DE SOLICITUD

### 1. "¿Qué salió hoy?" / "Último resultado"
**Acción**: El API necesita correrse localmente. Decirle al usuario:
```
python jps_edge_tool.py fetch --mode last
```
Si ya existe `last_result.json`, leer ese archivo y presentar resultado en tabla limpia.

### 2. "Analiza los históricos" / "¿Qué números han salido más?"
**Acción**:
- Si `historical_data.json` existe en el workspace → correr `analyze` en sandbox
- Si no existe → pedir al usuario: `python jps_edge_tool.py fetch --mode history --days 90`
```bash
python ".../jps_edge_tool.py" analyze
```
Luego leer `analysis_report.json` y presentar:
- Tabla top 15 números con frecuencia, z-score y peso
- Estado de Reventada (significativo o normal)
- Chi-cuadrado resultado

### 3. "Construir apuesta" / "Dame tickets" / "¿Cómo apuesto ₡X?"
**Acción**: Preguntar primero: ¿tiene `historical_data.json`? ¿cuántos tickets? ¿perfil de riesgo?
Luego correr bet engine:
```bash
python ".../jps_edge_tool.py" bet --budget BUDGET --n N_TICKETS --profile PROFILE
```
Presentar la tabla de tickets + Monte Carlo en formato limpio.

**Perfiles disponibles**:
- `conservative` → Rev ratio 25% (más base, menos riesgo volátil)
- `balanced` → Rev ratio 45% (equilibrio estándar)
- `aggressive` → Rev ratio 65% (maximiza upside, alta varianza)

### 4. "Simular mi apuesta" / "Cómo le va en Monte Carlo"
Si hay `output.json` → leerlo y presentar métricas.
Si no → correr `simulate` en sandbox.

### 5. "Auditar" / "¿Gané?" / "El resultado fue X"
Extraer número exacto y reventada SI/NO del mensaje del usuario.
```bash
python ".../jps_edge_tool.py" audit --exacto NN --reventada SI
```
Presentar tabla de auditoría con estado WIN/LOSS.

### 6. Pipeline completo ("prepárame para el sorteo de hoy")
- Verificar si hay `historical_data.json` reciente
- Correr `run --budget X --n N`
- Presentar análisis + tickets + Monte Carlo

---

## CÓMO PRESENTAR RESULTADOS

### Resultado del API (último sorteo)
Presentar así:
```
Día: YYYY-MM-DD
Sorteo      | Hora  | Exacto | Mega | Reventada | Bolita
Mañana      | 10:55 | 47     | 23   | SI        | ROJA
Media tarde | 14:00 | 08     | 61   | NO        | AZUL
Tarde       | 18:00 | (sin resultado)
```

### Análisis estadístico
- Siempre mostrar estado de Reventada primero (significativo o no)
- Luego top 10 números por frecuencia con z-score
- Terminar con disclaimer

### Monte Carlo
Interpretar Mediana como "resultado típico esperado" y P95 como "escenario optimista máximo".
Si Mediana = -total_apostado → advertir que es normal para este tipo de apuesta.

### Tickets
Mostrar siempre en tabla ordenada con columnas: #, Núm, Base, Rev, Total, EV_est

---

## CÁLCULOS MANUALES (si necesito hacerlos sin el tool)

### Auditoría manual:
```
hit_exacto = (num_apostado == resultado_exacto)
exacto_win = base × 70  si hit_exacto, sino 0
rev_win    = rev × 200  si (hit_exacto AND reventada == SI), sino 0
recuperado = exacto_win + rev_win
neto       = recuperado - (base + rev)
roi        = neto / (base + rev)
```

### EV esperado por ticket:
```
EV = (1/100) × [70 × base + (1/3) × 200 × rev] - (base + rev)
```
Para base=200, rev=200: EV = 0.01 × [14000 + 13333] - 400 = 273.33 - 400 = -₡126.67 por sorteo

### Límites válidos:
- base ≥ ₡100, rev ≥ ₡100
- base y rev en múltiplos de ₡100
- rev ≤ base
- ticket ≤ 25% del budget total

---

## COMPORTAMIENTO FRENTE A "PREDICCIONES"

Si el usuario pide "¿cuál número va a salir?" o "predice el ganador":

**NO**: Afirmar que un número está "caliente" o "por salir".
**SÍ**: Ofrecer análisis de frecuencias históricas con contexto estadístico correcto.
Siempre aclarar que las desviaciones dentro del intervalo esperado son variabilidad normal.

Solo si las desviaciones son estadísticamente significativas (p < 0.05 en chi-cuadrado
o |z| > 2.576 para números individuales) mencionar que hay una anomalía observable,
pero sin implicar causalidad.

---

## ARCHIVOS SANDBOX vs WORKSPACE

**Workspace** (el usuario puede ver):
`C:\Users\AXIO\OneDrive\Documents\JPS Tiempos Architect\`

**Sandbox** (Codex usa para ejecutar):
`/sessions/optimistic-youthful-meitner/mnt/JPS Tiempos Architect/`

Los archivos son los mismos — el workspace está montado en el sandbox.
Codex puede leer Y escribir en el workspace desde el sandbox.

---

## NOTAS DE CONTEXTO DEL PROYECTO

- El usuario es Michael (msvv11@gmail.com)
- Juega Nuevos Tiempos Reventados regularmente en JPS Costa Rica
- El API vive en: `https://integration.jps.go.cr`
- El API no está en la allowlist del sandbox → Codex no puede hacer fetch directamente
- El usuario debe correr los `fetch` commands localmente, luego Codex procesa
- Los sorteos son: Mañana (~10:55), Media tarde (~14:00), Tarde (~18:00) todos los días
- La moneda es Colones Costarricenses (₡)
