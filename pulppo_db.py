"""Conexión de solo lectura a la base de Pulppo (MongoDB Atlas).

Uso:
    from pulppo_db import get_db
    db = get_db()

La URI se lee de la variable de entorno MONGODB_URI (en Vercel) o, si no está, de ~/Proyectos/Pulppo/.env.
Nunca imprimirla.
"""
import os
from pathlib import Path

from dotenv import dotenv_values
from pymongo import MongoClient

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def get_client() -> MongoClient:
    uri = os.environ.get("MONGODB_URI") or (dotenv_values(ENV_PATH, encoding="utf-8-sig").get("MONGODB_URI") if ENV_PATH.exists() else None)
    if not uri:
        raise RuntimeError(f"Falta MONGODB_URI en {ENV_PATH}")
    # DATETIME_AUTO: hay documentos con fechas fuera de rango (guía §1).
    return MongoClient(uri, datetime_conversion="DATETIME_AUTO", tz_aware=True)


def get_db():
    return get_client()["pulppo"]
