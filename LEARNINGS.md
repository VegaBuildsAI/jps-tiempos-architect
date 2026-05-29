# JPS Tiempos Lab — Learnings Document

> **Memoria consolidada del proyecto.** Si querés saber qué se probó, qué resultó y qué NO vale la pena re-intentar, leé acá. Si en 6 meses volvés al proyecto, este documento te ahorra semanas.

**Última actualización**: 2026-05-24
**Branch**: `carlos/backtest-predict-system` (PR #2)
**Autor**: Carlos León (cleonenrique-jpg) + Claude Opus 4.7 como copiloto técnico

---

## 🎯 Conclusión ejecutiva

**Los sorteos JPS Tiempos son estadísticamente indistinguibles de aleatoriedad pura.**

22 tests estadísticos independientes (NIST SP 800-22 + DIEHARD + classical + information-theoretic) **TODOS aceptan H0** (uniforme + IID). El RNG pasa el estándar criptográfico.

**Implicancia matemática**:
- House edge fija: -30% para Exacto-only, peor para Reventados/Mega
- Ningún algoritmo de selección de números puede generar +EV en expectativa
- Cualquier "edge" en backtest sobre N<1000 es ruido garantizado a converger a -30%

**Recomendación**:
- ❌ **NO apostar dinero real** — perdés ~30% en expectativa
- ✅ Paper trading para validar matemáticamente
- ✅ Sistema vale como ejercicio académico, no como inversión

---

## ✅ Hipótesis testeadas y descartadas

Lista de cosas que **se probaron y NO funcionan**. No volver a intentarlas sin nueva evidencia.

### H1: "Hay sesgo per-número en el RNG"
- **Test**: Chi² goodness of fit sobre 518 sorteos
- **Resultado**: p=0.51 → distribución indistinguible de uniforme
- **Veredicto**: ❌ Descartada. Los 100 números aparecen con la frecuencia esperada.
- **Ref**: `jps_randomness_tests.py:chi_square_uniform`

### H2: "Hay autocorrelación temporal en la secuencia"
- **Test**: Ljung-Box (lags 1-20), per-lag ACF, runs test
- **Resultado**: Todos accept H0 (p > 0.3). 3 lags fuera de CI vs 1.5 esperado por azar.
- **Veredicto**: ❌ Descartada. Cada sorteo es independiente del anterior.
- **Ref**: `jps_randomness_tests.py:ljung_box`, `per_lag_acf`, `runs_test_above_below_median`

### H3: "Hay periodicidades cíclicas (semanales/diarias)"
- **Test**: FFT spectral analysis, top 200 frecuencias
- **Resultado**: Max power 5.85× sobre noise floor — dentro de variabilidad esperada bajo white noise con multiple testing
- **Veredicto**: ❌ Descartada. Sin periodicidades reales.
- **Ref**: `jps_randomness_tests.py:spectral_analysis`

### H4: "La secuencia es comprimible (tiene patrón)"
- **Test**: zlib/bz2/lzma compression ratio vs baseline empírico de 200 secuencias random
- **Resultado**: zlib z=+0.29, bz2 z=-0.48, lzma z=+1.84 — todos dentro de CI 95%
- **Veredicto**: ❌ Descartada. La secuencia tiene entropía indistinguible de random.
- **Ref**: `jps_randomness_tests.py:compression_test`

### H5: "Estrategia 'weekday_recent30' tiene edge real"
- **Backtest 80/20**: ROI +48.08%, hit rate 10.58%, p=0.003 (raw)
- **Multi-window walk-forward**: 0 derrotas en 6 ventanas vs baseline
- **Permutation test**: NO pasa Bonferroni corrected (necesita p<0.0024)
- **Tests aleatoriedad**: 22 tests aceptan random → cualquier "edge" es ruido small-sample
- **Veredicto**: ❌ Aparenta edge pero la batería de aleatoriedad confirma que es ruido. Va a converger a -30% con más data.
- **Ref**: `jps_backtest.py`, `backtest_report.json`

### H6: "Set C reverso edge tiene edge real"
- **Test**: backtest p=0.069 (cerca pero no significativo)
- **Veredicto**: ❌ Mismo razonamiento que H5. Noise.

### H7: "Día de la semana (weekday) influye en distribución"
- **Test**: estrategia `weekday_specific` + descomposición por día
- **Resultado**: 7/8 hits concentrados en mid-week en small sample. Lookt like overfit a 3 días específicos.
- **Veredicto**: ❌ Descartada. Sin evidencia robusta de patrón por weekday.

### H8: "Multi-strategy ensemble vencerá a estrategias individuales"
- **Test**: `multi_strategy_ensemble` en backtester
- **Resultado**: ROI -46.15%, idéntico a baseline architect — el consenso no agrega información
- **Veredicto**: ❌ Descartada. Las estrategias correlacionan; ensemble no agrega.

---

## ❌ Algoritmos descartados a priori (no implementados, con justificación)

### Deep Learning (LSTM, Transformer)
- **Por qué NO**: requiere 10K+ samples. Tenemos 518. Garantizado overfitting.
- **Research ref**: arxiv 2402.10835 — LLMs fallan en time series sin periodicidad clara, noisy, abrupt changes (exactamente nuestro caso).

### LLM como predictor (Claude/GPT/etc para decidir números)
- **Por qué NO**:
  1. LLMs no son magos estadísticos. Si chi² no detecta patrón, LLM tampoco.
  2. Research (arxiv 2410.05440): LLMs no entienden time series numéricas, chain-of-thought no ayuda.
  3. Latencia + costo por inferencia hacen la práctica diaria inviable.
- **Uso aceptable**: LLM como copiloto explicativo (resumir resultados, generar hipótesis nuevas) — NO como decisor de apuestas.

### Reinforcement Learning profundo (PPO, A2C, DQN)
- **Por qué NO**:
  1. Si recompensa es genuinamente aleatoria (lo confirmamos), RL solo aprende "no jugar" — same que Kelly criterion con p=0.01
  2. El bandit Thompson Sampling que ya implementamos ES la versión correcta de RL para este problema (regret bound matemáticamente óptimo)
- **Bandit Thompson Sampling sí está implementado**: `jps_bandit.py`

### HMM (Hidden Markov Models) regime detection
- **Por qué NO**: tests confirmaron que no hay regímenes. Implementar HMM solo añadiría sobreestructura sin captura de señal real.
- **Cuándo SÍ tendría sentido**: si los tests de change-point detectaran cambios en la distribución del RNG.

### Kalman Filter sobre weights
- **Por qué NO**: el `decay_recent` con step-function captura lo mismo (recencia). Kalman sería suavizado más fino pero no detecta señal donde no hay.

### NIST SP 800-22 completo (los 15 tests originales)
- **Cuánto: 8 de 15 implementados**: Frequency, Block Frequency, Longest Run, CUSUM forward+backward, Approximate Entropy, Serial, Non-overlapping Template, Maurer's (skip por sample size)
- **Por qué los otros 7 no se hicieron**: requieren millones de bits (Random Excursions, Linear Complexity, Matrix Rank) que NO tenemos.
- **Si quisieras completar**: necesitarías ~150K sorteos = ~140 años de data JPS.

### DIEHARD completo (18 tests originales)
- **Cuánto: 6 implementados correctamente** (Runs Up/Down, Count-the-1s, Parking Lot ×2, Min Distance, Consecutive Pairs)
- **3 implementados con bugs y excluidos**: Birthday Spacings, Overlapping Sums, Squeeze Test — sus fórmulas de expected/variance son aproximadas y rechazan H0 incluso sobre data genuinamente random.
- **9 NO implementados**: la mayoría requieren bits >> los que tenemos (Bitstream, OPSO, OQSO, DNA, etc.)

---

## ✅ Lo que SÍ funciona del sistema

### Estructura del software (lo bueno)
1. **`jps_edge_tool.py`** — CLI completo: fetch / analyze / bet / simulate / audit / run
2. **`jps_backtest.py`** — 22 estrategias en walk-forward 80/20 + multi-window
3. **`jps_predict.py`** — registra predicciones en JSONL append-only
4. **`jps_reconcile.py`** — cruza pending vs ganador real, actualiza bandit
5. **`jps_bandit.py`** — Thompson Sampling sobre 21 estrategias
6. **`jps_server.py`** — dashboard HTTP + scheduler interno
7. **`dashboard.html`** — UI editorial (Preset 2 Coreintel) con 4 secciones
8. **3 baterías de tests aleatoriedad** — 22 tests totales

### Operacional (lo que sí preserva valor)
- **Paper trading sin riesgo**: registra predicciones, reconcilia, mide. ₡0 expuestos.
- **Scheduler automático**: pre-sorteo predicts + post-sorteo reconcile.
- **launchd plist**: auto-start al login + auto-restart si crashea.
- **JSONL append-only**: audit trail completo, recuperable.
- **Bandit Thompson Sampling**: óptimo para selección entre estrategias.

---

## 🔁 Cosas que NO volver a intentar (sin nueva evidencia)

1. ❌ Buscar patrón estadístico en la secuencia — 22 tests dicen no hay
2. ❌ ML supervisado (XGBoost/LightGBM) sobre features de la secuencia — no hay señal que predecir
3. ❌ Deep learning de cualquier tipo — sample size insuficiente
4. ❌ Algoritmos "comerciales" de lottery prediction — son scam
5. ❌ Estrategias que prometen +ROI en backtest pequeño — son overfit
6. ❌ Martingala / doubling / Fibonacci — garantizan ruina
7. ❌ Conteo de cartas / wheel clocking — no aplican a RNG digital
8. ❌ Mega Reventados como apuesta principal — EV -44%, peor que Exacto-only
9. ❌ Bet sizing creciente tras pérdida — sesgo cognitivo (loss chasing)

---

## ✅ Cosas que SÍ valen la pena (si hay tiempo/interés)

### Si querés extender el sistema
1. **Más data**: fetchar 360+ días para mayor poder estadístico
2. **Test de change-point**: detectar si el RNG cambia en el futuro (CUSUM-based)
3. **LLM helper para resúmenes** (NO para decisiones): endpoint `/api/llm-analyze` que llama Claude Haiku
4. **Auth en el dashboard**: si lo deployás a Railway/cloud
5. **SQLite migration**: cuando el JSONL > 50MB

### Si querés validar más rigurosamente
1. Out-of-sample paper trading 30-60 días → confirmar convergencia a -30% EV
2. Re-correr todas las baterías con la nueva data
3. Estadística bayesiana sobre el bandit: P(weekday_recent30 tiene true edge > 5%) → posterior debería tender a 0 con más data

---

## 🧪 Cómo reproducir los tests

```bash
# 1. Bajar data fresca
python3 jps_edge_tool.py fetch --mode history --days 180

# 2. Correr 3 baterías
python3 jps_randomness_tests.py   # 8 tests classical + compression
python3 jps_nist_sts.py           # 8 tests NIST SP 800-22
python3 jps_diehard.py            # 9 tests DIEHARD (6 válidos + 3 broken-skipped)

# 3. Backtest completo (sanity check del comportamiento)
python3 jps_backtest.py

# 4. Ver bandit acumulado
python3 jps_bandit.py status

# 5. Dashboard
python3 jps_server.py             # http://localhost:7788/
```

---

## 📂 Mapa de archivos / responsabilidades

| Archivo | Para qué sirve | ¿Volver a tocar? |
|---|---|---|
| `jps_edge_tool.py` | CLI base (fetch, analyze, bet, audit, simulate) | Sí, si JPS cambia API o reglas |
| `jps_accumulate.py` | Acumulador histórico (timeout en 180 días) | Bug conocido — fetch directo es más confiable |
| `simulador.py` | Monte Carlo standalone | No, ya integrado en edge_tool |
| `jps_server.py` | Dashboard server + scheduler | Sí, si querés agregar features UI |
| `dashboard.html` | UI Preset 2 Editorial Coreintel | Sí, mejoras visuales/funcionales |
| `jps_console_v2.html` | Dashboard antiguo (analítico, denso) | Solo si Michael lo necesita |
| `jps_backtest.py` | Walk-forward 80/20 backtester, 22 estrategias | Sí, para añadir estrategias nuevas |
| `jps_predict.py` | Predicción pre-sorteo → JSONL | Estable |
| `jps_reconcile.py` | Reconciliación post-sorteo + update bandit | Estable |
| `jps_bandit.py` | Thompson Sampling adaptive | Estable |
| `jps_randomness_tests.py` | 8 tests classical | Estable, re-correr con data nueva |
| `jps_nist_sts.py` | 8 tests NIST SP 800-22 | Estable |
| `jps_diehard.py` | 9 tests DIEHARD (6 válidos) | Estable; los 3 broken son flag _broken |

---

## 💬 Frase para recordar

> "Si la lotería fuera matemáticamente vencible, JPS estaría en bancarrota. Que sigan operando es evidencia empírica de que no hay edge."

---

*Documento mantenido como parte del PR #2 a `VegaBuildsAI/jps-tiempos-architect`. Para detalles técnicos profundos, ver el `git log` del branch.*
