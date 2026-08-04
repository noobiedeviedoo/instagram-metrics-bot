#!/usr/bin/env python3
"""
Bot de metricas de Instagram.

Pensado para ejecutarse periodicamente desde GitHub Actions (cron semanal).
Cada ejecucion:
  1. Pide insights de cuenta (alcance, visitas al perfil, interacciones...)
     del dia y anade una fila por metrica a account_metrics.csv.
  2. Lista las publicaciones de los ultimos MEDIA_LOOKBACK_DAYS dias y, para
     cada una, pide sus insights (alcance, interacciones, likes, comentarios,
     guardados...) y anade una fila por metrica a media_metrics.csv.

Ambos CSV son append-only (mismo patron que published_log.csv del bot de
publicacion): cada ejecucion solo anade filas nuevas, nunca reescribe ni
borra el historico. Asi se puede leer la evolucion en el tiempo (p.ej. "el
alcance de un post concreto siguio subiendo 3 semanas despues de publicarlo")
y no depende de volver a llamar a la API para ver datos pasados.

Variables de entorno requeridas (configuradas como GitHub Actions Secrets):
    IG_ACCESS_TOKEN         - token de acceso de la Graph API (Instagram Login)
    IG_BUSINESS_ACCOUNT_ID  - ID de la cuenta profesional de Instagram

Variables de entorno opcionales:
    MEDIA_LOOKBACK_DAYS     - cuantos dias hacia atras revisar publicaciones
                              (por defecto 90)

El token necesita, ademas de los permisos que ya tenga para publicar, estos
dos: instagram_business_basic e instagram_business_manage_insights — ver
README para como anadirlos.

Nota: la parte de metricas por publicacion (MEDIA_METRICS) ya se probo
contra la API real y funciona (primera ejecucion: 21 publicaciones, 147
filas). La lista de metricas de cuenta (ACCOUNT_METRICS) se ajusto despues,
a partir de la documentacion oficial de Meta, tras ver que la mitad fallaban
(profile_views y website_clicks estan deprecadas desde la v22; accounts_engaged
y total_interactions necesitan metric_type=total_value) — este segundo ajuste
todavia no se ha vuelto a probar en vivo, conviene relanzar el workflow a
mano una vez mas y revisar el log.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CSV = REPO_ROOT / "account_metrics.csv"
MEDIA_CSV = REPO_ROOT / "media_metrics.csv"

# Tokens con prefijo IGAA (Instagram API con inicio de sesion de Instagram,
# igual que el bot de publicacion) solo son validos contra este host.
GRAPH_API_BASE = "https://graph.instagram.com"

# Metricas de cuenta (nivel perfil, via /insights). Se piden una por una: si
# Meta rechaza alguna, las demas se guardan igual.
#
# 'profile_views' y 'website_clicks' se han quitado de esta lista: Meta las
# deprecó en la v22 de la API (sustituidas por 'views', 'reach',
# 'follower_count' y 'reposts' — ver Instagram Platform Changelog). Pedirlas
# siempre devuelve error, no es un problema de esta cuenta en concreto.
#
# 'follower_count' tambien se ha quitado de aqui: con metric_type=total_value
# (el formato que necesitan accounts_engaged/total_interactions) esta metrica
# en concreto no devuelve datos, sin dar error tampoco — probamos con una
# cuenta de 340 seguidores y siguio sin llegar, asi que no es cosa del
# tamano de audiencia. El numero real de seguidores se pide aparte, con
# fetch_account_fields(), directamente al perfil (endpoint distinto al de
# insights) — es mas simple y mas fiable.
ACCOUNT_METRICS = [
    "reach",
    "views",
    "reposts",
    "accounts_engaged",
    "total_interactions",
]

# Metricas por publicacion.
MEDIA_METRICS = [
    "reach",
    "views",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
]

ACCOUNT_CSV_HEADER = ["fetched_at_utc", "metric", "value", "period_end_utc"]
MEDIA_CSV_HEADER = [
    "fetched_at_utc",
    "media_id",
    "posted_at_utc",
    "caption_excerpt",
    "metric",
    "value",
]


def _raise_with_body(resp: requests.Response):
    """Igual que raise_for_status(), pero imprimiendo el cuerpo de la
    respuesta primero (Meta manda el motivo real del error ahi dentro)."""
    if not resp.ok:
        print(f"  Respuesta de la API ({resp.status_code}): {resp.text}")
    resp.raise_for_status()


def append_csv_rows(path: Path, header: list[str], rows: list[list]):
    if not rows:
        return
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(header)
        writer.writerows(rows)


def fetch_account_insights(account_id: str, access_token: str) -> list[list]:
    """Pide cada metrica de ACCOUNT_METRICS por separado (period=day,
    metric_type=total_value — formato que exigen las metricas de cuenta
    actuales de Meta; el formato antiguo de serie temporal sin metric_type
    ya no vale para accounts_engaged/total_interactions y da valores
    enganosos, tipo 0, para follower_count). Si una metrica falla, se avisa
    por consola y se sigue con las demas — nunca se corta la ejecucion
    entera por una sola metrica no soportada."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for metric in ACCOUNT_METRICS:
        params = {
            "metric": metric,
            "period": "day",
            "metric_type": "total_value",
            "access_token": access_token,
        }
        resp = requests.get(f"{GRAPH_API_BASE}/{account_id}/insights", params=params, timeout=30)
        if not resp.ok:
            print(f"Aviso: no se pudo leer la metrica de cuenta '{metric}': {resp.text}")
            continue
        for entry in resp.json().get("data", []):
            total_value = entry.get("total_value", {})
            value = total_value.get("value")
            if value is None:
                # Por si alguna metrica concreta devuelve el formato antiguo
                # (serie temporal) en vez de total_value.
                values = entry.get("values", [])
                if values:
                    value = values[-1].get("value")
            if value is None:
                continue
            rows.append([now, metric, value, ""])
    return rows


