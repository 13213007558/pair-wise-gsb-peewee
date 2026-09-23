"""
Nestable execution budgets for SQLite connections.

This module uses SQLite's progress-handler hook to temporarily cap how much
work a connection will perform inside a ``with`` block. A budget can be
expressed as a wall-clock deadline and/or as a limit on the number of SQLite
VM instructions executed. Both the stdlib ``sqlite3`` driver and APSW are
supported through the same interface.

Example::

    from playhouse.sqlite_budget import BudgetExceeded, budget

    try:
        with budget(db, max_instructions=10 ** 6):
            db.execute_sql(expensive_query)
    except BudgetExceeded as exc:
        print('query ran out of budget:', exc)

Budgets nest: when several ``budget()`` blocks are active on the same
connection, whichever limit is reached first aborts the query, and the
resulting :class:`BudgetExceeded` identifies the layer whose budget was
exhausted. When the outermost block exits, the progress handler previously
registered through :func:`set_progress_handler` (if any) is restored.
"""
import time

from peewee import Database
from peewee import NotSupportedError
from peewee import OperationalError
from peewee import sqlite3

try:
    import apsw
except ImportError:
    apsw = None


# Default number of VM instructions between progress-handler callbacks.
DEFAULT_INTERVAL = 1000


class BudgetExceeded(OperationalError):
    """
    Raised when a query is aborted because it exhausted the execution
    budget of a surrounding ``budget()`` block.

    Subclasses peewee's ``OperationalError`` so existing error handling
    keeps working, but can be caught specifically to distinguish a budget
    interruption from an ordinary SQL error. The ``frame`` attribute holds
    the budget layer that tripped and ``reason`` describes which limit was
    hit.
    """
    def __init__(self, frame, reason):
        self.frame = frame
        self.reason = reason
        super(BudgetExceeded, self).__init__(
            'execution budget exhausted in %s: %s' % (frame.label, reason))


class _Frame(object):
    __slots__ = ('label', 'deadline', 'max_instructions', 'interval',
                 'instructions')

    def __init__(self, label, deadline, max_instructions, interval):
        self.label = label
        self.deadline = deadline
        self.max_instructions = max_instructions
        self.interval = interval
        self.instructions = 0

    def tick(self, instructions):
        # Account for the (approximate) instructions executed since the
        # last callback and report why this layer is exhausted, if it is.
        self.instructions += instructions
        if self.max_instructions is not None and \
                self.instructions >= self.max_instructions:
            return 'VM instruction limit of %d exceeded' % \
                    self.max_instructions
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return 'deadline of %.3f (monotonic) exceeded' % self.deadline
        return None


class _ConnectionState(object):
    __slots__ = ('conn', 'driver', 'frames', 'dispatcher', 'interval',
                 'app_handler', 'app_interval', 'tripped')

    def __init__(self, conn, driver):
        self.conn = conn
        self.driver = driver
        self.frames = []
        self.dispatcher = None
        self.interval = None
        self.app_handler = None
        self.app_interval = DEFAULT_INTERVAL
        self.tripped = None


# Driver connections do not support weak references, so states are keyed by
# id() and hold a strong reference to the connection to keep the id valid.
_states = {}


def _get_state(conn, driver):
    key = id(conn)
    state = _states.get(key)
    if state is None or state.conn is not conn:
        state = _ConnectionState(conn, driver)
        _states[key] = state
    return state


def _resolve(db_or_conn):
    if isinstance(db_or_conn, Database):
        conn = db_or_conn.connection()
    else:
        conn = db_or_conn
    if sqlite3 is not None and isinstance(conn, sqlite3.Connection):
        return conn, 'sqlite3'
    if apsw is not None and isinstance(conn, apsw.Connection):
        return conn, 'apsw'
    raise NotSupportedError(
        'execution budgets rely on the SQLite progress-handler hook, which '
        'is only available on sqlite3 and APSW connections (got %s)' %
        type(conn).__name__)


def _set_handler(conn, driver, handler, interval):
    if driver == 'sqlite3':
        conn.set_progress_handler(handler, interval)
    else:
        conn.setprogresshandler(handler, interval)


