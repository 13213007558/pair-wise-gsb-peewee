import time
import unittest

from peewee import NotSupportedError
from peewee import OperationalError
from peewee import SqliteDatabase
from peewee import TextField

from playhouse.sqlite_budget import BudgetExceeded
from playhouse.sqlite_budget import budget
from playhouse.sqlite_budget import set_progress_handler

from .base import BaseTestCase
from .base import ModelTestCase
from .base import TestModel

try:
    import apsw
    from playhouse.apsw_ext import APSWDatabase
except ImportError:
    apsw = None


database = SqliteDatabase(':memory:')


# Recursive computation that burns far more VM instructions than any of the
# budgets used below. sum(1..100000) = 5000050000.
RECURSIVE_SQL = ('WITH RECURSIVE cnt(x) AS ('
                 'SELECT 1 UNION ALL SELECT x + 1 FROM cnt LIMIT 100000) '
                 'SELECT sum(x) FROM cnt')
RECURSIVE_SUM = 5000050000


class Data(TestModel):
    key = TextField()


class TestBudget(ModelTestCase):
    database = database
    requires = [Data]

    def test_recursive_query_exhausts_budget_then_limit_revoked(self):
        conn = database.connection()
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(database, max_instructions=2000, interval=100,
                        label='recursive'):
                database.execute_sql(RECURSIVE_SQL).fetchone()
        exc = ctx.exception
        self.assertEqual(exc.frame.label, 'recursive')
        self.assertIn('recursive', str(exc))
        self.assertIn('instruction', str(exc))

        # The budget was fully revoked on the same connection: a short
        # query and a nested transaction both run to completion.
        self.assertIs(database.connection(), conn)
        self.assertEqual(database.execute_sql('SELECT 1 + 1').fetchone()[0], 2)
        with database.atomic():
            Data.create(key='k1')
            with database.atomic():
                Data.create(key='k2')
        self.assertEqual([row.key for row in
                          Data.select().order_by(Data.key)], ['k1', 'k2'])

        # And the recursive query itself now runs unimpeded.
        self.assertEqual(database.execute_sql(RECURSIVE_SQL).fetchone()[0],
                         RECURSIVE_SUM)

    def test_deadline_budget(self):
        # Deadline already in the past: the first check aborts the query.
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(database, deadline=time.monotonic() - 1,
                        interval=100, label='too-late'):
                database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertEqual(ctx.exception.frame.label, 'too-late')
        self.assertIn('deadline', str(ctx.exception))

        self.assertEqual(database.execute_sql('SELECT 1').fetchone()[0], 1)

    def test_nested_budgets_earliest_limit_wins(self):
        # Inner limit is smaller: the inner layer trips.
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(database, max_instructions=10 ** 9, interval=100,
                        label='outer'):
                with budget(database, max_instructions=1000, interval=100,
                            label='inner'):
                    database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertEqual(ctx.exception.frame.label, 'inner')

        # Outer limit is smaller: the outer layer trips even though the
        # inner budget is also active.
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(database, max_instructions=1000, interval=100,
                        label='outer'):
                with budget(database, max_instructions=10 ** 9,
                            interval=100, label='inner'):
                    database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertEqual(ctx.exception.frame.label, 'outer')

        # Both budgets were revoked on exit.
        self.assertEqual(database.execute_sql(RECURSIVE_SQL).fetchone()[0],
                         RECURSIVE_SUM)

    def test_default_label_identifies_layer(self):
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(database, max_instructions=10 ** 9, interval=100):
                with budget(database, max_instructions=1000, interval=100):
                    database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertIn('budget layer 2', str(ctx.exception))

    def test_application_handler_chained_and_restored(self):
        calls = []
        set_progress_handler(database, lambda: calls.append(1) or 0, 100)
        try:
            # The application's handler keeps firing while a budget runs.
            with budget(database, max_instructions=10 ** 9, interval=100):
                database.execute_sql(RECURSIVE_SQL).fetchone()
            self.assertTrue(len(calls) > 0)

            # It is still installed after the outermost budget exits.
            calls[:] = []
            database.execute_sql(RECURSIVE_SQL).fetchone()
            self.assertTrue(len(calls) > 0)
        finally:
            set_progress_handler(database, None)

        # Removed entirely: no more callbacks.
        calls[:] = []
        database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertEqual(calls, [])

    def test_application_abort_is_not_budget_error(self):
        set_progress_handler(database, lambda: 1, 100)
        try:
            with self.assertRaises(OperationalError) as ctx:
                with budget(database, max_instructions=10 ** 9, interval=100):
                    database.execute_sql(RECURSIVE_SQL).fetchone()
            self.assertNotIsInstance(ctx.exception, BudgetExceeded)
        finally:
            set_progress_handler(database, None)

    def test_sql_error_not_confused_with_budget_abort(self):
        with self.assertRaises(OperationalError) as ctx:
            with budget(database, max_instructions=10 ** 9, interval=100):
                database.execute_sql('SELECT * FROM no_such_table')
        self.assertNotIsInstance(ctx.exception, BudgetExceeded)

    def test_transaction_state_follows_sqlite_after_abort(self):
        # Aborted inside a transaction: peewee rolls the transaction back.
        with self.assertRaises(BudgetExceeded):
            with database.atomic():
                Data.create(key='lost')
                with budget(database, max_instructions=1000, interval=100):
                    database.execute_sql(RECURSIVE_SQL).fetchone()
        self.assertEqual(Data.select().count(), 0)

        # The connection is healthy: nested transactions work afterwards.
        with database.atomic():
            Data.create(key='k1')
            with database.atomic():
                Data.create(key='k2')
        self.assertEqual(Data.select().count(), 2)

    def test_raw_connection_accepted(self):
        conn = database.connection()
        with self.assertRaises(BudgetExceeded):
            with budget(conn, max_instructions=1000, interval=100):
                conn.execute(RECURSIVE_SQL).fetchone()
        self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)

    def test_requires_a_limit(self):
        with self.assertRaises(ValueError):
            budget(database)

    def test_unsupported_driver_reports_missing_capability(self):
        class FakeConnection(object):
            pass
        with self.assertRaises(NotSupportedError) as ctx:
            budget(FakeConnection(), max_instructions=10)
        self.assertIn('sqlite3 and APSW', str(ctx.exception))


