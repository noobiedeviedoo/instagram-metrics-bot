#!/usr/bin/env python3
"""
Bot de grafico semanal - genera una imagen con la evolucion de seguidores y
las publicaciones con mas alcance, y la manda por Telegram.

Pensado para ejecutarse desde GitHub Actions, en speak-analysis.yml, despues
de send_weekly_audio.py (mismo cron, mismo chat de Telegram). No llama a
ninguna API de pago: toda la imagen sale de los CSV que ya estan en el repo
(account_metrics.csv, media_metrics.csv), igual que analyze_metrics.py solo
lee lo que ya escribio fetch_metrics.py.

Dos paneles en una sola imagen:
    1. Seguidores a lo largo del tiempo (un punto por dia, el ultimo valor
       de ese dia si hubo varias ejecuciones). Con pocas semanas de
       historico se vera casi plano — es normal, se vuelve mas util cuanto
       mas tiempo lleve corriendo el bot.
    2. Las publicaciones con mas alcance ahora mismo (top 8), para ver de un
       vistazo que formato esta funcionando.

Para no mandar la misma imagen dos veces si el workflow se ejecuta mas de
una vez con los mismos datos, se guarda un hash de los datos ya mandados en
`.last_sent_chart_hash` (mismo patron que `.last_sent_analysis_hash` de
send_weekly_audio.py, pero en fichero aparte porque el grafico puede cambiar
en una semana en la que el texto del analisis no cambie, o al reves).

Variables de entorno requeridas (GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN   - token del bot de Telegram (el mismo que tts-telegram-bot)
    TELEGRAM_CHAT_ID     - chat_id al que mandar la imagen
"""

import csv
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # sin esto, matplotlib intenta abrir una ventana y falla en GitHub Actions (no hay pantalla)
import matplotlib.pyplot as plt
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"
HASH_MARKER_PATH = REPO_ROOT / ".last_sent_chart_hash"

TOP_POSTS = 8
COLOR_ACCENT = "#C13584"  # magenta similar al degradado de Instagram
COLOR_TEXT = "#2C2C2A"
COLOR_MUTED = "#8A8985"


