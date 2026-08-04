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

## Aviso importante

Este script se ha escrito sin poder probarlo contra la API real de Instagram
(el entorno donde lo cree tiene bloqueada la salida a `graph.instagram.com`).
La logica sigue la documentacion oficial de Meta, pero **la primera vez**
deberias:

1. Lanzarlo a mano desde GitHub Actions (`workflow_dispatch`, boton "Run
   workflow").
2. Revisar el log de esa ejecucion.
3. Si alguna metrica sale como "no soportada" o similar, dimelo (o pegame el
   error) y ajusto la lista en `scripts/fetch_metrics.py` — son listas
   sueltas (`ACCOUNT_METRICS`, `MEDIA_METRICS`) faciles de tocar.

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

## Y despues, ¿como te asesoro con esto?

Una vez haya unas semanas de datos en `account_metrics.csv` y
`media_metrics.csv`, puedo leerlos cuando quieras y darte un analisis (que
esta funcionando, que dias/formatos rinden mejor, si el alcance esta subiendo
o bajando...). Si quieres que te lo mande yo solo cada semana sin que lo
pidas, dimelo y monto una rutina programada de Cowork para eso.

## Estructura del repo

```
instagram-metrics-bot/
├── scripts/
│   └── fetch_metrics.py       # Pide insights y los anade a los CSV
├── .github/workflows/
│   └── fetch-metrics.yml      # Cron semanal que lo ejecuta
├── account_metrics.csv        # Se crea solo en la primera ejecucion
├── media_metrics.csv          # Se crea solo en la primera ejecucion
├── requirements.txt
├── .env.example
├── .gitattributes
└── .gitignore
```
