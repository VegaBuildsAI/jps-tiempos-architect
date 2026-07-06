# JPS Tiempos Lab — Kickoff Brief: Full History + Observability Logs + Betting Module por Usuario

## Cómo usar este documento

Este brief está escrito para arrancar una sesión de **Claude Code (Opus 4.8)** en **Plan Mode**.
Pega o abre este archivo, pide que arme el plan de implementación en base a él, revísalo, y luego
ejecuta. No es un documento de "hazlo ya" — es el contexto que Opus necesita para no reconstruir
cosas que ya existen y no romper el pipeline en producción (Railway).

**Regla no negociable en todo el proyecto:** cualquier output (código, análisis, UI) debe conservar
el disclaimer:
> "Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se
> garantiza ningún resultado."

---

## 0. Estado real del sistema (verificado 2026-07-04, no confundir con CLAUDE.md)

`CLAUDE.md` en la raíz del proyecto describe una versión **desactualizada** del sistema (solo
`jps_edge_tool.py` + `simulador.py`, fetch manual, sin dashboard). La realidad actual es mucho más
grande. Inventario verificado:

| Archivo | Líneas | Rol real |
|---|---|---|
| `jps_edge_tool.py` | 1286 | CLI: fetch/analyze/bet/audit/simulate/run |
| `jps_accumulate.py` | 120 | Acumulador diario — hoy **re-fetchea 180 días completos en cada corrida** y mergea por fecha en `historical_accumulated.json` |
| `jps_predict.py` | 435 | Genera predicción pre-sorteo (14 estrategias + bandit adaptive), la registra en `predictions_log.jsonl` |
| `jps_reconcile.py` | 327 | Cruza `predictions_log.jsonl` (pending/committed) contra resultados reales, actualiza el bandit, emite `audit_result.json` |
| `jps_bandit.py` | 455 | Thompson sampling sobre las estrategias, estado en `bandit_state.json` |
| `jps_backtest.py` | 956 | Walk-forward backtesting + funciones `select_*` (incluye `select_architect`) |
| `jps_server.py` | 2780 | **Dashboard Flask-like** (servidor HTTP propio), desplegado en Railway (`railway.json` → `python3 jps_server.py`). Tiene login multi-usuario, pipeline en vivo, panel de anomalías, panel "Architect", monitor de pipeline/bandit/predictions/analysis/backtest/pnl |
| `jps_diehard.py`, `jps_nist_sts.py`, `jps_randomness_tests.py` | — | Baterías de tests de aleatoriedad sobre el RNG del juego (no tocar en este trabajo salvo que se pida) |

**Ya existe y NO hay que reconstruir:**
- Cálculo de top-25 (`compute_top25()`, `jps_server.py` ~línea 144)
- Detección de anomalías (`compute_anomalies()`, ~línea 201) — chi², z-score, pares reverso, decade bias
- Panel "Architect" en el frontend (`renderArchitect()`, ~línea 2248) — ya combina top25 + anomalías + output de Monte Carlo + budget/profile en una recomendación visual
- `/api/commit` y `/api/skip` (~línea 1221) — el usuario ya puede confirmar un monto real apostado o saltar una predicción, y eso ya viaja a `jps_reconcile.py` vía `actual_bet_per_ticket`
- Login multi-usuario vía `JPS_USERS="user1:pass1,user2:pass2"` (env var, ~línea 1042)
- Monitor de observabilidad parcial: `/api/monitor/pipeline`, `/bandit`, `/predictions`, `/analysis`, `/backtest`, `/pnl`

