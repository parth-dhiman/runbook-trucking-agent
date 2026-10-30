"""MongoDB service: connection to the local database and load storage."""

import os
from urllib.parse import quote_plus

from pymongo import MongoClient
from pymongo.database import Database

HOST = "localhost"
PORT = 27017
DB_NAME = "runbook_trucking"

_client: MongoClient | None = None


def get_db() -> Database:
    """Connect with MONGO_USERNAME and MONGO_PASSWORD from the environment."""
    global _client
    if _client is None:
        user = quote_plus(os.environ["MONGO_USERNAME"])
        password = quote_plus(os.environ["MONGO_PASSWORD"])
        _client = MongoClient(f"mongodb://{user}:{password}@{HOST}:{PORT}/", serverSelectionTimeoutMS=5000)
    return _client[DB_NAME]


def insert_load(load: dict) -> str:
    """Insert a load document and return its Mongo id."""
    return str(get_db().loads.insert_one(load).inserted_id)


def find_load(**filters) -> dict | None:
    return get_db().loads.find_one(filters)


def update_load(load_id: str, fields: dict) -> None:
    get_db().loads.update_one({"load_id": load_id}, {"$set": fields})
