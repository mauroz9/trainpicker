import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    ConversationHandler,
    CallbackQueryHandler # Añadido para escuchar los botones
)

# Importamos nuestro scraper y la base de datos
from scraper import get_trains
from database import add_alert, init_db, get_user_alerts

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

ORIGEN, DESTINO, FECHA = range(3)

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
        "🔹 /info - Muestra este mensaje de ayuda.\n\n"
        "💡 *¿Cómo crear una alerta?*\n"
        "Usa /buscar. Si el tren que quieres aparece como '❌ COMPLETO', verás un botón debajo del mensaje para crear la alerta. ¡Púlsalo y yo me encargo del resto!"
    )
    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = "🚄 ¡Hola! Te ayudaré a buscar trenes.\nUsa /buscar para iniciar una búsqueda."
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
    fecha = update.message.text
    context.user_data['fecha'] = fecha # Guardamos la fecha en memoria
    origen = context.user_data['origen']
    destino = context.user_data['destino']
    
    await update.message.reply_text("⏳ Buscando trenes en Renfe... Esto puede tardar unos segundos.")

    trenes = await get_trains(origen, destino, fecha)

    if not trenes:
        await update.message.reply_text("❌ No se han encontrado trenes o hubo un error al leer la web de Renfe.")
        return ConversationHandler.END

    mensaje_respuesta = f"🚆 **Trenes {origen} - {destino} el {fecha}**\n\n"
    keyboard = []

    for tren in trenes:
        if tren['disponible']:
            estado = "✅ DISPONIBLE"
            mensaje_respuesta += f"🕒 {tren['salida']} - {tren['llegada']} | {estado}\n"
        else:
            estado = "❌ COMPLETO"
            mensaje_respuesta += f"🕒 {tren['salida']} - {tren['llegada']} | {estado}\n"
            
            # Si está completo, creamos un botón para él. 
            # El callback_data guarda la hora de salida para identificarlo luego.
            boton = InlineKeyboardButton(
                text=f"🔔 Crear alerta: {tren['salida']}", 
                callback_data=f"alerta_{tren['salida']}"
            )
            keyboard.append([boton]) # Lo metemos como una fila nueva

    # Si hay botones (trenes llenos), creamos el panel (Markup)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    # Enviamos el mensaje con los botones adjuntos
    await update.message.reply_text(mensaje_respuesta, reply_markup=reply_markup)
    return ConversationHandler.END

async def listar_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    alertas = get_user_alerts(user_id)
    
    if not alertas:
        await update.message.reply_text("📭 No tienes ninguna alerta configurada en este momento.")
        return

    mensaje = "📋 *Tus Alertas Configuradas:*\n\n"
    for alerta in alertas:
        alert_id, origin, destination, date, train_time, is_active = alerta
        estado = "✅ Consultando..." if is_active else "❌ Inactiva / Ya avisada"
        
        mensaje += f"🔹ID{alert_id}: *{origin} ➡️ {destination}* "
        mensaje += f"   📅 {date} | 🕒 {train_time} | {estado}\n\n"

    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def manejar_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Esta función se activa cuando un usuario hace clic en un botón Inline."""
    query = update.callback_query
    await query.answer() # Esto quita el icono de "cargando" en el botón del usuario
    
    datos = query.data # Aquí viene el "alerta_10:30"
    
    if datos.startswith("alerta_"):
        hora_tren = datos.split("_")[1]
        user_id = query.from_user.id
        
        # Recuperamos los datos de la memoria de la conversación
        origen = context.user_data.get('origen')
        destino = context.user_data.get('destino')
        fecha = context.user_data.get('fecha')
        
        if not (origen and destino and fecha):
            await query.edit_message_text(text="⚠️ La sesión expiró o faltan datos. Vuelve a usar /buscar.")
            return
        
        # Guardamos en la base de datos SQLite
        add_alert(user_id, origen, destino, fecha, hora_tren)
        
        # Modificamos el mensaje original para confirmar que se ha creado la alerta
        texto_confirmacion = (
            f"✅ **¡Alerta registrada con éxito!**\n\n"
            f"Te avisaré automáticamente si detecto una plaza libre para el tren "
            f"{origen}-{destino} de las {hora_tren} el día {fecha}."
        )
        await query.edit_message_text(text=texto_confirmacion)

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operación cancelada. Escribe /buscar cuando quieras volver a intentarlo.")
    return ConversationHandler.END

def main():
    if not TOKEN:
        print("Error: No hay token configurado.")
        return

    init_db() # Nos aseguramos de que la base de datos exista al arrancar
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
    application.add_handler(conv_handler)


    print("Bot en ejecución...")
    application.run_polling()

if __name__ == '__main__':
    main()