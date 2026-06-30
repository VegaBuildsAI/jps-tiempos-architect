# JPS Tiempos Lab — Manifesto

> **"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio.
> No se garantiza ningún resultado."**

---

## Por qué existe esta herramienta

El juego Nuevos Tiempos Reventados de JPS Costa Rica es, por diseño, un sistema aleatorio puro.
Cada sorteo es independiente. Ningún número tiene memoria. Ningún patrón garantiza nada.

Y aún así — los datos existen. Los sorteos quedan registrados. La historia es consultable.

Esta herramienta nació de una pregunta honesta: *si ya existen datos históricos, ¿qué información real se puede extraer de ellos, y cuál es el límite de lo que esa información puede decir?*

La respuesta define el espíritu del proyecto: **análisis riguroso dentro de límites honestos**.

---

## Los tres principios

### 1. Transparencia estadística

Toda decisión de apuesta que esta herramienta sugiere es trazable hasta su origen matemático.
Los z-scores, el chi-cuadrado, las frecuencias, los pesos bayesianos — todo está expuesto.
No hay "magia" ni "intuición computacional". Solo estadística clásica aplicada con honestidad.

### 2. Rigor sobre optimismo

Es fácil construir una herramienta que muestre solo lo que el usuario quiere ver.
Este proyecto elige lo contrario: el EV esperado siempre es negativo (como en todo juego de azar con house edge), y eso se muestra sin disculpas. La mediana del Monte Carlo es pérdida. El upside real es el P95, no el promedio. Esa información se entrega completa, no suavizada.

### 3. Perspectivas múltiples sobre certezas únicas

No existe el "mejor número". Existen perspectivas:
- **Set A** (frecuencia histórica + Monte Carlo) dice una cosa.
- **Set B** (peso + tasa de Reventada) dice otra.
- **Set C** (espejo de dígitos) introduce una dimensión diferente.
- **Set D** (The Genie: anomalías + desplazados) explora el borde.

Cuatro perspectivas independientes. El jugador decide cuál — o cuántas — usar.

---

## Qué no es esta herramienta

- **No es un predictor.** Ningún sistema puede predecir números de lotería. Esta herramienta no lo intenta.
- **No es un sistema ganador.** El EV de cada ticket es negativo en expectativa. Eso no cambia.
- **No es un consejo financiero.** Jugar debe estar dentro de lo que el jugador puede perder.

---

## Qué sí es

Una **consola de análisis estadístico** que convierte datos históricos en decisiones de apuesta *informadas y auditables*, para un jugador que ya ha decidido jugar y quiere hacerlo con el máximo de información disponible.

La diferencia entre apostar a ciegas y apostar con datos no es la probabilidad de ganar — sigue siendo 1/100 por número. La diferencia es que el segundo jugador entiende exactamente lo que está haciendo y por qué.

---

## Arquitectura de la honestidad

Cada componente del sistema tiene una responsabilidad definida:

| Capa | Responsabilidad |
|---|---|
| **Acumulador** | Preservar la historia sin corromperla |
| **Analizador** | Medir desviaciones, no inventarlas |
| **Motor de apuestas** | Optimizar dentro de restricciones reales del juego |
| **Monte Carlo** | Cuantificar riesgo, no esconderlo |
| **Auditor** | Rendir cuentas después de cada sorteo |
| **The Architect** | Ofrecer perspectivas, no certezas |

---

## El disclaimer como principio

El disclaimer no es letra pequeña. Es el centro del manifesto.

> *"Todos los números tienen exactamente la misma probabilidad en un sistema aleatorio. No se garantiza ningún resultado."*

Aparece en el código. Aparece en la interfaz. Aparece en este documento. Permanece en todo output generado por el sistema. No se elimina. No se suaviza.

---

## Visión a largo plazo

Este proyecto es un laboratorio vivo. La historia acumulada crece con cada sorteo. Los métodos estadísticos pueden evolucionar. La interfaz puede mejorar.

Lo que no cambia: el compromiso con la transparencia, el rigor matemático, y la honestidad sobre los límites de lo que cualquier análisis histórico puede decir sobre el futuro de un sistema aleatorio.

---

## El veredicto empírico

El laboratorio no se quedó en la teoría. Se construyó una batería de 22 tests estadísticos independientes — clásicos, NIST SP 800-22, DIEHARD — para responder una pregunta concreta: ¿el RNG de JPS tiene algún sesgo explotable?

**Los 22 tests aceptan H0.** Ninguno encuentra desviación de aleatoriedad pura. Ningún backtest de estrategia sobrevive corrección por comparaciones múltiples. El EV permanece fijo en -30% para Exacto, peor para Reventados y Mega.

Esto no es una limitación del proyecto — es su validación más fuerte. El Principio 2 (Rigor sobre optimismo) no es una pose: es lo que el proyecto encontró cuando se puso a prueba a sí mismo, con las mismas herramientas con las que analiza los sorteos.

> *"Si la lotería fuera matemáticamente vencible, JPS estaría en bancarrota. Que sigan operando es evidencia empírica de que no hay edge."*

La recomendación que se sigue de esto es directa: este sistema es para **paper trading y ejercicio académico**, no para apostar dinero real esperando vencer la casa. Quien use The Architect para apostar, apuesta sabiendo exactamente cuál es su desventaja matemática — no por ignorarla.

---

## De laboratorio local a servicio vivo

El sistema ahora corre 24/7, accesible desde cualquier navegador, con sesión y login. Este cambio es de **infraestructura**, no de filosofía — y vale la pena decirlo explícitamente para que nunca se confundan.

- **El login no es exclusividad, es higiene operativa.** Antes el laboratorio vivía en una laptop; ahora vive en un servidor público. Una pantalla de acceso es lo mínimo razonable para algo que escribe en un volumen persistente y consulta un API externo en nombre de quien sea que lo abra.
- **El pipeline corre solo, pero no decide solo.** Que el sistema haga fetch, analice y simule automáticamente al entrar no cambia su naturaleza: sigue siendo un espejo de los datos históricos, no un oráculo. La automatización ahorra clics, no incertidumbre.
- **La disponibilidad no es una promesa de resultado.** Que The Architect esté siempre prendido y respondiendo en segundos no implica que esté más cerca de "saber" qué número va a salir. El veredicto del §"Veredicto empírico" no cambia porque el servidor nunca se apague.

El laboratorio creció en alcance. No creció en certeza — porque no podía: la aleatoriedad no negocia con la infraestructura.

---

*JPS Tiempos Lab — análisis estadístico para Nuevos Tiempos Reventados, Costa Rica.*
*Construido con Python, estadística clásica, y respeto por la aleatoriedad.*
