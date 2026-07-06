# JPS Tiempos Lab — Railway Update Brief (Claude Code)

> **Para**: Claude Code (CLI)  
> **Propósito**: Sincronizar Railway con el estado actual del repo, reconfigurar los 3 usuarios reales, y auditar conectividad completa.  
> **Prerequisito**: Estar en el directorio del proyecto. `git status` limpio antes de empezar.

---

## 0 — CARGA DE CONTEXTO (Obsidian Memory — leer en orden, solo lo necesario)

Claude Code **NO debe leer todos los archivos**. Seguir este índice para cargar solo el contexto relevante a la tarea activa:

| Si la tarea es… | Leer primero | Leer solo si hay duda |
|---|---|---|
| Deploy / Railway | este archivo + `railway.json` + `Dockerfile` | `RAILWAY_DEPLOY.md` (sección Troubleshooting) |
| Auth / usuarios | `jps_server.py` líneas 1068–1200 | `jps_db.py` líneas 1–110 |
| Lógica de apuesta | `AUDIT_BRIEF_FOR_CLAUDE_CODE.md` sección "AREAS TO AUDIT" | `jps_edge_tool.py` funciones indicadas |
| Scheduler / auto-pipeline | `jps_server.py` líneas 529–650 | - |
| DB / persistencia | `jps_db.py` completo (183 líneas) | - |
| Reconcile usuario | `jps_reconcile_user.py` | `jps_db.py` |
| Estadísticas / aleatoriedad | `LEARNINGS.md` (veredictos) | `jps_randomness_tests.py` |
| Mecánica del juego | `CLAUDE.md` tabla de mecánica | - |

**Regla de oro**: Si no está en este índice, no lo leas hasta que el error lo requiera.

---

## 1 — ESTADO DEL REPO: QUÉ EXISTE VS QUÉ ESTÁ EN RAILWAY

### Archivos Python en el proyecto (todos deben estar en Dockerfile):

```
jps_accumulate.py       ← en Dockerfile ✓
jps_backtest.py         ← en Dockerfile ✓
jps_bandit.py           ← en Dockerfile ✓
jps_db.py               ← en Dockerfile ✓
jps_diehard.py          ← en Dockerfile ✓
jps_edge_tool.py        ← en Dockerfile ✓
jps_logging.py          ← en Dockerfile ✓
jps_nist_sts.py         ← en Dockerfile ✓
jps_predict.py          ← en Dockerfile ✓
jps_randomness_tests.py ← en Dockerfile ✓
jps_reconcile.py        ← en Dockerfile ✓
jps_reconcile_user.py   ← en Dockerfile ✓
jps_server.py           ← en Dockerfile ✓
simulador.py            ← en Dockerfile ✓
```

**Verificación rápida**: si se agrega cualquier `.py` nuevo, DEBE aparecer en el bloque `COPY` del `Dockerfile` antes de hacer push.

```bash
# Detectar cualquier .py no incluido en el Dockerfile
comm -23 \
  <(ls *.py | sort) \
  <(grep -oP '[\w]+\.py' Dockerfile | sort -u)
```

### Variables de entorno Railway (obligatorias):

| Variable | Valor | Notas |
|---|---|---|
| `JPS_DATA_DIR` | `/data` | Apunta al Volume montado |
| `JPS_USERS` | `user1:pass1,user2:pass2,user3:pass3` | **Ver Sección 2** |
| `JPS_SESSION_SECRET` | string aleatorio largo | Si no existe, se genera y persiste en DB — ok dejarlo vacío primera vez |
| `TZ` | `America/Costa_Rica` | Crítico para que el scheduler corra a la hora correcta |
| `PORT` | (Railway lo inyecta solo) | No tocar |

> ⚠️ **NO configurar** `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` si ya usás `JPS_USERS`. El código los combina, pero tener ambos puede crear un cuarto usuario fantasma.

---

## 2 — CONFIGURACIÓN DE LOS 3 USUARIOS RAILWAY

Los 3 usuarios de Railway son **diferentes** a los del entorno local (`michael:test123, laura:test456`).

### Pasos para reconfigurar:

**2.1 Actualizar variable `JPS_USERS` en Railway:**

Ir a Railway → tu servicio → tab **Variables** → editar `JPS_USERS`:

```
user1_real:password1_real,user2_real:password2_real,user3_real:password3_real
```

Formato exacto: `nombre:password` separados por coma, sin espacios, sin comillas.

**2.2 Verificar aislamiento por usuario:**

Cada usuario en Railway tiene:
- **Sesión propia**: cookie HMAC firmada, tabla `sessions` en SQLite (`jps.db`)
- **Apuestas propias**: tabla `user_bets`, clave `(username, draw_date, session)`
- **Auditorías propias**: tabla `user_audits`, misma clave compuesta
- **Vista compartida**: análisis estadístico, histórico — visible para todos

