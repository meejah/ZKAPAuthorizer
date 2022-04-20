# Copyright 2022 PrivateStorage.io, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
A system for replicating local SQLite3 database state to remote storage.

Theory of Operation
===================

A function to wrap a ``sqlite3.Connection`` in a new type is provided.  This
new type provides facilities for accomplishing two goals:

* It (can someday) presents an expanded connection interface which includes
  the ability to switch the database into "replicated" mode.  This is an
  application-facing interface meant to be used when the application is ready
  to discharge its responsibilities in the replication process.

* It (can someday) expose the usual cursor interface wrapped around the usual
  cursor behavior combined with extra logic to record statements which change
  the underlying database (DDL and DML statements).  This recorded data then
  feeds into the above replication process once it is enabled.

An application's responsibilities in the replication process are to arrange
for remote storage of "snapshots" and "event streams".  See the
replication/recovery design document for details of these concepts.

Once replication has been enabled, the application (can someday be) informed
whenever the event stream changes (respecting database transactionality) and
data can be shipped to remote storage as desired.

It is essential to good replication performance that once replication is
enabled all database-modifying actions are captured in the event stream.  This
is the reason for providing a ``sqlite3.Connection``-like object for use by
application code rather than a separate side-car interface: it minimizes the
opportunities for database changes which are overlooked by this replication
system.
"""

__all__ = [
    "ReplicationAlreadySetup",
    "fail_setup_replication",
    "setup_tahoe_lafs_replication",
    "with_replication",
    "statements_to_snapshot",
    "connection_to_statements",
    "snapshot",
]

from io import BytesIO
from sqlite3 import Connection, Cursor
from typing import BinaryIO, Callable, Iterator, Optional

import cbor2
from attrs import Factory, define, field, frozen
from compose import compose
from twisted.application.service import IService, Service
from twisted.internet.defer import CancelledError, Deferred, DeferredSemaphore, succeed
from twisted.python.failure import Failure
from twisted.python.filepath import FilePath
from twisted.python.lockfile import FilesystemLock

from .config import REPLICA_RWCAP_BASENAME, Config
from .sql import bind_arguments, statement_mutates
from .tahoe import ITahoeClient, attenuate_writecap
from ._types import CapStr


@frozen
class Change:
    """
    Represent an item in a replication event stream
    """

    sequence: int  # the sequence-number of this event
    statement: str  # the SQL statement string


@frozen
class EventStream:
    """
    A series of database operations represented as `Change` instances.
    """

    changes: tuple[Change, ...]

    def highest_sequence(self) -> Optional[int]:
        """
        :returns: the highest sequence number in this EventStream (or
            None if there are no events)
        """
        if not self.changes:
            return None
        return max(change.sequence for change in self.changes)

    def to_bytes(self) -> BinaryIO:
        """
        :returns BinaryIO: a producer of bytes representing this EventStream.
        """
        return BytesIO(
            cbor2.dumps(
                {
                    "events": tuple(
                        (event.sequence, event.statement.encode("utf8"))
                        for event in self.changes
                    )
                }
            )
        )

    @classmethod
    def from_bytes(cls, stream: BinaryIO):
        """
        :returns EventStream: an instance of EventStream from the given
            bytes (which should have been produced by a prior call to
            ``to_bytes``)
        """
        data = cbor2.load(stream)
        return cls(
            changes=tuple(
                Change(seq, statement.decode("utf8"))
                for seq, statement in data["events"]
            )
        )


class ReplicationAlreadySetup(Exception):
    """
    An attempt was made to setup of replication but it is already set up.
    """


async def fail_setup_replication():
    """
    A replication setup function that always fails.
    """
    raise Exception("Test not set up for replication")


async def setup_tahoe_lafs_replication(client: ITahoeClient) -> str:
    """
    Configure the ZKAPAuthorizer plugin that lives in the Tahoe-LAFS node with
    the given configuration to replicate its state onto Tahoe-LAFS storage
    servers using that Tahoe-LAFS node.
    """
    # Find the configuration path for this node's replica.
    config_path = client.get_private_path(REPLICA_RWCAP_BASENAME)

    # Take an advisory lock on the configuration path to avoid concurrency
    # shennanigans.
    config_lock = FilesystemLock(config_path.asTextMode().path + ".lock")
    config_lock.lock()
    try:

        # Check to see if there is already configuration.
        if config_path.exists():
            raise ReplicationAlreadySetup()

        # Create a directory with it
        rw_cap = await client.make_directory()

        # Store the resulting write-cap in the node's private directory
        config_path.setContent(rw_cap.encode("ascii"))

    finally:
        # On success and failure, release the lock since we're done with the
        # file for now.
        config_lock.unlock()

    # Attenuate it to a read-cap
    rocap = attenuate_writecap(rw_cap)

    # Return the read-cap
    return rocap


def is_replication_setup(config: Config) -> bool:
    """
    :return: ``True`` if and only if replication has previously been setup for
        the Tahoe-LAFS node associated with the given configuration.
    """
    # Find the configuration path for this node's replica.
    return FilePath(config.get_private_path(REPLICA_RWCAP_BASENAME)).exists()


def get_replica_rwcap(config: Config) -> CapStr:
    """
    :return: a mutable directory capability for our replica.
    :raises: Exception if replication is not setup
    """
    rwcap_file = FilePath(config.get_private_path(REPLICA_RWCAP_BASENAME))
    return rwcap_file.getContent()


def with_replication(connection: Connection):
    """
    Wrap a replicating support layer around the given connection.
    """
    return _ReplicationCapableConnection(connection)


@define
class _ReplicationCapableConnection:
    """
    Wrap a ``sqlite3.Connection`` to provide additional snapshot- and
    streaming replication-related features.

    All of this type's methods are intended to behave the same way as
    ``sqlite3.Connection``\ 's methods except they may also add some
    additional functionality to support replication.
    """

    # the "real" / normal sqlite connection
    _conn: Connection

    def snapshot(self) -> bytes:
        """
        Create and return a byte string representing a consistent, self-contained
        snapshot of the wrapped database.
        """
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
    """
    Wrap a ``sqlite3.Cursor`` to provide additional streaming
    replication-related features.

    All of this type's attributes and methods are intended to behave the same
    way as ``sqlite3.Cursor``\ 's methods except they may also add some
    additional functionality to support replication.
    """

    _cursor: Cursor
    _observers: list = Factory(list)
    # true while statements are "important" (which is pased along to
    # the observers and interpreted as being "important data that the
    # user will be interested in preserving")
    _important: bool = False

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        return self._cursor.close()

    def execute(self, statement, row=None):
        """
        sqlite's Cursor API

        :param row: the arguments
        """
        if row is None:
            args = (statement,)
        else:
            args = (statement, row)
        self._cursor.execute(*args)
        if statement_mutates(statement):
            self._observed_mutations(statement, (row,))

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, n):
        return self._cursor.fetchmany(n)

    def fetchone(self):
        return self._cursor.fetchone()

    def executemany(self, statement, rows):
        self._cursor.executemany(statement, rows)
        if statement_mutates(statement):
            self._observed_mutations(statement, rows)

    def _observed_mutations(self, statement, rows):
        """
        One or more statements that mutate the database have been
        executed. The transaction is still active. Notify observers.
        """
        for ob in self._observers:
            ob(self, self._important, statement, rows)

    def important(self):
        """
        Create a new context-manager that -- while active -- sets the
        'important' flag to true and resets it afterwards.
        """
        return _Important(self)


def netstring(bs: bytes) -> bytes:
    """
    Encode a single string as a netstring.

    :see: http://cr.yp.to/proto/netstrings.txt
    """
    return b"".join(
        [
            str(len(bs)).encode("ascii"),
            b":",
            bs,
            b",",
        ]
    )


def statements_to_snapshot(statements: Iterator[str]) -> Iterator[bytes]:
    """
    Take a snapshot of the database reachable via the given connection.

    The snapshot is consistent and write transactions on the given connection
    are blocked until it has been completed.
    """
    for statement in statements:
        # Use netstrings to frame each statement.  Statements can have
        # embedded newlines (and CREATE TABLE statements especially tend to).
        yield netstring(statement.strip().encode("utf-8"))


def connection_to_statements(connection: Connection) -> Iterator[str]:
    """
    Create an iterator of SQL statements as strings representing a consistent,
    self-contained snapshot of the database reachable via the given
    connection.
    """
    return connection.iterdump()


# Convenience API to dump statements, netstring-encoding them, and
# concatenating them all into a single byte string.
snapshot: Callable[[Connection], bytes] = compose(
    b"".join, statements_to_snapshot, connection_to_statements
)


async def tahoe_lafs_uploader(
    client: ITahoeClient,
    recovery_cap: str,
    get_snapshot_data: Callable[[], BinaryIO],
    entry_name: str,
) -> None:
    """
    Upload a replica to Tahoe, linking the result into the given
    recovery mutable capbility under the name 'snapshot.sql'
    """
    snapshot_immutable_cap = await client.upload(get_snapshot_data)
    await client.link(recovery_cap, entry_name, snapshot_immutable_cap)


def get_tahoe_lafs_direntry_uploader(
    client: ITahoeClient,
    directory_mutable_cap: str,
):
    """
    Bind a Tahoe client to a mutable directory in a callable that will
    upload some data and link it into the mutable directory under the
    given name.

    :return Callable[str, [Callable[[], BinaryIO]], None]: A callable that
        will upload some data as the latest replica snapshot. The data
        isn't given directly, but instead from a zero-argument callable
        itself to facilitate retrying.
    """

    async def upload(
        entry_name: str, get_data_provider: Callable[[], BinaryIO]
    ) -> None:
        await tahoe_lafs_uploader(
            client, directory_mutable_cap, get_data_provider, entry_name
        )

    return upload


@define
class _Important:
    """
    A context-manager to set and unset the ._important flag on a
    _ReplicationCapableConnection
    """

    _replication_conn: _ReplicationCapableConnection

    def __enter__(self):
        self._replication_conn._important = True

    def __exit__(self, *args):
        self._replication_conn._important = False


@define
class _ReplicationService(Service):
    """
    Perform all activity related to maintaining a remote replica of the local
    ZKAPAuthorizer database.

    :ivar _connection: A connection to the database being replicated.

    :ivar _replicating: The long-running replication operation.  This is never
        expected to complete but it will be cancelled when the service stops.
    """

    name = "replication-service"  # type: ignore # Service assigns None, screws up type inference

    _connection: _ReplicationCapableConnection = field()
    _uploader: Callable[[str, BinaryIO], None] = field()
    _replicating: Optional[Deferred] = field(init=False, default=None)

    _store = field(default=None)  #: VoucherStore
    _accumulated_size: int = 0
    _trigger: DeferredSemaphore = DeferredSemaphore(1)

    def startService(self) -> None:
        super().startService()

        # restore our state .. this number will be bigger than what we
        # would have recorded through "normal" means which only counts
        # the statement-sizes .. but maybe fine?
        self._accumulated_size = len(self._store.get_events().to_bytes())

        # should we do an upload immediately? or hold the lock?
        if not self.big_enough():
            self._trigger.acquire()

        # Tell the store to initiate replication when appropriate.  The
        # service should only be created and started if replication has been
        # turned on - so, make sure replication is turned on at the database
        # layer.
        self._replicating = Deferred.fromCoroutine(self.wait_for_uploads())
        self._replicating.addErrback(self._replication_fail)
        self._connection.add_mutation_observer(self.observed_event)

    def _replication_fail(self, fail):
        """
        Replicating has failed for some reason
        """
        print(f"Replication failure: {fail}")

    def queue_upload(self):
        """
        Ask for an upload to occur
        """
        if self._trigger.tokens:
            self._trigger.release()
        else:
            # we're already uploading
            pass

    async def wait_for_uploads(self):
        """
        An infinite async loop that processes uploads
        """
        while True:
            await self._trigger.acquire()
            try:
                await self._do_one_upload()
            except Exception:
                # probably log the error?
                pass

    async def _do_one_upload(self):
        """
        Process a single upload.
        """
        events = self._store.get_events()
        # upload latest event-stream
        await self._uploader(
            "event-stream-{}".format(events.higest_sequence()),
            events.to_bytes,
        )
        # prune the database
        self._store.prune_events_to(events.higest_sequence())

    def stopService(self) -> Deferred:
        """
        Cancel the replication operation and then wait for it to complete.
        """
        super().stopService()

        replicating = self._replicating
        if replicating is None:
            return succeed(None)

        self._replicating = None

        def catch_cancelled(err: Failure) -> None:
            err.trap(CancelledError)
            return None

        replicating.addErrback(catch_cancelled)
        replicating.cancel()
        return replicating

    def observed_event(self, cursor, important, statement, args):
        """
        A mutating SQL statement was observed by the cursor
        """
        bound_statement = bind_arguments(cursor, statement, args)
        self._store.add_event.wrapped(cursor, bound_statement)
        # note that we're ignoring a certain amount of size overhead
        # here: the _actual_ size will be some CBOR information and
        # the sequence number, although the statement text should
        # still dominate.
        self._accumulated_size += len(bound_statement)
        if important or self.big_enough():
            self.queue_upload()
            self._accumulated_size = 0

    def big_enough(self):
        return self._accumulated_size >= 570000


def replication_service(connection, store, uploader) -> IService:
    """
    Return a service which implements the replication process documented in
    the ``backup-recovery`` design document.
    """
    return _ReplicationService(connection=connection, store=store, uploader=uploader)
