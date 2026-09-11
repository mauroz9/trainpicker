#!/usr/bin/env python3
"""Vuelca, itinerario a itinerario, como interpreta TrainPicker una respuesta DWR de Renfe.

Sirve para comprobar contra datos reales por que un tren sale como disponible o
como completo, sin tener que adivinar (issue #25).

Como capturar la respuesta cruda:
  1. Abre la busqueda en https://www.renfe.com con las DevTools en la pestaña Red.
  2. Filtra por `getTrainsList.dwr`, boton derecho sobre la peticion ->
     "Copiar respuesta", y pegala en un fichero.

Uso:
    python3 scripts/diagnosticar_dwr.py respuesta.dwr 13/09/2026
    python3 scripts/diagnosticar_dwr.py respuesta.dwr 13/09/2026 --salida 18:12
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import _iter_itinerary_fields, _text_field, evaluar_disponibilidad  # noqa: E402

SEÑALES = ("completo", "tarifasDisponibles", "razonNoDisponible", "soloPlazaH")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fichero", help="fichero con la respuesta cruda de getTrainsList.dwr")
    parser.add_argument("fecha", help="fecha buscada en formato DD/MM/AAAA")
    parser.add_argument("--salida", help="filtrar por hora de salida, p.ej. 18:12")
    args = parser.parse_args()

    with open(args.fichero, encoding="utf-8", errors="replace") as handle:
        texto = handle.read()

    d, m, y = args.fecha.split("/")
    fecha_iso = f"{y}-{m}-{d}"

    itinerarios = _iter_itinerary_fields(texto)
    print(f"Itinerarios encontrados en la respuesta: {len(itinerarios)}")
    print(f"Filtrando por fecha {fecha_iso}" + (f" y salida {args.salida}" if args.salida else ""))
    print()

    mostrados = 0
    for fields in itinerarios:
        if _text_field(fields, "fecha") != fecha_iso:
            continue
        salida = _text_field(fields, "horaSalida")
        if args.salida and salida != args.salida:
            continue

        llegada = _text_field(fields, "horaLlegada")
        disponible, motivo = evaluar_disponibilidad(fields)
        mostrados += 1

        print(f"{salida} - {llegada}  tren={fields.get('_codigosTrenTramo') or '<ninguno>'}  "
              f"{_text_field(fields, 'descripcionEstacionOrigen')} -> "
              f"{_text_field(fields, 'descripcionEstacionDestino')}")
        print(f"    disponible={disponible}" + ("" if disponible else f"  motivo={motivo}"))
        print("    señales: " + ", ".join(
            f"{nombre}={fields.get(nombre, '<ausente>')}" for nombre in SEÑALES
        ))
        print()

    if not mostrados:
        print("Ningun itinerario coincide con el filtro.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
