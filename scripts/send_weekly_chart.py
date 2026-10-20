#!/usr/bin/env python3
"""
Bot de grafico semanal - genera una imagen con el resumen de la semana de la
cuenta y la manda por Telegram.

Pensado para ejecutarse desde GitHub Actions, en speak-analysis.yml, despues
de send_weekly_audio.py (mismo cron, mismo chat de Telegram). No llama a
ninguna API de pago: toda la imagen sale de los CSV que ya estan en el repo
(account_metrics.csv, media_metrics.csv), igual que analyze_metrics.py solo
lee lo que ya escribio fetch_metrics.py.

Disenada para leerse en el movil, de arriba abajo:
    1. Cuatro cifras de la semana (alcance, visualizaciones, interacciones y
       seguidores) con el cambio frente a la semana anterior, y otras cuatro de lo
       que lleva del perfil al negocio (visitas al perfil, clics a la web,
       seguidores nuevos y perdidos).
    2. Cuatro minigraficos, uno por metrica, con las ultimas SEMANAS_GRAFICO
       semanas dia a dia. Uno por metrica (y no todas en un grafico con dos
       ejes e indices, como antes) para que las lineas no se monten y cada
       una se lea en su valor real. La semana actual va sombreada; solo se
       rotulan el ultimo valor y el maximo, y en el eje X solo los lunes.
    3. Las publicaciones de los ultimos DIAS_POSTS dias comparadas con la
       mediana de alcance de la cuenta (antes: siempre el mismo top 8
       historico, que no cambiaba nunca).

Para no mandar la misma imagen dos veces si el workflow se ejecuta mas de
una vez con los mismos datos, se guarda un hash de los datos ya mandados en
`.last_sent_chart_hash` (mismo patron que `.last_sent_analysis_hash` de
send_weekly_audio.py, pero en fichero aparte porque el grafico puede cambiar
en una semana en la que el texto del analisis no cambie, o al reves).

Variables de entorno requeridas (GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN   - token del bot de Telegram (el mismo que tts-telegram-bot)
    TELEGRAM_CHAT_ID     - chat_id al que mandar la imagen
Opcional:
    SOLO_IMAGEN=ruta.png - genera la imagen ahi y no manda nada (para probar)
"""

import csv
import hashlib
import os
import re
import statistics
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # sin esto, matplotlib intenta abrir una ventana y falla en GitHub Actions (no hay pantalla)
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"
MEDIA_INFO_CSV = REPO_ROOT / "media_info.csv"
HASH_MARKER_PATH = REPO_ROOT / ".last_sent_chart_hash"

SEMANAS_GRAFICO = 4
DIAS_POSTS = 14
MAX_POSTS = 6
DIAS_MEDIANA = 90  # posts de los ultimos 90 dias para calcular "tu mediana"

# Colores (paleta de referencia de la skill dataviz, modo claro)
COLOR_FONDO = "#fcfcfb"
COLOR_TEXTO = "#0b0b0b"
COLOR_TEXTO_2 = "#52514e"
COLOR_APAGADO = "#898781"
COLOR_REJILLA = "#e1e0d9"
COLOR_EJE = "#c3c2b7"
COLOR_SERIE = "#2a78d6"        # una sola serie por minigrafico: siempre el mismo azul
COLOR_SEMANA = "#f0efec"       # sombreado de la semana actual
COLOR_SUBE = "#006300"         # texto de "sube" (con flecha, nunca solo color)
COLOR_BAJA = "#d03b3b"

METRICAS = [
    # (metrica en el CSV, titulo, se suma en la semana o se toma el ultimo valor)
    ("reach", "Alcance", "suma"),
    ("views", "Visualizaciones", "suma"),
    ("total_interactions", "Interacciones", "suma"),
    ("followers_count", "Seguidores", "ultimo"),
]

