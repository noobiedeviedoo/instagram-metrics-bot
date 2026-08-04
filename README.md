# Bot de metricas de Instagram

## Objetivo

Cada semana, pide a la Graph API de Instagram las metricas de tu cuenta
(alcance, visitas al perfil, interacciones...) y de tus publicaciones
recientes (alcance, likes, comentarios, guardados...), y las guarda en dos
CSV dentro de este mismo repo. Con ese historico, Claude (o cualquier otra
herramienta) puede leer la evolucion en el tiempo y darte analisis y consejos
sin tener que llamar a la API cada vez que preguntes.

Es un proyecto independiente del bot de publicacion (`automatizacion-instagram`)
a proposito, aunque los dos hablan con la misma cuenta de Instagram — asi cada
uno tiene su propio repo, su propio cron y sus propios secrets, y un fallo en
uno no afecta al otro.

## Esquema

![Esquema del bot de metricas de Instagram y del analisis semanal por audio](docs/esquema-bot.svg)

## Arquitectura

**Entorno:** GitHub Actions (gratuito, sin mantenimiento, no depende de que
tengas el ordenador encendido) — igual que el bot de publicacion.

**Frecuencia:** cron semanal, lunes 07:00 UTC (`.github/workflows/fetch-metrics.yml`).
Puedes cambiar el cron o lanzarlo a mano desde la pestana Actions de GitHub
(`workflow_dispatch`).

**Datos guardados:**
- `account_metrics.csv` — una fila por metrica de cuenta y ejecucion (alcance,
  visitas al perfil, cuentas alcanzadas, interacciones totales, clics a la
  web, seguidores).
- `media_metrics.csv` — una fila por metrica y publicacion de los ultimos 90
  dias (alcance, reproducciones, likes, comentarios, veces compartida,
  guardados, interacciones totales). Como se vuelve a pedir cada semana para
  las mismas publicaciones, puedes ver como evoluciona un post con el tiempo,
  no solo su valor final.

Ambos CSV son de solo-anadir (append-only), igual que `published_log.csv` del
otro bot, y tienen `merge=union` en `.gitattributes` para que nunca haya
conflictos de git si se ejecuta mas de una vez seguida.

## Estado

- `fetch_metrics.py` — validado contra la API real desde el 2026-08-04 (21
  publicaciones, metricas de cuenta y de posts llegando bien, incluidos
  seguidores reales via `fetch_account_fields()` — la metrica de insights
  `follower_count` no daba datos ni con `metric_type=total_value`, asi que
  seguidores/num. de publicaciones se piden directo al perfil en vez de a
  `/insights`).
- `send_weekly_audio.py` / `speak-analysis.yml` — probado de punta a punta el
  2026-08-04 con un analisis escrito a mano: genero el audio y llego bien por
  Telegram.
- **Pendiente:** la tarea programada semanal de Cowork (el paso 2 del flujo
  de abajo, el que hace que Claude escriba el analisis solo cada lunes sin
  que se lo pidas) todavia no esta creada — el intento de crearla fue
  rechazado, probablemente por guardar el token de GitHub en texto plano
  dentro del archivo de la tarea. Mientras se resuelve, puedes pedirme el
  analisis en cualquier momento en una conversacion normal (ver mas abajo).

## Puesta en marcha (pasos que tienes que hacer tu)

### 1. Crear el repositorio en GitHub

Esta carpeta ya tiene `git init` hecho localmente. Crea un repositorio vacio
en GitHub (puede ser privado) y anade el remoto:

```
git remote add origin <URL-de-tu-repo>
git branch -M main
git push -u origin main
```

### 2. Dar permiso de insights al token

El token que ya usas para publicar (en `token facebook app.txt`, carpeta
`BOTS`) se genero solo con permiso de publicar. Para leer metricas hace falta
ademas:

- `instagram_business_basic`
- `instagram_business_manage_insights`

