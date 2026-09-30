"""
Composite foreign-key tests.

A CompositeForeignKey maps a group of local columns to a group of unique
columns on the target model. Table creation emits a real composite FOREIGN
KEY constraint, and joins / prefetch / lazy-load always use the whole key
group (never a string-concatenated key).
"""
from peewee import *

from .base import ModelTestCase
from .base import TestModel
from .base import get_in_memory_db


class Account(TestModel):
    tenant_id = IntegerField()
    code = CharField()
    name = CharField(default='')

    class Meta:
        # Unique combination that is *not* the primary key.
        indexes = ((('tenant_id', 'code'), True),)


class Order(TestModel):
    tenant_id = IntegerField()
    account_code = CharField()
    account = CompositeForeignKey(Account, fields=(
        ('tenant_id', 'tenant_id'),
        ('account_code', 'code'),
    ), backref='orders')


class Note(TestModel):
    tenant_id = IntegerField(null=True)
    account_code = CharField(null=True)
    account = CompositeForeignKey(Account, fields=(
        ('tenant_id', 'tenant_id'),
        ('account_code', 'code'),
    ), backref='notes')


class Employee(TestModel):
    tenant_id = IntegerField()
    code = CharField()
    mgr_tenant = IntegerField(null=True)
    mgr_code = CharField(null=True)
    manager = CompositeForeignKey('self', fields=(
        ('mgr_tenant', 'tenant_id'),
        ('mgr_code', 'code'),
    ), backref='reports')

    class Meta:
        indexes = ((('tenant_id', 'code'), True),)


class TestCompositeFKDDL(ModelTestCase):
    requires = [Account, Order]

    def test_create_table_emits_composite_constraint(self):
        sql, _ = Order._schema._create_table().query()
        self.assertIn(
            'FOREIGN KEY ("tenant_id", "account_code") '
            'REFERENCES "account" ("tenant_id", "code")', sql)

    def test_self_referential_constraint(self):
        sql, _ = Employee._schema._create_table().query()
        self.assertIn(
            'FOREIGN KEY ("mgr_tenant", "mgr_code") '
            'REFERENCES "employee" ("tenant_id", "code")', sql)

    def test_constraint_options(self):
        class Ref(Model):
            tenant_id = IntegerField()
            code = CharField()

            class Meta:
                database = self.database
                indexes = ((('tenant_id', 'code'), True),)

        class Opt(Model):
            tenant_id = IntegerField()
            code = CharField()
            ref = CompositeForeignKey(Ref, fields=(
                ('tenant_id', 'tenant_id'), ('code', 'code')),
                on_delete='CASCADE', on_update='CASCADE')

            class Meta:
                database = self.database

        sql, _ = Opt._schema._create_table().query()
        self.assertIn('ON DELETE CASCADE', sql)
        self.assertIn('ON UPDATE CASCADE', sql)


