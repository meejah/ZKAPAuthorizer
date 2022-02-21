"""
Tests for ``_zkapauthorizer.recover``, the replication recovery system.
"""

from sqlite3 import Connection, connect
from typing import Dict, Iterator

from allmydata.client import read_config
from fixtures import TempDir
from hypothesis import assume, note, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from hypothesis.strategies import data, lists, randoms, sampled_from
from testresources import setUpResources, tearDownResources
from testtools import TestCase
from testtools.matchers import (
    AfterPreprocessing,
    Always,
    Equals,
    IsInstance,
    MatchesStructure,
)
from testtools.twistedsupport import AsynchronousDeferredRunTest, failed, succeeded
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.python.filepath import FilePath

from ..recover import (
    AlreadyRecovering,
    RecoveryStages,
    StatefulRecoverer,
    get_tahoe_lafs_downloader,
    make_canned_downloader,
    make_fail_downloader,
    noop_downloader,
    recover,
)
from ..tahoe import link, make_directory, upload
from .fixtures import Treq
from .resources import client_manager
from .sql import Table, create_table
from .strategies import deletes, inserts, sql_identifiers, tables, updates


def snapshot(connection: Connection) -> Iterator[str]:
    """
    Take a snapshot of the database reachable via the given connection.
    """
    for statement in connection.iterdump():
        yield statement + "\n"


def equals_db(reference: Connection):
    """
    :return: A matcher for a SQLite3 connection to a database with the same
        state as the reference connection's database.
    """
    return AfterPreprocessing(
        lambda actual: list(actual.iterdump()),
        Equals(list(reference.iterdump())),
    )


class SnapshotMachine(RuleBasedStateMachine):
    """
    Transition rules for a state machine corresponding to the state of a
    SQLite3 database.  Transitions are schema changes, row inserts, row
    updates, row deletions, etc.
    """

    def __init__(self, case):
        super().__init__()
        self.case = case
        self.connection = connect(":memory:")
        self.tables: Dict[str, Table] = {}

    @invariant()
    def snapshot_equals_database(self):
        """
        At all points a snapshot of the database can be used to construct a new
        database with the same contents.
        """
        statements = list(snapshot(self.connection))
        new = connect(":memory:")
        recover(statements, new)
        self.case.assertThat(
            new,
            equals_db(reference=self.connection),
            "source (reference) database iterdump does not equal "
            "sink (actual) database iterdump",
        )

    @rule(
        name=sql_identifiers(),
        table=tables(),
    )
    def create_table(self, name, table):
        """
        Create a new table in the database.
        """
        assume(name not in self.tables)
        self.tables[name] = table
        statement = create_table(name, table)
        note("executing {!r}".format(statement))
        self.connection.execute(statement)

    @precondition(lambda self: len(self.tables) > 0)
    @rule(
        change_types=lists(sampled_from([inserts, deletes, updates]), min_size=1),
        random=randoms(),
        data=data(),
    )
    def modify_rows(self, change_types, random, data):
        """
        Change some rows in some tables.
        """
        for change_type in change_types:
            # Choose a table to impact
            table_name = random.choice(sorted(self.tables))
            # Construct the change
            changes = data.draw(lists(change_type(table_name, self.tables[table_name])))
            # Execute the changes
            for change in changes:
                statement = change.statement()
                args = change.arguments()
                note("executing {!r} {!r}".format(statement, args))
                self.connection.execute(statement, args)


class StatefulRecoverTests(TestCase):
    """
    Stateful tests for ``recover``.
    """

    def test_recover(self):
        """
        Test the snapshot/recovery system using ``SnapshotMachine``.
        """
        # Many shallow runs are probably more useful than fewer deep runs.
        # That is, exercise breadth in preference to depth.
        #
        # Also try to play along with any profile that has been loaded.
        max_examples = settings.default.max_examples * 10
        stateful_step_count = int(max(3, settings.default.stateful_step_count / 10))

        run_state_machine_as_test(
            lambda: SnapshotMachine(self),
            settings=settings(
                max_examples=max_examples,
                stateful_step_count=stateful_step_count,
            ),
        )


