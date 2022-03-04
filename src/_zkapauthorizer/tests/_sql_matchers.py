"""
Testtools matchers related to SQL functionality.
"""

from sqlite3 import Connection
from typing import Iterator, Tuple, Union

from attrs import define, field
from testtools.matchers import AfterPreprocessing, Annotate, Equals, Mismatch

from ._float_matchers import matches_float_within_distance
from .sql import escape

SQLType = Union[int, float, str, bytes, None]


def equals_database(reference: Connection):
    """
    :return: A matcher for a SQLite3 connection to a database with the same
        state as the reference connection's database.
    """

    # The implementation strategy here is motivated by the need to apply a
    # custom floating point comparison function.  This means we can't just
    # compare dumped SQL statement strings.  Instead of trying to parse the
    # SQL statement strings to extract the floating point values, we dump the
    # database ourselves without bothering to generate the SQL statement
    # strings in the first place.  Then we can dig into the resulting values,
    # notice floats, and compare them with our custom logic.
    #
    # We need custom logic to compare floats because SQLite3 bugs cause
    # certain values not to round-trip through the database correctly.  This
    # is a huge bummer!  Fortunately the error is small and does not
    # accumulate.

    return AfterPreprocessing(
        lambda actual: list(structured_dump(actual)),
        _MatchesDump(list(structured_dump(reference))),
    )


def structured_dump(db: Connection) -> Iterator[Tuple]:
    """
    Dump the whole database, schema and rows, without trying to do any string
    formatting.
    """
    tables = list(_structured_dump_tables(db))
    for (name, sql) in tables:
        yield sql
        yield from _structured_dump_table(db, name)


def _structured_dump_tables(db: Connection) -> Iterator[Tuple[str, str]]:
    curs = db.cursor()
    curs.execute(
        """
        SELECT [name], [sql]
        FROM [sqlite_master]
        WHERE [sql] NOT NULL and [type] == 'table'
        ORDER BY [name]
        """
    )
    yield from iter(curs)


def _structured_dump_table(
    db: Connection, table_name: str
) -> Iterator[Tuple[str, str, Tuple[SQLType, ...]]]:
    """
    Dump a single database table's rows without trying to do any string
    formatting.
    """
    curs = db.cursor()
    curs.execute(f"PRAGMA table_info({escape(table_name)})")

    columns = list(
        (name, type_) for (cid, name, type_, notnull, dftl_value, pk) in list(curs)
    )
    column_names = ", ".join(escape(name) for (name, type_) in columns)
    curs.execute(
        f"""
        SELECT {column_names}
        FROM {escape(table_name)}
        """
    )

    for rows in iter(lambda: curs.fetchmany(1024), []):
        for row in rows:
            yield "INSERT", table_name, row


@define
class _MatchStatement:
    """
    Match a single structured SQL statement.  Statements are tuples like those
    that ``equals_db`` deals with, not actual SQL strings.
    """

    reference = field()

    def match(self, actual):
        def match_field(reference):
            if not isinstance(reference, float):
                return Equals(reference)

            # We can't compare floats for exact equality, not for the usual
            # reason but because of limitations of SQLite3's support for
            # floats.  This is particularly bad on Windows.
            #
            # https://www.exploringbinary.com/incorrect-decimal-to-floating-point-conversion-in-sqlite/
            # https://www.mail-archive.com/sqlite-users@mailinglists.sqlite.org/msg56817.html
            # https://www.sqlite.org/src/tktview?name=1248e6cda8
            return matches_float_within_distance(reference, 0)

        if actual[:1] == ("INSERT",):
            if self.reference[:1] != ("INSERT",):
                return Mismatch(
                    f"{actual} != {self.reference}",
                )
            # Match an insert-type statement.
            actual_name, actual_row = actual[1:]
            reference_name, reference_row = self.reference[1:]
            if actual_name != reference_name:
                return Mismatch(
                    f"table name {actual_name} != {reference_name}",
                )
            if len(actual_row) != len(reference_row):
                return Mismatch(
                    f"length {len(actual_row)} != {len(reference_row)}",
                )
            for (actual_field, reference_field) in zip(actual_row, reference_row):
                matcher = match_field(reference_field)
                mismatch = matcher.match(actual_field)
                if mismatch is not None:
                    return mismatch
        else:
            # Match a DDL statement
            return Equals(self.reference).match(actual)


@define
class _MatchesDump:
    """
    Match a complete database dump's worth of structured SQL statements.
    """

    reference = field()

    def match(self, actual):
        for n, (a, r) in enumerate(zip(actual, self.reference)):
            mismatch = Annotate(f"row {n}", _MatchStatement(r)).match(a)
            if mismatch is not None:
                return mismatch

        if len(actual) != len(self.reference):
            return Mismatch(
                f"reference has {len(self.reference)} items; "
                f"actual as {len(actual)} items",
            )

        return None
