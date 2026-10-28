"""Tests de la resolucion de estaciones contra el catalogo de Renfe (issue #40).

Desde la web nueva de renfe.com (oct. 2026) el scraper ya no pilota el
autocompletado: elige la estacion en el catalogo `estacionesEstatico` y manda
el POST a `buscarTren.do` el mismo. Las entradas son extractos reales del
catalogo, en su orden original. No hacen ninguna llamada de red.

Ejecutar con:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import _es_fecha_sistema_nuevo, _payload_busqueda, elegir_estacion  # noqa: E402


def estacion(codigo, nombre, clave=None):
    return {
        "cdgoEstacion": codigo,
        "desgEstacion": nombre,
        "desgEstacionPlano": nombre,
        "clave": clave or f"0071,{codigo},{codigo}",
    }


CATALOGO = [
    estacion("MADRI", "MADRID (TODAS)", "0071,MADRI,null"),
    estacion("60000", "MADRID-PUERTA DE ATOCHA-ALMUDENA GRANDES", "0071,60000,00600"),
    estacion("17000", "MADRID-CHAMARTÍN-CLARA CAMPOAMOR"),
    estacion("51003", "SEVILLA-SANTA JUSTA"),
    estacion("51100", "SAN BERNARDO"),
    estacion("51400", "PUERTO DE SANTA MARÍA"),
    estacion("51409", "CÁDIZ-ESTADIO"),
    estacion("51405", "CÁDIZ"),
]


def clave_de(texto):
    elegida = elegir_estacion(CATALOGO, texto)
    return elegida and elegida["clave"]


class ElegirEstacionTest(unittest.TestCase):
    def test_primera_que_contiene_el_texto_como_el_filtro_de_renfe(self):
        self.assertEqual(clave_de("Madrid"), "0071,MADRI,null")
        self.assertEqual(clave_de("sevilla"), "0071,51003,51003")

    def test_coincidencia_exacta_gana_al_orden_del_catalogo(self):
        # CÁDIZ-ESTADIO va antes en el catalogo y tambien contiene "cadiz".
        self.assertEqual(clave_de("Cadiz"), "0071,51405,51405")

    def test_ignora_tildes_y_trata_guiones_como_espacios(self):
        self.assertEqual(clave_de("madrid chamartin"), "0071,17000,17000")
        self.assertEqual(clave_de("Puerto de Santa Maria"), "0071,51400,51400")

    def test_nombre_guardado_por_el_bot_vuelve_a_su_estacion(self):
        # main.py guarda descripcionEstacionOrigen.title() y scheduler.py lo
        # reutiliza para recapturar la sesion.
        self.assertEqual(clave_de("Madrid-Puerta De Atocha-Almudena Grandes"), "0071,60000,00600")

    def test_nombres_antiguos_del_dwr_encuentran_la_estacion_actual(self):
        self.assertEqual(clave_de("Sevilla-San Bernardo"), "0071,51100,51100")
        self.assertEqual(clave_de("El Puerto De Santa María"), "0071,51400,51400")

    def test_sin_coincidencia_devuelve_none(self):
        self.assertIsNone(elegir_estacion(CATALOGO, "Atlantis"))
        self.assertIsNone(elegir_estacion(CATALOGO, "   "))


class PayloadBusquedaTest(unittest.TestCase):
    def test_usa_clave_y_nombre_del_catalogo_y_fecha_dd_mm_aaaa(self):
        payload = _payload_busqueda(CATALOGO[3], CATALOGO[0], "15/11/2026")
        self.assertEqual(payload["cdgoOrigen"], "0071,51003,51003")
        self.assertEqual(payload["cdgoDestino"], "0071,MADRI,null")
        self.assertEqual(payload["desOrigen"], "SEVILLA-SANTA JUSTA")
        self.assertEqual(payload["FechaIdaSel"], "15/11/2026")
        self.assertEqual(payload["FechaVueltaSel"], "")


class UmbralSistemaNuevoTest(unittest.TestCase):
    def test_fechas_desde_el_umbral_van_al_sistema_nuevo(self):
        self.assertFalse(_es_fecha_sistema_nuevo("24/01/2027", "2027-01-25"))
        self.assertTrue(_es_fecha_sistema_nuevo("25/01/2027", "2027-01-25"))
        self.assertTrue(_es_fecha_sistema_nuevo("01/02/2027", "2027-01-25"))

    def test_umbral_ilegible_no_bloquea_la_busqueda(self):
        self.assertFalse(_es_fecha_sistema_nuevo("01/02/2027", "pronto"))


if __name__ == "__main__":
    unittest.main()
