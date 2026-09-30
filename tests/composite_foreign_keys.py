"""Tests for composite foreign-key relationships."""

from peewee import *

from .base import IS_SQLITE
from .base import ModelTestCase
from .base import TestModel
from .base import get_in_memory_db
from .base import skip_unless


database = get_in_memory_db(pragmas={'foreign_keys': 1})


class TenantParent(TestModel):
    tenant = CharField()
    code = CharField()
    name = CharField()

    class Meta:
        database = database
        primary_key = CompositeKey('tenant', 'code')


class TenantChild(TestModel):
    tenant = CharField()
    code = CharField()
    label = CharField()
    parent = CompositeForeignKeyField(
        TenantParent,
        ('tenant', 'code'),
        backref='children')

    class Meta:
        database = database


class UniqueTenant(TestModel):
    id = AutoField()
    tenant = CharField()
    code = CharField()

    class Meta:
        database = database
        indexes = ((('tenant', 'code'), True),)


class UniqueChild(TestModel):
    id = AutoField()
    tenant = CharField()
    code = CharField()
    target = CompositeForeignKeyField(
        UniqueTenant,
        ('tenant', 'code'),
        ('tenant', 'code'),
        backref='children')

    class Meta:
        database = database


class Node(TestModel):
    tenant = CharField()
    code = CharField()
    parent_code = CharField(null=True)
    parent = CompositeForeignKeyField(
        'self',
        ('tenant', 'parent_code'),
        ('tenant', 'code'),
        backref='children',
        null=True)

    class Meta:
        database = database
        primary_key = CompositeKey('tenant', 'code')