# Segunda fila de cifras: lo que lleva del perfil al negocio (sin minigrafico)
METRICAS_NEGOCIO = [
    ("profile_views", "Visitas al perfil", "suma"),
    ("website_clicks", "Clics a tu web", "suma"),
    ("follows", "Seguidores nuevos", "suma"),
    ("unfollows", "Dejaron de seguirte", "suma"),
]
SUBIR_ES_MALO = {"unfollows"}

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_corta(d: date) -> str:
    return f"{d.day} {MESES[d.month - 1]}"


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


def resumen_cuenta(path: Path) -> dict | None:
    """Series diarias de las ultimas SEMANAS_GRAFICO semanas y, por metrica,
    el valor de esta semana (ultimos 7 dias con dato) frente a la anterior."""
    todas = METRICAS + METRICAS_NEGOCIO
    series = {m: ultimo_valor_por_dia(path, m) for m, _t, _a in todas}
    todos = sorted(set().union(*(series[m] for m, _t, _a in METRICAS)))
    if not todos:
        return None
    fin = date.fromisoformat(todos[-1])
    inicio = fin - timedelta(days=SEMANAS_GRAFICO * 7 - 1)
    semana_desde = fin - timedelta(days=6)
    anterior_desde = fin - timedelta(days=13)

    resumen = {"fin": fin, "inicio": inicio, "semana_desde": semana_desde, "metricas": {}}
    for metrica, titulo, agregado in todas:
        datos = {date.fromisoformat(d): v for d, v in series[metrica].items()}
        en_rango = sorted((d, v) for d, v in datos.items() if d >= inicio)
        esta = [v for d, v in datos.items() if semana_desde <= d <= fin]
        previa = [v for d, v in datos.items() if anterior_desde <= d < semana_desde]
        if agregado == "suma":
            actual = sum(esta) if esta else None
            antes = sum(previa) if previa else None
        else:
            actual = datos[max(datos)] if datos else None
            anteriores = [d for d in datos if d < semana_desde]
            antes = datos[max(anteriores)] if anteriores else None
        resumen["metricas"][metrica] = {
            "titulo": titulo, "agregado": agregado, "serie": en_rango, "actual": actual, "antes": antes,
            # Con que dia se compara (para "ultimo": puede ser de antes de hace 7 dias si hubo un hueco)
            "antes_fecha": max(anteriores) if agregado == "ultimo" and anteriores else None,
            "subir_es_malo": metrica in SUBIR_ES_MALO,
        }
    return resumen


def formatos_por_post() -> dict[str, str]:
    """{media_id: 'reel' | 'carrusel' | 'foto' | 'vídeo'} de media_info.csv."""
    if not MEDIA_INFO_CSV.exists():
        return {}
    with MEDIA_INFO_CSV.open(newline="", encoding="utf-8") as f:
        return {row["media_id"]: row["formato"] for row in csv.DictReader(f)}


def posts_recientes(path: Path) -> tuple[list[dict], float | None]:
    """Publicaciones de los ultimos DIAS_POSTS dias con su alcance mas
    reciente, y la mediana de alcance de los posts de los ultimos DIAS_MEDIANA
    dias (para saber si un post va bien o mal "para lo que es tu cuenta")."""
    if not path.exists():
        return [], None

    posts: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("metric") != "reach":
                continue
            try:
                valor = int(row.get("value", ""))
                publicado = datetime.fromisoformat(row["posted_at_utc"].replace("+0000", "+00:00")).date()
            except (ValueError, KeyError):
                continue
            media_id = row.get("media_id", "")
            fetched_at = row.get("fetched_at_utc", "")
            existente = posts.get(media_id)
            if existente is None or fetched_at >= existente["fetched_at"]:
                caption = (row.get("caption_excerpt", "") or "").strip()
                posts[media_id] = {"fetched_at": fetched_at, "reach": valor, "caption": caption, "fecha": publicado}

    if not posts:
        return [], None
    formatos = formatos_por_post()
    for media_id, p in posts.items():
        p["formato"] = formatos.get(media_id, "")
    ultimo = max(p["fecha"] for p in posts.values())
    hoy = max(ultimo, date.today())
    recientes = sorted(
        (p for p in posts.values() if p["fecha"] > hoy - timedelta(days=DIAS_POSTS)),
        key=lambda p: p["fecha"], reverse=True,
    )[:MAX_POSTS]
    base = [p["reach"] for p in posts.values() if p["fecha"] > hoy - timedelta(days=DIAS_MEDIANA)]
    mediana = statistics.median(base) if len(base) >= 3 else None
    return recientes, mediana


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