class TestCompositeFKRelations(ModelTestCase):
    requires = [Account, Order, Note, Employee]

    def setUp(self):
        super(TestCompositeFKRelations, self).setUp()
        # Two tenants share the same code; the composite key is what
        # disambiguates them.
        self.a1 = Account.create(tenant_id=1, code='a', name='acct-1')
        self.a2 = Account.create(tenant_id=2, code='a', name='acct-2')
        self.o1 = Order.create(tenant_id=1, account_code='a')
        self.o2 = Order.create(tenant_id=2, account_code='a')

    def test_lazy_load_uses_full_key(self):
        o1 = Order.get_by_id(self.o1.id)
        o2 = Order.get_by_id(self.o2.id)
        with self.assertQueryCount(2):
            self.assertEqual(o1.account.name, 'acct-1')
            self.assertEqual(o2.account.name, 'acct-2')
        # Cached on the instance now.
        with self.assertQueryCount(0):
            self.assertEqual(o1.account.name, 'acct-1')

    def test_lazy_load_cache_invalidation(self):
        order = Order.get_by_id(self.o1.id)
        self.assertEqual(order.account.name, 'acct-1')
        # Changing a local column invalidates the cached relation.
        order.tenant_id = 2
        with self.assertQueryCount(1):
            self.assertEqual(order.account.name, 'acct-2')

    def test_incomplete_key_issues_no_query(self):
        note = Note(tenant_id=1)  # account_code is NULL.
        with self.assertQueryCount(0):
            self.assertIsNone(note.account)
        note = Note(account_code='a')  # tenant_id is NULL.
        with self.assertQueryCount(0):
            self.assertIsNone(note.account)

    def test_missing_key_not_nullable_raises_without_query(self):
        order = Order()
        with self.assertQueryCount(0):
            self.assertRaises(Account.DoesNotExist, lambda: order.account)

    def test_assignment_by_instance(self):
        order = Order(account=self.a2)
        self.assertEqual(order.tenant_id, 2)
        self.assertEqual(order.account_code, 'a')
        self.assertEqual(order.account.name, 'acct-2')
        order.save()
        self.assertEqual(Order.get_by_id(order.id).tenant_id, 2)

    def test_assignment_by_tuple(self):
        order = Order()
        order.account = (2, 'a')
        self.assertEqual((order.tenant_id, order.account_code), (2, 'a'))
        order.save()
        self.assertEqual(Order.get_by_id(order.id).account.name, 'acct-2')

    def test_assignment_validation(self):
        order = Order()
        # Wrong number of values.
        self.assertRaises(ValueError, setattr, order, 'account', (1,))
        self.assertRaises(ValueError, setattr, order, 'account', (1, 'a', 3))
        # None on a non-nullable relation.
        self.assertRaises(ValueError, setattr, order, 'account', None)
        # Partially-null composite value.
        self.assertRaises(ValueError, setattr, order, 'account', (1, None))
        # Wrong type altogether.
        self.assertRaises(ValueError, setattr, order, 'account', 'a')
        self.assertRaises(ValueError, setattr, order, 'account',
                          Account(tenant_id=1, code=None))

    def test_nullable_relation(self):
        note = Note.create()
        self.assertIsNone(Note.get_by_id(note.id).account)
        note.account = self.a1
        note.save()
        self.assertEqual(Note.get_by_id(note.id).account.name, 'acct-1')
        note.account = None
        note.save()
        fresh = Note.get_by_id(note.id)
        self.assertIsNone(fresh.tenant_id)
        with self.assertQueryCount(0):
            self.assertIsNone(fresh.account)

    def test_backref(self):
        self.assertEqual([o.id for o in self.a1.orders], [self.o1.id])
        self.assertEqual([o.id for o in self.a2.orders], [self.o2.id])

    def test_join_uses_full_key(self):
        query = (Order.select(Order, Account)
                 .join(Account)
                 .order_by(Order.id))
        sql, _ = query.sql()
        self.assertIn('"t1"."tenant_id" = "t2"."tenant_id"', sql)
        self.assertIn('"t1"."account_code" = "t2"."code"', sql)
        self.assertEqual([o.account.name for o in query],
                         ['acct-1', 'acct-2'])

    def test_join_on_clause_with_field(self):
        query = (Order.select(Order, Account)
                 .join(Account, on=Order.account)
                 .order_by(Order.id))
        self.assertEqual([o.account.name for o in query],
                         ['acct-1', 'acct-2'])

    def _assert_prefetch(self, **kwargs):
        orders = prefetch(Order.select().order_by(Order.id), Account,
                          **kwargs)
        self.assertEqual([o.account.name for o in orders],
                         ['acct-1', 'acct-2'])
        accounts = prefetch(Account.select().order_by(Account.tenant_id),
                            Order, **kwargs)
        self.assertEqual([[o.id for o in a.orders] for a in accounts],
                         [[self.o1.id], [self.o2.id]])

    def test_prefetch_where(self):
        self._assert_prefetch(prefetch_type=PREFETCH_TYPE.WHERE)

    def test_prefetch_join(self):
        self._assert_prefetch(prefetch_type=PREFETCH_TYPE.JOIN)

    def test_prefetch_keys_not_concatenated(self):
        # (1, '23') and (12, '3') would collide if the key columns were
        # concatenated into a single string.
        a3 = Account.create(tenant_id=1, code='23', name='acct-3')
        a4 = Account.create(tenant_id=12, code='3', name='acct-4')
        o3 = Order.create(tenant_id=1, account_code='23')
        o4 = Order.create(tenant_id=12, account_code='3')
        orders = {(o.tenant_id, o.account_code): o for o in
                  prefetch(Order.select(), Account)}
        self.assertEqual(orders[(1, '23')].account.name, 'acct-3')
        self.assertEqual(orders[(12, '3')].account.name, 'acct-4')
        o4_fresh = Order.get_by_id(o4.id)
        self.assertEqual(o4_fresh.account.name, 'acct-4')

    def test_with_related(self):
        orders = (Order.select().order_by(Order.id)
                  .with_related(Order.account))
        self.assertEqual([o.account.name for o in orders],
                         ['acct-1', 'acct-2'])
        accounts = (Account.select().order_by(Account.tenant_id)
                    .with_related(Account.orders))
        self.assertEqual([[o.id for o in a.orders] for a in accounts],
                         [[self.o1.id], [self.o2.id]])

    def test_filter_across_relation(self):
        query = Order.filter(account__name='acct-2')
        self.assertEqual([o.id for o in query], [self.o2.id])

    def test_rollback_keeps_state_consistent(self):
        order = Order.get_by_id(self.o1.id)
        try:
            with self.database.atomic():
                order.account = self.a2
                order.save()
                self.database.execute_sql('INSERT INTO bogus VALUES (1)')
        except OperationalError:
            pass

        # The database still references the original account.
        fresh = Order.get_by_id(self.o1.id)
        self.assertEqual((fresh.tenant_id, fresh.account_code), (1, 'a'))
        with self.assertQueryCount(1):
            self.assertEqual(fresh.account.name, 'acct-1')

        # The failed write left the in-memory object with the rolled-back
        # key; restoring the local columns must drop the stale cached
        # relation and reload from the database.
        self.assertEqual((order.tenant_id, order.account_code), (2, 'a'))
        order.tenant_id = 1
        with self.assertQueryCount(1):
            self.assertEqual(order.account.name, 'acct-1')


