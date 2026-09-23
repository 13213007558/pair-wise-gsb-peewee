import time
import unittest

from peewee import IntegrityError
from peewee import NotSupportedError
from peewee import OperationalError
from peewee import SqliteDatabase

from playhouse.sqlite_budget import BudgetExceeded
from playhouse.sqlite_budget import QueryBudget
from playhouse.sqlite_budget import clear_progress_handler
from playhouse.sqlite_budget import execution_budget
from playhouse.sqlite_budget import set_progress_handler

try:
    import apsw
    from playhouse.apsw_ext import APSWDatabase
except ImportError:
    apsw = None
    APSWDatabase = None


LONG_QUERY = (
    'WITH RECURSIVE cnt(x) AS ('
    'SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 100000000)'
    ' SELECT count(x) FROM cnt')
SHORT_QUERY = (
    'WITH RECURSIVE cnt(x) AS ('
    'SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 200000)'
    ' SELECT count(x) FROM cnt')


class BudgetTestMixin(object):
    database_class = None

    def setUp(self):
        super(BudgetTestMixin, self).setUp()
        self.db = self.database_class(':memory:')

    def tearDown(self):
        self.db.close()
        super(BudgetTestMixin, self).tearDown()

    def test_instruction_budget_aborts_and_is_revoked(self):
        with self.assertRaises(BudgetExceeded) as ctx:
            with QueryBudget(self.db, max_instructions=5000,
                             name='count-job') as budget:
                self.db.execute_sql(LONG_QUERY)
        self.assertIs(ctx.exception.budget, budget)
        self.assertIsInstance(ctx.exception, OperationalError)
        self.assertIn('count-job', str(ctx.exception))

        row = self.db.execute_sql(SHORT_QUERY).fetchone()
        self.assertEqual(row[0], 200000)

        with self.db.atomic():
            self.db.execute_sql('CREATE TABLE kv (k TEXT)')
            with self.db.atomic():
                self.db.execute_sql("INSERT INTO kv (k) VALUES ('a')")
        row = self.db.execute_sql('SELECT k FROM kv').fetchone()
        self.assertEqual(row[0], 'a')

    def test_deadline_budget(self):
        start = time.monotonic()
        with self.assertRaises(BudgetExceeded) as ctx:
            with QueryBudget(self.db, timeout=0.1, name='deadline-layer'):
                self.db.execute_sql(LONG_QUERY)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn('deadline-layer', str(ctx.exception))

        deadline = time.monotonic() + 0.1
        with self.assertRaises(BudgetExceeded):
            with QueryBudget(self.db, deadline=deadline):
                self.db.execute_sql(LONG_QUERY)

        row = self.db.execute_sql(SHORT_QUERY).fetchone()
        self.assertEqual(row[0], 200000)

    def test_nested_budgets_inner_trips(self):
        outer = QueryBudget(self.db, max_instructions=10 ** 9,
                            name='outer')
        with outer:
            with self.assertRaises(BudgetExceeded) as ctx:
                with QueryBudget(self.db, max_instructions=5000,
                                 name='inner') as inner:
                    self.db.execute_sql(LONG_QUERY)
            self.assertIs(ctx.exception.budget, inner)
            self.assertIn('inner', str(ctx.exception))
            self.assertFalse(outer.exhausted)

            row = self.db.execute_sql(SHORT_QUERY).fetchone()
            self.assertEqual(row[0], 200000)

    def test_nested_budgets_outer_trips(self):
        with self.assertRaises(BudgetExceeded) as ctx:
            with QueryBudget(self.db, max_instructions=5000,
                             name='outer') as outer:
                with QueryBudget(self.db, max_instructions=10 ** 9,
                                 name='inner') as inner:
                    self.db.execute_sql(LONG_QUERY)
        self.assertIs(ctx.exception.budget, outer)
        self.assertIn('outer', str(ctx.exception))
        self.assertFalse(inner.exhausted)

        row = self.db.execute_sql(SHORT_QUERY).fetchone()
        self.assertEqual(row[0], 200000)

    def test_app_handler_coexists_and_is_restored(self):
        calls = []

        def app_handler():
            calls.append(1)
            return 0

        set_progress_handler(self.db, app_handler)
        try:
            with self.assertRaises(BudgetExceeded):
                with QueryBudget(self.db, max_instructions=5000):
                    self.db.execute_sql(LONG_QUERY)
            self.assertTrue(calls)

            del calls[:]
            self.db.execute_sql(SHORT_QUERY).fetchone()
            self.assertTrue(calls)
        finally:
            clear_progress_handler(self.db)

        del calls[:]
        self.db.execute_sql(SHORT_QUERY).fetchone()
        self.assertEqual(calls, [])

    def test_app_handler_can_abort(self):
        set_progress_handler(self.db, lambda: 1)
        try:
            with self.assertRaises(OperationalError) as ctx:
                self.db.execute_sql(LONG_QUERY)
            self.assertNotIsInstance(ctx.exception, BudgetExceeded)
        finally:
            clear_progress_handler(self.db)

    def test_sql_errors_are_not_relabelled(self):
        with QueryBudget(self.db, max_instructions=10 ** 9):
            with self.assertRaises(OperationalError) as ctx:
                self.db.execute_sql('SELECT * FROM no_such_table')
            self.assertNotIsInstance(ctx.exception, BudgetExceeded)

        self.db.execute_sql('CREATE TABLE uniq (k TEXT UNIQUE)')
        self.db.execute_sql("INSERT INTO uniq VALUES ('a')")
        with QueryBudget(self.db, max_instructions=10 ** 9):
            with self.assertRaises(IntegrityError):
                self.db.execute_sql("INSERT INTO uniq VALUES ('a')")

    def test_abort_inside_transaction_rolls_back(self):
        self.db.execute_sql('CREATE TABLE kv (k TEXT)')
        with self.assertRaises(BudgetExceeded):
            with self.db.atomic():
                self.db.execute_sql("INSERT INTO kv VALUES ('a')")
                with QueryBudget(self.db, max_instructions=5000):
                    self.db.execute_sql(LONG_QUERY)
        row = self.db.execute_sql('SELECT count(*) FROM kv').fetchone()
        self.assertEqual(row[0], 0)

        self.db.execute_sql("INSERT INTO kv VALUES ('b')")
        row = self.db.execute_sql('SELECT count(*) FROM kv').fetchone()
        self.assertEqual(row[0], 1)

    def test_execution_budget_alias(self):
        budget = execution_budget(self.db, max_instructions=100)
        self.assertIsInstance(budget, QueryBudget)
        with self.assertRaises(BudgetExceeded):
            with budget:
                self.db.execute_sql(LONG_QUERY)

    def test_raw_connection_accepted(self):
        conn = self.db.connection()
        with self.assertRaises(BudgetExceeded):
            with QueryBudget(conn, max_instructions=5000):
                self.db.execute_sql(LONG_QUERY)
        row = self.db.execute_sql(SHORT_QUERY).fetchone()
        self.assertEqual(row[0], 200000)

    def test_requires_a_limit(self):
        self.assertRaises(ValueError, QueryBudget, self.db)
        self.assertRaises(ValueError, QueryBudget, self.db,
                          deadline=1, timeout=1)

    def test_unsupported_driver(self):
        class FakeConn(object):
            pass

        class FakeDb(object):
            def connection(self):
                return FakeConn()

        with self.assertRaises(NotSupportedError):
            with QueryBudget(FakeDb(), max_instructions=10):
                pass


class TestSqliteBudget(BudgetTestMixin, unittest.TestCase):
    database_class = SqliteDatabase


@unittest.skipUnless(apsw is not None, 'requires apsw')
class TestAPSWBudget(BudgetTestMixin, unittest.TestCase):
    database_class = APSWDatabase
