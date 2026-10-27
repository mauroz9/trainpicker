"""Tests de la redaccion del token de Telegram en los logs (issue #38)."""
import io
import logging

from log_config import RedactarTokenFilter, redactar_token

TOKEN = "123456789:AAFakeToken_with-chars"
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"


def _logger_capturado():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.addFilter(RedactarTokenFilter())
    logger = logging.getLogger("test_log_config")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, buffer


def test_redactar_token_en_url():
    assert redactar_token(URL) == "https://api.telegram.org/bot<redacted>/getUpdates"


def test_filtro_redacta_mensaje_con_args():
    logger, buffer = _logger_capturado()
    logger.info('HTTP Request: POST %s "%s"', URL, "HTTP/1.1 200 OK")
    salida = buffer.getvalue()
    assert TOKEN not in salida
    assert "bot<redacted>/getUpdates" in salida


def test_filtro_redacta_traceback():
    logger, buffer = _logger_capturado()
    try:
        raise RuntimeError(f"fallo llamando a {URL}")
    except RuntimeError:
        logger.exception("Error critico")
    salida = buffer.getvalue()
    assert TOKEN not in salida
    assert "Traceback" in salida
