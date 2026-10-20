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

from fetch_metrics import ACCOUNT_CSV, pedir_metricas_cuenta


def datos_existentes() -> set[tuple[str, str]]:
    """(dia, metrica) que ya estan en el CSV: solo se rellena lo que falta, asi
    tambien sirve para anadir hacia atras una metrica nueva en dias que ya
    tenian las demas."""
    if not ACCOUNT_CSV.exists():
        return set()
    with ACCOUNT_CSV.open(newline="", encoding="utf-8") as f:
        return {(row["fetched_at_utc"][:10], row["metric"]) for row in csv.DictReader(f)}


def main():
    token = os.environ["IG_ACCESS_TOKEN"]
    account_id = os.environ["IG_BUSINESS_ACCOUNT_ID"]
    desde = date.fromisoformat(os.environ["DESDE"])
    hasta = date.fromisoformat(os.environ["HASTA"])
    prueba = os.environ.get("PRUEBA", "true").lower() == "true"
    ya_hay = datos_existentes()

    filas = []
    dia = desde
    while dia <= hasta:
        periodo = dia - timedelta(days=1)
        inicio = datetime.combine(periodo, time(0), timezone.utc)
        valores = pedir_metricas_cuenta(account_id, token, int(inicio.timestamp()),
                                        int((inicio + timedelta(days=1)).timestamp()))
        nuevas = {m: v for m, v in valores.items() if (dia.isoformat(), m) not in ya_hay}
        print(f"{dia} (total del {periodo}): " + ", ".join(f"{m}={v}" for m, v in valores.items())
              + f"  → nuevas: {', '.join(nuevas) or 'ninguna'}")
        recogida = datetime.combine(dia, time(8), timezone.utc).isoformat(timespec="seconds")
        fin = datetime.combine(dia, time(0), timezone.utc).isoformat(timespec="seconds")
        filas += [[recogida, m, v, fin] for m, v in nuevas.items()]
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
