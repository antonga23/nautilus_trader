# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Append-only double-entry ledger for the bonus-EV arbitrage platform.

Bets are placed by hand at a South African sportsbook and at a crypto betting
exchange, then recorded here afterwards. The ledger is therefore the only durable
record of what happened, and it is built so that every rand can be traced from the
bank account through the venue, the crypto wallet and the exchange to a settled
bet, from the stored rows alone.

Three invariants carry that guarantee:

* **Balanced or rejected.** Every entry's postings must sum to exactly zero in the
  base currency. An entry that does not is rejected with
  :class:`UnbalancedEntry`, and header and lines are written inside a single
  transaction, so a rejected entry leaves nothing behind.
* **Append-only.** A posted entry is never updated or deleted -- database triggers
  refuse both. A correction is a new entry that reverses the original and links to
  it through ``journal_entry.reversal_of``.
* **Cost basis is recorded, not recomputed.** Crypto disposals consume lots FIFO by
  default; each consumption row stores the lot, the quantity, the method used and
  the realized gain, so a later change of policy cannot silently restate history.

Crypto asset accounts are carried at cost basis in the base currency. Value only
moves to ``PnL:FX`` when an asset actually leaves the book (a disposal), which is
what makes the realized gain on each lot reproducible from the stored rows. Moving
crypto between two owned accounts is not a disposal and produces no P&L; a network
fee is, because the fee leaves the book.

One consequence to know about: exchange-denominated bets are priced at the current
average cost of the asset and do not open new lots, so USDC net won on an exchange
is carried at the cost of the USDC already held rather than at its market value on
the day it was won. Lots are opened by purchases only.

Amounts are persisted as TEXT holding the exact decimal string. SQLite's numeric
types would coerce them to IEEE floats, so every aggregation happens in Python over
:class:`~decimal.Decimal` instead of in SQL.

