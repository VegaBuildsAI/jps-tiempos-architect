# Auditoría — Componentes OFFLINE en Mission Control

**Fecha:** 2026-07-04
**Dashboard:** https://jps-tiempos.up.railway.app/monitor
**Componentes en estado `no existe`:** Histórico acumulado · Backtest report · Auditoría
**Veredicto:** No es una falla del dashboard ni de red. Los tres artefactos **nunca se generan en el entorno desplegado (Railway)**. La causa es arquitectónica, no un bug de código puntual.

---

## 1. Resumen ejecutivo

El monitor marca un componente como **`no existe`** cuando su archivo JSON no está presente en el filesystem del contenedor de Railway. Los tres componentes offline dependen de archivos que:

1. Están **excluidos de la imagen Docker** (`.dockerignore` bloquea todos los `*.json`).
2. Están **excluidos del repositorio** (`.gitignore`).
3. Los produce **scripts que el pipeline automático de Railway nunca ejecuta**.

Es decir: la única forma de que existan en Railway es que el proceso los escriba en el volumen `/data` en tiempo de ejecución — y el pipeline automático no corre los pasos que los crean. Por eso quedan permanentemente en `no existe`, mientras que los otros 6 artefactos (que sí genera el pipeline) aparecen online.

Nota importante: **los tres archivos SÍ existen en tu máquina local** (workspace), con fechas viejas:
`historical_accumulated.json` (15-may), `backtest_report.json` (02-jun), `audit_result.json` (18-may). Nunca fueron subidos al volumen de Railway, y aunque lo estuvieran, se perderían salvo que vivan en `/data`.

---

## 2. Cómo decide el monitor "existe" vs "no existe"

`jps_server.py` resuelve cada archivo con `_data_path()` y luego `_file_info()`:

```python
DATA_DIR = os.environ.get("JPS_DATA_DIR", HERE)   # en Railway = /data (volumen)

def _data_path(filename):
    p = os.path.join(DATA_DIR, filename)          # 1º busca en el volumen /data
    if os.path.exists(p): return p
    return os.path.join(HERE, filename)            # 2º fallback: /app (imagen)

def _file_info(filename):
    p = _data_path(filename)
    if not os.path.exists(p):
        return {"exists": False, "age_minutes": None}   # → el UI muestra "no existe"
```

El frontend (`FILE_LABELS` + `fileRow`) pinta `no existe` con `if(!f.exists) return '<span class="muted">no existe</span>'`.

Conclusión: un componente cae en `no existe` si el archivo **no está ni en `/data` ni en `/app`**.

---

## 3. Por qué `/app` (la imagen) no tiene los datos

`.dockerignore`:

```
# Datos runtime — viven en el volume, no en la imagen
*.json
!railway.json
...
backtest_report.json
```

Esto excluye **todos los JSON** (salvo `railway.json`) de la imagen Docker. Es una decisión de diseño correcta —los datos deben vivir en el volumen persistente, no hornearse en la imagen— pero significa que `/app` está vacío de datos. Todo depende de lo que el proceso escriba en `/data`.

`.gitignore` refuerza lo mismo: `historical_accumulated.json`, `backtest_report.json` y `audit_result.json` están listados como archivos generados y no viajan por git.

---

## 4. Por qué `/data` (el volumen) tampoco los tiene

El pipeline automático que corre en Railway es `pipeline()` en `jps_server.py`, lanzado por `_run_auto_pipeline()`. Hace exactamente **5 pasos + reconcile**:

| Paso | Acción | Archivo escrito en `/data` |
|---|---|---|
| 1 | Fetch histórico API JPS | `historical_data.json` ✅ |
| 2 | Fetch último resultado | `last_result.json` ✅ |
| 3 | Análisis estadístico | `analysis_report.json` ✅ |
| 4 | Input + `simulador.py` | `input.json`, `output.json` ✅ |
| 5 | Leer output | — |
| + | `jps_reconcile.py` | `predictions_log.jsonl`, `bandit_state.json` ✅ |

**Esos son precisamente los 6 componentes que aparecen ONLINE.**

El pipeline **nunca invoca** los scripts que producen los tres archivos faltantes. Verificado con grep sobre `jps_server.py`: no hay ninguna llamada a `jps_accumulate`, `jps_backtest`, ni al paso `audit`.

---

## 5. Diagnóstico por componente