def miles(n: float) -> str:
    """1234 -> '1.234' (separador de miles a la espanola)."""
    return f"{n:,.0f}".replace(",", ".")


def texto_cambio(m: dict) -> tuple[str, str]:
    """Texto y color del cambio frente a la semana anterior."""
    actual, antes = m["actual"], m["antes"]
    if actual is None or antes is None:
        return "sin semana anterior", COLOR_APAGADO
    if m["agregado"] == "ultimo":
        dif = actual - antes
        desde = f"desde el {fecha_corta(m['antes_fecha'])}"
        if dif == 0:
            return f"= igual {desde}", COLOR_APAGADO
        return (f"▲ +{dif} {desde}", COLOR_SUBE) if dif > 0 else (f"▼ {dif} {desde}", COLOR_BAJA)
    if actual == antes or (antes and abs(actual - antes) / antes < 0.01):
        return "= igual que la anterior", COLOR_APAGADO
    color = COLOR_SUBE if (actual > antes) != m.get("subir_es_malo", False) else COLOR_BAJA
    flecha = "▲" if actual > antes else "▼"
    if antes < 20:
        # Con cifras pequenas el porcentaje engana (de 2 a 8 = "+300%"): mejor la diferencia
        return f"{flecha} {actual - antes:+d} (la anterior: {antes})", color
    return f"{flecha} {(actual - antes) / antes * 100:+.0f}% vs la anterior", color


def dibujar_cifra(ax, m: dict):
    ax.axis("off")
    ax.text(0, 0.92, m["titulo"],
            fontsize=10, color=COLOR_TEXTO_2, transform=ax.transAxes, va="top")
    valor = miles(m["actual"]) if m["actual"] is not None else "—"
    ax.text(0, 0.62, valor, fontsize=22, fontweight="bold", color=COLOR_TEXTO, transform=ax.transAxes, va="top")
    texto, color = texto_cambio(m)
    ax.text(0, 0.08, texto, fontsize=9, color=color, transform=ax.transAxes, va="bottom")


