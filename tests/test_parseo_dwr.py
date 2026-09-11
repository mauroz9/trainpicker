"""Tests de regresion del parseo de la respuesta DWR de Renfe.

El caso que motiva estos tests (issue #25) es el trayecto San Bernardo ->
Puerto de Santa Maria del 13/09/2026: la web de Renfe muestra "Tren completo,
sin plazas disponibles en este horario" para el tren de las 18:12 (llegada
19:26) y el bot lo marcaba como disponible.

Los payloads son reconstrucciones del formato DWR
(`dwr.engine.remote.handleCallback`) con los nombres de campo reales que usa
`scraper.py`. No hacen ninguna llamada de red.

Ejecutar con:  python3 -m unittest discover -s tests -v
"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import evaluar_disponibilidad, parsear_dwr_renfe  # noqa: E402
from scheduler import _alert_matches_train  # noqa: E402
from main import _build_trains_message, _get_selected_train  # noqa: E402

FECHA = "13/09/2026"
SAN_BERNARDO = "SEVILLA-SAN BERNARDO"
PUERTO = "EL PUERTO DE SANTA MAR\\u00cdA"


def itinerario(
    hora_salida="18:12",
    hora_llegada="19:26",
    fecha="2026-09-13",
    origen=SAN_BERNARDO,
    destino=PUERTO,
    cdgo_tren="18012",
    completo="false",
    razon_no_disponible="null",
    solo_plaza_h="false",
    solo_plazas_h_tarifa=None,
    tarifas=None,
    tarifa_minima='"13.20"',
    directo="true",
    extra="",
):
    """Construye un objeto itinerario tal y como lo serializa el DWR de Renfe.

    Los campos van en orden alfabetico, que es el que usa DWR al serializar el
    bean, y `acercamientoViajeDestino` es por eso el primero: es el marcador con
    el que `scraper.py` localiza cada itinerario. `cdgoTren` NO es un campo del
    itinerario -en la respuesta real vive anidado en `trayectos: [...]`, un
    objeto por tramo- asi que se serializa ahi, no en el nivel superior; leerlo
    del nivel superior (como hacia una version anterior de este fixture) daba
    una falsa sensacion de cobertura, porque `scraper.py` nunca ve ese campo en
    produccion (ver `_codigos_tren_tramo`, issue #25). Lo mismo pasa con
    `soloPlazasH` (plural): la señal real de si la tarifa ofertada es de plaza
    reservada vive anidada en cada fila de `tarifasDisponibles`, no en el
    `soloPlazaH` (singular) de nivel superior -que puede venir en `false`
    aunque la unica tarifa ofertada sea de plaza bicicleta, verificado en vivo
    (ver `_tarifa_solo_plaza_reservada`, issue #25-B). Por defecto
    `solo_plazas_h_tarifa` sigue a `solo_plaza_h` (caso normal, donde ambas
    señales coinciden); se pasa distinto solo para reproducir el mismatch.
    """
    if solo_plazas_h_tarifa is None:
        solo_plazas_h_tarifa = solo_plaza_h
    if tarifas is None:
        tarifas = (
            f'[{{cdgoClase:"T",plazaH:true,precioTarifa:"13.20",'
            f'soloPlazasH:{solo_plazas_h_tarifa},titulo:"Adulto ida"}}]'
        )
    trayectos = (
        ",trayectos:[{cdgoEstacionDestinoTrayecto:\"51400\",cdgoEstacionOrigenTrayecto:\"51100\","
        f"cdgoProducto:\"X\",cdgoTren:\"{cdgo_tren}\",horaLlegada:\"{hora_llegada}:00\","
        f"horaSalida:\"{hora_salida}:00\",razonNoDisponible:{razon_no_disponible},tipoTren:\"MD\"}}]"
    )
    return (
        "{acercamientoViajeDestino:null,acercamientoViajeOrigen:null,"
        f"completo:{completo},"
        f"descripcionEstacionDestino:\"{destino}\","
        f"descripcionEstacionOrigen:\"{origen}\","
        f"directo:{directo},duracionViaje:\"1:14\",fecha:\"{fecha}\","
        f"horaLlegada:\"{hora_llegada}\",horaSalida:\"{hora_salida}\","
        "plazaBDisponible:true,plazaHDisponible:false,"
        f"razonNoDisponible:{razon_no_disponible},soloPlazaH:{solo_plaza_h},"
        f"tarifaMinima:{tarifa_minima},tarifasDisponibles:{tarifas}{trayectos}{extra}}}"
    )


def respuesta_dwr(*itinerarios):
    return (
        "throw 'allowScriptTagRemoting is false.';\n"
        "//#DWR-REPLY\n"
        'dwr.engine.remote.handleCallback("0","0",[' + ",".join(itinerarios) + "]);\n"
    )


# El tren de las 18:12 tal y como lo devuelve Renfe cuando esta agotado.
DIRECTO_COMPLETO = itinerario(
    completo="true", razon_no_disponible='"3"', tarifas="null", tarifa_minima="null",
)


class TestFalsoPositivoIssue25(unittest.TestCase):
    """Un itinerario con plaza no puede marcar como libre a otro que este completo."""

    def _tren(self, trenes, salida, llegada):
        encontrado = [t for t in trenes if t["salida"] == salida and t["llegada"] == llegada]
        self.assertEqual(len(encontrado), 1, f"esperado 1 tren {salida}-{llegada}, hay {len(encontrado)}")
        return encontrado[0]

    def test_enlace_con_misma_hora_de_salida_no_libera_el_directo(self):
        # 18:12-19:26 directo y agotado; 18:12-20:05 via enlace y con plaza.
        texto = respuesta_dwr(
            DIRECTO_COMPLETO,
            itinerario(cdgo_tren="18030", hora_llegada="20:05", directo="false"),
        )

        trenes = parsear_dwr_renfe(texto, FECHA)

        self.assertEqual(len(trenes), 2, "los dos itinerarios deben listarse por separado")
        self.assertFalse(self._tren(trenes, "18:12", "19:26")["disponible"])
        self.assertTrue(self._tren(trenes, "18:12", "20:05")["disponible"])

    def test_acercamiento_desde_otra_estacion_no_libera_el_directo(self):
        texto = respuesta_dwr(
            DIRECTO_COMPLETO,
            itinerario(cdgo_tren="18041", origen="SEVILLA-SANTA JUSTA", hora_llegada="19:40"),
        )

        trenes = parsear_dwr_renfe(texto, FECHA)

        self.assertFalse(self._tren(trenes, "18:12", "19:26")["disponible"])

    def test_tramo_anidado_no_sustituye_al_itinerario(self):
        # Un enlace lleva dentro sus tramos, que repiten la misma forma (y por
        # tanto el marcador `acercamientoViajeDestino`). El troceado anterior
        # los tomaba por trenes del listado.
        tramos = (
            ",tramos:[{acercamientoViajeDestino:null,cdgoTren:\"18099\",completo:false,"
            "fecha:\"2026-09-13\",horaLlegada:\"18:47\",horaSalida:\"18:12\","
            "razonNoDisponible:null,soloPlazaH:false,tarifasDisponibles:[{precio:5.0}]}]"
        )
        texto = respuesta_dwr(itinerario(
            completo="true", razon_no_disponible='"3"', tarifas="null",
            tarifa_minima="null", extra=tramos,
        ))

        trenes = parsear_dwr_renfe(texto, FECHA)

        self.assertEqual(len(trenes), 1, "el tramo interno no es un tren del listado")
        self.assertFalse(trenes[0]["disponible"])
        self.assertEqual(trenes[0]["llegada"], "19:26")

    def test_mismo_tren_en_dos_filas_de_tarifa_si_hace_or(self):
        # El deduplicado original existia para esto y debe seguir funcionando:
        # el mismo itinerario listado dos veces, con plaza en una de ellas.
        texto = respuesta_dwr(
            itinerario(tarifas="null", tarifa_minima="null"),
            itinerario(),
        )

        trenes = parsear_dwr_renfe(texto, FECHA)

        self.assertEqual(len(trenes), 1)
        self.assertTrue(trenes[0]["disponible"])
        self.assertIsNone(trenes[0]["motivo"])

    def test_dos_trenes_reales_en_la_misma_franja_no_se_funden(self):
        # Verificado en vivo el 11/09/2026 contra Renfe (San Bernardo -> Puerto
        # de Santa Maria, 13/09/2026): el tren 13083 (con plaza) y el 35083
        # ("Tren Completo") salen ambos a las 18:12 y llegan a las 19:26. Antes
        # de leer `cdgoTren` desde `trayectos` (ver `_codigos_tren_tramo`),
        # `codigo_tren` era siempre `None` para los dos, la identidad se
        # reducia a salida/llegada/origen/destino y el OR de disponibilidad
        # fundia ambos en un unico tren "disponible", perdiendo que 35083
        # esta completo.
        texto = respuesta_dwr(
            itinerario(cdgo_tren="13083"),
            itinerario(
                cdgo_tren="35083", completo="true", razon_no_disponible='"3"',
                tarifas="null", tarifa_minima="null",
            ),
        )

        trenes = parsear_dwr_renfe(texto, FECHA)

        self.assertEqual(len(trenes), 2, "13083 y 35083 son trenes reales distintos")
        por_tren = {t["tren"]: t["disponible"] for t in trenes}
        self.assertEqual(por_tren, {"13083": True, "35083": False})


class TestSenalesDeDisponibilidad(unittest.TestCase):
    """Traduccion de las señales de Renfe (`listaTrenes.js`) a `disponible`."""

    def _disponible(self, **kw):
        trenes = parsear_dwr_renfe(respuesta_dwr(itinerario(**kw)), FECHA)
        self.assertEqual(len(trenes), 1)
        return trenes[0]

    def test_tren_normal_disponible(self):
        self.assertTrue(self._disponible()["disponible"])

    def test_completo_true(self):
        tren = self._disponible(completo="true")
        self.assertFalse(tren["disponible"])
        self.assertEqual(tren["motivo"], "completo")

    def test_sin_tarifas(self):
        tren = self._disponible(tarifas="null", tarifa_minima="null")
        self.assertFalse(tren["disponible"])
        self.assertEqual(tren["motivo"], "sin_tarifas")

    def test_solo_plaza_h(self):
        # Caso normal: nivel superior y anidado coinciden (verificado en vivo,
        # nunca se han visto en desacuerdo en esta direccion: 89/89 casos).
        tren = self._disponible(solo_plaza_h="true")
        self.assertFalse(tren["disponible"])
        self.assertEqual(tren["motivo"], "solo_plaza_reservada")

    def test_solo_bici_con_nivel_superior_en_false(self):
        # Verificado en vivo el 11/09/2026 (tren 13083, San Bernardo -> Puerto
        # de Santa Maria): `soloPlazaH` de nivel superior viene en `false`
        # (parece sin restriccion) pero la unica fila de tarifa ofertada tiene
        # `soloPlazasH:true` -es una plaza bicicleta, no una normal-. Sin mirar
        # el campo anidado, el bot marcaba este tren como disponible sin serlo
        # (12 de 794 itinerarios reales muestreados). El motivo no distingue H
        # de bici porque el propio campo de Renfe tampoco lo hace.
        tren = self._disponible(solo_plaza_h="false", solo_plazas_h_tarifa="true")
        self.assertFalse(tren["disponible"])
        self.assertEqual(tren["motivo"], "solo_plaza_reservada")

    def test_tarifa_normal_disponible_aunque_top_no_marque_plaza_h_ni_bici(self):
        # Lo contrario tambien ocurre en datos reales: `plazaHDisponible` y
        # `plazaBDisponible` de nivel superior en `false` no implica tarifa
        # restringida si la propia fila de tarifa dice `soloPlazasH:false`
        # (29 de 794 itinerarios reales). Debe seguir disponible.
        self.assertTrue(self._disponible(solo_plaza_h="false", solo_plazas_h_tarifa="false")["disponible"])

    def test_razon_vacia_es_disponible(self):
        # `razonNoDisponible: ""` es "sin incidencia" en el JS de Renfe
        # (`razon != "" && razon != "8"`), no un bloqueo.
        self.assertTrue(self._disponible(razon_no_disponible='""')["disponible"])

    def test_razon_8_es_informativa(self):
        self.assertTrue(self._disponible(razon_no_disponible='"8"')["disponible"])

    def test_razones_bloqueantes(self):
        for codigo in ("3", "4", "5", "6", "7", "99"):
            with self.subTest(razon=codigo):
                tren = self._disponible(razon_no_disponible=f'"{codigo}"')
                self.assertFalse(tren["disponible"])
                self.assertEqual(tren["motivo"], f"razon_{codigo}")

    def test_motivos_acumulados(self):
        tren = self._disponible(completo="true", razon_no_disponible='"3"', tarifas="null")
        self.assertEqual(tren["motivo"], "completo+sin_tarifas+razon_3")

    def test_fail_closed_si_falta_toda_señal(self):
        # Renfe cambia el formato: sin ninguna de las cuatro señales el tren se
        # marca no disponible y se avisa por log.
        bloque = (
            '{acercamientoViajeDestino:null,cdgoTren:"18012",fecha:"2026-09-13",'
            'horaLlegada:"19:26",horaSalida:"18:12"}'
        )
        with self.assertLogs("scraper", level=logging.WARNING) as capturado:
            trenes = parsear_dwr_renfe(respuesta_dwr(bloque), FECHA)

        self.assertFalse(trenes[0]["disponible"])
        self.assertEqual(trenes[0]["motivo"], "formato_desconocido")
        self.assertIn("no se encontro ninguna señal", capturado.output[0])

    def test_evaluar_disponibilidad_directamente(self):
        self.assertEqual(evaluar_disponibilidad({}), (False, "formato_desconocido"))
        self.assertEqual(evaluar_disponibilidad({"completo": "false", "razonNoDisponible": '""'}), (True, None))
        self.assertEqual(evaluar_disponibilidad({"completo": "true"}), (False, "completo"))


class TestCamposDelTren(unittest.TestCase):
    def test_estaciones_con_escape_unicode(self):
        tren = parsear_dwr_renfe(respuesta_dwr(itinerario()), FECHA)[0]
        self.assertEqual(tren["destino"], "El Puerto De Santa María")

    def test_estaciones_ya_decodificadas_no_se_corrompen(self):
        # Si la respuesta llega ya decodificada, el texto debe pasar intacto
        # (antes se convertia en mojibake y ese nombre acababa guardado en la
        # alerta y reescrito en el autocompletado de Renfe).
        tren = parsear_dwr_renfe(
            respuesta_dwr(itinerario(destino="EL PUERTO DE SANTA MARÍA")), FECHA
        )[0]
        self.assertEqual(tren["destino"], "El Puerto De Santa María")

    def test_tarifa_minima_numerica(self):
        tren = parsear_dwr_renfe(respuesta_dwr(itinerario(tarifa_minima="13.2")), FECHA)[0]
        self.assertEqual(tren["precio_desde"], "13.2")

    def test_solo_devuelve_la_fecha_buscada(self):
        texto = respuesta_dwr(
            itinerario(),
            itinerario(fecha="2026-09-14", hora_salida="09:00", hora_llegada="10:14"),
        )
        trenes = parsear_dwr_renfe(texto, FECHA)
        self.assertEqual([t["salida"] for t in trenes], ["18:12"])

    def test_orden_por_salida_y_llegada(self):
        texto = respuesta_dwr(
            itinerario(hora_salida="20:10", hora_llegada="21:24", cdgo_tren="1"),
            itinerario(hora_salida="18:12", hora_llegada="20:05", cdgo_tren="2"),
            itinerario(hora_salida="18:12", hora_llegada="19:26", cdgo_tren="3"),
        )
        trenes = parsear_dwr_renfe(texto, FECHA)
        self.assertEqual(
            [(t["salida"], t["llegada"]) for t in trenes],
            [("18:12", "19:26"), ("18:12", "20:05"), ("20:10", "21:24")],
        )

    def test_avisa_si_hay_itinerarios_pero_ninguno_de_la_fecha(self):
        # Es lo que se veria si `fecha` dejase de ser un campo del itinerario:
        # el bot diria "no se han encontrado trenes" para todo. Debe dejar
        # rastro en el log en vez de fallar en silencio.
        texto = respuesta_dwr(itinerario(fecha="2026-09-14"))
        with self.assertLogs("scraper", level=logging.WARNING) as capturado:
            self.assertEqual(parsear_dwr_renfe(texto, FECHA), [])
        self.assertIn("ninguno para 2026-09-13", capturado.output[0])
        self.assertIn("2026-09-14", capturado.output[0])

    def test_respuesta_vacia_o_rota(self):
        self.assertEqual(parsear_dwr_renfe("", FECHA), [])
        self.assertEqual(parsear_dwr_renfe("cualquier cosa", FECHA), [])
        self.assertEqual(parsear_dwr_renfe(respuesta_dwr(itinerario()), "13-09-2026"), [])


class TestIdentidadDelTrenEnElFlujo(unittest.TestCase):
    """La salida por si sola no identifica un tren: debe viajar con la llegada."""

    TRENES = [
        {"salida": "18:12", "llegada": "19:26", "disponible": False,
         "origen": "Sevilla-San Bernardo", "destino": "El Puerto De Santa María"},
        {"salida": "18:12", "llegada": "20:05", "disponible": True,
         "origen": "Sevilla-San Bernardo", "destino": "El Puerto De Santa María"},
    ]

    def test_callback_data_incluye_la_llegada(self):
        _, markup = _build_trains_message(FECHA, self.TRENES)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "alerta_18:12_19:26")

    def test_cabecera_usa_la_ruta_mayoritaria(self):
        trenes = self.TRENES + [{
            "salida": "17:00", "llegada": "18:30", "disponible": True,
            "origen": "Sevilla-Santa Justa", "destino": "El Puerto De Santa María",
        }]
        mensaje, _ = _build_trains_message(FECHA, trenes)
        self.assertIn("Sevilla-San Bernardo", mensaje.splitlines()[0])

    def test_seleccion_del_tren_por_salida_y_llegada(self):
        datos = {"trenes_encontrados": self.TRENES}
        self.assertEqual(_get_selected_train(datos, "18:12", "19:26")["llegada"], "19:26")
        self.assertEqual(_get_selected_train(datos, "18:12", "20:05")["llegada"], "20:05")
        # Botones antiguos (callback_data sin llegada) siguen resolviendo.
        self.assertIsNotNone(_get_selected_train(datos, "18:12"))
        self.assertIsNone(_get_selected_train(datos, "18:12", "23:59"))

    def test_alerta_solo_casa_con_su_itinerario(self):
        alerta = {"alert_id": 1, "user_id": 1, "train_time": "18:12", "arrival_time": "19:26"}
        self.assertTrue(_alert_matches_train(alerta, self.TRENES[0]))
        self.assertFalse(_alert_matches_train(alerta, self.TRENES[1]))

    def test_alertas_antiguas_sin_llegada_siguen_casando(self):
        alerta = {"alert_id": 1, "user_id": 1, "train_time": "18:12", "arrival_time": ""}
        self.assertTrue(_alert_matches_train(alerta, self.TRENES[1]))


if __name__ == "__main__":
    unittest.main()
