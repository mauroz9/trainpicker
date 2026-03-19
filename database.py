import os
import sqlite3

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

if __name__ == "__main__":
    init_db()