**Discrepancias de datos encontradas (relevantes para todo lo que sigue):**
- `historical_data.json` local: **61 días** (lista plana)
- `historical_accumulated.json` local: **87 días / 261 sorteos** con slot (`manana`/`mediaTarde`/`tarde`) — desactualizado, `accumulate_log.txt` muestra fallos `403 Forbidden` repetidos desde 2026-05-21 en adelante (el API bloquea el entorno donde corrió eso)
- `predictions_log.jsonl` (único registro real, 2026-06-03) reporta `"n_draws_used_for_analysis": 520` — es decir, en algún punto el sistema (probablemente corriendo en Railway, con internet real) sí llegó a ~520 sorteos. El "526" que menciona el usuario es plausible como cifra actual/reciente del entorno vivo, pero **debe validarse empíricamente**, no asumirse.
- El API de JPS (`https://integration.jps.go.cr`) está bloqueado en el sandbox de Claude y aparentemente también fue bloqueado en el entorno donde corrió el acumulador después del 21-mayo. **Todo fetch real debe probarse donde haya internet real y sin bloqueo** (máquina del usuario o el propio Railway).
- Hallazgo crítico de arquitectura (bloqueante para el punto D): `SESSION_TOKEN` en `jps_server.py` línea 92 es **un solo secreto generado una vez al arrancar el proceso**, y el cookie de sesión que se entrega en `_handle_login_post()` (~línea 1308) es **el mismo para cualquier usuario que haga login exitoso**. Hoy el servidor solo sabe "autenticado: sí/no" — **no sabe cuál de los usuarios de `JPS_USERS` hizo la acción**. Ver Workstream C.
- **No existe una base de datos relacional en ningún lado del proyecto** (se buscó `Dockerfile`, `docker-compose`, y referencias a Postgres/SQLite/MySQL — no hay ninguna). Lo que hoy funciona como "base de datos" es un conjunto de archivos JSON/JSONL planos bajo `JPS_DATA_DIR` (`historical_accumulated.json`, `predictions_log.jsonl`, `bandit_state.json`, `audit_result.json`, etc.). Ese "esquema" **solo tiene datos reales y poblados en el volumen persistente de Railway** — la carpeta local (este repo en OneDrive) tiene copias parciales/desactualizadas, y no hay ningún entorno local corriendo hoy contra ese esquema. Ver Workstream E.
- **Sí existe un `Dockerfile`** en la raíz (usado por Railway vía `railway.json` → `builder: DOCKERFILE`). Corre `python3 jps_server.py`, usa **solo la stdlib de Python** (sin `requirements.txt`), copia archivos `.py`/`.html` **uno por uno por nombre** (no hay `COPY *.py`), lee `JPS_DATA_DIR` (default `/data`) y `PORT` (default `7788`, Railway lo inyecta). El propio comentario del Dockerfile ya documenta el comando para correrlo local con volumen: `docker run -p 7788:7788 -v $(pwd)/data:/data -e JPS_DATA_DIR=/data jps-tiempos` — pero nadie lo ha corrido así todavía (no hay carpeta `data/` local poblada). Ver Workstream E.

---

## 1. Workstream A — Fetch de histórico completo + validación de conteo

**Lo que pide el usuario:** que el fetch traiga TODOS los sorteos del historial (no una ventana de
N días), que se valide que el API realmente entrega ~526 (o el número que sea) sorteos, que el JSON
cargue esos sorteos completos, y que `analyze` corra sobre ese rango total.

**Problema actual:**
- `jps_edge_tool.py fetch --mode history --days 60` (default 60) usa una ventana fija de días desde
  `datetime.now()`. No hay forma de pedir "todo".
- `jps_accumulate.py` usa una ventana fija de **180 días**, re-fetcheada por completo en cada corrida
  (`main()`, líneas 107-117), y luego mergea por fecha contra `historical_accumulated.json`
  (`merge_records()`, líneas 68-97). Esto funciona para "no perder nada dentro de esos 180 días" pero
  nunca alcanza el historial completo si el juego lleva más de 180 días activo, y desperdicia una
  llamada pesada al API cada vez que solo hace falta el resultado del día.

**Diseño propuesto:**