def dibujar_minigrafico(ax, m: dict, resumen: dict):
    ax.set_facecolor(COLOR_FONDO)
    ax.set_title(m["titulo"], loc="left", fontsize=11, fontweight="bold", color=COLOR_TEXTO, pad=6)
    ax.axvspan(resumen["semana_desde"] - timedelta(hours=12), resumen["fin"] + timedelta(days=1.5),
               color=COLOR_SEMANA, zorder=0, lw=0)
    serie = m["serie"]
    if not serie:
        ax.text(0.5, 0.5, "Sin datos", transform=ax.transAxes, ha="center", va="center", color=COLOR_APAGADO)
    else:
        # Tramos de dias seguidos con linea continua; los huecos (dias sin dato) con linea punteada
        tramo = [serie[0]]
        for anterior, actual in zip(serie, serie[1:]):
            if (actual[0] - anterior[0]).days > 1:
                ax.plot(*zip(*tramo), color=COLOR_SERIE, lw=2, zorder=3)
                ax.plot([anterior[0], actual[0]], [anterior[1], actual[1]],
                        color=COLOR_SERIE, lw=1.2, ls=":", alpha=0.6, zorder=2)
                tramo = []
            tramo.append(actual)
        ax.plot(*zip(*tramo), color=COLOR_SERIE, lw=2, zorder=3)

        # Solo dos etiquetas: el ultimo valor y el maximo (si no es el mismo punto)
        ultimo = serie[-1]
        ax.scatter([ultimo[0]], [ultimo[1]], s=36, color=COLOR_SERIE, edgecolor=COLOR_FONDO, lw=1.5, zorder=4)
        ax.annotate(miles(ultimo[1]), ultimo, textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=9, fontweight="bold", color=COLOR_TEXTO)
        if m["agregado"] == "suma":
            maximo = max(serie, key=lambda p: p[1])
            if maximo != ultimo:
                ax.scatter([maximo[0]], [maximo[1]], s=24, color=COLOR_SERIE, zorder=4)
                ax.annotate(f"máx. {miles(maximo[1])}\n{fecha_corta(maximo[0])}", maximo,
                            textcoords="offset points", xytext=(0, 6), ha="center", va="bottom",
                            fontsize=8, color=COLOR_TEXTO_2)
        valores = [v for _d, v in serie]
        bajo, alto = min(valores), max(valores)
        margen = (alto - bajo) * 0.35 or max(alto * 0.05, 1)
        ax.set_ylim(0 if m["agregado"] == "suma" else bajo - margen, alto + margen)

    ax.set_xlim(resumen["inicio"] - timedelta(days=0.5), resumen["fin"] + timedelta(days=1.5))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _p: fecha_corta(mdates.num2date(x).date())))
    ax.yaxis.set_major_locator(plt.MaxNLocator(3, integer=True))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _p: miles(y)))
    ax.grid(axis="y", color=COLOR_REJILLA, lw=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_EJE)
    ax.tick_params(colors=COLOR_APAGADO, labelsize=8, length=0)


def dibujar_posts(ax, posts: list[dict], mediana: float | None):
    ax.set_facecolor(COLOR_FONDO)
    ax.set_title(f"Tus publicaciones de los últimos {DIAS_POSTS} días", loc="left",
                 fontsize=11, fontweight="bold", color=COLOR_TEXTO, pad=6)
    if not posts:
        ax.text(0.5, 0.5, f"No has publicado en los últimos {DIAS_POSTS} días.",
                transform=ax.transAxes, ha="center", va="center", color=COLOR_APAGADO)
        ax.axis("off")
        return
    filas = posts[::-1]  # la mas reciente arriba
    etiquetas = [f"{etiqueta_corta(p['caption'], 22)}  ·  {p['formato'] + ', ' if p['formato'] else ''}"
                 f"{fecha_corta(p['fecha'])}" for p in filas]
    valores = [p["reach"] for p in filas]
    barras = ax.barh(range(len(filas)), valores, color=COLOR_SERIE, height=0.55, zorder=3)
    ax.bar_label(barras, labels=[miles(v) for v in valores], padding=4, fontsize=9, color=COLOR_TEXTO)
    ax.set_yticks(range(len(filas)), etiquetas)
    tope = max(valores + ([mediana] if mediana else []))
    ax.set_xlim(0, tope * 1.35)
    if mediana:
        ax.axvline(mediana, color=COLOR_TEXTO_2, lw=1.2, ls="--", zorder=4)
        ax.text(mediana, len(filas) - 0.45, f"  tu mediana: {miles(mediana)}", fontsize=8,
                color=COLOR_TEXTO_2, ha="left", va="bottom")
    ax.set_ylim(-0.6, len(filas) - 0.1)
    ax.xaxis.set_visible(False)
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    ax.spines["left"].set_color(COLOR_EJE)
    ax.tick_params(axis="y", colors=COLOR_TEXTO, labelsize=9, length=0)
    ax.text(0, -0.08, "Alcance acumulado hasta hoy: los posts más nuevos aún están creciendo.",
            transform=ax.transAxes, fontsize=8, color=COLOR_APAGADO, va="top")