def seguidores_por_dia(path: Path) -> list[tuple[str, int]]:
    """Una entrada por dia (AAAA-MM-DD), quedandose con el ULTIMO valor de
    'followers_count' de ese dia (puede haber varias ejecuciones el mismo
    dia, sobre todo en pruebas). Ignora a proposito la metrica antigua
    'follower_count' (singular, sin 's') que en las primeras pruebas del bot
    devolvia siempre 0 — ver README, seccion 'Estado'."""
    if not path.exists():
        return []

    por_dia: dict[str, tuple[str, int]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("metric") != "followers_count":
                continue
            fetched_at = row.get("fetched_at_utc", "")
            dia = fetched_at[:10]
            if not dia:
                continue
            try:
                valor = int(row.get("value", ""))
            except ValueError:
                continue
            anterior = por_dia.get(dia)
            if anterior is None or fetched_at >= anterior[0]:
                por_dia[dia] = (fetched_at, valor)

    return sorted((dia, valor) for dia, (_fetched_at, valor) in por_dia.items())


def top_posts_por_alcance(path: Path, top_n: int = TOP_POSTS) -> list[tuple[str, int]]:
    """Para cada publicacion, el valor MAS RECIENTE de 'reach' (no el
    historial completo, igual que summarize_media_metrics() en
    analyze_metrics.py), y devuelve las 'top_n' con mas alcance."""
    if not path.exists():
        return []

    posts: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("metric") != "reach":
                continue
            media_id = row.get("media_id", "")
            fetched_at = row.get("fetched_at_utc", "")
            try:
                valor = int(row.get("value", ""))
            except ValueError:
                continue
            existente = posts.get(media_id)
            if existente is None or fetched_at >= existente["fetched_at"]:
                caption = (row.get("caption_excerpt", "") or "(sin caption)").strip()
                posts[media_id] = {"fetched_at": fetched_at, "reach": valor, "caption": caption}

    ordenados = sorted(posts.values(), key=lambda p: p["reach"], reverse=True)[:top_n]
    return [(p["caption"], p["reach"]) for p in ordenados]


# DejaVu Sans (la fuente que usa matplotlib) no tiene glifos de emoji: sin
# quitarlos, cada emoji sale como un cuadrado vacio en la imagen. Cubre los
# rangos Unicode de emoji mas comunes (no hace falta ser exhaustivo, solo
# evitar los "tofu boxes" en las etiquetas de los posts).
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def etiqueta_corta(caption: str, max_chars: int = 26) -> str:
    caption = EMOJI_PATTERN.sub("", caption).replace("\n", " ").strip()
    if not caption:
        caption = "(sin texto)"
    if len(caption) <= max_chars:
        return caption
    return caption[: max_chars - 1].rstrip() + "…"


def generar_grafico(seguidores: list[tuple[str, int]], top_posts: list[tuple[str, int]], destino: str):
    fig, (ax_seguidores, ax_posts) = plt.subplots(
        2, 1, figsize=(9, 7.5), height_ratios=[0.7, 1.4], constrained_layout=True
    )
    fig.patch.set_facecolor("white")

    # --- Panel 1: seguidores en el tiempo ---
    ax_seguidores.set_title("Seguidores", loc="left", fontsize=14, fontweight="bold", color=COLOR_TEXT)
    if seguidores:
        dias = [d for d, _v in seguidores]
        valores = [v for _d, v in seguidores]
        ax_seguidores.plot(dias, valores, marker="o", color=COLOR_ACCENT, linewidth=2)
        for dia, valor in zip(dias, valores):
            ax_seguidores.annotate(
                str(valor),
                (dia, valor),
                textcoords="offset points",
                xytext=(0, 10),
                ha="center",
                fontsize=10,
                color=COLOR_TEXT,
            )
        if len(dias) == 1:
            # Con un solo punto, matplotlib deja muchisimo aire vertical de
            # sobra alrededor del dato — se fuerza un rango estrecho
            # alrededor del valor para que el panel no salga casi vacio.
            valor = valores[0]
            margen = max(valor * 0.12, 5)
            ax_seguidores.set_ylim(valor - margen, valor + margen)
            ax_seguidores.text(
                0.5,
                -0.25,
                "Primer dato registrado — la tendencia se vera con mas semanas de historico.",
                transform=ax_seguidores.transAxes,
                ha="center",
                fontsize=9,
                color=COLOR_MUTED,
                style="italic",
            )
        else:
            ax_seguidores.margins(y=0.35)
        ax_seguidores.margins(x=0.15)
    else:
        ax_seguidores.text(
            0.5, 0.5, "Todavia no hay datos de seguidores.",
            transform=ax_seguidores.transAxes, ha="center", va="center", color=COLOR_MUTED,
        )
    ax_seguidores.spines[["top", "right", "left"]].set_visible(False)
    ax_seguidores.get_yaxis().set_visible(False)
    ax_seguidores.tick_params(axis="x", colors=COLOR_MUTED, labelsize=9)

    # --- Panel 2: publicaciones con mas alcance ---
    ax_posts.set_title(
        "Publicaciones con mas alcance ahora mismo", loc="left", fontsize=14, fontweight="bold", color=COLOR_TEXT
    )
    if top_posts:
        etiquetas = [etiqueta_corta(caption) for caption, _reach in top_posts][::-1]
        valores = [reach for _caption, reach in top_posts][::-1]
        barras = ax_posts.barh(etiquetas, valores, color=COLOR_ACCENT, height=0.6)
        ax_posts.bar_label(barras, padding=4, fontsize=9, color=COLOR_TEXT)
        ax_posts.tick_params(axis="y", labelsize=9, colors=COLOR_TEXT)
        ax_posts.tick_params(axis="x", colors=COLOR_MUTED, labelsize=9)
        ax_posts.set_xlabel("Alcance", color=COLOR_MUTED, fontsize=9)
        ax_posts.spines[["top", "right"]].set_visible(False)
    else:
        ax_posts.text(
            0.5, 0.5, "Todavia no hay publicaciones con metricas.",
            transform=ax_posts.transAxes, ha="center", va="center", color=COLOR_MUTED,
        )
        ax_posts.axis("off")

    fig.savefig(destino, dpi=150, facecolor="white")
    plt.close(fig)


def send_photo_telegram(photo_path: str, caption: str, token: str, chat_id: str):
    with open(photo_path, "rb") as photo:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": photo},
            timeout=60,
        )
    if not resp.ok:
        print(f"Respuesta de Telegram ({resp.status_code}): {resp.text}")
    resp.raise_for_status()


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("Error: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el entorno.")

    seguidores = seguidores_por_dia(ACCOUNT_CSV)
    top_posts = top_posts_por_alcance(MEDIA_CSV)

    if not seguidores and not top_posts:
        print("No hay datos todavia en los CSV — nada que graficar. Saliendo sin error.")
        return

    firma = "|".join(f"{d}:{v}" for d, v in seguidores) + "#" + "|".join(f"{c}:{r}" for c, r in top_posts)
    firma_hash = hashlib.sha256(firma.encode("utf-8")).hexdigest()
    hash_anterior = HASH_MARKER_PATH.read_text(encoding="utf-8").strip() if HASH_MARKER_PATH.exists() else ""
    if firma_hash == hash_anterior:
        print("Los datos del grafico no han cambiado desde el ultimo envio — no se manda imagen duplicada.")
        return

    ruta_imagen = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            ruta_imagen = tmp.name

        print("Generando el grafico...")
        generar_grafico(seguidores, top_posts, ruta_imagen)

        ultimo_seguidores = seguidores[-1][1] if seguidores else None
        caption = "Resumen visual de la semana."
        if ultimo_seguidores is not None:
            caption += f" {ultimo_seguidores} seguidores ahora mismo."

        print("Mandando el grafico por Telegram...")
        send_photo_telegram(ruta_imagen, caption, token, chat_id)
        print("Enviado correctamente.")
    finally:
        if ruta_imagen and os.path.exists(ruta_imagen):
            os.remove(ruta_imagen)

    HASH_MARKER_PATH.write_text(firma_hash, encoding="utf-8")


if __name__ == "__main__":
    main()
