"""Entrada de Vercel: expone la app Flask de app.py."""
import sys
from pathlib import Path

from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app as flask_app  # noqa: E402


def app(environ, start_response):
    """El rewrite de vercel.json manda todo a /api/index?__ruta=/<ruta original>: devolverle a Flask la ruta original."""
    if environ.get("PATH_INFO", "").startswith("/api/index"):
        qs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        ruta = next((v for k, v in qs if k == "__ruta"), None)
        if ruta is not None:
            environ["PATH_INFO"] = ruta if ruta.startswith("/") else "/" + ruta
            environ["QUERY_STRING"] = urlencode([(k, v) for k, v in qs if k != "__ruta"])
    return flask_app(environ, start_response)