def _make_dispatcher(state):
    def dispatch():
        # Every active budget layer is charged for the work performed;
        # whichever limit is reached first aborts the query.
        for frame in state.frames:
            reason = frame.tick(state.interval)
            if reason is not None:
                state.tripped = (frame, reason)
                return 1
        # Chain to the application's own handler, if one is registered.
        if state.app_handler is not None:
            return 1 if state.app_handler() else 0
        return 0
    return dispatch


def _sync_handler(state):
    if state.frames:
        state.interval = min(frame.interval for frame in state.frames)
        if state.dispatcher is None:
            state.dispatcher = _make_dispatcher(state)
        _set_handler(state.conn, state.driver, state.dispatcher,
                     state.interval)
    else:
        # Outermost budget exited: restore the application's handler (or
        # clear the hook entirely if none was registered).
        state.dispatcher = None
        state.interval = None
        _set_handler(state.conn, state.driver, state.app_handler,
                     state.app_interval)


_interrupt_errors = [OperationalError]
if sqlite3 is not None:
    _interrupt_errors.append(sqlite3.OperationalError)
if apsw is not None:
    _interrupt_errors.append(apsw.InterruptError)
_INTERRUPT_ERRORS = tuple(_interrupt_errors)


class budget(object):
    """
    Context manager applying a temporary execution budget to a connection.

    :param db_or_conn: a peewee ``Database`` (its current connection is
        used) or a raw sqlite3 / APSW connection.
    :param deadline: absolute deadline as a ``time.monotonic()`` timestamp;
        the query is aborted once the clock passes it.
    :param max_instructions: maximum number of SQLite VM instructions the
        query may execute.
    :param interval: number of VM instructions between budget checks.
    :param label: optional name for this layer, reported by
        :class:`BudgetExceeded` when this budget trips.

    At least one of ``deadline`` or ``max_instructions`` is required.
    Nested budgets compose: the earliest limit to be reached wins, and the
    exception identifies the layer that was exhausted. Abortion follows
    SQLite's own semantics -- the interrupted statement is rolled back and
    any enclosing transaction is left in whatever state SQLite keeps it in.
    """
    def __init__(self, db_or_conn, deadline=None, max_instructions=None,
                 interval=DEFAULT_INTERVAL, label=None):
        if deadline is None and max_instructions is None:
            raise ValueError('budget() requires a deadline and/or '
                             'max_instructions')
        if interval <= 0:
            raise ValueError('interval must be a positive number of VM '
                             'instructions')
        self._conn, self._driver = _resolve(db_or_conn)
        self._frame = _Frame(label, deadline, max_instructions, interval)
        self._state = None

    def __enter__(self):
        state = _get_state(self._conn, self._driver)
        if self._frame.label is None:
            self._frame.label = 'budget layer %d' % (len(state.frames) + 1)
        state.frames.append(self._frame)
        _sync_handler(state)
        self._state = state
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        state = self._state
        self._state = None
        state.frames.remove(self._frame)
        _sync_handler(state)
        tripped = state.tripped
        if tripped is not None:
            state.tripped = None
            frame, reason = tripped
            if exc_value is not None and \
                    isinstance(exc_value, _INTERRUPT_ERRORS):
                # The abort came from a budget, not from ordinary SQL:
                # translate the driver's generic "interrupted" error into
                # one the caller can tell apart.
                raise BudgetExceeded(frame, reason)


def set_progress_handler(db_or_conn, handler, interval=DEFAULT_INTERVAL):
    """
    Register the application's own progress handler on a connection.

    ``handler`` is a zero-argument callable returning non-zero to abort the
    current query, or ``None`` to remove the handler. Handlers registered
    here keep running while a ``budget()`` block is active (both get a say)
    and are restored once the outermost budget exits. Registering directly
    through the driver instead would be overwritten by ``budget()`` and
    cannot be restored.
    """
    conn, driver = _resolve(db_or_conn)
    state = _get_state(conn, driver)
    state.app_handler = handler
    state.app_interval = interval
    if not state.frames:
        _set_handler(conn, driver, handler, interval)
