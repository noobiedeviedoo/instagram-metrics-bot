#!/usr/bin/env python3
"""
Analiza los CSV de metricas y escribe el resultado en weekly_analysis.txt,
usando la API de Claude directamente (Messages API) — sin pasar por Cowork,
para que todo el flujo semanal dependa solo de GitHub Actions.

Pensado para correr como paso extra dentro de fetch-metrics.yml, justo
despues de fetch_metrics.py y antes del commit final, para que
weekly_analysis.txt quede en el mismo commit que los CSV que acaba de leer.

Variables de entorno requeridas:
    ANTHROPIC_API_KEY   - API key de Anthropic (console.anthropic.com)

Variables de entorno opcionales:
    ANTHROPIC_MODEL     - modelo a usar (por defecto claude-sonnet-5)
"""

import csv
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"
ANALYSIS_PATH = REPO_ROOT / "weekly_analysis.txt"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Cuantas fotos/snapshots como maximo se le mandan a Claude — de sobra para
# el uso real (21 posts, unas pocas semanas de snapshots de cuenta), y evita
# que el prompt crezca sin limite a medida que los CSV se acumulan semana a
# semana (son append-only, solo crecen).
MAX_ACCOUNT_SNAPSHOTS = 12
MAX_MEDIA_POSTS = 40

PROMPT_TEMPLATE = """Eres un analista que le prepara a Miguel Ángel un resumen semanal hablado \
sobre el rendimiento de su cuenta de Instagram. Te paso dos resumenes ya procesados (no CSV en \
bruto): uno con los ultimos snapshots de metricas de cuenta, ordenados del mas antiguo al mas \
reciente, y otro con el ESTADO ACTUAL de cada publicacion (el dato mas reciente de cada una, no \
el historial completo), ordenado de la publicacion mas reciente a la mas antigua.

Tu tarea: escribir un analisis en espanol, de 150 a 250 palabras, en PROSA CONTINUA — nada de \
markdown, viñetas, tablas, emojis ni encabezados, porque este texto lo va a leer en voz alta un \
motor de texto a voz tal cual se lo mandes. Debe sonar como si se lo contaras a Miguel en persona: \
empieza con una frase resumen y luego desarrolla 3 o 5 puntos concretos y utiles.

Cosas que deberias mirar si los datos lo permiten (usa el 'followers_count' del ultimo snapshot \
de cuenta como referencia de audiencia):
- Como esta el alcance/interacciones del ultimo snapshot de cuenta frente al anterior (subiendo, \
bajando, estable) — solo si hay mas de un snapshot con fecha realmente distinta (dias distintos, \
no solo horas distintas del mismo dia).
- Que publicacion de la lista (que ya esta ordenada de mas reciente a mas antigua) tiene mas \
alcance y cual menos, expresado como porcentaje de los seguidores actuales.
- Cualquier patron que destaque: formato que retiene mas reproducciones (views mucho mayor que \
reach), cadencia de publicacion muy irregular mirando las fechas, una racha de posts flojos, etc.
- Si el snapshot de cuenta mas reciente tiene valores identicos al anterior, o si la publicacion \
mas reciente de la lista lleva ya muchos dias, dilo explicitamente en vez de asumir que es una \
foto nueva o una bajada real.

--- snapshots de account_metrics.csv (mas antiguo primero) ---
{account_summary}

--- estado actual de cada publicacion, de media_metrics.csv (mas reciente primero) ---
{media_summary}

Escribe solo el texto del analisis, sin ningun comentario tuyo antes o despues."""


