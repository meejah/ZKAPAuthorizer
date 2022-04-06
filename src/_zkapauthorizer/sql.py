"""
Model SQL-related datatypes.

This is focused on SQLite3 and no doubt nevertheless incomplete.  The goal is
to support testing the replication/recovery system.
"""

import re
from enum import Enum, auto
from sqlite3 import Cursor
from typing import Union

from attrs import define
from sqlparse import parse

SQLType = Union[int, float, str, bytes, None]


class StorageAffinity(Enum):
    """
    Represent the different "storage affinities" possible for a SQLite3
    column.
    """

    INT = auto()
    TEXT = auto()
    BLOB = auto()
    REAL = auto()
    NUMERIC = auto()


@define(frozen=True)
class Column:
    """
    Represent a column in a SQLite3 table.

    :ivar affinity: The expected type affinity for values in this column.  See
        https://www.sqlite.org/datatype3.html
    """

    affinity: StorageAffinity


@define(frozen=True)
class Table:
    """
    Represent a table in a SQLite3 database.

    :ivar columns: The columns that make up this table.
    """

    columns: list[tuple[str, Column]]


@define(frozen=True)
class Insert:
    """
    Represent an insertion of one row into a table.

    :ivar table_name: The name of the table where the row can be inserted.

    :ivar table: A representation of the table itself.

    :ivar fields: The values which can be inserted.
    """

    table_name: str
    table: Table
    fields: tuple[SQLType, ...]

    def statement(self):
        names = ", ".join((escape_identifier(name) for (name, _) in self.table.columns))
        placeholders = ", ".join("?" * len(self.table.columns))
        return (
            f"INSERT INTO {escape_identifier(self.table_name)} "
            f"({names}) "
            f"VALUES ({placeholders})"
        )

    def bound_statement(self, cursor):
        """
        :returns: the statement with all values interpolated into it
            rather than as separate values
        """
        names = ", ".join((escape_identifier(name) for (name, _) in self.table.columns))
        values = ", ".join(
            (quote_sql_value(cursor, value) for value in self.arguments())
        )
        return (
            f"INSERT INTO {escape_identifier(self.table_name)} "
            f"({names}) "
            f"VALUES ({values})"
        )

    def arguments(self):
        return self.fields


def quote_sql_value(cursor: Cursor, value: Union[int, float, str, bytes, None]) -> str:
    """
    Use the SQL `quote()` function to return the quoted version of
    `value`. Supports `int`, `float`, `None`, `str` and `bytes`.

    :returns: the quoted value
    """
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if value is None:
        return "NULL"
    if isinstance(value, (str, bytes)):
        cursor.execute("SELECT quote(?);", (value,))
        return cursor.fetchall()[0][0]
    raise ValueError("Do not know how to quote value of type f{type(value)}")


def bind_arguments(cursor, statement, args):
    """
    Interpolate the arguments into position in the statement. For
    example, a statement 'INSERT INTO foo VALUES (?, ?)' and args (1,
    'bar') should result in 'INSERT INTO foo VALUES (1, "bar")'

    This is a simple substitution based on the ? character, which MUST
    NOT appear elsewhere in the SQL.
    """

    to_sub = list(args)

    def substitute_args(match):
        return quote_sql_value(cursor, to_sub.pop(0))

    # replace subsequent "?" characters with the next argument, quoted
    return re.sub(r"([?])", substitute_args, statement)


@define(frozen=True)
class Update:
    """
    Represent an update to some rows in a table.

    Currently this updates all rows.

    :ivar table_name: The name of the table to which the update applies.

    :ivar table: A representation of the table itself.

    :ivar fields: The new values for each column in the table.
    """

    table_name: str
    table: Table
    fields: tuple[SQLType, ...]

    def statement(self):
        field_names = list(name for (name, _) in self.table.columns)
        assignments = ", ".join(
            f"{escape_identifier(name)} = ?" for name in field_names
        )
        return f"UPDATE {escape_identifier(self.table_name)} SET {assignments}"

    def bound_statement(self, cursor):
        """
        :returns: the statement with all values interpolated into it
            rather than as separate values
        """
        field_names = list(name for (name, _) in self.table.columns)
        assignments = ", ".join(
            f"{escape_identifier(name)} = {quote_sql_value(cursor, value)}"
            for name, value in zip(field_names, self.fields)
        )
        return f"UPDATE {escape_identifier(self.table_name)} SET {assignments}"

    def arguments(self):
        return self.fields


@define(frozen=True)
class Select:
    """
    Represent a query about a certain table

    :ivar table_name: valid SQL identifier for a table
    """

    table_name: str

    def statement(self):
        return f"SELECT * FROM {escape_identifier(self.table_name)}"

    def bound_statement(self, cursor):
        """
        :returns: the statement with all values interpolated into it
            rather than as separate values
        """
        return self.statement()

    def arguments(self):
        return tuple()


@define(frozen=True)
class Delete:
    """
    Represent the deletion of some rows from a table.

    Currently this deletes all rows.

    :ivar table_name: The name of the table from which to rows can be deleted.
    """

    table_name: str

    def statement(self):
        return f"DELETE FROM {escape_identifier(self.table_name)}"

    def bound_statement(self, cursor):
        """
        :returns: the statement with all values interpolated into it
            rather than as separate values
        """
        return self.statement()

    def arguments(self):
        return ()


def escape_identifier(string: str) -> str:
    """
    Escape an arbitrary string for use as a SQLite3 identifier.
    """
    return f"[{string}]"


def column_ddl(name: str, column: Column) -> str:
    """
    Get a column DDL fragment for a column of the given name and type.

    :return: *bar* in **create table foo ( bar )**
    """
    return f"{escape_identifier(name)} {column.affinity.name}"


def create_table(name: str, table: Table) -> str:
    """
    Get a table creation DDL statement for a table of the given name and type.
    """
    columns = ", ".join(column_ddl(name, column) for (name, column) in table.columns)
    return f"CREATE TABLE {escape_identifier(name)} ({columns})"


def statement_mutates(statement):
    """
    predicate to decide if `statement` will change the database
    """
    (statement,) = parse(statement)
    return statement.get_type() not in {"SELECT"}
