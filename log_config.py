import logging
import re

# La API de Telegram lleva el token del bot en la propia URL
# (`https://api.telegram.org/bot<TOKEN>/metodo`), asi que cualquier log que
# incluya esa URL filtra el secreto (issue #38).
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TOKEN_REDACTED = "bot<redacted>"

# httpx/httpcore registran a INFO cada peticion con la URL completa; con el
# polling de python-telegram-bot eso es una linea con el token cada ~10s.
_LOGGERS_RUIDOSOS = ("httpx", "httpcore")


def redactar_token(texto: str) -> str:
    return _TOKEN_RE.sub(_TOKEN_REDACTED, texto)


class RedactarTokenFilter(logging.Filter):
    """Red de seguridad: borra el token de cualquier mensaje o traceback.

    Silenciar httpx no basta: los errores de red de python-telegram-bot u otra
    libreria pueden volver a incluir la URL en el mensaje o en la excepcion.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redactar_token(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            # El Formatter reutiliza `exc_text` si ya esta relleno, asi que el
            # traceback que se escribe es el redactado.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redactar_token(record.exc_text)
        return True


def configurar_logging(fmt: str) -> None:
    logging.basicConfig(format=fmt, level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactarTokenFilter())
    for nombre in _LOGGERS_RUIDOSOS:
        logging.getLogger(nombre).setLevel(logging.WARNING)
