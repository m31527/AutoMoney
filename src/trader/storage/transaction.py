from collections.abc import Iterator
from contextlib import contextmanager
from sqlite3 import Connection
from uuid import uuid4


@contextmanager
def transaction(connection: Connection) -> Iterator[None]:
    """Nested operations must never commit the executor's outer transaction."""
    nested = connection.in_transaction
    name = "sp_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO {name}")
            connection.execute(f"RELEASE {name}")
        else:
            connection.rollback()
        raise
    else:
        if nested:
            connection.execute(f"RELEASE {name}")
        else:
            connection.commit()
