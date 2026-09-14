"""Tests de la recoleccion de basura de sesiones y del storage_state persistido.

Cubren las dos mejoras para agilizar el bot en momentos de cola sin hinchar el
disco del servidor:

- `prune_expired_session_cache`: borra las sesiones cacheadas de fechas ya
  pasadas para que `session_cache` no crezca sin tope en el volumen.
- persistencia del `storage_state` (solo cookies, con tope de tamano) para que
  el pase de la cola virtual de Renfe sobreviva a un reinicio del contenedor.

Ninguno hace red: la DB se aisla en un directorio temporal y el storage_state
se escribe a un fichero temporal.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOY = "14/09/2026"


class PruneExpiredSessionCacheTest(unittest.TestCase):
    def setUp(self):
        # Aislar la DB en un tmp para no tocar data/renfe_alerts.db.
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)
        import database
        self.database = importlib.reload(database)
        self.database.init_db()

    def tearDown(self):
        os.chdir(self._prev_cwd)
        # Devolver el modulo a su estado normal para no contaminar otros tests.
        import database
        importlib.reload(database)

    def _add(self, origin, destination, date):
        self.database.upsert_session_cache(
            self.database.build_search_key(origin, destination, date),
            "http://x", "POST", {"h": "1"}, "payload",
        )

    def _exists(self, origin, destination, date):
        key = self.database.build_search_key(origin, destination, date)
        return self.database.get_session_cache(key) is not None

    def test_borra_solo_fechas_pasadas(self):
        self._add("A", "B", "01/09/2026")   # pasada
        self._add("A", "B", "13/09/2026")   # pasada
        self._add("A", "B", "14/09/2026")   # hoy -> se conserva
        self._add("A", "B", "20/09/2026")   # futura
        self._add("Puerto de Santa María", "San Bernardo", "10/10/2026")  # con guiones en el nombre

        borradas = self.database.prune_expired_session_cache(today=HOY)

        self.assertEqual(borradas, 2)
        self.assertFalse(self._exists("A", "B", "01/09/2026"))
        self.assertFalse(self._exists("A", "B", "13/09/2026"))
        self.assertTrue(self._exists("A", "B", "14/09/2026"))
        self.assertTrue(self._exists("A", "B", "20/09/2026"))
        self.assertTrue(self._exists("Puerto de Santa María", "San Bernardo", "10/10/2026"))

    def test_idempotente_y_respeta_claves_sin_fecha(self):
        self._add("A", "B", "20/09/2026")
        self.database.upsert_session_cache(
            "clave-sin-fecha-valida", "http://x", "POST", {}, "p"
        )

        primera = self.database.prune_expired_session_cache(today=HOY)
        segunda = self.database.prune_expired_session_cache(today=HOY)

        self.assertEqual(primera, 0)
        self.assertEqual(segunda, 0)
        # Una clave cuyo ultimo segmento no es una fecha parseable no se toca:
        # mejor dejar una fila de mas que borrar por un formato inesperado.
        self.assertIsNotNone(self.database.get_session_cache("clave-sin-fecha-valida"))


class StorageStatePersistenceTest(unittest.TestCase):
    def setUp(self):
        import scraper
        self.scraper = scraper
        self._tmp = tempfile.mkdtemp()
        self._prev_path = scraper.STORAGE_STATE_PATH
        self._prev_max = scraper.STORAGE_STATE_MAX_BYTES
        scraper.STORAGE_STATE_PATH = os.path.join(self._tmp, "renfe_storage_state.json")

    def tearDown(self):
        self.scraper.STORAGE_STATE_PATH = self._prev_path
        self.scraper.STORAGE_STATE_MAX_BYTES = self._prev_max

    def test_persiste_solo_cookies_y_recarga(self):
        state = {
            "cookies": [
                {"name": "Queue-it", "value": "pass123"},
                {"name": "sess", "value": "abc"},
            ],
            "origins": [
                {"origin": "https://x",
                 "localStorage": [{"name": "big", "value": "Z" * 5000}]},
            ],
        }
        self.scraper._persist_storage_state(state)

        raw = json.load(open(self.scraper.STORAGE_STATE_PATH, encoding="utf-8"))
        self.assertEqual(raw["origins"], [])  # localStorage descartado
        self.assertEqual(len(raw["cookies"]), 2)

        loaded = self.scraper._load_storage_state_from_disk()
        self.assertEqual(loaded["cookies"][0]["name"], "Queue-it")

    def test_no_sobreescribe_si_supera_el_tope(self):
        self.scraper._persist_storage_state(
            {"cookies": [{"name": "ok", "value": "1"}], "origins": []}
        )
        self.scraper.STORAGE_STATE_MAX_BYTES = 100
        self.scraper._persist_storage_state(
            {"cookies": [{"name": "x", "value": "Y" * 10000}], "origins": []}
        )
        raw = json.load(open(self.scraper.STORAGE_STATE_PATH, encoding="utf-8"))
        self.assertEqual(raw["cookies"][0]["name"], "ok")

    def test_sin_cookies_no_crea_fichero(self):
        self.scraper._persist_storage_state({"cookies": [], "origins": []})
        self.assertFalse(os.path.exists(self.scraper.STORAGE_STATE_PATH))
        self.assertIsNone(self.scraper._load_storage_state_from_disk())


if __name__ == "__main__":
    unittest.main()
