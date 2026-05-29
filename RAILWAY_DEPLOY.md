# Deploy a Railway · Guía paso a paso

> Lleva el sistema 24/7 a la nube de Railway con persistencia (Volume),
> autenticación Basic, y auto-restart. Tu Mac puede apagarse y el sistema
> sigue prediciendo y reconciliando solo.

**Tiempo estimado**: 30-45 minutos
**Costo estimado**: $3-5 USD/mes (Railway Hobby plan)

---

## 📋 Pre-requisitos

- [ ] Cuenta en [Railway](https://railway.app) (puede ser con GitHub)
- [ ] Tu fork de GitHub ya pusheado al día (`cleonenrique-jpg/jps-tiempos-architect`)
- [ ] Tarjeta de crédito vinculada en Railway (necesaria aunque el plan tenga free credit)
- [ ] Los archivos locales que querés migrar:
  - `predictions_log.jsonl`
  - `bandit_state.json`
  - `historical_data.json`

---

## 🚀 Paso 1: Crear el proyecto en Railway

1. **Login**: https://railway.app → "Login with GitHub"
2. **New Project** → **Deploy from GitHub repo**
3. Autorizar Railway a leer `cleonenrique-jpg/jps-tiempos-architect`
4. Seleccionar el repo y la branch `carlos/backtest-predict-system`

Railway detecta automáticamente el `Dockerfile` que ya tenés en el repo y empieza el primer build.

---

## 💾 Paso 2: Crear el Volume (CRÍTICO — no saltarse)

Sin esto, los datos se borran en cada redeploy.

1. En tu proyecto Railway, click en el service que se creó
2. Tab **Variables** → no toques nada aún, vamos a **Settings**
3. Tab **Settings** → scroll hasta **Volumes** → **+ New Volume**
4. Mount Path: `/data`
5. Size: 1 GB (suficiente, sobra mucho)
6. **Create**

---

## 🔐 Paso 3: Variables de entorno

En tab **Variables** del service, agregar:

| Variable | Valor | Descripción |
|---|---|---|
| `JPS_DATA_DIR` | `/data` | Donde el código guarda JSONL/JSON (mismo que Volume mount) |
| `BASIC_AUTH_USER` | `carlos` (o el que quieras) | Usuario para acceder al dashboard |
| `BASIC_AUTH_PASS` | un password fuerte | Password — usá uno largo y random |
| `PORT` | `7788` | Railway lo pone automáticamente pero por las dudas |
| `TZ` | `America/Costa_Rica` | Para que el scheduler use hora local de CR |

⚠️ **Sin `BASIC_AUTH_*` el dashboard queda PÚBLICO**. Cualquiera con la URL puede ver tu data y confirmar apuestas en tu nombre.

---

## 🚢 Paso 4: Deploy y verificación

1. Railway redespliega automáticamente al cambiar variables
2. Ir a tab **Deployments** → ver el log del último deploy
3. Esperar a que diga `[SCHED ...] Scheduler iniciado · 6 slots/día`

### Obtener la URL pública

1. Tab **Settings** → **Networking** → **+ Generate Domain**
2. Te da algo tipo `jps-tiempos-architect-production.up.railway.app`
3. Abrila — deberías ver el prompt de Basic Auth

### Smoke test

```bash
# Healthcheck (sin auth)
curl https://TU-DOMINIO/api/status
# → debe responder con JSON

# Con auth (reemplaza user:pass)
curl -u carlos:tupassword https://TU-DOMINIO/predictions_log.jsonl
# → debe devolver el JSONL (vacío si recién deployaste)
```

---

## 📦 Paso 5: Migrar tus datos locales al Volume

Tu data actual está en tu Mac. Hay que subirla al Volume de Railway.

### Opción A: Railway CLI (recomendada)

```bash
# Instalar Railway CLI
brew install railway   # macOS
# o: npm i -g @railway/cli

# Login
railway login

# Linkear al proyecto
cd "/Users/carlosleon/VS code - trabajos/Proyectos/jps-tiempos-architect "
railway link
# → seleccionar tu proyecto

# Copiar archivos al volumen (usando shell del container)
railway run --service jps-tiempos-architect bash
# Dentro del container:
ls /data    # vacío inicialmente
exit

# Copiar archivos via SSH al container (alternativo)
railway ssh
```

### Opción B: Endpoint de upload temporal (más simple sin CLI)

Activá temporalmente este endpoint en el server agregando este código a `jps_server.py`,
deploya, hacé el upload, y removelo:

```python
# DENTRO de do_POST, agregar antes del else final:
elif parsed.path == "/api/upload-data":
    # SOLO HABILITAR TEMPORALMENTE PARA MIGRACIÓN
    filename = payload.get("filename")
    content = payload.get("content")
    if filename and content and filename in ("predictions_log.jsonl", "bandit_state.json", "historical_data.json"):
        with open(os.path.join(DATA_DIR, filename), "w") as f:
            f.write(content)
        self.send_json({"ok": True, "saved": filename})
```

Después de upload, **borrá este endpoint** para evitar inyección.

### Opción C: Re-generar desde cero (más simple si no te importa empezar fresh)

1. Una vez deployado, el server arranca con data vacía
2. El primer scheduler tick a las 13:30 va a hacer `fetch --days 180` → recupera histórico
3. El bandit empieza con prior uniforme; con días de paper trading aprende solo
4. Perdés el track record actual (1 apuesta confirmada → ya está perdida igual, no importante)

**Recomendación**: Opción C es la más simple. Tu sistema está al inicio del paper trading real, no hay tanto que perder.

---

## 🕐 Paso 6: Verificar el scheduler

Después del primer día, mirar logs:

1. Railway dashboard → tu service → tab **Deployments** → click en deployment activo → **View Logs**
2. Buscar líneas con `[SCHED ...]`
3. Deberías ver eventos en los slots 12:05 / 13:30 / 15:40 / 17:00 / 18:40 / 20:00

⚠️ **Importante**: la timezone del container depende de `TZ`. Si `TZ=America/Costa_Rica`, los horarios coinciden con sorteos JPS reales. Si no setás `TZ`, Railway usa UTC y el scheduler corre 6 horas tarde.

---

## 🌐 Paso 7: Acceder desde el teléfono

1. Tu URL es algo como `https://jps-tiempos-architect-production.up.railway.app`
2. Abrila en Safari/Chrome móvil
3. Te va a pedir user/password (el del Basic Auth)
4. Dashboard responsive — funciona en móvil sin problemas

**Tip**: Agregarla como atajo en pantalla de inicio para acceso rápido.

---

## 🛡️ Paso 8: Seguridad básica

### Si el password de Basic Auth se compromete

1. Railway → Variables → cambiar `BASIC_AUTH_PASS`
2. Redeploy automático
3. Las sesiones del browser piden auth de nuevo

### Si querés cerrar el público temporalmente

```bash
railway service:stop
```

O en Railway dashboard → Settings → Suspend.

---

## 💰 Paso 9: Monitorear costo

1. Railway → Project Settings → **Usage**
2. Ver $ gastados del mes en curso
3. Setear alerta de costo si querés (Project → Settings → Usage Limits)

**Estimación realista**:
- Container 24/7: ~$3/mes
- Volume 1 GB: ~$0.25/mes
- Egress (tráfico salida): negligible para uso personal
- **Total ~$3.5/mes** = ₡1,800/mes

---

## 🚨 Troubleshooting

### "Application failed to respond"
- Logs muestran que el container arrancó pero no responde en 7788
- Causa probable: el código usa `PORT` env var pero no fue picked up
- Solución: asegurarse que el `CMD` del Dockerfile usa `${PORT:-7788}`

### "Healthcheck failing"
- `railway.json` define `healthcheckPath: /api/status`
- Si requires auth, el healthcheck falla. Ya está EXCLUIDO de auth en el código.
- Verificar logs: `[SCHED ...] Scheduler iniciado` debe aparecer

### "Volume mostly empty after redeploy"
- Volume está mountado pero `JPS_DATA_DIR` env var no apunta a `/data`
- Verificar Variables tab: `JPS_DATA_DIR=/data`

### El scheduler no corre a las horas esperadas
- TZ del container es UTC. Setear `TZ=America/Costa_Rica` en Variables.
- Después de cambiar TZ, redeploy y verificar logs.

### "predict failed con error vacío"
- Ya está arreglado: el server captura stdout+stderr ahora
- Si vuelve a pasar, revisar logs del deploy y buscar `[SCHED ... ✗ predict failed]`

---

## 📚 Referencias

- [Railway Volumes docs](https://docs.railway.com/reference/volumes)
- [Railway Variables docs](https://docs.railway.com/reference/variables)
- [Railway CLI docs](https://docs.railway.com/reference/cli-api)
- [Healthcheck config](https://docs.railway.com/reference/healthchecks)

---

## ✅ Checklist final

- [ ] Proyecto creado en Railway con repo conectado
- [ ] Volume `/data` (1 GB) creado y mounted
- [ ] Variables `JPS_DATA_DIR`, `BASIC_AUTH_USER`, `BASIC_AUTH_PASS`, `TZ` configuradas
- [ ] Domain público generado
- [ ] Smoke test con curl pasa
- [ ] Browser muestra prompt Basic Auth y dashboard al loguearse
- [ ] Logs muestran `Scheduler iniciado · 6 slots/día`
- [ ] Después de 24h, logs muestran al menos un ciclo predict + reconcile completo

---

> ⚠️ **Caveat final**: este deploy te da infraestructura, NO edge. Recordá el verdict
> de los tests de aleatoriedad (LEARNINGS.md): el RNG de JPS es indistinguible de
> aleatorio puro. Esperar -30% ROI en expectativa con apuestas reales. Lo que ganás
> con Railway es **paper trading confiable 24/7**, no plata.