"""

from __future__ import annotations

import contextlib
import enum
import sqlite3
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Final
from typing import NoReturn
from typing import Protocol

from tools.bonusev.core.money import DEFAULT_BASE_CURRENCY
from tools.bonusev.core.money import ONE
from tools.bonusev.core.money import ZAR
from tools.bonusev.core.money import ZERO
from tools.bonusev.core.money import FxQuote
from tools.bonusev.core.money import apply_rate
from tools.bonusev.core.money import dec
from tools.bonusev.core.money import implied_rate
from tools.bonusev.core.money import quantize
from tools.bonusev.core.money import quantize_rate
from tools.bonusev.core.money import total


# datetime.UTC is a 3.11+ symbol and this package also runs on the 3.10 deploy
# host, so fall back to the equivalent timezone.utc there. (ruff UP017 targets
# py3.12 and would rewrite the fallback back to datetime.UTC.)
try:
    from datetime import UTC
except ImportError:  # pragma: no cover - Python 3.10 deploy host
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017


class LedgerError(Exception):
    """
    Base class for every ledger-domain failure raised by this module.
    """


class UnbalancedEntry(LedgerError):
    """
    Raised when an entry's postings do not sum to zero in the base currency.
    """


class UnknownAccount(LedgerError):
    """
    Raised when a posting references an account code that is not in the chart.
    """


class InsufficientLots(LedgerError):
    """
    Raised when a crypto disposal exceeds the quantity remaining across lots.
    """


class AppendOnlyViolation(LedgerError):
    """
    Raised when a caller tries to change a posted entry in place.
    """


class EntryAlreadyReversed(LedgerError):
    """
    Raised when an entry that already carries a reversal is reversed again.
    """


class AccountType(str, enum.Enum):
    """
    The five classical account types.
    """

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


class EntryKind(str, enum.Enum):
    """
    What real-world action an entry records.
    """

    OPENING_BALANCE = "opening_balance"
    FIAT_DEPOSIT = "fiat_deposit"
    CRYPTO_PURCHASE = "crypto_purchase"
    CRYPTO_TRANSFER = "crypto_transfer"
    BET_PLACED = "bet_placed"
    BET_SETTLED = "bet_settled"
    BONUS_CREDITED = "bonus_credited"
    REVERSAL = "reversal"


class CostBasisMethod(str, enum.Enum):
    """
    Lot selection policy for a crypto disposal.
    """

    FIFO = "fifo"
    LIFO = "lifo"


class BetOutcome(str, enum.Enum):
    """
    How a recorded bet resolved, including Asian-handicap half outcomes.
    """

    WON = "won"
    LOST = "lost"
    VOID = "void"
    HALF_WON = "half_won"
    HALF_LOST = "half_lost"


class BetFunding(str, enum.Enum):
    """
    Which venue balance a bet was funded from.

    Bonus funds are tracked apart from cash because they are not withdrawable until the
    wagering requirement clears, so a book balance that mixes the two overstates what
    can actually be taken out.

    """

    CASH = "cash"
    BONUS = "bonus"
    EXCHANGE = "exchange"


PNL_BETTING: Final = "PnL:Betting"
PNL_FX: Final = "PnL:FX"
EXPENSE_FEES: Final = "Expense:Fees"
EQUITY_OPENING: Final = "Equity:Opening"

# The globally seeded part of the chart. Bank, venue and wallet accounts are named
# after real-world counterparties and are created on demand by the helpers.
GLOBAL_CHART: Final[tuple[tuple[str, str, AccountType, str], ...]] = (
    (PNL_BETTING, "Betting P&L", AccountType.INCOME, ZAR),
    (PNL_FX, "FX and crypto disposal P&L", AccountType.INCOME, ZAR),
    (EXPENSE_FEES, "Fees and charges", AccountType.EXPENSE, ZAR),
    (EQUITY_OPENING, "Opening balances", AccountType.EQUITY, ZAR),
)


def bank_code(name: str) -> str:
    """
    Return the chart code for a ZAR bank account.

    >>> bank_code("FNB")
    'Bank:FNB:ZAR'

    """
    return f"Bank:{name}:{ZAR}"


def venue_cash_code(venue: str) -> str:
    """
    Return the chart code for a sportsbook's withdrawable ZAR cash balance.

    >>> venue_cash_code("Betway")
    'Venue:Betway:Cash:ZAR'

    """
    return f"Venue:{venue}:Cash:{ZAR}"


def venue_bonus_code(venue: str) -> str:
    """
    Return the chart code for a sportsbook's non-withdrawable bonus balance.

    >>> venue_bonus_code("Betway")
    'Venue:Betway:BonusFunds:ZAR'

    """
    return f"Venue:{venue}:BonusFunds:{ZAR}"


def exchange_code(venue: str, asset: str = "USDC") -> str:
    """
    Return the chart code for a betting exchange's crypto balance.

    >>> exchange_code("SXbet")
    'Exchange:SXbet:USDC'

    """
    return f"Exchange:{venue}:{asset}"


def wallet_code(name: str, asset: str) -> str:
    """
    Return the chart code for a self-custody crypto wallet balance.

    >>> wallet_code("Ledger", "USDC")
    'Wallet:Ledger:USDC'

    """
    return f"Wallet:{name}:{asset}"


DDL_LEDGER_ACCOUNT: Final = """
CREATE TABLE IF NOT EXISTS ledger_account (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('asset', 'liability', 'equity', 'income', 'expense')),
    currency TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
)
"""

DDL_JOURNAL_ENTRY: Final = """
CREATE TABLE IF NOT EXISTS journal_entry (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    external_ref TEXT,
    created_by TEXT NOT NULL,
    reversal_of INTEGER REFERENCES journal_entry (id)
)
"""

DDL_POSTING: Final = """
CREATE TABLE IF NOT EXISTS posting (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES journal_entry (id),
    ledger_account_id INTEGER NOT NULL REFERENCES ledger_account (id),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    fx_rate_to_base TEXT NOT NULL,
    base_amount TEXT NOT NULL
)
"""

DDL_FX_RATE: Final = """
CREATE TABLE IF NOT EXISTS fx_rate (
    id INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    base_ccy TEXT NOT NULL,
    quote_ccy TEXT NOT NULL,
    rate TEXT NOT NULL,
    source TEXT NOT NULL
)
"""

DDL_CRYPTO_LOT: Final = """
CREATE TABLE IF NOT EXISTS crypto_lot (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    qty TEXT NOT NULL,
    unit_cost_base TEXT NOT NULL,
    remaining_qty TEXT NOT NULL,
    source_entry_id INTEGER NOT NULL REFERENCES journal_entry (id)
)
"""

# `method` is stored per consumption rather than read from configuration, so a
# later switch from FIFO to LIFO cannot retroactively restate a closed disposal.
DDL_LOT_CONSUMPTION: Final = """
CREATE TABLE IF NOT EXISTS lot_consumption (
    id INTEGER PRIMARY KEY,
    lot_id INTEGER NOT NULL REFERENCES crypto_lot (id),
    disposal_entry_id INTEGER NOT NULL REFERENCES journal_entry (id),
    qty TEXT NOT NULL,
    method TEXT NOT NULL,
    realized_gain_base TEXT NOT NULL
)
"""

# Append-only is enforced in the database, not just in this module, so that a
# stray UPDATE from a console or another service cannot rewrite history either.
DDL_APPEND_ONLY_TRIGGERS: Final[tuple[str, ...]] = tuple(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
BEFORE {operation} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only; post a reversing entry instead');
END
"""
    for table in ("journal_entry", "posting")
    for operation in ("UPDATE", "DELETE")
)

DDL_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_posting_entry ON posting (entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_posting_account ON posting (ledger_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_entry_tenant_ts ON journal_entry (tenant_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_lot_asset ON crypto_lot (tenant_id, asset, acquired_at, id)",
)

SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    DDL_LEDGER_ACCOUNT,
    DDL_JOURNAL_ENTRY,
    DDL_POSTING,
    DDL_FX_RATE,
    DDL_CRYPTO_LOT,
    DDL_LOT_CONSUMPTION,
    *DDL_INDEXES,
    *DDL_APPEND_ONLY_TRIGGERS,
)


class Cursor(Protocol):
    """
    The DB-API cursor surface this module relies on.
    """

    @property
    def lastrowid(self) -> int | None: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


class Connection(Protocol):
    """
    The DB-API connection surface this module relies on.
    """

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> Cursor: ...

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]], /) -> Cursor: ...


