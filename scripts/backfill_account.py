#!/usr/bin/env python3
"""
Rellena huecos de account_metrics.csv (dias en que fetch_metrics.py no corrio,
p. ej. del 2 al 19 de octubre de 2026 por el token caducado).

Para cada dia D del hueco pide a la API el total del dia anterior completo
(since = D-1 00:00 UTC, until = D 00:00 UTC), que es lo que mide la recogida
de cada manana, y lo anade como si se hubiera recogido el dia D a las 08:00
UTC. En period_end_utc se apunta el fin del periodo para distinguir estas
filas de las normales. Instagram solo guarda unos 30 dias: lo anterior no se
puede recuperar. Seguidores y numero de publicaciones son una foto del momento
y no se pueden pedir hacia atras.

Lo lanza a mano backfill.yml. Variables de entorno:
    IG_ACCESS_TOKEN, IG_BUSINESS_ACCOUNT_ID
    DESDE, HASTA  - primer y ultimo dia a rellenar (AAAA-MM-DD, dia de recogida)
    PRUEBA        - "true": solo imprime los valores, no escribe nada
"""
import csv
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

import requests

from fetch_metrics import ACCOUNT_CSV, ACCOUNT_METRICS, GRAPH_API_BASE


def total_del_dia(account_id: str, token: str, metric: str, dia: date) -> int | None:
    inicio = datetime.combine(dia, time(0), timezone.utc)
    params = {
        "metric": metric,
        "period": "day",
        "metric_type": "total_value",
        "since": int(inicio.timestamp()),
        "until": int((inicio + timedelta(days=1)).timestamp()),
        "access_token": token,
    }
    resp = requests.get(f"{GRAPH_API_BASE}/{account_id}/insights", params=params, timeout=30)
    if not resp.ok:
        print(f"  Aviso: {metric} del {dia}: {resp.text}")
        return None
    for entry in resp.json().get("data", []):
        valor = entry.get("total_value", {}).get("value")
        if valor is not None:
            return valor
    return None


def dias_con_datos() -> set[str]:
    if not ACCOUNT_CSV.exists():
        return set()
    with ACCOUNT_CSV.open(newline="", encoding="utf-8") as f:
        return {row["fetched_at_utc"][:10] for row in csv.DictReader(f)}


def main():
    token = os.environ["IG_ACCESS_TOKEN"]
    account_id = os.environ["IG_BUSINESS_ACCOUNT_ID"]
    desde = date.fromisoformat(os.environ["DESDE"])
    hasta = date.fromisoformat(os.environ["HASTA"])
    prueba = os.environ.get("PRUEBA", "true").lower() == "true"
    ya_hay = dias_con_datos()

    filas = []
    dia = desde
    while dia <= hasta:
        periodo = dia - timedelta(days=1)
        marca = "(ya hay datos de este dia)" if dia.isoformat() in ya_hay else ""
        valores = {m: total_del_dia(account_id, token, m, periodo) for m in ACCOUNT_METRICS}
        print(f"{dia} (total del {periodo}): " + ", ".join(f"{m}={v}" for m, v in valores.items()) + f" {marca}")
        if not marca:
            recogida = datetime.combine(dia, time(8), timezone.utc).isoformat(timespec="seconds")
            fin = datetime.combine(dia, time(0), timezone.utc).isoformat(timespec="seconds")
            filas += [[recogida, m, v, fin] for m, v in valores.items() if v is not None]
        dia += timedelta(days=1)

    if prueba:
        print(f"\nPRUEBA: no se escribe nada ({len(filas)} filas se anadirian).")
        return
    if not filas:
        print("Nada que anadir.")
        return
    # Insertar en orden cronologico: los graficos y el analisis leen por fecha
    with ACCOUNT_CSV.open(newline="", encoding="utf-8") as f:
        lector = csv.reader(f)
        cabecera = next(lector)
        todas = list(lector)
    todas = sorted(todas + filas, key=lambda fila: fila[0])
    with ACCOUNT_CSV.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(cabecera)
        escritor.writerows(todas)
    print(f"\n{len(filas)} filas anadidas a account_metrics.csv")


if __name__ == "__main__":
    sys.exit(main())
