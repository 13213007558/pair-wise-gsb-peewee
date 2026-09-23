"""
Execution budgets for SQLite connections.

This module provides a nestable context-manager, :class:`QueryBudget`, that
installs a temporary SQLite progress-handler on a Peewee connection. While
the budget is active, long-running queries can be interrupted when they
exceed a wall-clock deadline or a (virtual-machine) instruction limit. When
the context exits, the previous progress-handler -- including one registered
by the application itself -- is restored.

Both the standard-library ``sqlite3`` driver and APSW are supported through
a single interface. Drivers that do not expose a progress-handler API raise
:py:class:`NotSupportedError`.
"""
import time

from peewee import NotSupportedError
from peewee import OperationalError

__all__ = [
    'BudgetExceeded',
    'QueryBudget',
    'clear_progress_handler',
    'execution_budget',
    'set_progress_handler',
]

DEFAULT_GRANULARITY = 1000


class BudgetExceeded(OperationalError):
    """
    Raised when a query is aborted because an execution budget was
    exhausted. Subclasses :py:class:`OperationalError`, so existing
    handlers keep working, but callers can catch this class specifically
    to distinguish a budget interruption from an ordinary SQL error.

    The ``budget`` attribute holds the :class:`QueryBudget` layer whose
    limit was exhausted.
    """
    def __init__(self, budget):
        self.budget = budget
        super(BudgetExceeded, self).__init__(
            'query budget exhausted: %s' % budget.describe())


class _ConnectionState(object):
    def __init__(self, conn):
        self.conn = conn
        self.driver = _detect_driver(conn)
        self.app_handler = None
        self.app_granularity = DEFAULT_GRANULARITY
        self.budgets = []
        self.granularity = DEFAULT_GRANULARITY


_states = {}


def _detect_driver(conn):
    if hasattr(conn, 'set_progress_handler'):
        return 'stdlib'
    if hasattr(conn, 'setprogresshandler'):
        return 'apsw'
    raise NotSupportedError(
        'execution budgets require a SQLite driver that exposes a progress '
        'handler API (the standard-library sqlite3 module or APSW); %r does '
        'not provide this capability' % (type(conn).__name__,))


def _resolve_conn(db):
    if hasattr(db, 'connection') and callable(db.connection):
        return db.connection()
    return db


def _get_state(db, create=False):
    conn = _resolve_conn(db)
    key = id(conn)
    state = _states.get(key)
    if state is None and create:
        state = _states[key] = _ConnectionState(conn)
    return state


def _drop_state(state):
    if _states.get(id(state.conn)) is state:
        del _states[id(state.conn)]


def _make_handler(state):
    def progress_handler():
        for budget in state.budgets:
            if budget._tick(state):
                return 1
        if state.app_handler is not None:
            return 1 if state.app_handler() else 0
        return 0
    return progress_handler


def _install(state):
    granularities = [budget.granularity for budget in state.budgets]
    if state.app_handler is not None:
        granularities.append(state.app_granularity)
    state.granularity = (min(granularities) if granularities
                         else DEFAULT_GRANULARITY)
    handler = _make_handler(state)
    if state.driver == 'stdlib':
        state.conn.set_progress_handler(handler, state.granularity)
    else:
        state.conn.setprogresshandler(handler, state.granularity)


def _clear(state):
    if state.driver == 'stdlib':
        state.conn.set_progress_handler(None, 0)
    else:
        state.conn.setprogresshandler(None)


def _is_interrupt(exc):
    return 'interrupt' in str(exc).lower()


class QueryBudget(object):
    """
    Nestable context-manager that limits how much work a single query may
    do on the given Peewee database (or raw SQLite connection).

    :param db: a Peewee ``Database`` (e.g. ``SqliteDatabase`` or
        ``playhouse.apsw_ext.APSWDatabase``) or a raw SQLite connection.
    :param max_instructions: maximum number of SQLite virtual-machine
        instructions (approximate) a query may execute before it is
        aborted.
    :param deadline: absolute deadline, expressed in ``time.monotonic()``
        seconds, after which queries are aborted.
    :param timeout: convenience alternative to ``deadline``; maximum number
        of seconds, measured from entering the context, before queries are
        aborted.
    :param name: optional label used to identify this layer in the
        :class:`BudgetExceeded` exception.
    :param granularity: number of VM instructions between progress-handler
        callbacks (smaller is more precise but slower).

    When budgets are nested, whichever limit is reached first aborts the
    query, and the raised :class:`BudgetExceeded` identifies the layer
    whose budget was exhausted.
    """
    def __init__(self, db, max_instructions=None, deadline=None, timeout=None,
                 name=None, granularity=None):
        if max_instructions is None and deadline is None and timeout is None:
            raise ValueError('specify max_instructions, deadline or timeout')
        if deadline is not None and timeout is not None:
            raise ValueError('specify either deadline or timeout, not both')
        self.db = db
        self.max_instructions = max_instructions
        self.deadline = deadline
        self.timeout = timeout
        self.name = name
        self.granularity = granularity or DEFAULT_GRANULARITY
        self.exhausted = False
        self._ticks = 0
        self._state = None

    def describe(self):
        parts = []
        if self.name:
            parts.append('layer %r' % (self.name,))
        if self.max_instructions is not None:
            parts.append('max_instructions=%s' % self.max_instructions)
        if self.deadline is not None or self.timeout is not None:
            parts.append('deadline budget')
        return ', '.join(parts) or 'unnamed budget'

    def _tick(self, state):
        if self.exhausted:
            return True
        self._ticks += 1
        if self.max_instructions is not None:
            if self._ticks * state.granularity >= self.max_instructions:
                self.exhausted = True
        if not self.exhausted and self.deadline is not None:
            if time.monotonic() >= self.deadline:
                self.exhausted = True
        return self.exhausted

    def __enter__(self):
        if self.timeout is not None:
            self.deadline = time.monotonic() + self.timeout
        self.exhausted = False
        self._ticks = 0
        self._state = _get_state(self.db, create=True)
        self._state.budgets.append(self)
        _install(self._state)
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        state = self._state
        self._state = None
        if state is None:
            return False
        if self in state.budgets:
            state.budgets.remove(self)
        tripped = [budget for budget in state.budgets if budget.exhausted]
        if self.exhausted:
            tripped.append(self)
        if state.budgets or state.app_handler is not None:
            _install(state)
        else:
            _clear(state)
            _drop_state(state)
        if (exc_value is not None and tripped
                and not isinstance(exc_value, BudgetExceeded)
                and _is_interrupt(exc_value)):
            raise BudgetExceeded(tripped[0]) from exc_value
        return False


def execution_budget(db, **kwargs):
    """
    Return a :class:`QueryBudget` context-manager for the given database.
    """
    return QueryBudget(db, **kwargs)


def set_progress_handler(db, handler, granularity=DEFAULT_GRANULARITY):
    """
    Register an application progress-handler on the given database (or raw
    connection). Handlers registered through this helper remain active
    while a :class:`QueryBudget` is in effect and are restored when the
    budget exits. The handler is called with no arguments and should
    return a truthy value to abort the current query.
    """
    state = _get_state(db, create=True)
    state.app_handler = handler
    state.app_granularity = granularity
    _install(state)


def clear_progress_handler(db):
    """
    Remove an application progress-handler previously registered with
    :func:`set_progress_handler`. Active budgets are left in place.
    """
    state = _get_state(db)
    if state is None:
        return
    state.app_handler = None
    if state.budgets:
        _install(state)
    else:
        _clear(state)
        _drop_state(state)