class Store(Protocol):
    """
    A connection provider exposing an all-or-nothing transaction scope.

    Satisfied by the platform's shared ``core.db`` store and by
    :class:`SqliteStore` below, which is what keeps this module testable against a
    plain :mod:`sqlite3` connection.

    """

    def transaction(self) -> AbstractContextManager[Connection]: ...


class SqliteStore:
    """
    Minimal :class:`Store` over a plain :mod:`sqlite3` connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        # Autocommit mode hands transaction control to us, so BEGIN/COMMIT below
        # bracket exactly one entry rather than whatever sqlite3 decides to imply.
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """
        Return the underlying connection, for schema inspection in tests.
        """
        return self._conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Connection]:
        """
        Run a block inside one transaction, rolling back on any exception.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")


@dataclass(frozen=True)
class PostingInput:
    """
    One line of an entry, in its native currency plus the rate used.

    ``base_amount`` is normally derived as ``amount * fx_rate_to_base``. It is only
    passed explicitly when the base value comes from cost basis (a FIFO lot
    consumption), where the exact lot arithmetic -- not the rate -- is
    authoritative and the rate is recorded as the implied one.

    """

    account_code: str
    amount: Decimal
    currency: str = ZAR
    fx_rate_to_base: Decimal = ONE
    base_amount: Decimal | None = None


@dataclass(frozen=True)
class AccountBalance:
    """
    An account's native and base-currency balance at a point in time.
    """

    code: str
    type: AccountType
    currency: str
    amount: Decimal
    base_amount: Decimal


@dataclass(frozen=True)
class TrialBalance:
    """
    Every account's balance for a tenant, which must sum to zero in base.
    """

    tenant_id: str
    as_of: datetime | None
    base_currency: str
    rows: tuple[AccountBalance, ...]
    total_base: Decimal

    @property
    def is_balanced(self) -> bool:
        """
        Return whether the book balances, which it always should.
        """
        return self.total_base == ZERO


@dataclass(frozen=True)
class StatementLine:
    """
    One posting against an account, with the running base balance after it.
    """

    entry_id: int
    ts_utc: str
    kind: str
    description: str
    external_ref: str | None
    amount: Decimal
    currency: str
    fx_rate_to_base: Decimal
    base_amount: Decimal
    running_base: Decimal


@dataclass(frozen=True)
class LotSlice:
    """
    The part of one crypto lot that a disposal consumes.
    """

    lot_id: int
    qty: Decimal
    unit_cost_base: Decimal
    cost_base: Decimal
    remaining_after: Decimal


@dataclass(frozen=True)
class DisposalPlan:
    """
    The lots a pending disposal will consume, priced before anything is written.
    """

    asset: str
    qty: Decimal
    method: CostBasisMethod
    slices: tuple[LotSlice, ...] = field(default_factory=tuple)
    cost_base: Decimal = ZERO


# returned = stake * stake_fraction + gross_return * gross_fraction. Void refunds the
# stake and so nets zero; the Asian-handicap halves split the stake between a refund
# and a settled half.
_RETURN_FRACTIONS: Final[dict[BetOutcome, tuple[Decimal, Decimal]]] = {
    BetOutcome.WON: (ZERO, ONE),
    BetOutcome.LOST: (ZERO, ZERO),
    BetOutcome.VOID: (ONE, ZERO),
    BetOutcome.HALF_WON: (Decimal("0.5"), Decimal("0.5")),
    BetOutcome.HALF_LOST: (Decimal("0.5"), ZERO),
}

_SELECT_LOTS: Final = (
    "SELECT id, remaining_qty, unit_cost_base FROM crypto_lot "
    "WHERE tenant_id = ? AND asset = ? ORDER BY acquired_at "
)

# SQLite cannot parameterise ORDER BY, so each method gets its own fixed query
# rather than a direction interpolated into the SQL at call time.
_LOT_QUERIES: Final[dict[CostBasisMethod, str]] = {
    CostBasisMethod.FIFO: _SELECT_LOTS + "ASC, id ASC",
    CostBasisMethod.LIFO: _SELECT_LOTS + "DESC, id DESC",
}

_SELECT_POSTINGS: Final = """
SELECT a.code, a.type, a.currency, p.amount, p.base_amount,
       p.fx_rate_to_base, j.id, j.ts_utc, j.kind, j.description, j.external_ref
FROM posting p
JOIN journal_entry j ON j.id = p.entry_id
JOIN ledger_account a ON a.id = p.ledger_account_id
WHERE a.tenant_id = ?
"""


def utc_now() -> datetime:
    """
    Return the current time as a timezone-aware UTC datetime.
    """
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    """
    Render ``ts`` as a UTC ISO-8601 string that sorts chronologically as text.
    """
    if ts.tzinfo is None:
        raise LedgerError("timestamps must be timezone-aware")

    return ts.astimezone(UTC).isoformat()


def _pro_rata(amount: Decimal, part: Decimal, whole: Decimal, currency: str) -> Decimal:
    """
    Return the ``part / whole`` share of ``amount``, rounded to ``currency``.
    """
    return apply_rate(amount, implied_rate(part, whole), currency)


