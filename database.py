import os
import sqlite3
import json
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple

os.makedirs("data", exist_ok=True)
DB_NAME = "data/renfe_alerts.db"

ActiveAlert = Tuple[int, int, str, str, str, str, str]
UserAlert = Tuple[int, str, str, str, str, str, int]


@contextmanager
def _get_connection() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_NAME)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with _get_connection() as conn:
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS session_cache (
                search_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                method TEXT NOT NULL,
                headers_json TEXT NOT NULL,
                post_data TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            cursor.execute("ALTER TABLE alerts ADD COLUMN arrival_time TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def upsert_session_cache(
    search_key: str,
    url: str,
    method: str,
    headers: Dict[str, str],
    post_data: Optional[str],
) -> None:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO session_cache (search_key, url, method, headers_json, post_data, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(search_key) DO UPDATE SET
                url = excluded.url,
                method = excluded.method,
                headers_json = excluded.headers_json,
                post_data = excluded.post_data,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (search_key, url, method, json.dumps(headers), post_data),
        )
        conn.commit()


def get_session_cache(search_key: str) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT url, method, headers_json, post_data
            FROM session_cache
            WHERE search_key = ?
            ''',
            (search_key,),
        )
        row = cursor.fetchone()

    if not row:
        return None

    url, method, headers_json, post_data = row
    return {
        "url": url,
        "method": method,
        "headers": json.loads(headers_json),
        "post_data": post_data,
    }


def delete_session_cache(search_key: str) -> None:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM session_cache WHERE search_key = ?', (search_key,))
        conn.commit()

def add_alert(user_id, origin, destination, date, train_time, arrival_time):
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alerts (user_id, origin, destination, date, train_time, arrival_time)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, origin, destination, date, train_time, arrival_time))
        conn.commit()

def get_active_alerts() -> List[ActiveAlert]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, user_id, origin, destination, date, train_time, arrival_time FROM alerts WHERE is_active = 1')
        alerts = cursor.fetchall()
    return alerts

def deactivate_alert(alert_id):
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE alerts SET is_active = 0 WHERE id = ?', (alert_id,))
        conn.commit()

def get_user_alerts(user_id) -> List[UserAlert]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, origin, destination, date, train_time, arrival_time, is_active 
            FROM alerts 
            WHERE user_id = ? 
            ORDER BY substr(date, 7, 4) || substr(date, 4, 2) || substr(date, 1, 2) ASC, train_time ASC
        ''', (user_id,))
        alerts = cursor.fetchall()
    return alerts

def delete_alert(alert_id):
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM alerts WHERE id = ?', (alert_id,))
        conn.commit()

if __name__ == "__main__":
    init_db()