1. **Determinar el rango real disponible.** El API (`/api/App/nuevostiempos/historical`) requiere
   `fechaInicio`/`fechaFin`. No hay documentación de un piso de fecha. Probar empíricamente: pedir un
   rango muy amplio (ej. `fechaInicio=2020-01-01`) y ver qué devuelve — si trunca, pagina, o devuelve
   todo. Confirmar si hay paginación (revisar headers/response de `api_get()`) o si es una sola
   respuesta completa.
2. **Nuevo modo de fetch:** `fetch --mode history --full` (o `--days 0` como sinónimo de "todo") que:
   - Calcule `fechaInicio` como una fecha muy anterior (o la fecha del primer sorteo conocido menos
     margen) y `fechaFin = hoy`.
   - Traiga la respuesta completa y la mergee (no sobreescriba) contra `historical_accumulated.json`
     usando la misma lógica de `merge_records()` de `jps_accumulate.py` (reutilizar esa función en vez
     de duplicarla).
3. **Validación de conteo:** después del fetch completo, contar sorteos (`sum` de slots con `numero`
   no nulo, igual que ya hace `AUDIT_BRIEF_FOR_CLAUDE_CODE.md` sugiere) y compararlo contra:
   - El conteo esperado por calendario (3 sorteos/día × días transcurridos desde el primer día
     conocido, con tolerancia por sorteos sin resultado — `tarde` a veces no cierra).
   - Reportar huecos de fechas (días donde falta 1+ slot) — esto también sirve para el requisito
     de observabilidad (Workstream B).
   - Loggear el resultado de la validación (ej. "526 sorteos recibidos, 0 huecos, rango
     2026-XX-XX → 2026-07-04") en el log diario.
4. **Cambiar la cadencia operativa** (esto es lo que el usuario pide explícitamente: *"que solo vaya
   digiriendo la información del last result cada día"*):
   - **Bootstrap (una sola vez, o cuando se detecte un hueco grande):** `fetch --mode history --full`.
   - **Operación diaria normal:** solo `fetch --mode last` (ya existe, trae el resultado del día) →
     mergear ese único día-registro dentro de `historical_accumulated.json` con `merge_records()` →
     regenerar `historical_data.json` (lista) para que el resto del pipeline (`analyze`, `predict`)
     lo siga consumiendo igual que hoy.
   - Modificar `jps_accumulate.py` para que deje de re-fetchear 180 días cada vez. Debe:
     a. Si `historical_accumulated.json` no existe o tiene huecos grandes (>N días sin datos) →
        fetch completo.
     b. Si ya está poblado y corrió recientemente → solo `fetch --mode last` + merge de un día.
5. **`analyze` ya opera sobre `historical_data.json` completo** (no hay cambio de lógica ahí, solo
   asegurar que ese archivo efectivamente contenga el histórico completo tras el bootstrap).

**Archivos a tocar:** `jps_edge_tool.py` (`cmd_fetch`), `jps_accumulate.py` (`main`, reusar
`merge_records`). Considerar extraer `merge_records()` a un módulo compartido si `jps_edge_tool.py`
también necesita llamarla (hoy solo vive en `jps_accumulate.py`).

**Criterios de aceptación:**
- Correr el fetch completo en un entorno con internet real (no sandbox) produce un
  `historical_accumulated.json` cuyo conteo de sorteos coincide (±tolerancia por sorteos sin cerrar)
  con lo que el usuario ve en el dashboard/API oficial.
- Una corrida diaria posterior NO vuelve a pedir 180 días — solo pide `last` y el conteo total sube
  en como máximo 3.
- `analyze` reporta el nuevo total y no rompe `predict`/`reconcile`/`bandit` (que ya dependen de
  `historical_data.json`).

---

## 2. Workstream B — Logs diarios de observabilidad total del sistema

**Lo que pide el usuario:** *"que guarde los logs de cada día, para observabilidad. Quiero saber el
comportamiento entero del sistema."*

**Estado actual:** solo `accumulate_log.txt`, y solo cubre corridas del acumulador (con mensajes como
`403 Forbidden` repetidos). No hay registro unificado de: fetch manual, `analyze`, `bet`, `predict`,
`commit`/`skip` desde el dashboard, `reconcile`, actualizaciones del bandit, corridas de backtest, ni
errores del servidor.

**Diseño propuesto:**
- Un logger centralizado (ej. `jps_logging.py`) con una función simple `log_event(component, event,
  detail_dict)` que escribe una línea JSONL con timestamp ISO, componente (`fetch`, `accumulate`,
  `analyze`, `predict`, `commit`, `skip`, `reconcile`, `bandit`, `backtest`, `server`), y el detalle.
- Un archivo por día: `logs/YYYY-MM-DD.jsonl` (más fácil de rotar/inspeccionar que un solo log
  gigante). Todos los módulos (`jps_edge_tool.py`, `jps_accumulate.py`, `jps_predict.py`,
  `jps_reconcile.py`, `jps_server.py`) importan y llaman `log_event(...)` en sus puntos clave, en vez
  de (o además de) sus prints actuales.
- Respetar `JPS_DATA_DIR` (igual que el resto de archivos de datos) para que funcione igual en local
  y en Railway.
- Un comando/endpoint `daily-report --date YYYY-MM-DD` (o `/api/monitor/day/<fecha>`) que resuma, a
  partir del log de ese día: qué corrió, qué predicciones se generaron, qué usuarios
  confirmaron/saltearon, qué resultado real hubo, y el ROI del día — esto alimenta directamente la
  auditoría diaria que pide el usuario (tanto la del sistema como, más abajo, la de cada usuario).

**Archivos a tocar/crear:** nuevo `jps_logging.py`; hooks de logging en `jps_edge_tool.py`,
`jps_accumulate.py`, `jps_predict.py`, `jps_reconcile.py`, `jps_server.py`.

**Criterios de aceptación:**
- Cada acción del sistema (fetch, analyze, bet/predict, commit, skip, reconcile, bandit update,
  backtest) deja una línea en el log del día correspondiente.
- `daily-report --date X` reconstruye, sin tener que leer código, "qué pasó ese día" de punta a
  punta.

---

## 3. Workstream C — Identidad de usuario en el dashboard (bloqueante, hacer ANTES de D)

**Hallazgo crítico (ver sección 0):** el sistema de login soporta múltiples usuarios
(`JPS_USERS=user1:pass1,user2:pass2`) pero el cookie de sesión es el mismo token global
(`SESSION_TOKEN`, generado una vez al arrancar el proceso) para cualquier login exitoso. **Hoy no hay
forma de saber qué usuario está detrás de una acción.** Todo el requisito de "auditar por usuario" y
"historial para cada usuario" depende de resolver esto primero — no se puede saltar.

**Diseño propuesto (elegir uno, documentarlo en el plan):**
- **Opción simple:** dict en memoria `{session_token: username}` generado por-login (un token
  aleatorio nuevo por sesión, no uno fijo global), con expiración. Se pierde si el proceso reinicia
  (aceptable para un puñado de usuarios — solo obliga a re-loguearse).
- **Opción persistente:** cookie firmada (HMAC con un secret de servidor) que codifica
  `username + expiry`, verificable sin estado en memoria — sobrevive restarts de Railway.
- En cualquier caso, exponer una función `current_user(headers) -> Optional[str]` reutilizable desde
  cualquier handler.

**Archivos a tocar:** `jps_server.py` (sección de auth: `_parse_users`, `_handle_login_post`,
`_is_authed`, y todos los endpoints que deban saber quién llama).

**Criterios de aceptación:**
- Dos usuarios distintos logueados en dos navegadores/sesiones distintas son distinguibles por el
  servidor en cada request.
- `/api/commit`, `/api/skip`, y el nuevo endpoint de Workstream D pueden anexar `username` a lo que
  guardan.

---

## 4. Workstream D — Betting Module por usuario

**Lo que pide el usuario:** un módulo donde se muestre la apuesta del día — números que el usuario
elige del top-25, anomalías, "architect" (la recomendación/estrategia del sistema), y sesión en
vivo — para observar las apuestas que **cada usuario decide hacer manualmente**. Esto es distinto de
auditar las apuestas automáticas del sistema (eso ya se audita hoy vía `predictions_log.jsonl` +
`jps_reconcile.py` + `jps_bandit.py`). Ahora se quiere un historial **por usuario**, separado.

**Por qué no basta con extender `/api/commit`:** hoy `/api/commit` escribe en el mismo
`predictions_log.jsonl` que usa el bandit para aprender qué estrategia funciona. Si se mezclan ahí
las elecciones manuales del usuario (que pueden diferir de lo que el bandit/estrategia recomendó),
se contamina el aprendizaje del bandit: creería que la estrategia automática ganó o perdió cuando en
realidad el usuario apostó a números distintos.

**Diseño propuesto:**
1. **Archivo separado por usuario:** `user_bets_<username>.jsonl` (mismo patrón append-only que
   `predictions_log.jsonl`).
2. **Nuevo endpoint `/api/user-bet`** (distinto de `/api/commit`), que recibe: `username` (resuelto
   de la sesión — depende de Workstream C, no del payload), fecha del sorteo, sesión
   (`manana`/`mediaTarde`/`tarde`), números elegidos por el usuario (subset del top-25 mostrado ese
   momento), snapshot de las anomalías vigentes (`compute_anomalies()` en ese instante), snapshot de
   la recomendación "Architect" vigente (para poder comparar después qué tan distinto apostó el
   usuario vs. lo que el sistema recomendaba), monto apostado, y timestamp de cuándo se guardó
   (sirve como registro de "sesión en vivo": si el usuario apuesta minutos antes del cierre del
   sorteo vs. horas antes).
3. **Reconciliación separada:** extender `jps_reconcile.py` (o crear `jps_reconcile_user.py` que
   reutilice `payout_ticket()` de `jps_edge_tool.py`, igual que hace el reconcile actual) para cruzar
   `user_bets_<username>.jsonl` contra `historical_data.json`, produciendo
   `user_audit_<username>.json` con el mismo esquema que `audit_result.json` (para reusar cualquier
   render/UI que ya exista para ese esquema).
4. **UI nueva en el dashboard:** sección "Mis Apuestas" / "Betting Module" — muestra el top-25 actual
   con checkboxes para elegir números, el panel de anomalías y el panel Architect (reusar los que ya
   existen: `compute_anomalies`, `renderArchitect`), y debajo el historial de ESE usuario con estado
   WIN/LOSS/pending por sesión, ROI acumulado, y comparación "lo que elegiste vs. lo que el sistema
   recomendaba".
5. **Aislamiento entre usuarios:** cada usuario logueado solo ve y escribe su propio archivo — nunca
   el de otro usuario.

**Archivos a tocar/crear:** `jps_server.py` (nuevo endpoint + sección UI), nuevo
`jps_reconcile_user.py`, nuevos `user_bets_<username>.jsonl` en runtime.

**Criterios de aceptación:**
- Dos usuarios distintos pueden elegir números distintos el mismo día/sesión sin pisarse.
- Después del sorteo, cada usuario tiene su propio `user_audit_<username>.json` con WIN/LOSS y ROI,
  sin afectar el estado del bandit del sistema.
- El dashboard muestra, para el usuario logueado, su selección actual + anomalías + architect + su
  propio historial.

---

## 5. Workstream E — Esquema de datos + entorno local en Docker (paridad con Railway)

**Lo que pide el usuario:** que TODOS los cambios de los Workstreams A–D se puedan probar localmente
—con Docker— antes de cualquier deployment a Railway. Hoy eso no es posible: el "esquema" con datos
reales solo existe poblado en el volumen de Railway, y no hay un entorno local equivalente corriendo.

### 5.1 Documentar el esquema actual (no es SQL — son archivos)

No hay motor de base de datos. El "esquema" es este conjunto de archivos bajo `JPS_DATA_DIR`
(`/data` en Docker/Railway, o el directorio del repo en modo local clásico sin la env var):

| Archivo | Forma | Escrito por | Leído por | Notas |
|---|---|---|---|---|
| `historical_accumulated.json` | `dict[YYYY-MM-DD] → {dia, manana, mediaTarde, tarde}` | `jps_accumulate.py` (`merge_records`) | `jps_accumulate.py` | fuente de verdad de largo plazo |
| `historical_data.json` | `list[{dia, manana, mediaTarde, tarde}]` | `jps_accumulate.py` (derivado del accumulated), o `jps_edge_tool.py fetch` directo | `jps_edge_tool.py`, `jps_predict.py`, `jps_reconcile.py` | working dataset que consume el resto del pipeline |
| `last_result.json` | `{dia, manana, mediaTarde, tarde}` | `jps_edge_tool.py fetch --mode last` | dashboard (`/api/last`) | snapshot del día actual |
| `analysis_report.json` | ver `cmd_analyze()` en `jps_edge_tool.py` | `cmd_analyze` | `cmd_bet`, `jps_predict.py` | **global** — no debe contaminarse con análisis de sesión (bug conocido, ver sección 6) |
| `predictions_log.jsonl` | JSONL append-only, un `id` = `YYYY-MM-DD-session`, líneas `pending`→`committed`→`reconciled` | `jps_predict.py`, `/api/commit`, `/api/skip`, `jps_reconcile.py` | `jps_reconcile.py`, `jps_bandit.py`, dashboard monitor | **sistema**, no tocar desde Workstream D |
| `bandit_state.json` | `{strategies: {nombre: {alpha, beta, n_picks, n_observations, ...}}}` | `jps_bandit.py` | `jps_predict.py` (modo `adaptive`), dashboard `/api/monitor/bandit` | Thompson sampling |
| `audit_result.json` | ver `cmd_audit()` / `_audit_from_reconciled()` | `jps_edge_tool.py audit`, `jps_reconcile.py` | dashboard | última sesión auditada del sistema |
| `backtest_report.json`, `backtest_sessions.json` | ver `jps_backtest.py` | `jps_backtest.py` / `/api/backtest/run` | dashboard monitor | no es parte de este trabajo salvo que se rompa algo |
| `global_frequency_prior.json`, `mega_frequency_prior.json` | ver `_prior_doc()` | `cmd_import_page_stats` | `cmd_analyze` (opcional) | priors oficiales importados a mano |
| `accumulate_log.txt` | texto plano por línea con timestamp | `jps_accumulate.py` | humano | reemplazar/complementar con `logs/YYYY-MM-DD.jsonl` (Workstream B) |
| *(nuevo, Workstream B)* `logs/YYYY-MM-DD.jsonl` | JSONL, `{ts, component, event, detail}` | todos los módulos | `daily-report` | — |
| *(nuevo, Workstream D)* `user_bets_<username>.jsonl` | JSONL append-only, mismo patrón que `predictions_log.jsonl` + campo `username` | `/api/user-bet` | `jps_reconcile_user.py` | aislado por usuario |
| *(nuevo, Workstream D)* `user_audit_<username>.json` | mismo esquema que `audit_result.json` | `jps_reconcile_user.py` | dashboard (sección "Mis Apuestas") | aislado por usuario |

Este inventario **es el "esquema" a versionar y probar en Docker**. Si en algún momento la escritura
concurrente de varios usuarios a sus `user_bets_<username>.jsonl` da problemas (dos requests
simultáneos, corrupción de línea a medio escribir), evaluar migrar solo esa parte a **SQLite** (un
archivo `.db` dentro del mismo `JPS_DATA_DIR`, sin necesidad de un servidor de base de datos aparte,
disponible en la stdlib de Python vía `sqlite3` — consistente con el ethos "solo stdlib" del
Dockerfile actual). Esto es una decisión a tomar en Plan Mode, no algo a implementar por default.

### 5.2 Entorno local en Docker

- Crear `docker-compose.yml` (nuevo, en la raíz) que:
  - Construya desde el `Dockerfile` existente (`build: .`).
  - Monte un volumen local `./data_local:/data` (equivalente al volumen persistente de Railway).
  - Setee `JPS_DATA_DIR=/data`, `PORT=7788`, y `JPS_USERS=<usuarios de prueba>` para poder probar el
    login multi-usuario y, más adelante, el Betting Module por usuario (Workstream C+D) sin tocar
    credenciales reales de producción.
  - Publique el puerto `7788:7788` (igual que el `docker run` que ya documenta el propio Dockerfile).
- **Semilla de datos local:** como el API está bloqueado y `historical_accumulated.json`/
  `historical_data.json` locales están desactualizados (87 días / 61 días respectivamente, contra
  ~520+ en producción), sembrar `./data_local/` con:
  - Opción A (preferida si es viable): una copia real exportada del volumen de Railway (el usuario la
    descarga una vez y la coloca en `./data_local/`).
  - Opción B: los archivos locales actuales tal cual (87 días) — sirven para probar la lógica aunque
    el conteo no sea el de producción — dejando claro que la validación de "526 sorteos" (Workstream
    A) solo se puede confirmar con datos reales, no con esta semilla parcial.
- **Actualizar el `Dockerfile`** cada vez que se agregue un módulo nuevo: hoy copia archivos `.py`
  uno por uno por nombre (no hay `COPY *.py`), así que `jps_logging.py` (Workstream B) y
  `jps_reconcile_user.py` (Workstream D) deben agregarse explícitamente a la lista de `COPY`, o el
  build de Docker (y por lo tanto Railway) no los va a incluir.
- **Flujo de trabajo obligatorio para todo el plan:** implementar cada workstream → levantar
  `docker compose up --build` → probar el flujo completo contra `http://localhost:7788` con la semilla
  local → solo después de validar ahí, hacer commit/push para que Railway redepliegue (mismo
  `Dockerfile`, así que "funciona en el contenedor local" da alta confianza de que funciona igual en
  Railway).

**Archivos a crear:** `docker-compose.yml`, carpeta `data_local/` (gitignored), actualización del
`Dockerfile` a medida que se agreguen módulos.

**Criterios de aceptación:**
- `docker compose up --build` levanta el dashboard en `http://localhost:7788` sin depender del API
  real de JPS.
- Los cuatro workstreams (A–D) se pueden ejercitar completos contra ese contenedor local: fetch
  simulado/semilla → analyze → predict → commit/skip como distintos usuarios → reconcile → ver
  historial por usuario — todo antes de tocar Railway.
- El `Dockerfile` que usa este compose es el mismo que usa `railway.json` (no hay una versión "solo
  para local" divergente que luego sorprenda en producción).

---

## 6. Invariantes que NO deben romperse

- Disclaimer obligatorio en todo output.
- `rev ≤ base` siempre; montos en múltiplos de ₡100; mínimo ₡100 por modalidad.
- Fórmula de EV: `EV = (1/100) × [70 × base + (1/3) × 200 × rev] - (base + rev)`.
- No contaminar `analysis_report.json` global — ya existe un bug conocido relacionado
  (`cmd_session_analyze` sobreescribe y luego restaura el reporte global manualmente; si se toca esa
  zona de código, ver `AUDIT_BRIEF_FOR_CLAUDE_CODE.md` punto 3 y de paso arreglarlo).
- No romper el pipeline existente `predict → commit/skip → reconcile → bandit`.
- El fetch histórico real **no puede probarse en el sandbox de Claude** (API bloqueada, 403). Toda
  prueba de fetch en vivo debe hacerse en Railway o en la máquina del usuario con internet real.
- No perder datos históricos ya acumulados: todo cambio en el fetch/accumulate debe mergear, nunca
  sobreescribir a ciegas `historical_accumulated.json`.
- **Ningún cambio se despliega a Railway sin antes probarse contra el contenedor Docker local**
  (Workstream E). Si algo no se puede probar local (ej. el fetch real contra el API bloqueado), debe
  quedar explícitamente documentado como "pendiente de validar en Railway/máquina del usuario", nunca
  asumido como funcionando.

---

## 7. Orden de ejecución sugerido

1. **Fase 0a — Entorno local en Docker (Workstream E).** Levantar `docker-compose.yml` + semilla de
   datos local. Esto habilita poder probar TODO lo demás sin tocar Railway. Hacer esto primero.
2. **Fase 0b — Diagnóstico empírico.** Correr (el usuario, en su máquina o en Railway, no en el
   contenedor local si no tiene salida a internet real) un fetch con rango amplio y
   `fetch --mode last`, para confirmar el conteo real de sorteos disponibles y la fecha más antigua
   que el API entrega. Esto confirma o corrige la cifra "526".
3. **Fase 1 — Workstream A** (fetch completo + validación + cadencia diaria) — implementar y probar
   contra el contenedor Docker local con la semilla de datos.
4. **Fase 2 — Workstream B** (logging centralizado) — puede ir en paralelo con la Fase 1, tiene poca
   dependencia. Probar también en el contenedor local.
5. **Fase 3 — Workstream C** (identidad de usuario) — bloqueante para la Fase 4. Probar con los
   usuarios de prueba definidos en el `docker-compose.yml`.
6. **Fase 4 — Workstream D** (Betting Module por usuario) — probar con al menos 2 usuarios de prueba
   distintos en el contenedor local antes de tocar Railway.
7. **Fase 5 — Deployment.** Solo después de validar las Fases 1–4 en el contenedor local: commit,
   push, dejar que Railway redepliegue con el mismo `Dockerfile`, y correr el bootstrap de historial
   completo (Workstream A) una vez ahí, contra datos reales.
8. **Fase 6 — Actualizar `CLAUDE.md`** (y opcionalmente `AUDIT_BRIEF_FOR_CLAUDE_CODE.md`) para que
   reflejen el sistema real (dashboard, predict/reconcile/bandit, logging, multi-usuario, Docker
   local) en vez de la versión vieja de solo CLI.

---

## 8. Preguntas abiertas para resolver en Plan Mode antes de programar

- ¿Cuántos usuarios reales van a usar el Betting Module — solo Michael, o hay más personas con cuenta
  en `JPS_USERS`?
- El "top 25" que el usuario va a elegir: ¿top-25 Exacto solamente, el combinado (75% Exacto / 25%
  Mega), o debe poder alternar entre ambos?
- ¿Qué debe capturar exactamente "sesión en vivo" — basta el timestamp de cuándo se guardó la
  apuesta, o se necesita un estado explícito de "sesión abierta/cerrada" que se cierra automáticamente
  al llegar la hora del sorteo?
- ¿Dónde vive el `JPS_DATA_DIR` real en producción (volumen de Railway)? ¿Claude Code tiene forma de
  desplegar y correr el backfill ahí, o el usuario debe correr el bootstrap una vez manualmente tras
  el deploy?
- ¿Alcanza con que la sesión de usuario dure mientras el proceso de Railway esté vivo, o se necesita
  que sobreviva a un restart (Railway reinicia procesos con `restartPolicyType: ALWAYS`)?
- Para la semilla de datos del entorno Docker local (sección 5.2): ¿el usuario puede exportar/copiar
  el `historical_accumulated.json` real desde el volumen de Railway, o hay que trabajar solo con la
  copia local desactualizada (87 días) mientras se prueba la lógica?
- ¿Vale la pena migrar `user_bets_<username>.jsonl` (y quizás `predictions_log.jsonl`) a SQLite para
  evitar problemas de escritura concurrente entre usuarios, o el archivo plano append-only alcanza
  para el volumen de uso real (pocos usuarios, pocas escrituras por día)?

---

**Disclaimer:** Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio.
No se garantiza ningún resultado.