class Ledger:
    """
    Tenant-scoped double-entry ledger over a :class:`Store`.

    All amounts are :class:`~decimal.Decimal`. Debits are positive and credits are
    negative, so an entry balances when its base amounts sum to exactly zero.

    """

    def __init__(
        self,
        store: Store,
        tenant_id: str,
        base_currency: str = DEFAULT_BASE_CURRENCY,
        created_by: str = "system",
        cost_basis_method: CostBasisMethod = CostBasisMethod.FIFO,
    ) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.base_currency = base_currency
        self.created_by = created_by
        self.cost_basis_method = cost_basis_method

    # -- schema and chart ------------------------------------------------------

    def ensure_schema(self) -> None:
        """
        Create the ledger tables, indexes and append-only triggers if absent.
        """
        with self.store.transaction() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

    def seed_chart_of_accounts(self) -> None:
        """
        Create the P&L, fee and opening-equity accounts shared by every venue.
        """
        with self.store.transaction() as conn:
            for code, name, account_type, currency in GLOBAL_CHART:
                self._ensure_account(conn, code, name, account_type, currency)

    def ensure_account(
        self,
        code: str,
        name: str,
        account_type: AccountType,
        currency: str,
    ) -> int:
        """
        Create the account if it is missing and return its id.
        """
        with self.store.transaction() as conn:
            return self._ensure_account(conn, code, name, account_type, currency)

    def _ensure_account(
        self,
        conn: Connection,
        code: str,
        name: str,
        account_type: AccountType,
        currency: str,
    ) -> int:
        conn.execute(
            """
            INSERT INTO ledger_account (tenant_id, code, name, type, currency, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """,
            (self.tenant_id, code, name, account_type.value, currency),
        )

        return self._account_id(conn, code)

    def _account_id(self, conn: Connection, code: str) -> int:
        row = conn.execute(
            "SELECT id FROM ledger_account WHERE tenant_id = ? AND code = ?",
            (self.tenant_id, code),
        ).fetchone()
        if row is None:
            raise UnknownAccount(f"no account {code!r} for tenant {self.tenant_id!r}")

        return int(row[0])

    # -- posting ---------------------------------------------------------------

    def post_entry(
        self,
        kind: EntryKind,
        postings: Sequence[PostingInput],
        ts_utc: datetime | None = None,
        description: str = "",
        external_ref: str | None = None,
        created_by: str | None = None,
        reversal_of: int | None = None,
    ) -> int:
        """
        Post one balanced entry and return its id.

        Raises :class:`UnbalancedEntry` if the base amounts do not sum to zero. The
        header and its lines are written inside a single transaction, so a rejected
        entry leaves no rows behind.

        """
        with self.store.transaction() as conn:
            return self._post(
                conn,
                kind=kind,
                postings=postings,
                ts_utc=ts_utc,
                description=description,
                external_ref=external_ref,
                created_by=created_by,
                reversal_of=reversal_of,
            )

    def _post(
        self,
        conn: Connection,
        kind: EntryKind,
        postings: Sequence[PostingInput],
        ts_utc: datetime | None = None,
        description: str = "",
        external_ref: str | None = None,
        created_by: str | None = None,
        reversal_of: int | None = None,
    ) -> int:
        if not postings:
            raise UnbalancedEntry("an entry must have at least one posting")

        stamp = _iso(ts_utc or utc_now())
        cursor = conn.execute(
            """
            INSERT INTO journal_entry
                (tenant_id, ts_utc, kind, description, external_ref, created_by, reversal_of)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.tenant_id,
                stamp,
                kind.value,
                description,
                external_ref,
                created_by or self.created_by,
                reversal_of,
            ),
        )
        entry_id = cursor.lastrowid
        if entry_id is None:
            raise LedgerError("store did not report an id for the new journal entry")

        rows = [self._posting_row(conn, entry_id, posting) for posting in postings]
        conn.executemany(
            """
            INSERT INTO posting
                (entry_id, ledger_account_id, amount, currency, fx_rate_to_base, base_amount)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        # Validated after the writes so the failure path exercises the real
        # rollback rather than a pre-check that never touched the database.
        residual = total(dec(str(row[5])) for row in rows)
        if residual != ZERO:
            raise UnbalancedEntry(
                f"{kind.value} entry postings sum to {residual} {self.base_currency}, not zero",
            )

        return entry_id

    def _posting_row(
        self,
        conn: Connection,
        entry_id: int,
        posting: PostingInput,
    ) -> tuple[Any, ...]:
        base = posting.base_amount
        if base is None:
            base = apply_rate(posting.amount, posting.fx_rate_to_base, self.base_currency)

        return (
            entry_id,
            self._account_id(conn, posting.account_code),
            str(quantize(posting.amount, posting.currency)),
            posting.currency,
            str(quantize_rate(posting.fx_rate_to_base)),
            str(quantize(base, self.base_currency)),
        )

    def edit_entry(self, entry_id: int, **_changes: object) -> NoReturn:
        """
        Refuse an in-place edit of a posted entry.

        Present so that a caller reaching for an update finds a typed refusal here
        rather than writing raw SQL against the append-only tables.

        """
        raise AppendOnlyViolation(
            f"entry {entry_id} is posted and immutable; correct it with reverse_entry()",
        )

    def reverse_entry(
        self,
        entry_id: int,
        ts_utc: datetime | None = None,
        description: str = "",
        created_by: str | None = None,
    ) -> int:
        """
        Post the sign-flipped mirror of ``entry_id`` and return the new entry id.

        This is the only correction mechanism: the original entry stays exactly as
        posted and the reversal links back to it through ``reversal_of``.

        """
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM journal_entry WHERE reversal_of = ? AND tenant_id = ?",
                (entry_id, self.tenant_id),
            ).fetchone()
            if existing is not None:
                raise EntryAlreadyReversed(
                    f"entry {entry_id} was already reversed by {existing[0]}"
                )

            original = conn.execute(
                "SELECT kind, description, external_ref FROM journal_entry "
                "WHERE id = ? AND tenant_id = ?",
                (entry_id, self.tenant_id),
            ).fetchone()
            if original is None:
                raise LedgerError(f"no entry {entry_id} for tenant {self.tenant_id!r}")

            postings = self._mirror_postings(conn, entry_id)

            return self._post(
                conn,
                kind=EntryKind.REVERSAL,
                postings=postings,
                ts_utc=ts_utc,
                description=description or f"reversal of entry {entry_id}: {original[1]}",
                external_ref=original[2],
                created_by=created_by,
                reversal_of=entry_id,
            )

    def _mirror_postings(self, conn: Connection, entry_id: int) -> list[PostingInput]:
        rows = conn.execute(
            """
            SELECT a.code, p.amount, p.currency, p.fx_rate_to_base, p.base_amount
            FROM posting p
            JOIN ledger_account a ON a.id = p.ledger_account_id
            WHERE p.entry_id = ?
            ORDER BY p.id
            """,
            (entry_id,),
        ).fetchall()

        return [
            PostingInput(
                account_code=str(row[0]),
                amount=-dec(str(row[1])),
                currency=str(row[2]),
                fx_rate_to_base=dec(str(row[3])),
                base_amount=-dec(str(row[4])),
            )
            for row in rows
        ]

    # -- queries ---------------------------------------------------------------

    def balance(self, account: str, as_of: datetime | None = None) -> AccountBalance:
        """
        Return the balance of ``account``, optionally as at ``as_of``.
        """
        with self.store.transaction() as conn:
            account_row = conn.execute(
                "SELECT type, currency FROM ledger_account WHERE tenant_id = ? AND code = ?",
                (self.tenant_id, account),
            ).fetchone()
            if account_row is None:
                raise UnknownAccount(f"no account {account!r} for tenant {self.tenant_id!r}")

            rows = self._posting_rows(conn, as_of=as_of, account=account)

        return AccountBalance(
            code=account,
            type=AccountType(account_row[0]),
            currency=str(account_row[1]),
            amount=total(dec(str(row[3])) for row in rows),
            base_amount=total(dec(str(row[4])) for row in rows),
        )

    def trial_balance(
        self,
        tenant: str | None = None,
        as_of: datetime | None = None,
    ) -> TrialBalance:
        """
        Return every account's balance for a tenant; the base total must be zero.
        """
        tenant_id = tenant or self.tenant_id
        with self.store.transaction() as conn:
            accounts = conn.execute(
                "SELECT code, type, currency FROM ledger_account WHERE tenant_id = ? ORDER BY code",
                (tenant_id,),
            ).fetchall()
            rows = self._posting_rows(conn, as_of=as_of, tenant_id=tenant_id)

        native: dict[str, list[Decimal]] = {str(a[0]): [] for a in accounts}
        base: dict[str, list[Decimal]] = {str(a[0]): [] for a in accounts}
        for row in rows:
            native[str(row[0])].append(dec(str(row[3])))
            base[str(row[0])].append(dec(str(row[4])))

        balances = tuple(
            AccountBalance(
                code=str(a[0]),
                type=AccountType(a[1]),
                currency=str(a[2]),
                amount=total(native[str(a[0])]),
                base_amount=total(base[str(a[0])]),
            )
            for a in accounts
        )

        return TrialBalance(
            tenant_id=tenant_id,
            as_of=as_of,
            base_currency=self.base_currency,
            rows=balances,
            total_base=total(b.base_amount for b in balances),
        )

    def statement(self, account: str, as_of: datetime | None = None) -> list[StatementLine]:
        """
        Return every posting against ``account`` with a running base balance.
        """
        with self.store.transaction() as conn:
            rows = self._posting_rows(conn, as_of=as_of, account=account)

        lines: list[StatementLine] = []
        running = ZERO
        for row in rows:
            base_amount = dec(str(row[4]))
            running = running + base_amount
            lines.append(
                StatementLine(
                    entry_id=int(row[6]),
                    ts_utc=str(row[7]),
                    kind=str(row[8]),
                    description=str(row[9]),
                    external_ref=None if row[10] is None else str(row[10]),
                    amount=dec(str(row[3])),
                    currency=str(row[2]),
                    fx_rate_to_base=dec(str(row[5])),
                    base_amount=base_amount,
                    running_base=running,
                ),
            )

        return lines

    def _posting_rows(
        self,
        conn: Connection,
        as_of: datetime | None = None,
        account: str | None = None,
        tenant_id: str | None = None,
    ) -> list[Any]:
        sql = _SELECT_POSTINGS
        params: list[Any] = [tenant_id or self.tenant_id]
        if account is not None:
            sql += " AND a.code = ?"
            params.append(account)
        if as_of is not None:
            sql += " AND j.ts_utc <= ?"
            params.append(_iso(as_of))

        return conn.execute(sql + " ORDER BY j.ts_utc, j.id, p.id", params).fetchall()

    # -- crypto cost basis -----------------------------------------------------

    def _plan_disposal(self, conn: Connection, asset: str, qty: Decimal) -> DisposalPlan:
        method = self.cost_basis_method
        rows = conn.execute(_LOT_QUERIES[method], (self.tenant_id, asset)).fetchall()

        outstanding = qty
        slices: list[LotSlice] = []
        for row in rows:
            remaining = dec(str(row[1]))
            if outstanding <= ZERO or remaining <= ZERO:
                continue
            take = min(remaining, outstanding)
            unit_cost = dec(str(row[2]))
            slices.append(
                LotSlice(
                    lot_id=int(row[0]),
                    qty=take,
                    unit_cost_base=unit_cost,
                    cost_base=apply_rate(take, unit_cost, self.base_currency),
                    remaining_after=remaining - take,
                ),
            )
            outstanding -= take

        if outstanding > ZERO:
            raise InsufficientLots(f"disposal of {qty} {asset} exceeds lots by {outstanding}")

        return DisposalPlan(
            asset=asset,
            qty=qty,
            method=method,
            slices=tuple(slices),
            cost_base=total(s.cost_base for s in slices),
        )

    def _apply_disposal(
        self,
        conn: Connection,
        plan: DisposalPlan,
        entry_id: int,
        proceeds_base: Decimal,
    ) -> None:
        """
        Draw down the planned lots and record the realized gain on each slice.
        """
        allocated = ZERO
        last = len(plan.slices) - 1
        for index, lot_slice in enumerate(plan.slices):
            # Proceeds split across the consumed lots by quantity, with the final
            # slice absorbing the rounding residual so the per-slice gains add back
            # to the entry's total gain exactly.
            if index == last:
                share = proceeds_base - allocated
            else:
                share = _pro_rata(proceeds_base, lot_slice.qty, plan.qty, self.base_currency)
            allocated += share

            conn.execute(
                "UPDATE crypto_lot SET remaining_qty = ? WHERE id = ?",
                (str(lot_slice.remaining_after), lot_slice.lot_id),
            )
            conn.execute(
                """
                INSERT INTO lot_consumption
                    (lot_id, disposal_entry_id, qty, method, realized_gain_base)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    lot_slice.lot_id,
                    entry_id,
                    str(lot_slice.qty),
                    plan.method.value,
                    str(share - lot_slice.cost_base),
                ),
            )

    def average_cost_rate(self, asset: str) -> Decimal:
        """
        Return the base-currency cost per unit across the remaining lots.
        """
        with self.store.transaction() as conn:
            return self._average_cost_rate(conn, asset)

    def _average_cost_rate(self, conn: Connection, asset: str) -> Decimal:
        rows = conn.execute(
            "SELECT remaining_qty, unit_cost_base FROM crypto_lot "
            "WHERE tenant_id = ? AND asset = ?",
            (self.tenant_id, asset),
        ).fetchall()

        qty = total(dec(str(row[0])) for row in rows)
        if qty <= ZERO:
            raise InsufficientLots(f"no remaining {asset} lots to price a transfer against")

        cost = total(apply_rate(dec(str(r[0])), dec(str(r[1])), self.base_currency) for r in rows)

        return implied_rate(cost, qty)

    def lots(self, asset: str) -> list[tuple[int, Decimal, Decimal, Decimal]]:
        """
        Return ``(id, qty, unit_cost_base, remaining_qty)`` for an asset's lots.
        """
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT id, qty, unit_cost_base, remaining_qty FROM crypto_lot "
                "WHERE tenant_id = ? AND asset = ? ORDER BY acquired_at, id",
                (self.tenant_id, asset),
            ).fetchall()

        return [(int(r[0]), dec(str(r[1])), dec(str(r[2])), dec(str(r[3]))) for r in rows]

    def realized_fx_pnl(self, entry_id: int) -> Decimal:
        """
        Return the realized gain recorded against a disposal entry's lot slices.
        """
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT realized_gain_base FROM lot_consumption WHERE disposal_entry_id = ?",
                (entry_id,),
            ).fetchall()

        return total(dec(str(row[0])) for row in rows)

    def _record_fx(self, conn: Connection, quote: FxQuote) -> None:
        conn.execute(
            "INSERT INTO fx_rate (ts_utc, base_ccy, quote_ccy, rate, source) VALUES (?, ?, ?, ?, ?)",
            (
                _iso(quote.ts_utc),
                quote.base_ccy,
                quote.quote_ccy,
                str(quantize_rate(quote.rate)),
                quote.source,
            ),
        )

    # -- high-level posting helpers --------------------------------------------

    def record_opening_balance(
        self,
        account: str,
        amount: Decimal,
        ts_utc: datetime | None = None,
        fx_rate_to_base: Decimal = ONE,
        currency: str = ZAR,
        description: str = "opening balance",
    ) -> int:
        """
        Open ``account`` at ``amount`` against ``Equity:Opening`` and return the id.
        """
        base = apply_rate(amount, fx_rate_to_base, self.base_currency)
        postings = [
            PostingInput(account, amount, currency, fx_rate_to_base),
            PostingInput(EQUITY_OPENING, -base, self.base_currency),
        ]

        return self.post_entry(
            EntryKind.OPENING_BALANCE,
            postings,
            ts_utc=ts_utc,
            description=description,
        )

    def record_fiat_deposit(
        self,
        bank: str,
        venue: str,
        amount: Decimal,
        ts_utc: datetime | None = None,
        fee: Decimal = ZERO,
        external_ref: str | None = None,
    ) -> int:
        """
        Move ZAR from a bank account into a sportsbook's cash balance.
        """
        amount = quantize(amount, ZAR)
        fee = quantize(fee, ZAR)
        with self.store.transaction() as conn:
            self._ensure_account(
                conn, bank_code(bank), f"{bank} bank account", AccountType.ASSET, ZAR
            )
            self._ensure_account(
                conn,
                venue_cash_code(venue),
                f"{venue} cash balance",
                AccountType.ASSET,
                ZAR,
            )
            postings = [
                PostingInput(venue_cash_code(venue), amount),
                PostingInput(EXPENSE_FEES, fee),
                PostingInput(bank_code(bank), -(amount + fee)),
            ]

            return self._post(
                conn,
                kind=EntryKind.FIAT_DEPOSIT,
                postings=[p for p in postings if p.amount != ZERO],
                ts_utc=ts_utc,
                description=f"deposit {amount} {ZAR} from {bank} to {venue}",
                external_ref=external_ref,
            )

    def record_bonus_credited(
        self,
        venue: str,
        amount: Decimal,
        ts_utc: datetime | None = None,
        external_ref: str | None = None,
    ) -> int:
        """
        Credit a sportsbook bonus to its non-withdrawable bonus-funds account.
        """
        amount = quantize(amount, ZAR)
        with self.store.transaction() as conn:
            self._ensure_account(
                conn,
                venue_bonus_code(venue),
                f"{venue} bonus funds",
                AccountType.ASSET,
                ZAR,
            )
            postings = [
                PostingInput(venue_bonus_code(venue), amount),
                PostingInput(PNL_BETTING, -amount),
            ]

            return self._post(
                conn,
                kind=EntryKind.BONUS_CREDITED,
                postings=postings,
                ts_utc=ts_utc,
                description=f"bonus {amount} {ZAR} credited at {venue}",
                external_ref=external_ref,
            )

    def record_crypto_purchase(
        self,
        bank: str,
        wallet: str,
        asset: str,
        qty: Decimal,
        unit_price_base: Decimal,
        ts_utc: datetime | None = None,
        fee: Decimal = ZERO,
        source: str = "manual",
        external_ref: str | None = None,
    ) -> int:
        """
        Buy ``qty`` of ``asset`` with ZAR and open a cost-basis lot for it.

        The purchase fee is expensed rather than capitalised into the lot, so the lot's
        unit cost stays the price actually paid per unit and the realized gain on a
        later disposal is a clean market-to-market difference.

        """
        stamp = ts_utc or utc_now()
        unit_price_base = quantize_rate(unit_price_base)
        cost = apply_rate(qty, unit_price_base, self.base_currency)
        fee = quantize(fee, ZAR)
        code = wallet_code(wallet, asset)

        with self.store.transaction() as conn:
            self._ensure_account(
                conn, bank_code(bank), f"{bank} bank account", AccountType.ASSET, ZAR
            )
            self._ensure_account(conn, code, f"{wallet} {asset} wallet", AccountType.ASSET, asset)
            postings = [
                PostingInput(code, qty, asset, unit_price_base),
                PostingInput(EXPENSE_FEES, fee),
                PostingInput(bank_code(bank), -(cost + fee)),
            ]
            entry_id = self._post(
                conn,
                kind=EntryKind.CRYPTO_PURCHASE,
                postings=[p for p in postings if p.amount != ZERO],
                ts_utc=stamp,
                description=f"buy {qty} {asset} at {unit_price_base} {self.base_currency}",
                external_ref=external_ref,
            )
            self._record_fx(
                conn,
                FxQuote(stamp, self.base_currency, asset, unit_price_base, source),
            )
            conn.execute(
                """
                INSERT INTO crypto_lot
                    (tenant_id, asset, acquired_at, qty, unit_cost_base, remaining_qty,
                     source_entry_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.tenant_id,
                    asset,
                    _iso(stamp),
                    str(qty),
                    str(unit_price_base),
                    str(qty),
                    entry_id,
                ),
            )

            return entry_id

    def record_crypto_transfer(
        self,
        from_account: str,
        to_account: str,
        asset: str,
        qty: Decimal,
        market_rate: Decimal,
        ts_utc: datetime | None = None,
        network_fee_qty: Decimal = ZERO,
        source: str = "manual",
        external_ref: str | None = None,
    ) -> int:
        """
        Move crypto between two owned accounts and expense the network fee.

        The moved quantity carries its cost basis across at the current average
        cost, so an internal move produces no P&L. The network fee, however, leaves
        the book: it consumes lots, is expensed at market value, and the difference
        between market value and cost basis is the realized gain posted to
        ``PnL:FX``.

        """
        stamp = ts_utc or utc_now()
        market_rate = quantize_rate(market_rate)
        with self.store.transaction() as conn:
            self._ensure_account(conn, from_account, from_account, AccountType.ASSET, asset)
            self._ensure_account(conn, to_account, to_account, AccountType.ASSET, asset)
            cost_rate = self._average_cost_rate(conn, asset)
            moved = apply_rate(qty, cost_rate, self.base_currency)
            postings = [
                PostingInput(to_account, qty, asset, cost_rate, moved),
                PostingInput(from_account, -qty, asset, cost_rate, -moved),
            ]

            plan: DisposalPlan | None = None
            fee_base = ZERO
            if network_fee_qty > ZERO:
                plan = self._plan_disposal(conn, asset, network_fee_qty)
                fee_base = apply_rate(network_fee_qty, market_rate, self.base_currency)
                postings += [
                    PostingInput(
                        from_account,
                        -network_fee_qty,
                        asset,
                        implied_rate(plan.cost_base, network_fee_qty),
                        -plan.cost_base,
                    ),
                    PostingInput(EXPENSE_FEES, fee_base),
                    PostingInput(PNL_FX, plan.cost_base - fee_base),
                ]

            entry_id = self._post(
                conn,
                kind=EntryKind.CRYPTO_TRANSFER,
                postings=postings,
                ts_utc=stamp,
                description=f"transfer {qty} {asset} from {from_account} to {to_account}",
                external_ref=external_ref,
            )
            self._record_fx(conn, FxQuote(stamp, self.base_currency, asset, market_rate, source))
            if plan is not None:
                self._apply_disposal(conn, plan, entry_id, fee_base)

            return entry_id

    def _venue_account(
        self,
        conn: Connection,
        venue: str,
        funding: BetFunding,
        asset: str,
    ) -> tuple[str, str, Decimal]:
        """
        Resolve the funding account, its currency and its rate to base.
        """
        if funding is BetFunding.EXCHANGE:
            code = exchange_code(venue, asset)
            self._ensure_account(conn, code, f"{venue} {asset} balance", AccountType.ASSET, asset)

            return code, asset, self._average_cost_rate(conn, asset)

        code = venue_cash_code(venue) if funding is BetFunding.CASH else venue_bonus_code(venue)
        label = "cash balance" if funding is BetFunding.CASH else "bonus funds"
        self._ensure_account(conn, code, f"{venue} {label}", AccountType.ASSET, ZAR)

        return code, ZAR, ONE

    def record_bet_placed(
        self,
        venue: str,
        stake: Decimal,
        ts_utc: datetime | None = None,
        funding: BetFunding = BetFunding.CASH,
        asset: str = "USDC",
        external_ref: str | None = None,
        description: str = "",
    ) -> int:
        """
        Record a manually placed bet, charging the stake to the funding balance.

        The stake leaves the venue balance and lands in ``PnL:Betting``; the
        settlement entry credits whatever comes back, so the two together are the
        bet's P&L. Bonus-funded stakes are charged to the bonus-funds account, never
        to withdrawable cash.

        """
        with self.store.transaction() as conn:
            code, currency, rate = self._venue_account(conn, venue, funding, asset)
            stake = quantize(stake, currency)
            base = apply_rate(stake, rate, self.base_currency)
            postings = [
                PostingInput(code, -stake, currency, rate, -base),
                PostingInput(PNL_BETTING, base),
            ]

            return self._post(
                conn,
                kind=EntryKind.BET_PLACED,
                postings=postings,
                ts_utc=ts_utc,
                description=description or f"stake {stake} {currency} at {venue} ({funding.value})",
                external_ref=external_ref,
            )

    def record_bet_settled(
        self,
        venue: str,
        stake: Decimal,
        gross_return: Decimal,
        outcome: BetOutcome,
        ts_utc: datetime | None = None,
        funding: BetFunding = BetFunding.CASH,
        asset: str = "USDC",
        fee: Decimal = ZERO,
        external_ref: str | None = None,
        description: str = "",
    ) -> int:
        """
        Record a settlement, returning stake plus winnings to the funding balance.

        ``gross_return`` is the total the venue would pay on a full win (stake
        included). A void refunds the stake and so nets zero P&L; ``half_won`` and
        ``half_lost`` settle half the stake and refund the other half.

        """
        with self.store.transaction() as conn:
            code, currency, rate = self._venue_account(conn, venue, funding, asset)
            stake_fraction, return_fraction = _RETURN_FRACTIONS[outcome]
            returned = quantize(
                stake * stake_fraction + gross_return * return_fraction,
                currency,
            )
            fee = quantize(fee, currency)
            returned_base = apply_rate(returned, rate, self.base_currency)
            fee_base = apply_rate(fee, rate, self.base_currency)
            # The venue leg's base value is the other two legs netted rather than an
            # independently rounded product, or a half-cent would unbalance the entry.
            # A losing bet still posts all three (zero-valued) legs, because the fact
            # that the bet was settled at all is part of the audit trail.
            postings = [
                PostingInput(code, returned - fee, currency, rate, returned_base - fee_base),
                PostingInput(EXPENSE_FEES, fee_base),
                PostingInput(PNL_BETTING, -returned_base),
            ]

            return self._post(
                conn,
                kind=EntryKind.BET_SETTLED,
                postings=postings,
                ts_utc=ts_utc,
                description=description or f"settle {outcome.value} at {venue}",
                external_ref=external_ref,
            )
