import os
import asyncio
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from scraper import get_trains
from database import get_active_alerts, deactivate_alert, init_db

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - SCHEDULER - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def check_alerts():
    logger.info("Iniciando revisión de alertas...")

    alerts = get_active_alerts()
    if not alerts:
        logger.info("No hay alertas activas. Esperando al siguiente ciclo.")
        return

    grouped_searches = {}
    for alert in alerts:
        alert_id, user_id, origin, destination, date, train_time = alert
        key = (origin, destination, date)
        grouped_searches.setdefault(key, []).append({
            "alert_id": alert_id,
            "user_id": user_id,
            "train_time": train_time
        })

    async with Bot(token=TOKEN) as bot:
        for (origin, destination, date), users_waiting in grouped_searches.items():
            logger.info(f"Scrapeando Renfe para {origin}-{destination} el {date}")
            trenes = await get_trains(origin, destination, date)

            if not trenes:
                logger.error(f"Fallo al obtener trenes para {origin}-{destination}.")
                continue

            for tren_web in trenes:
                if tren_web.get('disponible'):
                    for user_data in users_waiting:
                        if user_data['train_time'] == tren_web.get('salida'):
                            mensaje = (
                                f"🚨 *¡PLAZA LIBRE DETECTADA!* 🚨\n\n"
                                f"🛤️ Trayecto: {origin} - {destination}\n"
                                f"📅 Fecha: {date}\n"
                                f"🕒 Hora: {tren_web['salida']}\n\n"
                                f"👉 ¡Corre a la web o app de Renfe para comprarlo antes de que vuele!"
                            )
                            try:
                                await bot.send_message(
                                    chat_id=user_data['user_id'],
                                    text=mensaje,
                                    parse_mode='Markdown'
                                )
                                logger.info(f"Mensaje enviado con éxito al usuario {user_data['user_id']}")
                                deactivate_alert(user_data['alert_id'])
                            except Exception as e:
                                logger.error(f"Error enviando mensaje a {user_data['user_id']}: {e}")

    logger.info("Revisión de alertas finalizada.")

async def main():
    if not TOKEN:
        logger.error("Error: No hay token de Telegram en el archivo .env")
        return

    logger.info("Iniciando Scheduler...")

    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts, 'interval', seconds=5)
    logger.info("Job programado: revisar alertas cada 5 segundos")

    scheduler.start()

    try:
        # Mantener el proceso vivo dentro del loop async
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()

if __name__ == '__main__':
    asyncio.run(main())