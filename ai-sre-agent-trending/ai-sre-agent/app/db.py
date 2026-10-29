import json

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings


def clean(obj):
    """Make anything JSON-safe (datetimes, decimals -> strings)."""
    return json.loads(json.dumps(obj, default=str))


def _conn():
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row, autocommit=True)


def fetch(sql, params=()):
    with _conn() as c:
        return clean(c.execute(sql, params).fetchall())


def fetch_one(sql, params=()):
    with _conn() as c:
        row = c.execute(sql, params).fetchone()
        return clean(row) if row else None


def run(sql, params=()):
    with _conn() as c:
        return c.execute(sql, params).rowcount


def audit(incident_id, actor, event, detail=None):
    """Append-only record of everything the system and humans do."""
    with _conn() as c:
        c.execute("INSERT INTO audit_log(incident_id, actor, event, detail) VALUES (%s,%s,%s,%s)",
                  (incident_id, actor, event, Jsonb(clean(detail or {}))))