class TestCompositeFKSelfRelation(ModelTestCase):
    requires = [Employee]

    def setUp(self):
        super(TestCompositeFKSelfRelation, self).setUp()
        self.e1 = Employee.create(tenant_id=1, code='root')
        self.e2 = Employee.create(tenant_id=2, code='root')
        self.e3 = Employee.create(tenant_id=1, code='worker',
                                  mgr_tenant=1, mgr_code='root')
        self.e4 = Employee.create(tenant_id=2, code='worker',
                                  mgr_tenant=2, mgr_code='root')

    def test_lazy_load(self):
        e3 = Employee.get_by_id(self.e3.id)
        e4 = Employee.get_by_id(self.e4.id)
        self.assertEqual(e3.manager.id, self.e1.id)
        self.assertEqual(e4.manager.id, self.e2.id)

    def test_backref(self):
        self.assertEqual([e.id for e in self.e1.reports], [self.e3.id])
        self.assertEqual([e.id for e in self.e2.reports], [self.e4.id])

    def test_incomplete_self_key(self):
        employee = Employee(tenant_id=9, code='x', mgr_code='root')
        with self.assertQueryCount(0):
            self.assertIsNone(employee.manager)

    def test_model_alias_join(self):
        Manager = Employee.alias()
        query = (Employee.select(Employee, Manager)
                 .join(Manager, on=Employee.manager)
                 .where(Employee.code == 'worker')
                 .order_by(Employee.tenant_id))
        employees = list(query)
        self.assertEqual([e.manager.tenant_id for e in employees], [1, 2])
        self.assertEqual([e.manager.id for e in employees],
                         [self.e1.id, self.e2.id])

    def test_prefetch_self_relation(self):
        employees = prefetch(
            Employee.select().where(Employee.code == 'worker')
            .order_by(Employee.tenant_id),
            Employee)
        self.assertEqual([e.manager.id for e in employees],
                         [self.e1.id, self.e2.id])


class TestCompositeFKConstraints(ModelTestCase):
    database = get_in_memory_db(pragmas={'foreign_keys': 1})
    requires = [Account, Order]

    def setUp(self):
        super(TestCompositeFKConstraints, self).setUp()
        self.a1 = Account.create(tenant_id=1, code='a')

    def test_orphan_insert_rejected(self):
        self.assertRaises(
            IntegrityError,
            Order.create, tenant_id=9, account_code='missing')

    def test_orphan_update_rejected(self):
        order = Order.create(tenant_id=1, account_code='a')
        order.tenant_id = 9
        self.assertRaises(IntegrityError, order.save)

    def test_rollback_after_failed_write(self):
        order = Order.create(tenant_id=1, account_code='a')
        try:
            with self.database.atomic():
                Order.create(tenant_id=9, account_code='missing')
        except IntegrityError:
            pass
        # The failed insert was rolled back; the valid row is intact and
        # its relation resolves against the database state.
        self.assertEqual(Order.select().count(), 1)
        fresh = Order.get_by_id(order.id)
        with self.assertQueryCount(1):
            self.assertEqual(fresh.account.id, self.a1.id)
