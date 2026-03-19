import os
import asyncio
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

# Importamos nuestras herramientas
from scraper import get_trains
from database import get_active_alerts, deactivate_alert

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - SCHEDULER - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

async def check_alerts():
    """Función principal que revisa las alertas activas."""
    logger.info("Iniciando revisión de alertas...")
    
    alerts = get_active_alerts()
    if not alerts:
        logger.info("No hay alertas activas. Esperando al siguiente ciclo.")
        return

    # Agrupamos las alertas por (origen, destino, fecha) para minimizar el scraping.
    # Si 3 usuarios quieren trenes distintos el mismo día para la misma ruta,
    # solo hacemos 1 petición a Renfe y revisamos las 3 horas.
    grouped_searches = {}
    for alert in alerts:
        alert_id, user_id, origin, destination, date, train_time = alert
        key = (origin, destination, date)
        
        if key not in grouped_searches:
            grouped_searches[key] = []
        
        grouped_searches[key].append({
            "alert_id": alert_id,
            "user_id": user_id,
            "train_time": train_time
        })

    # Inicializamos el bot de Telegram para enviar mensajes
    bot = Bot(token=TOKEN)

    # Procesamos cada búsqueda agrupada
    for (origin, destination, date), users_waiting in grouped_searches.items():
        logger.info(f"Scrapeando Renfe para {origin}-{destination} el {date}")
        
        # Llamamos al scraper
        trenes = await get_trains(origin, destination, date)
        
        if not trenes:
            logger.error(f"Fallo al obtener trenes para {origin}-{destination}.")
            continue

        # Revisamos si alguno de los trenes que los usuarios esperan está disponible
        for tren_web in trenes:
            if tren_web['disponible']:
                # Comprobamos si alguien estaba esperando ESTA hora concreta
                for user_data in users_waiting:
                    if user_data['train_time'] == tren_web['salida']:
                        # ¡BINGO! El tren está libre.
                        mensaje = (
                            f"🚨 **¡PLAZA LIBRE DETECTADA!** 🚨\n\n"
                            f"🛤️ Trayecto: {origin} - {destination}\n"
                            f"📅 Fecha: {date}\n"
                            f"🕒 Hora: {tren_web['salida']}\n\n"
                            f"👉 ¡Corre a la web o app de Renfe para comprarlo antes de que vuele!"
                        )
                        
                        try:
                            # Enviamos el mensaje al usuario
                            await bot.send_message(chat_id=user_data['user_id'], text=mensaje, parse_mode='Markdown')
                            logger.info(f"Mensaje enviado con éxito al usuario {user_data['user_id']}")
                            
                            # Desactivamos la alerta en la base de datos para no volver a avisar
                            deactivate_alert(user_data['alert_id'])
                        except Exception as e:
                            logger.error(f"Error enviando mensaje a {user_data['user_id']}: {e}")

    logger.info("Revisión de alertas finalizada.")

def main():
    if not TOKEN:
        logger.error("Error: No hay token de Telegram en el archivo .env")
        return

    logger.info("Iniciando Scheduler...")
    
    # Creamos el programador asíncrono
    scheduler = AsyncIOScheduler()
    
    # Añadimos el trabajo. Aquí he puesto 5 SEGUNDOS como pediste. 
    # Vuelvo a insistir: para producción cambia "seconds=5" por "minutes=5"
    scheduler.add_job(check_alerts, 'interval', seconds=5)
    
    scheduler.start()

    # Mantenemos el proceso vivo
    try:
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == '__main__':
    main()