def generar_grafico(resumen: dict, posts: list[dict], mediana: float | None, destino: str):
    n_posts = max(len(posts), 2)
    fig = plt.figure(figsize=(8, 11.6 + 0.35 * n_posts), facecolor=COLOR_FONDO)
    gs = fig.add_gridspec(
        5, 4, height_ratios=[0.6, 0.6, 1.6, 1.6, 0.55 + 0.32 * n_posts],
        left=0.06, right=0.95, top=0.91, bottom=0.04, hspace=0.45, wspace=0.35,
    )
    fig.text(0.06, 0.965, "Instagram · resumen semanal", fontsize=17, fontweight="bold", color=COLOR_TEXTO)
    fig.text(0.06, 0.94, f"Semana del {fecha_corta(resumen['semana_desde'])} al {fecha_corta(resumen['fin'])}"
             "  ·  cifras de la semana, comparadas con la anterior", fontsize=10, color=COLOR_TEXTO_2)

    metricas = [resumen["metricas"][m] for m, _t, _a in METRICAS]
    for i, m in enumerate(metricas):
        dibujar_cifra(fig.add_subplot(gs[0, i]), m)
    for i, (clave, _t, _a) in enumerate(METRICAS_NEGOCIO):
        ax = fig.add_subplot(gs[1, i])
        dibujar_cifra(ax, resumen["metricas"][clave])
        if i == 0:
            ax.text(0, 1.25, "DEL PERFIL A TU NEGOCIO", transform=ax.transAxes, fontsize=9,
                    fontweight="bold", color=COLOR_APAGADO)
    posiciones = [gs[2, 0:2], gs[2, 2:4], gs[3, 0:2], gs[3, 2:4]]
    for i, (pos, m) in enumerate(zip(posiciones, metricas)):
        ax = fig.add_subplot(pos)
        dibujar_minigrafico(ax, m, resumen)
        if i == 0:
            ax.text(0, 1.22, f"DÍA A DÍA, ÚLTIMAS {SEMANAS_GRAFICO} SEMANAS  ·  sombreado = esta semana",
                    transform=ax.transAxes, fontsize=9, fontweight="bold", color=COLOR_APAGADO)

    # Columna de etiquetas a la izquierda para los posts: el panel empieza mas a la derecha
    ax_posts = fig.add_subplot(gs[4, :])
    caja = ax_posts.get_position()
    ax_posts.set_position([caja.x0 + 0.3, caja.y0, caja.width - 0.3, caja.height])
    dibujar_posts(ax_posts, posts, mediana)

    fig.savefig(destino, dpi=150, facecolor=COLOR_FONDO)
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
    resumen = resumen_cuenta(ACCOUNT_CSV)
    posts, mediana = posts_recientes(MEDIA_CSV)
    if resumen is None:
        print("No hay datos todavia en los CSV — nada que graficar. Saliendo sin error.")
        return

    solo_imagen = os.environ.get("SOLO_IMAGEN", "").strip()
    if solo_imagen:
        generar_grafico(resumen, posts, mediana, solo_imagen)
        print(f"Imagen generada en {solo_imagen} (no se manda nada).")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("Error: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el entorno.")

    firma = "|".join(
        f"{m}:{d}:{v}" for m, datos in resumen["metricas"].items() for d, v in datos["serie"]
    ) + "#" + "|".join(f"{p['caption']}:{p['reach']}" for p in posts)
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
        generar_grafico(resumen, posts, mediana, ruta_imagen)

        caption = (f"Resumen de la semana del {fecha_corta(resumen['semana_desde'])} "
                   f"al {fecha_corta(resumen['fin'])}.")
        print("Mandando el grafico por Telegram...")
        send_photo_telegram(ruta_imagen, caption, token, chat_id)
        print("Enviado correctamente.")
    finally:
        if ruta_imagen and os.path.exists(ruta_imagen):
            os.remove(ruta_imagen)

    HASH_MARKER_PATH.write_text(firma_hash, encoding="utf-8")


if __name__ == "__main__":
    main()