El código ya soporta esto. No requiere cambios en código, solo en la env var.

**2.3 Validar que el server levanta con 3 usuarios:**

```bash
# Local (docker compose), para probar antes de push:
# Editar docker-compose.yml → JPS_USERS con los 3 usuarios reales
docker compose down && docker compose up --build
# Abrir http://localhost:7788 → probar login con cada uno
```

**2.4 En Railway, después de cambiar la variable:**

Railway hace redeploy automático. Verificar en logs:
```
# No debe aparecer BASIC_AUTH_USER como usuario separado
# Los 3 usuarios deben poder loguearse con sus credenciales
```

---

## 3 — WORKFLOW DE DEPLOY A RAILWAY

Railway detecta pushes a la branch configurada y redespliega automáticamente con el `Dockerfile`.

### 3.1 Secuencia estándar de sync:

```bash
# 1. Verificar estado limpio
git status
git diff --stat

# 2. Asegurarse que el Dockerfile incluye todos los .py nuevos
comm -23 <(ls *.py | sort) <(grep -oP '[\w]+\.py' Dockerfile | sort -u)
# Output vacío = todo incluido. Si hay output, agregar al COPY del Dockerfile.

# 3. Staging y commit
git add -A
git commit -m "feat: <descripción de los cambios>"

# 4. Push a la branch que Railway trackea
git push origin <branch-name>
# Típicamente: carlos/backtest-predict-system
```

### 3.2 Verificar el deploy en Railway:

```bash
# Railway CLI (si está instalado)
railway logs --tail

# O en el dashboard: Deployments → deployment activo → View Logs
# Buscar estas líneas para confirmar que todo levantó:
# [INFO] jps_db inicializado correctamente
# [INFO] Server corriendo en puerto XXXX
```

### 3.3 Smoke test post-deploy:

```bash
DOMAIN="tu-dominio.up.railway.app"

# Health (no requiere auth)
curl https://$DOMAIN/api/status
# Esperado: {"status":"ok", ...}

# Login check
curl -c /tmp/jar -b /tmp/jar \
  -d 'user=user1_real&pass=password1_real' \
  https://$DOMAIN/login
# Esperado: redirect a / (302)

# Datos de usuario
curl -c /tmp/jar -b /tmp/jar https://$DOMAIN/api/my-bets
# Esperado: JSON con array (vacío si no hay apuestas)
```

---

## 4 — AUDITORÍA DE CONECTIVIDAD COMPLETA

Ejecutar en orden. Cada ítem es un CHECK independiente.

### 4.1 Dockerfile — completitud

```bash
# Todos los .py del proyecto están en el COPY
comm -23 <(ls *.py | sort) <(grep -oP '[\w]+\.py' Dockerfile | sort -u)
# PASS: output vacío
# FAIL: aparece algún .py → agregarlo al COPY

# dashboard.html y jps_console_v2.html están incluidos
grep -c "dashboard.html\|jps_console_v2" Dockerfile
# PASS: ≥ 2
```

### 4.2 Imports — todos los módulos internos resuelven

```bash
# Verificar que todos los jps_*.py importados en server existen
python3 -c "
import jps_db, jps_logging, jps_accumulate, jps_reconcile, jps_reconcile_user
print('Todos los imports OK')
"
# PASS: imprime "Todos los imports OK"
# FAIL: ModuleNotFoundError → el archivo falta o tiene error de sintaxis
```

### 4.3 DB schema — tablas presentes

```bash
python3 -c "
import jps_db as db
db.init_db()
import sqlite3, os
conn = sqlite3.connect(os.path.join(db.DATA_DIR, 'jps.db'))
tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
required = {'meta', 'sessions', 'user_bets', 'user_audits'}
missing = required - tables
print('Tablas OK' if not missing else f'FALTAN: {missing}')
"
# PASS: "Tablas OK"
```

### 4.4 Auth — usuarios parseados correctamente

```bash
python3 -c "
import os
os.environ['JPS_USERS'] = 'u1:p1,u2:p2,u3:p3'  # simular 3 usuarios
import importlib, jps_server
importlib.reload(jps_server)
users = jps_server._parse_users()
print(f'Usuarios: {list(users.keys())}')
assert len(users) == 3, f'Esperaba 3, got {len(users)}'
print('Auth OK')
"
# PASS: "Usuarios: ['u1', 'u2', 'u3']" y "Auth OK"
```

### 4.5 Auto-pipeline — flujo sin internet

```bash
# Simular que historical_data.json existe y el pipeline puede analizarlo
python3 -c "
import jps_server, os
# Verificar que trigger_auto existe y tiene firma correcta
import inspect
sig = inspect.signature(jps_server.trigger_auto)
print(f'trigger_auto params: {list(sig.parameters.keys())}')
print('Auto-pipeline función OK')
"
```

