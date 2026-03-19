import os
import asyncio
import logging
from typing import Dict, List, Tuple, TypedDict

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from scraper import get_trains
from database import get_active_alerts, delete_alert, init_db

from datetime import datetime

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - SCHEDULER - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class WaitingUser(TypedDict):
    alert_id: int
    user_id: int
    train_time: str
    arrival_time: str


GroupedAlerts = Dict[Tuple[str, str, str], List[WaitingUser]]


def _group_alerts(alerts) -> GroupedAlerts:
    grouped_searches: GroupedAlerts = {}
    for alert in alerts:
        alert_id, user_id, origin, destination, date, train_time, arrival_time = alert
        key = (origin, destination, date)
        grouped_searches.setdefault(key, []).append({
            "alert_id": alert_id,
            "user_id": user_id,
            "train_time": train_time,
            "arrival_time": arrival_time,
        })
    return grouped_searches


async def _notify_users_for_route(
    bot: Bot,
    origin: str,
    destination: str,
    date: str,
    users_waiting: List[WaitingUser],
):
    trenes = await get_trains(origin, destination, date)
    if not trenes:
        return

    for tren_web in trenes:
        if not tren_web.get('disponible'):
            continue

        for user_data in users_waiting:
            if user_data['train_time'] != tren_web.get('salida'):
                continue

            mensaje = (
                f"🚨 *¡PLAZA LIBRE DETECTADA!* 🚨\n\n"
                f"🛤️ *Trayecto:* {origin} ➡️ {destination}\n"
                f"📅 *Fecha:* {date}\n"
                f"🕒 *Horario:* {user_data['train_time']} - {user_data['arrival_time']}\n\n"
                f"👉 ¡Corre a la app de Renfe antes de que vuele!"
            )

            try:
                await bot.send_message(
                    chat_id=user_data['user_id'],
                    text=mensaje,
                    parse_mode='Markdown'
                )
                delete_alert(user_data['alert_id'])
            except Exception as e:
                logger.error("Error enviando mensaje: %s", e)

async def check_alerts():
    logger.info("Iniciando revisión de alertas...")

    alerts = get_active_alerts()
    if not alerts:
        return
    
    now = datetime.now()
    valid_alerts = []

    for alert in alerts:
        alert_id, user_id, origin, destination, date_str, train_time, arrival_time = alert
        try:
            date_time_str = f"{date_str} {train_time}"
            date_time_train = datetime.strptime(date_time_str, "%d/%m/%Y %H:%M")

            if now > date_time_train:
                logger.info("🗑️ Eliminando alerta caducada %s: %s -> %s a las %s", alert_id, origin, destination, train_time)
                delete_alert(alert_id)
            else:
                valid_alerts.append(alert)

        except Exception as e:
            logger.error("Error al comprobar la caducidad de la alerta %s: %s", alert_id, e)
            valid_alerts.append(alert)

    if not valid_alerts:
        logger.info("Todas las alertas estaban caducadas. Fin de revisión.")
        return
    
    grouped_searches = _group_alerts(valid_alerts)

    async with Bot(token=TOKEN) as bot:
        for (origin, destination, date), users_waiting in grouped_searches.items():
            await _notify_users_for_route(bot, origin, destination, date, users_waiting)

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
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()

if __name__ == '__main__':
    asyncio.run(main())