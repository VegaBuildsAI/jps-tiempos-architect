# JPS Tiempos Architect — Resumen Ejecutivo

**Versión:** 3.0 · **Estado:** Desplegado en vivo (Railway)
**Juego:** Nuevos Tiempos Reventados — Junta de Protección Social, Costa Rica

> **"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."**

Este documento resume qué es el sistema hoy, qué encontró cuando se puso a prueba a sí mismo, y cómo opera en producción. Para el detalle técnico ver `TECH_SPEC.md`; para la filosofía del proyecto, `MANIFESTO.md`; para el historial de hipótesis probadas, `LEARNINGS.md`.

---

## Qué es

Una consola de análisis estadístico — CLI + dashboard web — que convierte datos históricos reales de Nuevos Tiempos Reventados en decisiones de apuesta auditables. No predice números. Mide desviaciones contra el modelo aleatorio teórico, construye tickets bajo restricciones de presupuesto, cuantifica riesgo con Monte Carlo, y audita cada resultado.

Hoy es un **servicio web desplegado**, no solo un script local: corre 24/7, tiene login, y ejecuta su propio pipeline automáticamente cada vez que alguien entra.

---

## El hallazgo central (la parte que no se puede suavizar)

Antes de construir nada de bet-engine, el proyecto se hizo la pregunta honesta: ¿el generador de números de JPS tiene algún sesgo real? Se corrieron **22 tests estadísticos independientes** sobre la secuencia histórica:

| Batería | Tests | Resultado |
|---|---|---|
| Clásicos + teoría de la información (chi², Ljung-Box, FFT espectral, compresión, entropía aproximada) | 8 | Acepta H0 |
| NIST SP 800-22 Rev 1a (estándar criptográfico) | 8 | Acepta H0 |
| DIEHARD (Marsaglia), tests válidos | 6 | Acepta H0 |

**Los 22 aceptan H0.** No hay sesgo por número, no hay autocorrelación temporal, no hay periodicidades, la secuencia no es comprimible. El RNG de JPS es indistinguible de aleatoriedad pura — pasa el mismo estándar que se usa para validar generadores criptográficos.

**Consecuencia matemática directa:**
- El house edge es fijo: **-30% en expectativa para Exacto puro**, peor para Reventados (-33%) y Mega (-44%).
- Ninguna estrategia de selección de números — ni las 22 evaluadas en el backtest, ni el bandit adaptativo, ni The Architect — puede tener EV positivo en expectativa. Esto se confirmó empíricamente, no solo se asumió por diseño.
- Cualquier "edge" que aparezca en una ventana de backtest pequeña es ruido estadístico garantizado a converger a -30% con más datos.

**Recomendación operativa:** el sistema está pensado para **paper trading y ejercicio académico** de gestión de riesgo — no para apostar dinero real esperando vencer la casa.

---

## Qué hace el sistema (capacidades)

| Capacidad | Módulo | Resumen |
|---|---|---|
| Ingesta de datos en vivo | `jps_edge_tool.py`, servidor | Consulta el API oficial de JPS (histórico + último resultado) |
| Análisis de frecuencias | motor estadístico | Z-scores, chi-cuadrado, pesos bayesianos, pares reverso |
| Motor de apuestas | `jps_edge_tool.py bet` | Construye tickets bajo presupuesto y perfil de riesgo (conservative/balanced/aggressive) |
| Simulación de riesgo | `simulador.py` | Monte Carlo (20,000+ trials) — mediana, P5, P95, probabilidad de ganar |
| **The Architect** | dashboard | 4 perspectivas independientes (Sets A–D: estrategia, frecuencia, reverso, anomalías) sobre los mismos datos |
| Backtesting walk-forward | `jps_backtest.py` | 22 estrategias validadas 80/20 sin data leakage |
| Predicción + reconciliación en vivo | `jps_predict.py` / `jps_reconcile.py` | Track record append-only, auditado contra resultados oficiales |
| Selección adaptativa | `jps_bandit.py` | Thompson Sampling entre estrategias, basado en su historial real de aciertos |
| Validación de aleatoriedad | `jps_randomness_tests.py`, `jps_nist_sts.py`, `jps_diehard.py` | Los 22 tests descritos arriba |
| Auditoría post-sorteo | motor de auditoría | Reconcilia cualquier ticket contra el resultado oficial: hit, recuperado, neto, ROI |

---

## Cómo opera hoy

1. **Acceso:** pantalla de login con sesión por cookie; soporta múltiples usuarios. El acceso es control operativo, no un producto comercial.
2. **Al entrar:** el dashboard dispara automáticamente el pipeline completo (fetch JPS → análisis → tickets → Monte Carlo → reconciliación), con caché de 10 minutos para no saturar el API oficial. Un overlay muestra el progreso en vivo.
3. **Sin intervención:** un scheduler interno corre predicción y reconciliación tres veces al día, alineado a los tres sorteos oficiales (mañana, media tarde, tarde).
4. **Persistencia:** todo el historial — predicciones, resultado del bandit, backtests — vive en un volumen persistente, independiente del ciclo de vida del contenedor.

---

## Arquitectura en una página

```
Usuario → Login (sesión) → Dashboard (auto-run) → Pipeline (fetch JPS → análisis → MC)
                                                         ↓
                                    Backtest · Predict/Reconcile · Bandit · Validación RNG
                                                         ↓
                                          Volumen persistente (historial, track record)
```

Desplegado en Railway sobre Docker: build reproducible, healthcheck automático, reinicio ante fallo, escalable a más usuarios sin cambios de código.

---

## Riesgos y uso responsable

- **No es un sistema ganador.** El EV es negativo por diseño matemático del juego, y eso se confirmó empíricamente sobre datos reales (sección "Hallazgo central").
- **No es asesoría financiera.** Cualquier monto jugado debe estar dentro de lo que la persona puede perder sin consecuencias.
- **No predice resultados.** Toda salida del sistema es análisis de datos pasados contra un modelo aleatorio — nunca una afirmación sobre el futuro.
- El disclaimer central del proyecto aparece en cada interfaz, cada documento, y cada archivo generado, sin excepción.

---

## Repositorio y operación

- **Código:** `VegaBuildsAI/jps-tiempos-architect` (GitHub, rama `master`)
- **Despliegue:** Railway, build vía `Dockerfile` + `railway.json`, volumen persistente en `/data`
- **Stack:** Python stdlib puro (sin dependencias externas), JavaScript vanilla en el dashboard — sin frameworks, sin build step

Para credenciales de acceso y configuración de entorno, ver las variables del proyecto en Railway — no se documentan en este archivo por tratarse de un repositorio público.
