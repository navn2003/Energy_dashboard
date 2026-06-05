import mysql.connector
import os

# ── DB config ─────────────────────────────────────────────────────────────────
# You can override any value via environment variable, e.g.:
#   export DB_HOST=65.1.28.178
# Or just hardcode them here during development.

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "65.1.28.178"),
    "user":     os.getenv("DB_USER",     "energy"),
    "password": os.getenv("DB_PASSWORD", "Energy@123"),
    "database": os.getenv("DB_NAME",     "energy_monitor"),
    "port":     int(os.getenv("DB_PORT", "3306")),
}


def get_connection():
    """Returns a new MySQL connection. Caller is responsible for closing it."""
    return mysql.connector.connect(**DB_CONFIG)
