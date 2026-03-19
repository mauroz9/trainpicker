import os
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    ConversationHandler,
    CallbackQueryHandler
)

from scraper import get_trains
from database import add_alert, delete_alert, init_db, get_user_alerts

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ORIGEN, DESTINO, FECHA = range(3)


def _build_trains_message(fecha: str, trenes: List[Dict[str, Any]]) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    origen_real = trenes[0]['origen']
    destino_real = trenes[0]['destino']
    mensaje_respuesta = f"🚆 **Trenes {origen_real} ➡️ {destino_real} el {fecha}**\n\n"

    keyboard = []
    for tren in trenes:
        if tren['disponible']:
            estado = "✅ DISPONIBLE"
        else:
            estado = "❌ COMPLETO"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🔔 Avisar: {tren['salida']} a {tren['llegada']}",
                    callback_data=f"alerta_{tren['salida']}"
                )
            ])
        mensaje_respuesta += f"🕒 {tren['salida']} - {tren['llegada']} | {estado}\n"

    return mensaje_respuesta, InlineKeyboardMarkup(keyboard) if keyboard else None


def _get_selected_train(context_data: Dict[str, Any], hora_tren: str) -> Optional[Dict[str, Any]]:
    trenes = context_data.get('trenes_encontrados', [])
    return next((tren for tren in trenes if tren['salida'] == hora_tren), None)

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la información del bot y los comandos disponibles."""
    mensaje = (
        "ℹ️ *Información y Comandos Disponibles*\n\n"
        "Soy un bot diseñado para vigilar la web de Renfe y avisarte en cuanto se libere una plaza en un tren completo.\n\n"
        "🛠️ *Lista de Comandos:*\n"
        "🔹 /start - Inicia el bot y muestra el mensaje de bienvenida.\n"
        "🔹 /buscar - Inicia una nueva búsqueda de trenes paso a paso.\n"
        "🔹 /listar - Muestra las alertas de trenes que tienes activas en este momento.\n"
        "🔹 /cancelar - Detiene la búsqueda actual si te has equivocado al meter un dato.\n"
        "🔹 /anular - Anula la alerta pulsando sobre ella.\n"
        "🔹 /info - Muestra este mensaje de ayuda.\n\n"
        "💡 *¿Cómo crear una alerta?*\n"
        "Usa /buscar. Si el tren que quieres aparece como '❌ COMPLETO', verás un botón debajo del mensaje para crear la alerta. ¡Púlsalo y yo me encargo del resto!"
    )
    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "🚄 *¡Hola! Soy TrainPicker, tu vigilante personal de Renfe* 🤖\n\n"
        "¿Cansado de ver el temido cartel de 'Tren Completo'? Mi misión es escanear la web de Renfe sin descanso y mandarte una alerta al instante en cuanto alguien devuelva un billete o se libere una plaza.\n\n"
        "Tú solo dime qué tren quieres, ¡y yo haré el trabajo duro!\n\n"
        "🛠️ *Comandos disponibles:*\n"
        "🔹 /buscar - Inicia una nueva búsqueda de trenes paso a paso.\n"
        "🔹 /listar - Muestra las alertas de trenes que tienes activas en este momento.\n"
        "🔹 /cancelar - Detiene la búsqueda actual si te has equivocado al meter un dato.\n"
        "🔹 /anular - Anula la alerta pulsando sobre ella.\n"
        "🔹 /info - Muestra este mensaje de ayuda.\n\n"
        "💡 *¿Listo para cazar billetes?*\n"
        "Pulsa o escribe /buscar para empezar."
    )
    await update.message.reply_text(welcome_message)

async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚉 Dime la estación de ORIGEN (Ej: Madrid):")
    return ORIGEN

async def recibir_origen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['origen'] = update.message.text
    await update.message.reply_text("🛤️ Perfecto. Ahora dime la estación de DESTINO (Ej: Sevilla):")
    return DESTINO

async def recibir_destino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['destino'] = update.message.text
    await update.message.reply_text("📅 Genial. Dime la FECHA del viaje en formato DD/MM/AAAA:")
    return FECHA

async def recibir_fecha_y_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha_str = update.message.text
    
    try:
        fecha_obj = datetime.strptime(fecha_str, "%d/%m/%Y")
        hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        if fecha_obj < hoy:
            await update.message.reply_text(
                "❌ *Error:* La fecha introducida ya ha pasado.\n"
                "Búsqueda cancelada. Escribe /buscar para volver a empezar.", 
                parse_mode='Markdown'
            )
            return ConversationHandler.END
            
    except ValueError:
        await update.message.reply_text(
            "❌ *Error:* Formato de fecha incorrecto.\n"
            "Debes usar el formato DD/MM/AAAA (ej: 25/04/2026).\n"
            "Búsqueda cancelada. Escribe /buscar para volver a empezar.", 
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['fecha'] = fecha_str 
    origen = context.user_data['origen']
    destino = context.user_data['destino']
    
    await update.message.reply_text("⏳ Consultando trenes...")
    
    try:
        trenes = await get_trains(origen, destino, fecha_str)
    except Exception as e:
        logger.error("Error crítico al buscar trenes: %s", e)
        await update.message.reply_text("❌ Ha ocurrido un problema de conexión con Renfe. Inténtalo de nuevo en unos minutos.")
        return ConversationHandler.END

    if not trenes:
        dias_diferencia = (fecha_obj - hoy).days
        
        if dias_diferencia > 60:
            mensaje_error = (
                "❌ *No se han encontrado trenes.*\n\n"
                "💡 *Consejo:* Estás buscando un tren para dentro de más de 2 meses. "
                "Renfe suele publicar los billetes con 30-60 días de antelación. "
                "Es muy probable que aún no estén a la venta. ¡Vuelve a intentarlo en unas semanas!"
            )
        else:
            mensaje_error = (
                "❌ *No se han encontrado trenes.*\n\n"
                "Revisa que los nombres de las estaciones sean correctos (ej: 'Madrid' o 'Sevilla') "
                "o es posible que no haya trenes directos para ese día."
            )
            
        await update.message.reply_text(mensaje_error, parse_mode='Markdown')
        return ConversationHandler.END

    context.user_data['trenes_encontrados'] = trenes
    mensaje_respuesta, reply_markup = _build_trains_message(fecha_str, trenes)
    await update.message.reply_text(mensaje_respuesta, reply_markup=reply_markup, parse_mode='Markdown')
    return ConversationHandler.END

async def listar_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    alertas = get_user_alerts(user_id)
    
    if not alertas:
        await update.message.reply_text("📭 No tienes alertas configuradas.")
        return

    mensaje = "📋 *Tus Alertas:*\n\n"
    for alerta in alertas:
        _, origin, destination, date, train_time, arrival_time, is_active = alerta
        estado = "✅ Buscando..." if is_active else "❌ Inactiva"
        
        mensaje += f"🔹 *{origin} ➡️ {destination}*\n"
        mensaje += f"   📅 {date} | 🕒 {train_time} - {arrival_time} | {estado}\n\n"

    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def cancel_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    alertas = get_user_alerts(user_id)
    
    if not alertas:
        await update.message.reply_text("📭 No tienes alertas activas para anular.")
        return

    await update.message.reply_text("🗑️ **Selecciona la alerta que quieres anular:**", parse_mode='Markdown')
    
    for alerta in alertas:
        alert_id, origin, destination, date, train_time, arrival_time, _ = alerta
        
        keyboard = [[InlineKeyboardButton(f"❌ Anular alerta", callback_data=f"borrar_{alert_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        mensaje = f"🛤️ {origin} ➡️ {destination}\n📅 {date} | 🕒 {train_time}"
        await update.message.reply_text(mensaje, reply_markup=reply_markup)

async def manejar_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    datos = query.data 
    
    if datos.startswith("alerta_"):
        hora_tren = datos.split("_")[1]
        user_id = query.from_user.id
        fecha = context.user_data.get('fecha')
        
        tren_elegido = _get_selected_train(context.user_data, hora_tren)
        
        if not tren_elegido:
            await query.edit_message_text(text="⚠️ Sesión expirada. Vuelve a usar /buscar.")
            return
        
        origen_real = tren_elegido['origen']
        destino_real = tren_elegido['destino']
        llegada_real = tren_elegido['llegada']
        
        add_alert(user_id, origen_real, destino_real, fecha, hora_tren, llegada_real)
        
        texto_confirmacion = (
            f"✅ **¡Alerta registrada con éxito!**\n\n"
            f"Te avisaré si detecto una plaza libre:\n"
            f"🛤️ {origen_real} ➡️ {destino_real}\n"
            f"📅 {fecha} | 🕒 {hora_tren} - {llegada_real}"
        )
        await query.edit_message_text(text=texto_confirmacion)
    elif datos.startswith("borrar_"):
        alert_id = datos.split("_")[1]
        try:
            delete_alert(alert_id)
            await query.edit_message_text(text="🗑️ La alerta ha sido anulada correctamente.")
        except Exception as e:
            logger.error("Error al borrar alerta: %s", e)
            await query.edit_message_text(text="⚠️ No se pudo anular la alerta. Inténtalo de nuevo.")

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Busqueda cancelada. Escribe /buscar cuando quieras volver a intentarlo.")
    return ConversationHandler.END

def main():
    if not TOKEN:
        logger.error("Error: No hay token configurado.")
        return

    init_db()
    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('buscar', iniciar_busqueda)],
        states={
            ORIGEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_origen)],
            DESTINO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_destino)],
            FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_fecha_y_buscar)],
        },
        fallbacks=[CommandHandler('cancelar', cancelar)]
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CallbackQueryHandler(manejar_boton))
    application.add_handler(CommandHandler('listar', listar_alertas))
    application.add_handler(CommandHandler('info', info_command))
    application.add_handler(CommandHandler('anular', cancel_alert))
    application.add_handler(conv_handler)

    logger.info("Bot en ejecución...")
    application.run_polling()

if __name__ == '__main__':
    main()