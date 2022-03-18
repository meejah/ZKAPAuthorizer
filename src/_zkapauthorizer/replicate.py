"""
A system for replicating local ZKAPAuthorizer state to remote storage.

Theory of Operation
===================

A new database connection type is provided by way of a new
``sqlite3.connect``-like function.  This connection type provides facilities
for accomplishing two goals:

* It presents an expanded connection interface which includes the ability to
  switch the database into "replicated" mode.  This is an application-facing
  interface meant to be used when the application is ready to discharge its
  responsibilities in the replication process.

* It exposes the usual cursor interface wrapped around the usual cursor
  behavior combined with extra logic to record statements which change the
  underlying database (DDL and DML statements).  This recorded data then feeds
  into the above replication process once it is enabled.

An application's responsibilities in the replication process are to arrange
for remote storage of "snapshots" and "event streams".  See the
replication/recovery design document for details of these concepts.

Once replication has been enabled, the application is informed whenever the
event stream changes (respecting database transactionality) and data can be
shipped to remote storage as desired.

It is essential to good replication performance that once replication is
enabled all database-modifying actions are captured in the event stream.  This
is the reason for providing a ``sqlite3.Connection``-like object for use by
application code rather than a separate side-car interface: it minimizes the
opportunities for database changes which are overlooked by this replication
system.
"""


from sqlite3 import Connection, Cursor
from typing import Iterator

from attrs import define


def with_replication(connection: Connection):
    """
    Wrap a replicating support layer around the given connection.
    """
    return _ReplicationCapableConnection(connection)


@define
class _ReplicationCapableConnection:
    _conn: Connection

    def snapshot(self):
        return snapshot(self._conn)

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def cursor(self):
        return _ReplicationCapableCursor(self._conn.cursor())


@define
class _ReplicationCapableCursor:
    _cursor: Cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        return self._cursor.close()

    def execute(self, statement, row=None):
        if row is None:
            args = (statement,)
        else:
            args = (statement, row)
        self._cursor.execute(*args)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, n):
        return self._cursor.fetchmany(n)

    def fetchone(self):
        return self._cursor.fetchone()

    def executemany(self, statement, rows):
        self._cursor.executemany(statement, rows)


def snapshot(connection: Connection) -> Iterator[str]:
    """
    Take a snapshot of the database reachable via the given connection.
    """
    for statement in connection.iterdump():
        yield statement + "\n"
