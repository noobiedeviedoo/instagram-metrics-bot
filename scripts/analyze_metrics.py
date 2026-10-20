#!/usr/bin/env python3
"""
Analiza los CSV de metricas y escribe el resultado en weekly_analysis.txt,
usando la API de Claude directamente (Messages API) — sin pasar por Cowork,
para que todo el flujo semanal dependa solo de GitHub Actions.

Pensado para correr como paso dentro de speak-analysis.yml, justo antes de
send_weekly_audio.py, una vez por semana — lee los CSV que fetch-metrics.yml
ha ido dejando con snapshots diarios durante toda la semana (ese workflow
corre a diario, pero ya no llama a la API de Claude: eso se hace aqui, una
vez, en el momento de generar el reporte semanal, no cada dia).

Variables de entorno requeridas:
    ANTHROPIC_API_KEY   - API key de Anthropic (console.anthropic.com)

Variables de entorno opcionales:
    ANTHROPIC_MODEL     - modelo a usar (por defecto claude-sonnet-5)
"""

import csv
import os
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

from send_weekly_chart import fecha_corta, formatos_por_post, resumen_cuenta

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"
ANALYSIS_PATH = REPO_ROOT / "weekly_analysis.txt"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PROMPT_TEMPLATE = """Preparas el resumen semanal HABLADO de la cuenta de Instagram de Miguel \
Ángel, fotógrafo (retrato, books, polaroids para modelos y artistas). Lo escucha en Telegram con un \
motor de texto a voz, así que escribe PROSA CONTINUA en español: nada de markdown, viñetas, emojis, \
encabezados ni enlaces.

Las cifras ya están calculadas (abajo). No recites datos día a día ni repitas todas las cifras: \
elige lo que de verdad importa.

Estructura, de 80 a 150 palabras en total:
1. Una frase titular con lo más importante de la semana: el cambio más grande, para bien o para \
mal, con su cifra.
2. Dos o tres hechos concretos que hayan cambiado respecto a la semana anterior, con números. \
Da prioridad a lo que lleva al negocio: visitas al perfil, clics al enlace de la web y seguidores \
ganados y perdidos. Si hay publicaciones nuevas, compáralas con su mediana y con su formato \
(alcance, y guardados y compartidos por cada 100 cuentas alcanzadas: dicen más que los likes). \
Con cifras pequeñas (menos de 20) habla en unidades, no en porcentajes.
3. Una sola recomendación concreta y comprobable para la semana que viene (qué publicar, cuándo \
o en qué formato), apoyada en los datos. Si el informe anterior dio una recomendación, di \
primero en una frase si se siguió y si funcionó según los datos.

Prohibido:
- Frases de relleno que no dicen nada nuevo: «no hay datos suficientes», «rendimiento irregular», \
«cadencia irregular», «sin tendencia clara», «conviene tomarlo como indicio». Solo puedes hablar de \
la frecuencia de publicación si cambió claramente respecto a lo habitual.
- Repetir ideas del informe anterior que no hayan cambiado.
- Inventar causas que los datos no muestran (fines de semana, algoritmo…) salvo que las cifras \
lo dejen claro.

--- cifras de la semana ---
{hechos}

--- informe de la semana pasada (no lo repitas) ---
{informe_anterior}

Escribe solo el texto del análisis, sin ningún comentario antes o después."""

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def pct(actual, antes) -> str:
    if actual is None or not antes:
        return "sin semana anterior para comparar"
    return f"{(actual - antes) / antes * 100:+.0f}% frente a la semana anterior ({antes})"


def cifras_de_la_semana() -> str:
    """Los mismos numeros que la grafica semanal (send_weekly_chart.py), ya
    comparados, para que Claude no tenga que hacer cuentas ni recite datos
    en bruto. Antes recibia los snapshots diarios tal cual y acababa
    recitandolos dia a dia («cayo a 85, luego a 47…»)."""
    resumen = resumen_cuenta(ACCOUNT_CSV)
    if resumen is None:
        return "(todavía no hay datos de cuenta)"
    m = resumen["metricas"]
    lineas = [f"Semana del {fecha_corta(resumen['semana_desde'])} al {fecha_corta(resumen['fin'])}."]
    for clave in ("reach", "views", "total_interactions"):
        datos = m[clave]
        lineas.append(f"- {datos['titulo']} (suma de la semana): {datos['actual']}, {pct(datos['actual'], datos['antes'])}.")
    seg = m["followers_count"]
    if seg["actual"] is not None and seg["antes"] is not None:
        lineas.append(f"- Seguidores: {seg['actual']} ({seg['actual'] - seg['antes']:+d} desde el {fecha_corta(seg['antes_fecha'])}).")
    elif seg["actual"] is not None:
        lineas.append(f"- Seguidores: {seg['actual']}.")
    lineas.append("Del perfil al negocio (lo que más importa: así llegan los clientes):")
    for clave in ("profile_views", "website_clicks", "follows", "unfollows"):
        datos = m[clave]
        if datos["actual"] is None:
            continue
        antes = f" (semana anterior: {datos['antes']})" if datos["antes"] is not None else ""
        lineas.append(f"- {datos['titulo']}: {datos['actual']}{antes}.")
    visitas, clics = m["profile_views"]["actual"], m["website_clicks"]["actual"]
    if visitas and clics is not None:
        lineas.append(f"- De cada 100 visitas al perfil, {clics / visitas * 100:.1f} pulsan el enlace de la web.")
    semana = [(d, v) for d, v in m["reach"]["serie"] if d >= resumen["semana_desde"]]
    if len(semana) >= 3:
        mejor = max(semana, key=lambda p: p[1])
        peor = min(semana, key=lambda p: p[1])
        lineas.append(f"- Mejor día de alcance: {DIAS_SEMANA[mejor[0].weekday()]} {fecha_corta(mejor[0])} ({mejor[1]}); "
                      f"peor: {DIAS_SEMANA[peor[0].weekday()]} {fecha_corta(peor[0])} ({peor[1]}).")
    lineas.append(publicaciones(resumen))
    return "\n".join(lineas)


