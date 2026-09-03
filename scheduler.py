import os
import asyncio
import logging
from typing import Any, Dict, List, Tuple, TypedDict

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from scraper import build_search_key, get_trains_cached_only, refresh_session
from database import get_active_alerts, delete_alert, get_session_cache, init_db

from datetime import datetime

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FAST_CHECK_INTERVAL_SECONDS = int(os.getenv("FAST_CHECK_INTERVAL_SECONDS", "3"))
SESSION_REFRESH_INTERVAL_SECONDS = int(os.getenv("SESSION_REFRESH_INTERVAL_SECONDS", "20"))
MAX_CONCURRENT_REFRESHES = int(os.getenv("MAX_CONCURRENT_REFRESHES", "2"))

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
    trenes: List[Dict[str, Any]],
):
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

def _get_valid_grouped_alerts() -> GroupedAlerts:
    """Descarta alertas de trenes ya pasados y agrupa el resto por ruta+fecha."""
    alerts = get_active_alerts()
    if not alerts:
        return {}

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

    return _group_alerts(valid_alerts)


async def fast_check_alerts():
    """Job rapido: solo lee la sesion cacheada, nunca abre Playwright.

    Si una ruta no tiene sesion cacheada valida, se salta ese ciclo para esa
    ruta sin bloquear la comprobacion de las demas. `refresh_sessions` es
    quien se encarga de recapturar la sesion con Playwright.
    """
    grouped_searches = _get_valid_grouped_alerts()
    if not grouped_searches:
        return

    async with Bot(token=TOKEN) as bot:
        for (origin, destination, date), users_waiting in grouped_searches.items():
            try:
                trenes = await get_trains_cached_only(origin, destination, date)
            except Exception as e:
                logger.exception("Error en fast_check_alerts para %s -> %s: %s", origin, destination, e)
                continue

            if trenes is None:
                continue

            await _notify_users_for_route(bot, origin, destination, date, users_waiting, trenes)


async def _refresh_route(
    semaphore: asyncio.Semaphore,
    bot: Bot,
    origin: str,
    destination: str,
    date: str,
    users_waiting: List[WaitingUser],
):
    async with semaphore:
        try:
            trenes = await refresh_session(origin, destination, date)
        except Exception as e:
            logger.exception("Error recapturando sesion para %s -> %s: %s", origin, destination, e)
            return

    await _notify_users_for_route(bot, origin, destination, date, users_waiting, trenes)


async def refresh_sessions():
    """Job lento: recaptura con Playwright solo las rutas sin cache valido.

    Acotado por `MAX_CONCURRENT_REFRESHES` para no abrir demasiados
    navegadores a la vez. Si la recaptura ya trae plaza libre, notifica al
    instante en vez de esperar al siguiente `fast_check_alerts`.
    """
    grouped_searches = _get_valid_grouped_alerts()
    if not grouped_searches:
        return

    routes_needing_refresh = [
        (route, users_waiting)
        for route, users_waiting in grouped_searches.items()
        if get_session_cache(build_search_key(*route)) is None
    ]

    if not routes_needing_refresh:
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REFRESHES)
    async with Bot(token=TOKEN) as bot:
        await asyncio.gather(*[
            _refresh_route(semaphore, bot, origin, destination, date, users_waiting)
            for (origin, destination, date), users_waiting in routes_needing_refresh
        ])

async def main():
    if not TOKEN:
        logger.error("Error: No hay token de Telegram en el archivo .env")
        return

    logger.info("Iniciando Scheduler...")

    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        fast_check_alerts, 'interval',
        seconds=FAST_CHECK_INTERVAL_SECONDS,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        refresh_sessions, 'interval',
        seconds=SESSION_REFRESH_INTERVAL_SECONDS,
        max_instances=1, coalesce=True,
    )
    logger.info(
        "Jobs programados: fast_check_alerts cada %ss (solo cache), "
        "refresh_sessions cada %ss (Playwright, max %s concurrentes)",
        FAST_CHECK_INTERVAL_SECONDS, SESSION_REFRESH_INTERVAL_SECONDS, MAX_CONCURRENT_REFRESHES,
    )

    scheduler.start()

    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()

if __name__ == '__main__':
    asyncio.run(main())