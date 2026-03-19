import os
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

os.makedirs("data", exist_ok=True)
DB_NAME = "data/renfe_alerts.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            date TEXT NOT NULL,
            train_time TEXT NOT NULL,
            arrival_time TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    try:
        cursor.execute("ALTER TABLE alerts ADD COLUMN arrival_time TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def add_alert(user_id, origin, destination, date, train_time, arrival_time):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO alerts (user_id, origin, destination, date, train_time, arrival_time)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, origin, destination, date, train_time, arrival_time))
    conn.commit()
    conn.close()

def get_active_alerts():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, origin, destination, date, train_time, arrival_time FROM alerts WHERE is_active = 1')
    alerts = cursor.fetchall()
    conn.close()
    return alerts

def deactivate_alert(alert_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE alerts SET is_active = 0 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()

def get_user_alerts(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, origin, destination, date, train_time, arrival_time, is_active FROM alerts WHERE user_id = ?', (user_id,))
    alerts = cursor.fetchall()
    conn.close()
    return alerts

def delete_alert(alert_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM alerts WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()

async def cancel_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    alertas = get_user_alerts(user_id)
    
    if not alertas:
        await update.message.reply_text("📭 No tienes alertas activas para cancelar.")
        return

    await update.message.reply_text("🗑️ **Selecciona la alerta que quieres eliminar:**")
    
    for alerta in alertas:
        alert_id, origin, destination, date, train_time, arrival_time, is_active = alerta
        
        texto_boton = f"❌ Borrar {train_time} ({origin} -> {destination})"
        keyboard = [[InlineKeyboardButton(texto_boton, callback_data=f"borrar_{alert_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        mensaje = f"📅 {date} | 🕒 {train_time} - {arrival_time}"
        await update.message.reply_text(mensaje, reply_markup=reply_markup)

if __name__ == "__main__":
    init_db()