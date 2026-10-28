"""MongoDB Atlas connection management."""
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

load_dotenv()

_client: MongoClient | None = None


def get_client() -> MongoClient:
    """Return a lazily created, process-wide MongoClient (thread-safe, pooled)."""
    global _client
    if _client is None:
        uri = os.getenv("MONGO_URI")
        if not uri:
            raise RuntimeError(
                "MONGO_URI is not set. Copy .env.example to .env and add your Atlas connection string."
            )
        _client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="pricing-platform")
    return _client


def get_db() -> Database:
    """Return the application database."""
    return get_client()[os.getenv("DB_NAME", "pricing_platform")]


def close_client() -> None:
    """Close the shared client (called on application shutdown)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
