import sqlite3

import pytest

from rce import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    """A fresh in-memory database with the schema fully migrated."""
    connection = db.connect(":memory:")
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