### 5.1 Histórico acumulado — `historical_accumulated.json`
- **Lo genera:** `jps_accumulate.py` (merge incremental de cada fetch diario).
- **Por qué falta:** el auto-pipeline solo guarda `historical_data.json` (snapshot del fetch), nunca corre el paso de acumulación. Nadie llama a `jps_accumulate.py` en Railway.
- **Caveat de código (bug secundario):** `jps_accumulate.py` escribe con ruta **hardcodeada a `HERE`** (`ACCUM = os.path.join(HERE, "historical_accumulated.json")`), ignorando `JPS_DATA_DIR`. Si se lo agregara al pipeline tal cual, escribiría en `/app` (efímero) y **no** en el volumen — habría que corregirlo para que use `DATA_DIR`.
- **Efecto colateral en el dashboard:** la tarjeta "FRESCURA DATOS" calcula la edad con `min(historical_accumulated, historical_data)`. Al faltar el acumulado, la frescura depende solo de `historical_data` (7.7h). Como 7.7h cae en el rango `240 < edad < 1440 min`, la salud del pipeline es **YELLOW** — que es justo lo que muestra el tablero.

### 5.2 Backtest report — `backtest_report.json`
- **Lo genera:** `jps_backtest.py` (línea 934, `save_json(report, "backtest_report.json")`).
- **Por qué falta:** el backtest es un proceso pesado y de ejecución bajo demanda; no forma parte del pipeline automático y nadie lo dispara en Railway.
- **Buena noticia:** `jps_backtest.py` importa `save_json` de `jps_edge_tool`, que **sí respeta `DATA_DIR`**. Si se ejecuta en Railway, el reporte aterrizaría correctamente en `/data`. (El único que usa `HERE` es el `.md` de resumen, no el JSON que lee el monitor.)

### 5.3 Auditoría — `audit_result.json`
- **Lo genera:** `jps_edge_tool.py audit` (línea 1168), que **requiere el resultado real del sorteo** como input (`--exacto NN --reventada SI/NO`).
- **Por qué falta:** es inherentemente **post-sorteo y manual** — no puede autogenerarse sin conocer el número ganador. El pipeline automático, que corre antes/independiente del sorteo, no tiene con qué producirlo.
- **Buena noticia:** la escritura respeta `DATA_DIR` (`_data_path` en `jps_edge_tool.py`), así que al correr `audit` en Railway el archivo iría a `/data` sin cambios de código.

---

## 6. Tabla resumen de causa raíz

| Componente | Script generador | ¿En imagen? | ¿En git? | ¿Lo corre el auto-pipeline? | Escribe en |
|---|---|---|---|---|---|
| Histórico acumulado | `jps_accumulate.py` | ❌ (`*.json`) | ❌ | ❌ | `HERE` ⚠️ (bug) |
| Backtest report | `jps_backtest.py` | ❌ | ❌ | ❌ | `DATA_DIR` ✅ |
| Auditoría | `jps_edge_tool.py audit` | ❌ | ❌ | ❌ (necesita resultado) | `DATA_DIR` ✅ |

Causa raíz común: **artefactos producidos fuera del alcance del pipeline automático, sobre un entorno donde los datos solo persisten si el proceso los escribe en `/data`.**

---

## 7. Remediación recomendada

### Histórico acumulado (prioridad alta — afecta la salud YELLOW)
1. Corregir `jps_accumulate.py` para que use `DATA_DIR` en lugar de `HERE`.
2. Agregar un paso al auto-pipeline: tras el fetch (paso 1), invocar la acumulación para mergear `historical_data.json` → `historical_accumulated.json` en `/data`.
3. Opcional inmediato: subir una semilla inicial de `historical_accumulated.json` al volumen para arrancar el histórico.

### Backtest report (prioridad media)
- No incluirlo en cada ciclo (es costoso). Mejor:
  - Un botón / endpoint "Correr backtest" en el server que dispare `jps_backtest.py` en background, **o**
  - Un job programado (worker/cron de Railway) semanal que lo regenere en `/data`.
- No requiere cambios de rutas (ya escribe en `DATA_DIR`).

### Auditoría (prioridad baja — por diseño es manual)
- Es correcto que quede vacío hasta que exista un sorteo que auditar. Opciones:
  - Que `jps_reconcile.py` emita un `audit_result.json` al reconciliar cada predicción con su resultado real (auto), **o**
  - Un endpoint/botón de auditoría manual que reciba `exacto` + `reventada`, **o**
  - Aceptar el `no existe` como estado válido "aún sin sorteo auditado" y ajustar el copy del UI para que no parezca error.

### Salvaguarda transversal
- Verificar que el **volumen de Railway esté montado en `/data`** y persista entre reinicios/redeploys. Si el volumen no está montado, incluso los 6 archivos online viven en `/app` efímero y se perderían en cada deploy.

---

## 8. Verificación sugerida

1. En Railway, confirmar el volumen: `railway volume` / panel → debe existir un volumen montado en `/data`.
2. `ls -la /data` dentro del contenedor: deberían verse solo los 6 archivos del pipeline.
3. Tras aplicar los fixes: `historical_accumulated.json` y (bajo demanda) `backtest_report.json` deben aparecer en `/data` y el monitor pasar de `no existe` a online.

> **Disclaimer del proyecto:** Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado. Esta auditoría es sobre infraestructura del tablero, no sobre desempeño de apuestas.
