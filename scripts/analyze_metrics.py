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

import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"
ANALYSIS_PATH = REPO_ROOT / "weekly_analysis.txt"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PROMPT_TEMPLATE = """Eres un analista que le prepara a Miguel Ángel un resumen semanal hablado \
sobre el rendimiento de su cuenta de Instagram. Te paso el contenido completo de dos CSV: \
uno con metricas de cuenta (varias filas por ejecucion semanal, con fecha) y otro con \
metricas por publicacion (alcance, reproducciones, likes, comentarios, veces compartida, \
guardados, interacciones totales — con varias filas por publicacion porque se vuelve a medir \
cada semana).

Tu tarea: escribir un analisis en espanol, de 150 a 250 palabras, en PROSA CONTINUA — nada de \
markdown, viñetas, tablas, emojis ni encabezados, porque este texto lo va a leer en voz alta un \
motor de texto a voz tal cual se lo mandes. Debe sonar como si se lo contaras a Miguel en persona: \
empieza con una frase resumen y luego desarrolla 3 o 5 puntos concretos y utiles.

Cosas que deberias mirar si los datos lo permiten (usa el numero mas reciente de \
'followers_count' como referencia de audiencia):
- Como esta el alcance/interacciones de esta ultima medicion frente a la anterior si hay mas de \
una fecha distinta en account_metrics.csv (subiendo, bajando, estable).
- Que publicacion reciente tiene mas alcance y cual menos, expresado como porcentaje de los \
seguidores actuales (alcance / followers_count * 100).
- Cualquier patron que destaque: formato que retiene mas reproducciones, cadencia de publicacion \
muy irregular, una racha de posts flojos, etc.
- Si algun dato parece no haberse actualizado desde la ultima vez (mismo fetched_at_utc que ya \
se comento antes), dilo explicitamente en vez de inventar.

--- account_metrics.csv ---
{account_csv}

--- media_metrics.csv (puede estar recortado a las ultimas filas si es muy largo) ---
{media_csv}

Escribe solo el texto del analisis, sin ningun comentario tuyo antes o despues."""


def read_csv_capped(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return "(no existe todavia)"
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text
    # Si es muy largo, nos quedamos con la cabecera + las ultimas filas (las
    # mas recientes), que es lo mas relevante para un analisis semanal.
    lines = text.splitlines()
    header, rows = lines[0], lines[1:]
    kept = []
    total = len(header)
    for line in reversed(rows):
        if total + len(line) > max_chars:
            break
        kept.append(line)
        total += len(line)
    kept.reverse()
    return header + "\n" + "\n".join(kept) + f"\n(... recortado, mostrando las {len(kept)} filas mas recientes de {len(rows)})"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.exit("Error: falta ANTHROPIC_API_KEY en el entorno.")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

    account_csv = read_csv_capped(ACCOUNT_CSV)
    media_csv = read_csv_capped(MEDIA_CSV)

    if account_csv == "(no existe todavia)" and media_csv == "(no existe todavia)":
        sys.exit("Error: no hay CSV que analizar todavia (fetch_metrics.py deberia correr antes que este script).")

    prompt = PROMPT_TEMPLATE.format(account_csv=account_csv, media_csv=media_csv)

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
            "max_tokens": 700,
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
