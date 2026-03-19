import os
import sqlite3

os.makedirs("data", exist_ok=True)
DB_NAME = "renfe_alerts.db"

def init_db():
    """Crea la tabla de alertas si no existe."""
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
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

def add_alert(user_id, origin, destination, date, train_time):
    """Añade una nueva alerta a la base de datos."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO alerts (user_id, origin, destination, date, train_time)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, origin, destination, date, train_time))
    conn.commit()
    conn.close()

def get_active_alerts():
    """Devuelve todas las alertas que están activas."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Obtenemos id_alerta, usuario, origen, destino, fecha y hora del tren
    cursor.execute('''
        SELECT id, user_id, origin, destination, date, train_time 
        FROM alerts 
        WHERE is_active = 1
    ''')
    alerts = cursor.fetchall()
    conn.close()
    return alerts

def deactivate_alert(alert_id):
    """Marca una alerta como inactiva para no volver a avisar."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE alerts SET is_active = 0 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Base de datos inicializada correctamente.")