def fetch_account_fields(account_id: str, access_token: str) -> list[list]:
    """Pide seguidores y numero de publicaciones directamente al perfil
    (endpoint GET /{account_id}, no /insights) — mas simple y fiable que la
    metrica de insights 'follower_count', que no esta devolviendo datos."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    params = {"fields": "followers_count,media_count", "access_token": access_token}
    resp = requests.get(f"{GRAPH_API_BASE}/{account_id}", params=params, timeout=30)
    if not resp.ok:
        print(f"Aviso: no se pudo leer followers_count/media_count del perfil: {resp.text}")
        return []
    data = resp.json()
    rows = []
    if "followers_count" in data:
        rows.append([now, "followers_count", data["followers_count"], ""])
    if "media_count" in data:
        rows.append([now, "media_count", data["media_count"], ""])
    return rows


def fetch_recent_media(account_id: str, access_token: str, since: datetime) -> list[dict]:
    """Lista publicaciones desde `since`, siguiendo paginacion. Devuelve
    id, caption y fecha de cada una."""
    media = []
    url = f"{GRAPH_API_BASE}/{account_id}/media"
    params = {
        "fields": "id,caption,timestamp",
        "access_token": access_token,
        "limit": 50,
    }
    while url:
        resp = requests.get(url, params=params, timeout=30)
        _raise_with_body(resp)
        payload = resp.json()
        stop = False
        for item in payload.get("data", []):
            ts = item.get("timestamp", "")
            try:
                posted_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None
            if posted_at and posted_at < since:
                stop = True
                continue
            media.append(item)
        next_url = payload.get("paging", {}).get("next")
        if stop or not next_url:
            break
        url, params = next_url, None  # la URL "next" ya trae todos los parametros
    return media


def fetch_media_insights(media_id: str, access_token: str) -> dict:
    """Devuelve {metrica: valor} para un media_id. Metricas no soportadas por
    ese tipo de contenido (p.ej. 'shares' en fotos antiguas) se omiten con
    aviso, sin romper el resto."""
    values = {}
    params = {
        "metric": ",".join(MEDIA_METRICS),
        "access_token": access_token,
    }
    resp = requests.get(f"{GRAPH_API_BASE}/{media_id}/insights", params=params, timeout=30)
    if not resp.ok:
        print(f"  Aviso: insights no disponibles para media {media_id}: {resp.text}")
        return values
    for entry in resp.json().get("data", []):
        name = entry.get("name")
        entry_values = entry.get("values", [])
        if name and entry_values:
            values[name] = entry_values[-1].get("value")
    return values


def main():
    access_token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "").strip()
    if not access_token or not account_id:
        sys.exit("Error: faltan IG_ACCESS_TOKEN o IG_BUSINESS_ACCOUNT_ID en el entorno.")

    lookback_days = int(os.environ.get("MEDIA_LOOKBACK_DAYS", "90"))

    print("Pidiendo metricas de cuenta...")
    account_rows = fetch_account_insights(account_id, access_token)
    print("Pidiendo seguidores y numero de publicaciones del perfil...")
    account_rows += fetch_account_fields(account_id, access_token)
    append_csv_rows(ACCOUNT_CSV, ACCOUNT_CSV_HEADER, account_rows)
    print(f"  {len(account_rows)} filas anadidas a {ACCOUNT_CSV.name}")

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    print(f"Listando publicaciones desde {since.date()}...")
    try:
        media_items = fetch_recent_media(account_id, access_token, since)
    except requests.RequestException as exc:
        sys.exit(f"Error listando publicaciones: {exc}")
    print(f"  {len(media_items)} publicaciones encontradas")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    media_rows = []
    for item in media_items:
        media_id = item.get("id")
        if not media_id:
            continue
        caption = (item.get("caption") or "").splitlines()[0][:80] if item.get("caption") else ""
        posted_at = item.get("timestamp", "")
        print(f"  Insights de {media_id} ({posted_at})...")
        metrics = fetch_media_insights(media_id, access_token)
        for metric, value in metrics.items():
            media_rows.append([now, media_id, posted_at, caption, metric, value])

    append_csv_rows(MEDIA_CSV, MEDIA_CSV_HEADER, media_rows)
    print(f"  {len(media_rows)} filas anadidas a {MEDIA_CSV.name}")


if __name__ == "__main__":
    main()