@skip_unless(IS_SQLITE, 'requires sqlite')
class TestCompositeForeignKeys(ModelTestCase):
    database = database
    requires = [TenantParent, TenantChild, UniqueTenant, UniqueChild, Node]

    def setUp(self):
        super(TestCompositeForeignKeys, self).setUp()
        database.foreign_keys = 1
        parents = [
            ('a', 'x', 'parent-a-x'),
            ('b', 'x', 'parent-b-x'),
            ('a', 'y', 'parent-a-y')]
        TenantParent.insert_many(parents, fields=[
            TenantParent.tenant, TenantParent.code, TenantParent.name]).execute()
        TenantChild.insert_many([
            ('a', 'x', 'child-a-x'),
            ('b', 'x', 'child-b-x'),
            ('a', 'y', 'child-a-y')], fields=[
                TenantChild.tenant, TenantChild.code,
                TenantChild.label]).execute()

    def test_schema_constraint(self):
        foreign_keys = database.get_foreign_keys(TenantChild._meta.table_name)
        self.assertEqual([fk.column for fk in foreign_keys], ['tenant', 'code'])
        self.assertEqual({fk.dest_table for fk in foreign_keys},
                         {TenantParent._meta.table_name})
        self.assertEqual([fk.dest_column for fk in foreign_keys],
                         ['tenant', 'code'])

    def test_assignment_and_lazy_load(self):
        child = TenantChild.get(TenantChild.label == 'child-b-x')
        with self.assertQueryCount(1):
            parent = child.parent
        self.assertEqual((parent.tenant, parent.code, parent.name),
                         ('b', 'x', 'parent-b-x'))
        with self.assertQueryCount(0):
            self.assertIs(child.parent, parent)

        replacement = TenantParent.get(
            (TenantParent.tenant == 'a') &
            (TenantParent.code == 'y'))
        child.parent = replacement
        self.assertEqual((child.tenant, child.code), ('a', 'y'))
        child.__data__['code'] = None
        self.reset_sql_history()
        self.assertIsNone(child.parent)
        self.assertEqual(len(self.history), 0)

    def test_assignment_validation(self):
        child = TenantChild()
        with self.assertRaises(ValueError):
            child.parent = ('a',)
        with self.assertRaises(ValueError):
            child.parent = ('a', None)
        with self.assertRaises(ValueError):
            child.parent = None
        with self.assertRaises(TypeError):
            child.parent = 'a-x'

    def test_default_join_uses_all_columns(self):
        query = (TenantChild
                 .select(TenantChild, TenantParent)
                 .join(TenantParent)
                 .order_by(TenantChild.tenant, TenantChild.code))
        sql, params = query.sql()
        self.assertIn('"tenant" = ', sql)
        self.assertIn('"code" = ', sql)
        self.assertNotIn("'a-x'", sql)
        self.assertEqual(params, [])

        rows = [(c.tenant, c.code, c.parent.name) for c in query]
        self.assertEqual(rows, [
            ('a', 'x', 'parent-a-x'),
            ('a', 'y', 'parent-a-y'),
            ('b', 'x', 'parent-b-x')])

    def test_alias_join_and_prefetch(self):
        parent_alias = TenantParent.alias()
        query = (TenantChild
                 .select(TenantChild, parent_alias)
                 .join(parent_alias, src=TenantChild))
        sql, _params = query.sql()
        self.assertEqual(sql.count('"tenant" ='), 1)
        self.assertEqual(sql.count('"code" ='), 1)
        for child in query:
            self.assertEqual(child.tenant, child.parent.tenant)
            self.assertEqual(child.code, child.parent.code)

        p_alias = TenantParent.alias()
        query = prefetch(p_alias.select(), TenantChild.select())
        for parent in query:
            for child in parent.children:
                self.assertEqual((child.tenant, child.code),
                                 (parent.tenant, parent.code))

    def test_prefetch_forward_and_backref(self):
        for strategy in (PREFETCH_TYPE.WHERE, PREFETCH_TYPE.JOIN):
            self.reset_sql_history()
            with self.assertQueryCount(2):
                children = TenantChild.select().order_by(
                    TenantChild.tenant, TenantChild.code)
                result = prefetch(children, TenantParent.select(),
                                  prefetch_type=strategy)
                self.assertEqual([
                    (c.tenant, c.code, c.parent.name) for c in result], [
                        ('a', 'x', 'parent-a-x'),
                          ('a', 'y', 'parent-a-y'),
                          ('b', 'x', 'parent-b-x')])

        for strategy in (PREFETCH_TYPE.WHERE, PREFETCH_TYPE.JOIN):
            self.reset_sql_history()
            with self.assertQueryCount(2):
                parents = TenantParent.select().order_by(
                    TenantParent.tenant, TenantParent.code)
                result = prefetch(parents, TenantChild.select(),
                                  prefetch_type=strategy)
                self.assertEqual([
                    (p.tenant, p.code,
                     [c.label for c in p.children]) for p in result], [
                        ('a', 'x', ['child-a-x']),
                        ('a', 'y', ['child-a-y']),
                        ('b', 'x', ['child-b-x'])])

    def test_with_related_forward_and_backref(self):
        query = (TenantChild
                 .select()
                 .order_by(TenantChild.tenant, TenantChild.code)
                 .with_related(Load(TenantChild.parent)))
        self.assertEqual([
            (c.tenant, c.code, c.parent.name) for c in query], [
                ('a', 'x', 'parent-a-x'),
                ('a', 'y', 'parent-a-y'),
                ('b', 'x', 'parent-b-x')])

        query = (TenantParent
                 .select()
                 .order_by(TenantParent.tenant, TenantParent.code)
                 .with_related(Load(TenantParent.children)))
        self.assertEqual([
            (p.tenant, p.code, len(p.children)) for p in query], [
                ('a', 'x', 1), ('a', 'y', 1), ('b', 'x', 1)])

    def test_non_primary_key_unique_target(self):
        UniqueTenant.create(tenant='a', code='x')
        UniqueTenant.create(tenant='b', code='x')
        UniqueChild.create(tenant='a', code='x')
        UniqueChild.create(tenant='b', code='x')
        children = prefetch(UniqueChild.select(), UniqueTenant.select())
        self.assertEqual([(c.tenant, c.code, c.target.id) for c in children], [
            ('a', 'x', 1), ('b', 'x', 2)])

    def test_self_reference(self):
        Node.create(tenant='a', code='root', parent_code=None)
        Node.create(tenant='b', code='root', parent_code=None)
        Node.create(tenant='a', code='leaf', parent_code='root')
        Node.create(tenant='b', code='leaf', parent_code='root')

        leaf = Node.get((Node.tenant == 'a') & (Node.code == 'leaf'))
        self.assertEqual((leaf.parent.tenant, leaf.parent.code),
                         ('a', 'root'))
        roots = prefetch(Node.select().where(Node.parent_code.is_null()),
                         Node.select().where(Node.parent_code.is_null(False)))
        for root in roots:
            for child in root.children:
                self.assertEqual(child.tenant, root.tenant)

    def test_foreign_key_constraint_enforced(self):
        with self.assertRaises(IntegrityError):
            TenantChild.create(tenant='a', code='missing', label='bad')
        self.assertEqual(
            TenantChild.select()
            .where((TenantChild.tenant == 'a') &
                   (TenantChild.code == 'missing'))
            .count(),
            0)

    def test_rollback_clears_unsaved_relationship_state(self):
        parent = TenantParent.get(
            (TenantParent.tenant == 'a') &
            (TenantParent.code == 'x'))
        child = TenantChild.create(tenant='a', code='x', label='good')
        self.assertEqual(child.parent, parent)
        child.code = 'missing'
        self.assertNotIn('parent', child.__rel__)

        with database.atomic():
            with self.assertRaises(IntegrityError):
                child.save()

        self.assertNotIn('parent', child.__rel__)
        self.assertEqual((child.tenant, child.code), ('a', 'missing'))