def publicaciones(resumen: dict) -> str:
    """Posts de esta semana frente a la mediana de los ultimos 90 dias, con
    guardados y compartidos por cada 100 de alcance."""
    if not MEDIA_CSV.exists():
        return "- Publicaciones: sin datos."
    posts: dict[str, dict] = {}
    with MEDIA_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            post = posts.setdefault(row.get("media_id", ""), {
                "id": row.get("media_id", ""), "fecha": row.get("posted_at_utc", "")[:10],
                "caption": row.get("caption_excerpt", ""), "m": {}})
            anterior = post["m"].get(row.get("metric", ""))
            if anterior is None or row.get("fetched_at_utc", "") >= anterior[1]:
                try:
                    post["m"][row.get("metric", "")] = (int(row.get("value", "")), row.get("fetched_at_utc", ""))
                except ValueError:
                    pass

    def ratio(p, metrica):
        alcance = p["m"].get("reach", (0, ""))[0]
        return p["m"].get(metrica, (0, ""))[0] / alcance * 100 if alcance else None

    desde_90 = (resumen["fin"] - timedelta(days=90)).isoformat()
    base = [p for p in posts.values() if p["fecha"] >= desde_90 and "reach" in p["m"]]
    if len(base) < 3:
        return "- Publicaciones: muy pocas para comparar."
    mediana = lambda valores: statistics.median([v for v in valores if v is not None])
    med_alcance = mediana([p["m"]["reach"][0] for p in base])
    med_guardados = mediana([ratio(p, "saved") for p in base])
    med_compartidos = mediana([ratio(p, "shares") for p in base])
    lineas = [f"- Mediana de tus publicaciones de los últimos 90 días ({len(base)} posts): alcance {med_alcance:.0f}, "
              f"guardados {med_guardados:.1f} y compartidos {med_compartidos:.1f} por cada 100 cuentas alcanzadas."]

    # Por formato (reel, carrusel, foto...): la comparacion que mas ayuda a decidir que publicar
    formatos = formatos_por_post()
    for p in posts.values():
        p["formato"] = formatos.get(p.get("id", ""), "")
    grupos: dict[str, list] = {}
    for p in base:
        if p["formato"]:
            grupos.setdefault(p["formato"], []).append(p)
    if len(grupos) > 1:
        lineas.append("- Por formato (últimos 90 días; con menos de 3 posts no es fiable):")
        for nombre, grupo in sorted(grupos.items(), key=lambda g: -len(g[1])):
            lineas.append(f"  · {nombre}: {len(grupo)} posts, alcance mediano {mediana([q['m']['reach'][0] for q in grupo]):.0f}, "
                          f"compartidos {mediana([ratio(q, 'shares') for q in grupo]):.1f}/100.")

    semana = sorted((p for p in base if p["fecha"] >= resumen["semana_desde"].isoformat()), key=lambda p: p["fecha"])
    por_semana = len(base) / (90 / 7)
    lineas.append(f"- Publicaciones esta semana: {len(semana)} (lo habitual: {por_semana:.1f} por semana).")
    for p in semana:
        texto = (p["caption"] or "sin texto").replace("\n", " ")[:60]
        lineas.append(f"  · {p['fecha']} {p['formato'] or ''} «{texto}»: alcance {p['m']['reach'][0]} (aún creciendo), "
                      f"guardados {ratio(p, 'saved'):.1f}/100, compartidos {ratio(p, 'shares'):.1f}/100.")
    if not semana:
        ultima = max(base, key=lambda p: p["fecha"])
        dias = (resumen["fin"] - date.fromisoformat(ultima["fecha"])).days
        lineas.append(f"  · Ninguna nueva; la última fue hace {dias} días.")
    return "\n".join(lineas)


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.exit("Error: falta ANTHROPIC_API_KEY en el entorno.")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

    if not ACCOUNT_CSV.exists():
        sys.exit("Error: no hay CSV que analizar todavia (fetch_metrics.py deberia correr antes que este script).")

    hechos = cifras_de_la_semana()
    informe_anterior = ANALYSIS_PATH.read_text(encoding="utf-8").strip() if ANALYSIS_PATH.exists() else "(no hay)"
    prompt = PROMPT_TEMPLATE.format(hechos=hechos, informe_anterior=informe_anterior)
    if os.environ.get("SOLO_PROMPT"):
        print(prompt)
        return

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
            # claude-sonnet-5 piensa antes de escribir y ese pensamiento cuenta
            # contra max_tokens: con 4096 (2026-10-21) se lo gasto entero
            # pensando y la respuesta llego sin texto. 16000 deja margen de
            # sobra, y effort "medium" basta para un resumen de 150 palabras.
            "max_tokens": 16000,
            "output_config": {"effort": "medium"},
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
        sys.exit(f"Error: la respuesta no traia texto (stop_reason={data.get('stop_reason')}, "
                 f"uso={data.get('usage')}).")

    ANALYSIS_PATH.write_text(texto + "\n", encoding="utf-8")
    print(f"Analisis escrito en {ANALYSIS_PATH.name} ({len(texto.split())} palabras aprox.)")


if __name__ == "__main__":
    main()