def summarize_account_metrics(path: Path, max_snapshots: int = MAX_ACCOUNT_SNAPSHOTS) -> str:
    """Agrupa las filas de account_metrics.csv por fecha/hora de ejecucion
    (fetched_at_utc) y devuelve una linea por snapshot, con todas sus
    metricas juntas — mucho mas legible y compacto que el CSV en bruto, y no
    hace falta recortar por caracteres porque un snapshot siempre es una
    linea corta."""
    if not path.exists():
        return "(no existe todavia)"

    snapshots: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("fetched_at_utc", "")
            snapshots.setdefault(ts, {})[row.get("metric", "")] = row.get("value", "")

    if not snapshots:
        return "(vacio)"

    ordenados = sorted(snapshots.items(), key=lambda kv: kv[0])[-max_snapshots:]
    lineas = []
    for ts, metricas in ordenados:
        pares = ", ".join(f"{metrica}={valor}" for metrica, valor in metricas.items())
        lineas.append(f"{ts}: {pares}")
    return "\n".join(lineas)


def summarize_media_metrics(path: Path, max_posts: int = MAX_MEDIA_POSTS) -> str:
    """Para cada publicacion, se queda solo con el valor MAS RECIENTE de
    cada metrica (no el historial completo) y ordena por fecha de
    publicacion real (posted_at_utc), de mas reciente a mas antigua. Esto
    evita el bug de recortar el CSV en bruto por numero de caracteres, que
    podia cortar justo las publicaciones mas nuevas si el archivo era largo
    (las filas de la ejecucion mas reciente se escriben publicacion-nueva-
    primero, asi que un recorte por "ultimas lineas del archivo" se quedaba
    con las publicaciones MAS VIEJAS de esa tanda, no las mas nuevas)."""
    if not path.exists():
        return "(no existe todavia)"

    posts: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            media_id = row.get("media_id", "")
            post = posts.setdefault(media_id, {
                "posted_at_utc": row.get("posted_at_utc", ""),
                "caption_excerpt": row.get("caption_excerpt", ""),
                "metrics": {},
            })
            metric = row.get("metric", "")
            fetched_at = row.get("fetched_at_utc", "")
            existente = post["metrics"].get(metric)
            if existente is None or fetched_at >= existente[1]:
                post["metrics"][metric] = (row.get("value", ""), fetched_at)

    if not posts:
        return "(vacio)"

    ordenados = sorted(posts.values(), key=lambda p: p["posted_at_utc"], reverse=True)[:max_posts]
    lineas = []
    for post in ordenados:
        pares = ", ".join(f"{metrica}={valor}" for metrica, (valor, _fetched_at) in post["metrics"].items())
        caption = post["caption_excerpt"] or "(sin caption)"
        lineas.append(f'{post["posted_at_utc"]} — "{caption}": {pares}')
    return "\n".join(lineas)


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.exit("Error: falta ANTHROPIC_API_KEY en el entorno.")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

    account_summary = summarize_account_metrics(ACCOUNT_CSV)
    media_summary = summarize_media_metrics(MEDIA_CSV)

    if account_summary == "(no existe todavia)" and media_summary == "(no existe todavia)":
        sys.exit("Error: no hay CSV que analizar todavia (fetch_metrics.py deberia correr antes que este script).")

    prompt = PROMPT_TEMPLATE.format(account_summary=account_summary, media_summary=media_summary)

    print(f"Pidiendo el analisis a {model}...")
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            # claude-sonnet-5 puede usar parte del presupuesto en un bloque
            # de "thinking" antes de escribir la respuesta final — con poco
            # margen (probado con 700) se queda sin tokens para el texto y
            # la respuesta llega vacia. Con 4096 sobra para el pensamiento y
            # para las 150-250 palabras del analisis.
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    if not resp.ok:
        print(f"Respuesta de la API ({resp.status_code}): {resp.text}")
    resp.raise_for_status()

    data = resp.json()
    texto = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text").strip()
    if not texto:
        sys.exit(f"Error: la respuesta de la API no traia texto: {data}")

    ANALYSIS_PATH.write_text(texto + "\n", encoding="utf-8")
    print(f"Analisis escrito en {ANALYSIS_PATH.name} ({len(texto.split())} palabras aprox.)")


if __name__ == "__main__":
    main()