@unittest.skipUnless(apsw is not None, 'requires APSW')
class TestAPSWBudget(BaseTestCase):
    def setUp(self):
        super(TestAPSWBudget, self).setUp()
        self.db = APSWDatabase(':memory:')
        self.db.connect()

    def tearDown(self):
        self.db.close()
        super(TestAPSWBudget, self).tearDown()

    def test_budget_aborts_then_limit_revoked(self):
        with self.assertRaises(BudgetExceeded) as ctx:
            with budget(self.db, max_instructions=1000, interval=100,
                        label='apsw-layer'):
                self.db.execute_sql(RECURSIVE_SQL).fetchone()
        # Same interface as the stdlib driver: identical exception type.
        self.assertEqual(ctx.exception.frame.label, 'apsw-layer')
        self.assertIsInstance(ctx.exception, OperationalError)

        # Budget revoked: short query and nested transaction succeed.
        self.assertEqual(self.db.execute_sql('SELECT 1 + 1').fetchone()[0], 2)
        self.db.execute_sql('CREATE TABLE data (key TEXT)')
        with self.db.atomic():
            self.db.execute_sql('INSERT INTO data (key) VALUES (?)', ('k1',))
            with self.db.atomic():
                self.db.execute_sql(
                    'INSERT INTO data (key) VALUES (?)', ('k2',))
        self.assertEqual(
            self.db.execute_sql('SELECT count(*) FROM data').fetchone()[0], 2)

    def test_application_handler_chained_and_restored(self):
        calls = []
        set_progress_handler(self.db, lambda: calls.append(1) or 0, 100)
        try:
            with budget(self.db, max_instructions=10 ** 9, interval=100):
                self.db.execute_sql(RECURSIVE_SQL).fetchone()
            self.assertTrue(len(calls) > 0)

            calls[:] = []
            self.db.execute_sql(RECURSIVE_SQL).fetchone()
            self.assertTrue(len(calls) > 0)
        finally:
            set_progress_handler(self.db, None)
