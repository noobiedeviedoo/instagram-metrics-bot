#!/usr/bin/env python3
"""
Bot de grafico semanal - genera una imagen con la tendencia semanal de la
cuenta y las publicaciones con mas alcance, y la manda por Telegram.

Pensado para ejecutarse desde GitHub Actions, en speak-analysis.yml, despues
de send_weekly_audio.py (mismo cron, mismo chat de Telegram). No llama a
ninguna API de pago: toda la imagen sale de los CSV que ya estan en el repo
(account_metrics.csv, media_metrics.csv), igual que analyze_metrics.py solo
lee lo que ya escribio fetch_metrics.py.

Dos paneles en una sola imagen:
    1. Tendencia semanal — un punto por dia (el ultimo snapshot de ese dia
       si hubo varias ejecuciones), con 4 lineas:
         - Tasa de interaccion (%) = total_interactions / reach, en el eje
           izquierdo, en su valor real (suele moverse en decenas de %).
         - Seguidores, alcance y reproducciones/alcance, las tres
           INDEXADAS a 100 en el primer dato disponible, en el eje derecho.
           Se indexan (en vez de mostrar el valor real) porque son cifras en
           escalas muy distintas entre si — lo que importa para ver
           progreso es cuanto ha subido o bajado cada una respecto al
           primer dato, no su valor absoluto.
       'Reproducciones/alcance' (views/reach) no se deja en valor real junto
       a la tasa de interaccion aunque las dos sean ratios: en la practica
       suele moverse en cientos de % (las reproducciones cuentan repasos de
       carrusel o repeticiones de video, no cuentas unicas), y compartir eje
       con una tasa de interaccion de decenas de % la aplastaria contra el
       cero. Indexarla junto a seguidores/alcance evita ese problema.
       Con pocas semanas de historico este panel se vera casi plano (un
       punto suelto por linea) — es normal, se vuelve mas util cuanto mas
       tiempo lleve corriendo el bot.
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
import math
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
COLOR_TASA = "#0F6E56"        # teal — unica linea en valor real (eje izquierdo)
COLOR_SEGUIDORES = "#534AB7"  # purpura — indexada (eje derecho)
COLOR_ALCANCE = "#185FA5"     # azul — indexada (eje derecho)
COLOR_REPRODUCCIONES = "#D85A30"  # coral — indexada (eje derecho)
COLOR_POSTS = "#C13584"       # magenta — barras del panel 2
COLOR_TEXT = "#2C2C2A"
COLOR_MUTED = "#8A8985"


def ultimo_valor_por_dia(path: Path, metric: str) -> dict[str, int]:
    """Para una metrica de account_metrics.csv, un valor por dia (AAAA-MM-DD):
    el ULTIMO de ese dia si hubo varias ejecuciones (sobre todo en pruebas).
    Para 'followers_count' ignora a proposito la metrica antigua
    'follower_count' (singular, sin 's') que en las primeras pruebas del bot
    devolvia siempre 0 — ver README, seccion 'Estado' (no hace falta
    filtrarla aparte: como el nombre no coincide, el filtro por 'metric' ya
    la deja fuera)."""
    if not path.exists():
        return {}

    por_dia: dict[str, tuple[str, int]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("metric") != metric:
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

    return {dia: valor for dia, (_fetched_at, valor) in por_dia.items()}


def indexar(valores: list[float | None]) -> list[float | None]:
    """Reexpresa una serie como indice sobre su primer valor disponible
    (primer_valor = 100). Une series en escalas muy distintas (seguidores en
    cientos, alcance en miles...) en un solo eje: lo que se ve es cuanto ha
    subido o bajado cada una desde el primer dato, no su cifra absoluta."""
    base = next((v for v in valores if v is not None and v != 0), None)
    if base is None:
        return [None] * len(valores)
    return [(v / base * 100) if v is not None else None for v in valores]


def construir_tendencia_semanal(path: Path) -> dict[str, list]:
    """Une reach, views, total_interactions y followers_count de
    account_metrics.csv en series diarias alineadas por fecha, y calcula a
    partir de ahi las 4 lineas del panel de tendencia. Un dia solo entra si
    tiene reach Y followers_count ese dia (hacen falta los dos para calcular
    las tasas y el indice de seguidores)."""
    reach = ultimo_valor_por_dia(path, "reach")
    views = ultimo_valor_por_dia(path, "views")
    interacciones = ultimo_valor_por_dia(path, "total_interactions")
    seguidores = ultimo_valor_por_dia(path, "followers_count")

    dias = sorted(set(reach) & set(seguidores))

    tasa_interaccion_pct: list[float | None] = []
    seguidores_valores: list[float | None] = []
    alcance_valores: list[float | None] = []
    reproducciones_valores: list[float | None] = []

    for dia in dias:
        r = reach.get(dia)
        v = views.get(dia)
        i = interacciones.get(dia)
        s = seguidores.get(dia)

        tasa_interaccion_pct.append((i / r * 100) if (i is not None and r) else None)
        seguidores_valores.append(s)
        alcance_valores.append(r)
        reproducciones_valores.append(v)

    return {
        "dias": dias,
        "tasa_interaccion_pct": tasa_interaccion_pct,
        "seguidores_idx": indexar(seguidores_valores),
        "alcance_idx": indexar(alcance_valores),
        "reproducciones_idx": indexar(reproducciones_valores),
        # se guardan tambien en crudo, solo para el texto del caption de Telegram
        "seguidores_ultimo": next((v for v in reversed(seguidores_valores) if v is not None), None),
        "tasa_interaccion_ultima": next((v for v in reversed(tasa_interaccion_pct) if v is not None), None),
    }


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


def _plot_linea(ax, dias, valores, color, marker, label, linestyle="-", anotar=True):
    """Dibuja una linea saltandose los None (dias sin dato para esa
    metrica) en vez de romper la llamada a plot(), y anota el ultimo valor
    disponible (salvo que 'anotar' sea False — se desactiva para las lineas
    indexadas cuando solo hay un dia, porque ese primer valor es SIEMPRE 100
    por definicion del indice, no aporta nada escribirlo tres veces
    superpuesto)."""
    xs = [d for d, v in zip(dias, valores) if v is not None]
    ys = [v for v in valores if v is not None]
    if not xs:
        return None
    linea, = ax.plot(xs, ys, marker=marker, color=color, linewidth=2, linestyle=linestyle, label=label)
    if anotar:
        ax.annotate(
            f"{ys[-1]:.0f}",
            (xs[-1], ys[-1]),
            textcoords="offset points",
            xytext=(6, 4),
            fontsize=8,
            color=color,
        )
    return linea


def generar_grafico(tendencia: dict[str, list], top_posts: list[tuple[str, int]], destino: str):
    fig, (ax_tendencia, ax_posts) = plt.subplots(
        2, 1, figsize=(9, 8.5), height_ratios=[1, 1.4], constrained_layout=True
    )
    fig.patch.set_facecolor("white")

    # --- Panel 1: tendencia semanal (eje izq. = tasa real, eje der. = indices) ---
    ax_tendencia.set_title("Tendencia semanal", loc="left", fontsize=14, fontweight="bold", color=COLOR_TEXT)
    dias = tendencia["dias"]
    if dias:
        ax_der = ax_tendencia.twinx()
        un_solo_dia = len(dias) == 1

        lineas = []
        lineas.append(_plot_linea(
            ax_tendencia, dias, tendencia["tasa_interaccion_pct"], COLOR_TASA, "o", "Tasa de interacción (%)"
        ))
        lineas.append(_plot_linea(
            ax_der, dias, tendencia["seguidores_idx"], COLOR_SEGUIDORES, "^", "Seguidores (índice)",
            linestyle="--", anotar=not un_solo_dia,
        ))
        lineas.append(_plot_linea(
            ax_der, dias, tendencia["alcance_idx"], COLOR_ALCANCE, "D", "Alcance (índice)",
            linestyle="--", anotar=not un_solo_dia,
        ))
        lineas.append(_plot_linea(
            ax_der, dias, tendencia["reproducciones_idx"], COLOR_REPRODUCCIONES,
            "s", "Reproducciones/alcance (índice)", linestyle="--", anotar=not un_solo_dia,
        ))
        lineas = [l for l in lineas if l is not None]

        ax_tendencia.set_ylabel("Tasa de interacción (%)", color=COLOR_TASA, fontsize=9)
        ax_der.set_ylabel("Índice (primer dato = 100)", color=COLOR_TEXT, fontsize=9)
        ax_tendencia.legend(handles=lineas, loc="upper left", fontsize=8, frameon=False, ncols=2)

        if un_solo_dia:
            ax_tendencia.text(
                0.5,
                -0.22,
                "Primer dato registrado — las 3 lineas indexadas parten todas de 100 (se solapan) "
                "y se separaran con mas semanas de historico.",
                transform=ax_tendencia.transAxes,
                ha="center",
                fontsize=9,
                color=COLOR_MUTED,
                style="italic",
            )
        ax_tendencia.margins(x=0.15, y=0.3)
        ax_der.margins(y=0.3)
        ax_der.spines[["top", "left"]].set_visible(False)
    else:
        ax_tendencia.text(
            0.5, 0.5, "Todavia no hay suficientes datos de cuenta.",
            transform=ax_tendencia.transAxes, ha="center", va="center", color=COLOR_MUTED,
        )
    ax_tendencia.spines[["top", "right"]].set_visible(False)
    ax_tendencia.tick_params(axis="x", colors=COLOR_MUTED, labelsize=9)
    ax_tendencia.tick_params(axis="y", colors=COLOR_TASA, labelsize=9)

    # --- Panel 2: publicaciones con mas alcance ---
    ax_posts.set_title(
        "Publicaciones con mas alcance ahora mismo", loc="left", fontsize=14, fontweight="bold", color=COLOR_TEXT
    )
    if top_posts:
        etiquetas = [etiqueta_corta(caption) for caption, _reach in top_posts][::-1]
        valores = [reach for _caption, reach in top_posts][::-1]
        barras = ax_posts.barh(etiquetas, valores, color=COLOR_POSTS, height=0.6)
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

    tendencia = construir_tendencia_semanal(ACCOUNT_CSV)
    top_posts = top_posts_por_alcance(MEDIA_CSV)

    if not tendencia["dias"] and not top_posts:
        print("No hay datos todavia en los CSV — nada que graficar. Saliendo sin error.")
        return

    firma = "|".join(
        f"{d}:{ti}:{s}:{a}:{r}"
        for d, ti, s, a, r in zip(
            tendencia["dias"],
            tendencia["tasa_interaccion_pct"],
            tendencia["seguidores_idx"],
            tendencia["alcance_idx"],
            tendencia["reproducciones_idx"],
        )
    ) + "#" + "|".join(f"{c}:{r}" for c, r in top_posts)
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
        generar_grafico(tendencia, top_posts, ruta_imagen)

        caption = "Resumen visual de la semana."
        if tendencia["seguidores_ultimo"] is not None:
            caption += f" {tendencia['seguidores_ultimo']:.0f} seguidores"
        if tendencia["tasa_interaccion_ultima"] is not None:
            caption += f", {tendencia['tasa_interaccion_ultima']:.1f}% de tasa de interacción"
        caption += "."

        print("Mandando el grafico por Telegram...")
        send_photo_telegram(ruta_imagen, caption, token, chat_id)
        print("Enviado correctamente.")
    finally:
        if ruta_imagen and os.path.exists(ruta_imagen):
            os.remove(ruta_imagen)

    HASH_MARKER_PATH.write_text(firma_hash, encoding="utf-8")


if __name__ == "__main__":
    main()