Ve a tu app en [developers.facebook.com](https://developers.facebook.com/),
revisa los permisos concedidos al token de tu cuenta de Instagram y anade
esos dos si no los tiene, o genera un token nuevo que ya los incluya. Puedes
usar el mismo token para los dos bots (publicar y metricas) o uno distinto
para cada uno — como prefieras.

### 3. Configurar los Secrets del repo

En GitHub: **Settings → Secrets and variables → Actions → New repository
secret**. Anade:

- `IG_ACCESS_TOKEN`
- `IG_BUSINESS_ACCOUNT_ID` (el mismo ID que usas en el otro bot: `17841441636956500`)

### 4. Primera ejecucion manual

Pestana **Actions** del repo → **Recoger metricas de Instagram (programado)**
→ **Run workflow**. Revisa el log como se explica arriba.

## Analisis semanal por audio (Telegram)

Ademas de guardar los datos, cada semana Claude los analiza de verdad (no una
plantilla de numeros) y te manda el resultado como audio a tu bot de Telegram
de texto-a-voz (`tts-telegram-bot`). Son tres piezas encadenadas, cada una
con su propio motivo para existir por separado:

1. **`fetch-metrics.yml`** (lunes 07:00 UTC) — como antes, actualiza los CSV.
2. **Tarea programada de Cowork** (lunes, un rato despues — pendiente de
   crear, ver "Estado" arriba) — Claude hace `git pull`, lee los CSV frescos,
   escribe el analisis en `weekly_analysis.txt` y hace `git push`. Para esto
   Claude usa un token de GitHub *fine-grained*, limitado solo a este repo y
   solo con permiso `Contents: Read and write` (sin `workflow`, a proposito).
   El token se pone en el remote de git justo antes de cada `pull`/`push` y
   se quita otra vez inmediatamente despues — no se queda guardado de forma
   permanente en `.git/config`, porque esa carpeta la compartes tu tambien
   (si el token se quedara puesto, tus propios `git push` fallarian para
   cualquier cambio en `.github/workflows/`, que es justo lo que paso la
   primera vez que se probo).
3. **`speak-analysis.yml`** (lunes 10:00 UTC) — lee `weekly_analysis.txt`,
   genera el audio con `edge-tts` (la misma libreria que usa `tts-telegram-bot`)
   y lo manda por Telegram. Solo manda audio si el texto cambio desde el
   ultimo envio (compara un hash guardado en `.last_sent_analysis_hash`), asi
   que ejecutarlo dos veces seguidas no te duplica el mensaje.

Por que en tres pasos y no uno: el entorno donde corre Claude tiene bloqueado
el acceso a los servicios de texto-a-voz (lo probe con varios, todos
rechazados por la politica de red), pero GitHub Actions tiene internet
completo. Y el analisis en si necesita el razonamiento de Claude, que GitHub
Actions no tiene. Cada pieza corre donde puede hacer su parte.

### Secrets nuevos para este paso

Ademas de `IG_ACCESS_TOKEN` e `IG_BUSINESS_ACCOUNT_ID`, anade en
**Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN` — el token de tu bot `tts-telegram-bot` (el mismo que
  tienes en `BOTS/tts-telegram-bot/.env`).
- `TELEGRAM_CHAT_ID` — tu chat_id de Telegram.

**Aviso de seguridad:** ese mismo token esta ahora mismo escrito en texto
plano en `BOTS/tts-telegram-bot/README.md`, ademas de en su `.env`. No pasa
nada mientras esa carpeta no sea un repo de git publico, pero conviene
quitarlo del README y dejarlo solo en `.env` (que si esta en `.gitignore`).

## ¿Y si quiero pedirte un analisis fuera de la rutina semanal?

Puedo leer `account_metrics.csv` y `media_metrics.csv` cuando quieras, en
cualquier conversacion, y darte un analisis al momento — no hace falta
esperar al lunes.

## Estructura del repo

```
instagram-metrics-bot/
├── scripts/
│   ├── fetch_metrics.py         # Pide insights y los anade a los CSV
│   └── send_weekly_audio.py     # Convierte weekly_analysis.txt a audio y lo manda
├── .github/workflows/
│   ├── fetch-metrics.yml        # Cron semanal: actualiza los CSV
│   └── speak-analysis.yml       # Cron semanal: manda el audio si hay analisis nuevo
├── account_metrics.csv          # Se crea solo en la primera ejecucion
├── media_metrics.csv            # Se crea solo en la primera ejecucion
├── weekly_analysis.txt          # Lo escribe Claude cada semana (tarea de Cowork)
├── .last_sent_analysis_hash     # Lo escribe speak-analysis.yml, evita duplicados
├── requirements.txt
├── .env.example
├── .gitattributes
└── .gitignore
```
