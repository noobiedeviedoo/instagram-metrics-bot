#!/usr/bin/env python3
"""
Bot de audio semanal - convierte a voz el analisis y lo manda por Telegram.

Pensado para ejecutarse desde GitHub Actions, en un cron posterior al de
fetch_metrics.py (necesita que el analisis ya este escrito en el repo antes
de correr). El analisis en si NO lo escribe este script: lo escribe Claude,
en una tarea programada de Cowork, en el archivo `weekly_analysis.txt` de la
raiz del repo (mismo patron que account_metrics.csv/media_metrics.csv: este
script solo lee lo que ya esta en el repo, no llama a ninguna IA).

Para no reenviar el mismo audio si el workflow se ejecuta mas de una vez
antes de que haya analisis nuevo, se guarda un hash del ultimo texto enviado
en `.last_sent_analysis_hash` y se compara en cada ejecucion.

Variables de entorno requeridas (GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN   - token del bot de Telegram (el mismo que tts-telegram-bot)
    TELEGRAM_CHAT_ID     - chat_id al que mandar el audio

Variables de entorno opcionales:
    TTS_VOICE            - voz de edge-tts (por defecto es-ES-AlvaroNeural,
                            la misma que usa tts-telegram-bot por defecto)
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import edge_tts
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_PATH = REPO_ROOT / "weekly_analysis.txt"
HASH_MARKER_PATH = REPO_ROOT / ".last_sent_analysis_hash"


def send_audio_telegram(audio_path: str, token: str, chat_id: str, caption: str = ""):
    with open(audio_path, "rb") as audio:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendAudio",
            data={"chat_id": chat_id, "title": "Analisis semanal de Instagram", "caption": caption[:1024]},
            files={"audio": audio},
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

    if not ANALYSIS_PATH.exists():
        print(f"No existe {ANALYSIS_PATH.name} todavia — nada que mandar. Saliendo sin error.")
        return

    texto = ANALYSIS_PATH.read_text(encoding="utf-8").strip()
    if not texto:
        print(f"{ANALYSIS_PATH.name} esta vacio — nada que mandar.")
        return

    texto_hash = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    hash_anterior = HASH_MARKER_PATH.read_text(encoding="utf-8").strip() if HASH_MARKER_PATH.exists() else ""
    if texto_hash == hash_anterior:
        print("El analisis no ha cambiado desde el ultimo envio — no se manda audio duplicado.")
        return

    voz = os.environ.get("TTS_VOICE", "es-ES-AlvaroNeural")
    print(f"Generando audio con la voz {voz}...")

    ruta_audio = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            ruta_audio = tmp.name

        import asyncio

        async def generar():
            communicate = edge_tts.Communicate(texto, voz)
            await communicate.save(ruta_audio)

        asyncio.run(generar())

        print("Mandando audio por Telegram...")
        # El caption de Telegram tiene limite de 1024 caracteres; el texto
        # completo ya va hablado en el audio, el caption es solo un resumen.
        send_audio_telegram(ruta_audio, token, chat_id, caption=texto[:1000])
        print("Enviado correctamente.")
    finally:
        if ruta_audio and os.path.exists(ruta_audio):
            os.remove(ruta_audio)

    HASH_MARKER_PATH.write_text(texto_hash, encoding="utf-8")


if __name__ == "__main__":
    main()