class StatefulRecovererTests(TestCase):
    """
    Tests for ``StatefulRecoverer``.
    """

    def test_succeeded_after_recover(self):
        """
        ``StatefulRecoverer`` automatically progresses to the succeeded stage when
        recovery completes without exception.
        """
        recoverer = StatefulRecoverer()
        with connect(":memory:") as conn:
            cursor = conn.cursor()
            first = Deferred.fromCoroutine(recoverer.recover(noop_downloader, cursor))
            self.assertThat(
                first,
                succeeded(Always()),
            )
            self.assertThat(
                recoverer.state(),
                MatchesStructure(
                    stage=Equals(RecoveryStages.succeeded),
                ),
            )

    def test_state_recovered(self):
        """
        After ``StatefulRecoverer`` reaches the ``succeeded`` state the state
        represented by the downloaded snapshot is present in the database
        itself.
        """
        snapshot = (
            b"CREATE TABLE [succeeded] ( [a] TEXT );\n"
            b"INSERT INTO [succeeded] ([a]) VALUES ('yes');\n"
        )
        downloader = make_canned_downloader(snapshot)
        recoverer = StatefulRecoverer()
        with connect(":memory:") as conn:
            cursor = conn.cursor()
            first = Deferred.fromCoroutine(recoverer.recover(downloader, cursor))
            self.assertThat(first, succeeded(Always()))

            cursor.execute("SELECT * FROM [succeeded]")
            self.assertThat(
                cursor.fetchall(),
                Equals([("yes",)]),
            )

    def test_failed_after_download_failed(self):
        """
        ``StatefulRecoverer`` automatically progresses to the failed stage when
        download fails with an exception.
        """
        downloader = make_fail_downloader(OSError("Something is wrong"))
        recoverer = StatefulRecoverer()
        with connect(":memory:") as conn:
            cursor = conn.cursor()
            first = Deferred.fromCoroutine(recoverer.recover(downloader, cursor))
            self.assertThat(
                first,
                succeeded(Always()),
            )
            self.assertThat(
                recoverer.state(),
                MatchesStructure(
                    stage=Equals(RecoveryStages.download_failed),
                ),
            )

    def test_failed_after_recover_failed(self):
        """
        ``StatefulRecoverer`` automatically progresses to the failed stage when
        recovery fails with an exception.
        """
        downloader = make_canned_downloader(b"non-sql junk to provoke a failure")
        recoverer = StatefulRecoverer()
        with connect(":memory:") as conn:
            cursor = conn.cursor()
            first = Deferred.fromCoroutine(recoverer.recover(downloader, cursor))
            self.assertThat(
                first,
                succeeded(Always()),
            )
            self.assertThat(
                recoverer.state(),
                MatchesStructure(
                    stage=Equals(RecoveryStages.import_failed),
                ),
            )

    def test_cannot_recover_twice(self):
        """
        A second call to ``StatefulRecoverer.recover`` fails with
        ``AlreadyRecovering``.
        """
        downloader = noop_downloader
        recoverer = StatefulRecoverer()
        with connect(":memory:") as conn:
            cursor = conn.cursor()
            self.assertThat(
                Deferred.fromCoroutine(recoverer.recover(downloader, cursor)),
                succeeded(Always()),
            )
            second = Deferred.fromCoroutine(recoverer.recover(downloader, cursor))
            self.assertThat(
                second,
                failed(
                    AfterPreprocessing(
                        lambda f: f.value,
                        IsInstance(AlreadyRecovering),
                    ),
                ),
            )


class TahoeLAFSDownloaderTests(TestCase):
    """
    Tests for ``get_tahoe_lafs_downloader`` and ``tahoe_lafs_downloader``.
    """

    # Support test methods that return a Deferred.
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=60.0)

    # Get a Tahoe-LAFS client node connected to a storage node.
    resources = [("client", client_manager)]

    def setUp(self):
        super().setUp()
        setUpResources(self, self.resources, None)
        self.addCleanup(lambda: tearDownResources(self, self.resources, None))

    @inlineCallbacks
    def test_get_downloader(self):
        """
        ``get_tahoe_lafs_downloader`` returns a downloader factory that can be
        used to download objects using a Tahoe-LAFS client.
        """
        snapshot_path = FilePath(self.useFixture(TempDir()).join("snapshot-source"))
        snapshot_path.setContent(b"snapshot data")

        config = read_config(self.client.node_dir.path, "tub.port")
        # AsynchronousDeferredRunTest sets reactor on us.
        httpclient = self.useFixture(Treq(self.reactor, case=self)).client()

        replica_dir_cap_str = yield Deferred.fromCoroutine(
            make_directory(httpclient, self.client.node_url),
        )
        snapshot_cap_str = yield Deferred.fromCoroutine(
            upload(httpclient, snapshot_path, self.client.node_url)
        )
        yield Deferred.fromCoroutine(
            link(
                httpclient,
                self.client.node_url,
                replica_dir_cap_str,
                "snapshot.sql",
                snapshot_cap_str,
            )
        )

        get_downloader = get_tahoe_lafs_downloader(httpclient, config)
        download = get_downloader(replica_dir_cap_str)

        downloaded_snapshot_path = yield Deferred.fromCoroutine(
            download(lambda state: None)
        )
        self.assertThat(
            downloaded_snapshot_path.getContent(),
            Equals(snapshot_path.getContent()),
        )
