# GITHUB CLEANUP & VERCEL DEPLOY PLAN
**Para ejecutar en Claude Code — modelo recomendado: claude-opus-4-8 (high)**

---

## CONTEXTO DEL REPO

```
Repo:    https://github.com/VegaBuildsAI/jps-tiempos-architect.git
Rama actual: codex/local-merge-pr-2  (20 commits adelante de origin/master)
Ramas remotas extra: origin/pr-2     (eliminar)
Cambios sin commit: ~25 archivos modificados + archivos nuevos sin trackear
objetivo: todo en master, ramas basura eliminadas, listo para Vercel
```

---

## PASO 1 — Commit de todos los cambios pendientes

En la rama `codex/local-merge-pr-2`, hay modificaciones sin commit y archivos nuevos sin trackear. Hacer un commit que los consolide todos.

```bash
git add -A
git commit -m "chore: consolidate all pending changes — deploy-ready cleanup

- Updated jps_edge_tool.py, simulador.py, jps_server.py
- Added AGENTS.md, tests/ directory, global_frequency_prior.json
- Updated assets/brand CSS, Dockerfile, railway.json
- Added mega_frequency_prior.json, historical_real_* sessions
- Updated documentation: LEARNINGS.md, TECH_SPEC.md, README.md"
```

---

## PASO 2 — Cambiar a master y hacer merge

```bash
git checkout master
git merge codex/local-merge-pr-2 --no-ff -m "merge: integrate all work from codex/local-merge-pr-2 into master

Brings in 20+ commits including:
- Railway + Docker deploy support
- NIST/DIEHARD randomness test battery
- Bandit algorithm and backtest engine
- Auto-scheduler and reconcile system
- Vercel deploy configuration"
```

> **Nota:** Si hay conflictos (poco probable dado que master solo tiene el initial commit), resolverlos aceptando todos los cambios de `codex/local-merge-pr-2` con:
> ```bash
> git checkout --theirs .
> git add -A
> git merge --continue
> ```

---

## PASO 3 — Crear vercel.json

Crear el archivo `vercel.json` en la raíz del proyecto con el siguiente contenido:

```json
{
  "version": 2,
  "builds": [
    {
      "src": "jps_server.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/api/(.*)",
      "dest": "jps_server.py"
    },
    {
      "src": "/dashboard",
      "dest": "jps_server.py"
    },
    {
      "src": "/console",
      "dest": "jps_server.py"
    },
    {
      "src": "/(.*)",
      "dest": "jps_server.py"
    }
  ],
  "env": {
    "PORT": "8080"
  }
}
```

> **Nota importante sobre Vercel + Python server:** `jps_server.py` usa `http.server.HTTPServer` (servidor persistente), lo cual no es compatible directo con el runtime serverless de Vercel. Si el deploy falla, crear `api/index.py` como wrapper WSGI — ver PASO 3B al final de este documento.

---

## PASO 4 — Crear requirements.txt si no existe

Verificar si existe `requirements.txt`. Si no existe, crearlo:

```
requests>=2.31.0
scipy>=1.11.0
numpy>=1.24.0
```

Verificar que todos los imports en `jps_server.py` y `jps_edge_tool.py` estén cubiertos y agregar los que falten.

---

## PASO 5 — Commit de archivos Vercel

```bash
git add vercel.json requirements.txt
git commit -m "feat(vercel): add vercel.json + requirements.txt for Vercel deployment"
```

---

## PASO 6 — Push de master a GitHub

```bash
git push origin master
```

Si rechaza por fast-forward (no debería, master solo tiene 1 commit), forzar:
```bash
git push origin master --force-with-lease
```

---

## PASO 7 — Eliminar rama remota pr-2

```bash
git push origin --delete pr-2
```

Verificar que se eliminó:
```bash
git branch -r
```
El output debe mostrar solo `origin/HEAD -> origin/master` y `origin/master`.

---

## PASO 8 — Eliminar rama local codex/local-merge-pr-2

```bash
git branch -d codex/local-merge-pr-2
```

---

## PASO 9 — Verificación final

```bash
git status          # debe decir: On branch master, nothing to commit
git branch -a       # debe mostrar solo: * master y remotes/origin/master
git log --oneline -5  # verificar que los commits están ahí
git remote -v       # verificar remote correcto
```

Estado esperado:
```
On branch master
Your branch is up to date with 'origin/master'.

nothing to commit, working tree clean
```

---

## PASO 10 — Conectar en Vercel (manual, no automatizable)

Una vez que el repo está limpio en GitHub, el usuario debe:

1. Ir a **vercel.com** → "Add New Project"
2. Importar `VegaBuildsAI/jps-tiempos-architect`
3. Framework: **Other**
4. Root directory: `/` (raíz)
5. Build Command: (dejar vacío)
6. Output Directory: (dejar vacío)
7. Agregar variable de entorno: `PORT = 8080`
8. Click **Deploy**

---

## PASO 3B — Wrapper WSGI para Vercel (si el deploy directo falla)

Si Vercel no puede correr `jps_server.py` como está (porque usa `HTTPServer` bloqueante), crear `api/index.py`:

```python
# api/index.py — WSGI wrapper for Vercel serverless
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from http.server import BaseHTTPRequestHandler
import json

# Import the handler class from jps_server
from jps_server import JPS_Handler

class handler(BaseHTTPRequestHandler):
    """Minimal WSGI-compatible passthrough for Vercel"""
    def do_GET(self):
        JPS_Handler.do_GET(self)
    def do_POST(self):
        JPS_Handler.do_POST(self)
```

Y actualizar `vercel.json` para apuntar a `api/index.py`:

```json
{
  "version": 2,
  "builds": [{ "src": "api/index.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "api/index.py" }]
}
```

Commit y push este cambio también.

---

## RESUMEN DE COMANDOS (secuencia completa)

```bash
# 1. Commit todo en rama actual
git add -A
git commit -m "chore: consolidate all pending changes — deploy-ready cleanup"

# 2. Merge a master
git checkout master
git merge codex/local-merge-pr-2 --no-ff -m "merge: integrate all work into master"

# 3. Crear vercel.json y requirements.txt (Claude los escribe)

# 4. Commit archivos Vercel
git add vercel.json requirements.txt
git commit -m "feat(vercel): add vercel.json + requirements.txt"

# 5. Push master
git push origin master

# 6. Eliminar ramas basura
git push origin --delete pr-2
git branch -d codex/local-merge-pr-2

# 7. Verificar
git status && git branch -a && git log --oneline -3
```

---

*Plan generado: 2026-06-23 | Repo: VegaBuildsAI/jps-tiempos-architect*