### 4.6 Reconcile usuario — por usuario funciona

```bash
python3 -c "
from jps_reconcile_user import reconcile_users
# Dry-run: no debe crashear aunque no haya apuestas pendientes
n = reconcile_users()
print(f'reconcile_users() = {n} apuestas reconciliadas')
"
# PASS: cualquier número ≥ 0 sin exception
```

### 4.7 Edge tool — comandos básicos funcionan

```bash
# Analyze (requiere historical_data.json)
python3 jps_edge_tool.py analyze
# PASS: genera analysis_report.json sin error

# Bet engine
python3 jps_edge_tool.py bet --budget 3000 --n 3 --profile balanced
# PASS: genera input.json + output.json con 3 tickets

# Audit simulado
python3 jps_edge_tool.py audit --exacto 47 --reventada NO
# PASS: genera audit_result.json
```

### 4.8 Archivos huérfanos — no deben estar en ROOT del proyecto

Los siguientes son datos de sesión/temporales y deben existir solo en `/data` (Railway) o `data_local/` (local). Si están en la raíz del repo y `.gitignore` no los excluye, pueden subirse por error:

```bash
# Verificar que .gitignore excluye los JSON de datos
cat .gitignore | grep -E "historical|last_result|analysis_report|bandit_state|predictions_log|output\.json|input\.json|audit_result"
# PASS: cada nombre aparece (o hay un patrón *.json / *.jsonl)
# FAIL: faltan → agregarlos a .gitignore para no commitear datos de producción
```

### 4.9 Volume Railway — datos no en imagen Docker

```bash
# En el Dockerfile NO debe haber COPY de archivos .json de datos
grep "COPY.*\.json\|COPY.*\.jsonl\|COPY.*\.db" Dockerfile
# PASS: output vacío (ningún dato persistente en la imagen)
```

---

## 5 — SEPARACIÓN LOCAL vs RAILWAY (referencia rápida)

| Concepto | Local (docker compose) | Railway |
|---|---|---|
| Usuarios | `michael:test123,laura:test456` | 3 usuarios reales (ver Sección 2) |
| Data dir | `./data_local` → `/data` | Volume `/data` |
| Session secret | `local-dev-secret-not-for-prod` | Generado y persistido en DB |
| Internet JPS API | Disponible | Disponible |
| Fetch desde sandbox Claude | ❌ Bloqueado | N/A |
| Branch | cualquiera | la branch configurada en Railway |
| Auto-redeploy | No (hay que hacer `docker compose up --build`) | Sí (push → build → deploy) |

---

## 6 — CHECKLIST FINAL ANTES DE PUSH

```
[ ] git status → sin archivos no deseados (*.json de datos, *.db, __pycache__)
[ ] Todos los .py nuevos están en el COPY del Dockerfile
[ ] JPS_USERS en Railway tiene exactamente los 3 usuarios correctos
[ ] BASIC_AUTH_USER / BASIC_AUTH_PASS no están configurados en Railway (o son consistentes)
[ ] TZ=America/Costa_Rica está en Railway Variables
[ ] Volume /data sigue montado (verificar en Railway → Settings → Volumes)
[ ] /api/status responde 200 después del deploy
[ ] Los 3 usuarios pueden loguearse independientemente
[ ] user_bets de user A no son visibles para user B (aislamiento)
[ ] reconcile_users() no crashea en producción (verificar logs post-deploy)
```

---

## 7 — TROUBLESHOOTING RÁPIDO

| Síntoma | Causa probable | Fix |
|---|---|---|
| Login redirige a `/login` en loop | Cookie no se setea (HTTPS required para SameSite) | Railway genera HTTPS automático — verificar que la URL sea `https://` |
| `jps_db no disponible` en logs | DB no puede crearse en el path | Verificar que `JPS_DATA_DIR=/data` y el Volume está montado |
| 3 usuarios pero solo 1 puede entrar | `BASIC_AUTH_USER` sobreescribe `JPS_USERS` de forma inesperada | Remover `BASIC_AUTH_USER/PASS` de Variables Railway |
| Deploy falla con "ModuleNotFoundError" | `.py` nuevo no está en `COPY` del Dockerfile | Agregar al bloque COPY y hacer nuevo push |
| `reconcile_users()` falla en Railway | `historical_data.json` no existe en `/data` | Correr fetch desde Railway terminal: `railway run python3 jps_accumulate.py` |
| Datos de un usuario ven datos de otro | Bug en `current_user()` — raro pero posible si hay cookies viejas | Forzar logout de todos: cambiar `JPS_SESSION_SECRET` en Variables |

---

> **Disclaimer**: Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado.
