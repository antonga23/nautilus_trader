# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Pure-stdlib unit tests for the bonus-EV double-entry ledger.

Exercises the ledger against an in-memory :mod:`sqlite3` connection through its
:class:`Store` protocol, so nothing here needs the compiled nautilus wheel. The
tests are the deliverable's real guarantee: they pin the balanced-or-rejected
invariant, the all-or-nothing write, FIFO cost basis against a hand-worked
example, the settlement outcomes, bonus-fund segregation, and append-only
correction by reversal.

"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import datetime
from datetime import timedelta
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The sys.path bootstrap above must run before the package import, so that this
# file works under a bare `pytest` as well as `python3 -m pytest` from the root.
from tools.bonusev.core import ledger as led  # noqa: E402
from tools.bonusev.core import money  # noqa: E402


START = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
TENANT = "tenant-1"


def at(minutes: int) -> datetime:
    """
    Return a deterministic UTC timestamp ``minutes`` after the fixture epoch.
    """
    return START + timedelta(minutes=minutes)


@pytest.fixture
def ledger() -> led.Ledger:
    """
    Build a ledger over an in-memory SQLite database with the chart seeded.
    """
    store = led.SqliteStore(sqlite3.connect(":memory:"))
    ledger = led.Ledger(store, TENANT, created_by="tester")
    ledger.ensure_schema()
    ledger.seed_chart_of_accounts()

    return ledger


def raw(ledger: led.Ledger) -> sqlite3.Connection:
    """
    Return the underlying connection for assertions the API deliberately hides.
    """
    store = ledger.store
    assert isinstance(store, led.SqliteStore)

    return store.connection


def entry_base_amounts(ledger: led.Ledger, entry_id: int) -> list[Decimal]:
    """
    Return the base amounts of every posting on an entry.
    """
    rows = (
        raw(ledger)
        .execute(
            "SELECT base_amount FROM posting WHERE entry_id = ? ORDER BY id",
            (entry_id,),
        )
        .fetchall()
    )

    return [Decimal(row[0]) for row in rows]


def assert_entry_balances(ledger: led.Ledger, entry_id: int) -> None:
    """
    Assert an entry has postings and that they sum to exactly zero in base.
    """
    amounts = entry_base_amounts(ledger, entry_id)
    assert amounts, f"entry {entry_id} has no postings"
    assert sum(amounts, Decimal(0)) == Decimal(0)


# -- balanced-or-rejected -------------------------------------------------------


def test_post_entry_rejects_unbalanced_and_leaves_no_rows(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(0))
    entries_before = raw(ledger).execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0]
    postings_before = raw(ledger).execute("SELECT COUNT(*) FROM posting").fetchone()[0]

    postings = [
        led.PostingInput(led.venue_cash_code("Betway"), Decimal("100.00")),
        led.PostingInput(led.bank_code("FNB"), Decimal("-99.99")),
    ]
    with pytest.raises(led.UnbalancedEntry, match=r"0\.01"):
        ledger.post_entry(led.EntryKind.FIAT_DEPOSIT, postings, ts_utc=at(1))

    # The header and both lines were written before the balance check ran, so an
    # unchanged row count is evidence the transaction actually rolled back.
    assert raw(ledger).execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0] == entries_before
    assert raw(ledger).execute("SELECT COUNT(*) FROM posting").fetchone()[0] == postings_before
    assert ledger.trial_balance().is_balanced


def test_post_entry_rejects_empty_entry(ledger: led.Ledger) -> None:
    with pytest.raises(led.UnbalancedEntry):
        ledger.post_entry(led.EntryKind.FIAT_DEPOSIT, [], ts_utc=at(0))


def test_unknown_account_is_rejected(ledger: led.Ledger) -> None:
    postings = [
        led.PostingInput("Venue:Nowhere:Cash:ZAR", Decimal(1)),
        led.PostingInput(led.PNL_BETTING, Decimal(-1)),
    ]
    with pytest.raises(led.UnknownAccount):
        ledger.post_entry(led.EntryKind.FIAT_DEPOSIT, postings, ts_utc=at(0))


# -- property / fuzz ------------------------------------------------------------


def test_every_helper_entry_balances_under_random_sequences(ledger: led.Ledger) -> None:
    rng = random.Random(20260724)  # noqa: S311 - deterministic fixture data, not crypto
    lot_qty = Decimal(0)
    minute = 0

    for _ in range(120):
        minute += 1
        choice = rng.choice(["deposit", "bonus", "buy", "bet", "settle", "transfer"])
        amount = Decimal(rng.randrange(100, 500_00)) / Decimal(100)

        if choice == "deposit":
            entry_id = ledger.record_fiat_deposit(
                "FNB",
                "Betway",
                amount,
                ts_utc=at(minute),
                fee=Decimal(rng.randrange(0, 500)) / Decimal(100),
            )
        elif choice == "bonus":
            entry_id = ledger.record_bonus_credited("Betway", amount, ts_utc=at(minute))
        elif choice == "buy":
            qty = Decimal(rng.randrange(1_000000, 500_000000)) / Decimal(1_000000)
            entry_id = ledger.record_crypto_purchase(
                "FNB",
                "Ledger",
                money.USDC,
                qty,
                Decimal(rng.randrange(1600, 2100)) / Decimal(100),
                ts_utc=at(minute),
                fee=Decimal(rng.randrange(0, 5000)) / Decimal(100),
            )
            lot_qty += qty
        elif choice == "bet":
            entry_id = ledger.record_bet_placed(
                "Betway",
                amount,
                ts_utc=at(minute),
                funding=rng.choice([led.BetFunding.CASH, led.BetFunding.BONUS]),
            )
        elif choice == "settle":
            entry_id = ledger.record_bet_settled(
                "Betway",
                amount,
                amount * Decimal("2.35"),
                rng.choice(list(led.BetOutcome)),
                ts_utc=at(minute),
                funding=rng.choice([led.BetFunding.CASH, led.BetFunding.BONUS]),
                fee=Decimal(rng.randrange(0, 300)) / Decimal(100),
            )
        else:
            if lot_qty <= Decimal("0.1"):
                continue
            fee_qty = (lot_qty / Decimal(10)).quantize(Decimal("0.000001"))
            entry_id = ledger.record_crypto_transfer(
                led.wallet_code("Ledger", money.USDC),
                led.exchange_code("SXbet"),
                money.USDC,
                Decimal(rng.randrange(1, 1_000000)) / Decimal(1_000000),
                Decimal(rng.randrange(1600, 2100)) / Decimal(100),
                ts_utc=at(minute),
                network_fee_qty=fee_qty,
            )
            lot_qty -= fee_qty

        assert_entry_balances(ledger, entry_id)
        assert ledger.trial_balance().is_balanced

    final = ledger.trial_balance()
    assert final.total_base == Decimal(0)
    assert len(final.rows) > 4


def test_trial_balance_as_of_is_balanced_at_every_point(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(5000), ts_utc=at(1), fee=Decimal("12.34"))
    ledger.record_bonus_credited("Betway", Decimal(1000), ts_utc=at(2))
    ledger.record_bet_placed("Betway", Decimal(250), ts_utc=at(3))
    ledger.record_bet_settled(
        "Betway",
        Decimal(250),
        Decimal("612.50"),
        led.BetOutcome.WON,
        ts_utc=at(4),
    )

    for minute in range(6):
        assert ledger.trial_balance(as_of=at(minute)).is_balanced


# -- multi-currency and FX snapshots -------------------------------------------


def test_crypto_purchase_records_rate_and_reproducible_base_amount(ledger: led.Ledger) -> None:
    entry_id = ledger.record_crypto_purchase(
        "FNB",
        "Ledger",
        money.USDC,
        Decimal(100),
        Decimal("18.5"),
        ts_utc=at(1),
        fee=Decimal("25.00"),
    )

    wallet = ledger.balance(led.wallet_code("Ledger", money.USDC))
    assert wallet.currency == money.USDC
    assert wallet.amount == Decimal("100.000000")
    assert wallet.base_amount == Decimal("1850.00")

    row = (
        raw(ledger)
        .execute(
            "SELECT amount, currency, fx_rate_to_base, base_amount FROM posting "
            "WHERE entry_id = ? AND currency = ?",
            (entry_id, money.USDC),
        )
        .fetchone()
    )
    assert Decimal(row[0]) * Decimal(row[2]) == Decimal(row[3])

    quote = raw(ledger).execute("SELECT base_ccy, quote_ccy, rate FROM fx_rate").fetchone()
    assert (quote[0], quote[1], Decimal(quote[2])) == (
        "ZAR",
        money.USDC,
        Decimal("18.500000000000"),
    )

    assert ledger.balance(led.bank_code("FNB")).base_amount == Decimal("-1875.00")
    assert ledger.balance(led.EXPENSE_FEES).base_amount == Decimal("25.00")


def test_base_currency_is_configurable(ledger: led.Ledger) -> None:
    usd_ledger = led.Ledger(ledger.store, "tenant-usd", base_currency=money.USD)
    usd_ledger.seed_chart_of_accounts()
    usd_ledger.record_fiat_deposit("Chase", "Betway", Decimal(100), ts_utc=at(1))

    assert usd_ledger.trial_balance().is_balanced
    assert usd_ledger.balance(led.venue_cash_code("Betway")).base_amount == Decimal("100.00")
    # Tenants share a store but never share a book.
    assert ledger.trial_balance().rows != usd_ledger.trial_balance().rows


# -- FIFO cost basis ------------------------------------------------------------


def _seed_three_lots(ledger: led.Ledger) -> None:
    """
    Buy 1000 @ 18.00, 500 @ 19.50 and 800 @ 17.25 ZAR/USDC, in that order.
    """
    for minute, (qty, rate) in enumerate(
        [
            (Decimal(1000), Decimal("18.00")),
            (Decimal(500), Decimal("19.50")),
            (Decimal(800), Decimal("17.25")),
        ],
        start=1,
    ):
        ledger.record_crypto_purchase(
            "FNB",
            "Ledger",
            money.USDC,
            qty,
            rate,
            ts_utc=at(minute),
        )


def test_fifo_partial_disposals_realize_hand_worked_pnl(ledger: led.Ledger) -> None:
    _seed_three_lots(ledger)

    # Disposal 1 burns 1200 USDC of network fee at 20.00 ZAR/USDC.
    #   lot 1: 1000 @ 18.00 = 18 000.00 cost, 20 000.00 proceeds -> +2 000.00
    #   lot 2:  200 @ 19.50 =  3 900.00 cost,  4 000.00 proceeds ->   +100.00
    first = ledger.record_crypto_transfer(
        led.wallet_code("Ledger", money.USDC),
        led.exchange_code("SXbet"),
        money.USDC,
        Decimal(100),
        Decimal("20.00"),
        ts_utc=at(10),
        network_fee_qty=Decimal(1200),
    )
    assert ledger.realized_fx_pnl(first) == Decimal("2100.00")

    # Disposal 2 burns 700 USDC at 16.00 ZAR/USDC, against what FIFO left behind.
    #   lot 2: 300 @ 19.50 = 5 850.00 cost, 4 800.00 proceeds -> -1 050.00
    #   lot 3: 400 @ 17.25 = 6 900.00 cost, 6 400.00 proceeds ->   -500.00
    second = ledger.record_crypto_transfer(
        led.wallet_code("Ledger", money.USDC),
        led.exchange_code("SXbet"),
        money.USDC,
        Decimal(100),
        Decimal("16.00"),
        ts_utc=at(20),
        network_fee_qty=Decimal(700),
    )
    assert ledger.realized_fx_pnl(second) == Decimal("-1550.00")

    # Income accounts carry credits negative, so a net +550.00 gain reads as -550.00.
    assert ledger.balance(led.PNL_FX).base_amount == Decimal("-550.00")
    assert [(qty, remaining) for _, qty, _, remaining in ledger.lots(money.USDC)] == [
        (Decimal(1000), Decimal(0)),
        (Decimal(500), Decimal(0)),
        (Decimal(800), Decimal(400)),
    ]
    assert ledger.trial_balance().is_balanced


def test_disposal_records_method_per_consumption(ledger: led.Ledger) -> None:
    _seed_three_lots(ledger)
    entry_id = ledger.record_crypto_transfer(
        led.wallet_code("Ledger", money.USDC),
        led.exchange_code("SXbet"),
        money.USDC,
        Decimal(10),
        Decimal("20.00"),
        ts_utc=at(10),
        network_fee_qty=Decimal(1200),
    )

    rows = (
        raw(ledger)
        .execute(
            "SELECT method, qty, realized_gain_base FROM lot_consumption "
            "WHERE disposal_entry_id = ? ORDER BY id",
            (entry_id,),
        )
        .fetchall()
    )
    assert [row[0] for row in rows] == ["fifo", "fifo"]
    assert [Decimal(row[1]) for row in rows] == [Decimal(1000), Decimal(200)]
    assert [Decimal(row[2]) for row in rows] == [Decimal("2000.00"), Decimal("100.00")]


def test_lifo_selects_the_newest_lots(ledger: led.Ledger) -> None:
    lifo = led.Ledger(
        ledger.store,
        TENANT,
        created_by="tester",
        cost_basis_method=led.CostBasisMethod.LIFO,
    )
    _seed_three_lots(lifo)
    entry_id = lifo.record_crypto_transfer(
        led.wallet_code("Ledger", money.USDC),
        led.exchange_code("SXbet"),
        money.USDC,
        Decimal(10),
        Decimal("20.00"),
        ts_utc=at(10),
        network_fee_qty=Decimal(800),
    )

    methods = (
        raw(lifo)
        .execute(
            "SELECT method FROM lot_consumption WHERE disposal_entry_id = ?",
            (entry_id,),
        )
        .fetchall()
    )
    assert [m[0] for m in methods] == ["lifo"]
    # 800 @ 17.25 = 13 800.00 cost against 16 000.00 proceeds.
    assert lifo.realized_fx_pnl(entry_id) == Decimal("2200.00")


def test_disposal_beyond_remaining_lots_is_rejected(ledger: led.Ledger) -> None:
    _seed_three_lots(ledger)
    with pytest.raises(led.InsufficientLots):
        ledger.record_crypto_transfer(
            led.wallet_code("Ledger", money.USDC),
            led.exchange_code("SXbet"),
            money.USDC,
            Decimal(10),
            Decimal("20.00"),
            ts_utc=at(10),
            network_fee_qty=Decimal(9999),
        )

    assert ledger.lots(money.USDC)[0][3] == Decimal(1000)
    assert ledger.trial_balance().is_balanced


# -- bet placement and settlement ----------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected_return", "expected_pnl"),
    [
        (led.BetOutcome.WON, Decimal("250.00"), Decimal("150.00")),
        (led.BetOutcome.LOST, Decimal("0.00"), Decimal("-100.00")),
        (led.BetOutcome.VOID, Decimal("100.00"), Decimal("0.00")),
        (led.BetOutcome.HALF_WON, Decimal("175.00"), Decimal("75.00")),
        (led.BetOutcome.HALF_LOST, Decimal("50.00"), Decimal("-50.00")),
    ],
)
def test_settlement_outcomes(
    ledger: led.Ledger,
    outcome: led.BetOutcome,
    expected_return: Decimal,
    expected_pnl: Decimal,
) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))
    ledger.record_bet_placed("Betway", Decimal(100), ts_utc=at(2))
    settle = ledger.record_bet_settled(
        "Betway",
        Decimal(100),
        Decimal(250),
        outcome,
        ts_utc=at(3),
    )

    cash = ledger.balance(led.venue_cash_code("Betway"))
    assert cash.base_amount == Decimal("1000.00") - Decimal("100.00") + expected_return
    # PnL:Betting is an income account, so a profit shows as a credit (negative).
    assert ledger.balance(led.PNL_BETTING).base_amount == -expected_pnl
    assert_entry_balances(ledger, settle)
    assert ledger.trial_balance().is_balanced


def test_void_returns_the_stake_and_nets_zero(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))
    before = ledger.balance(led.venue_cash_code("Betway")).base_amount

    ledger.record_bet_placed("Betway", Decimal("137.50"), ts_utc=at(2))
    ledger.record_bet_settled(
        "Betway",
        Decimal("137.50"),
        Decimal("399.99"),
        led.BetOutcome.VOID,
        ts_utc=at(3),
    )

    assert ledger.balance(led.venue_cash_code("Betway")).base_amount == before
    assert ledger.balance(led.PNL_BETTING).base_amount == Decimal("0.00")


def test_settlement_fee_lands_on_expense_fees(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))
    ledger.record_bet_placed("Betway", Decimal(100), ts_utc=at(2))
    ledger.record_bet_settled(
        "Betway",
        Decimal(100),
        Decimal(250),
        led.BetOutcome.WON,
        ts_utc=at(3),
        fee=Decimal("7.50"),
    )

    assert ledger.balance(led.EXPENSE_FEES).base_amount == Decimal("7.50")
    assert ledger.balance(led.venue_cash_code("Betway")).base_amount == Decimal("1142.50")
    assert ledger.trial_balance().is_balanced


def test_bonus_funded_bets_never_touch_withdrawable_cash(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))
    ledger.record_bonus_credited("Betway", Decimal(500), ts_utc=at(2))
    ledger.record_bet_placed("Betway", Decimal(200), ts_utc=at(3), funding=led.BetFunding.BONUS)
    ledger.record_bet_settled(
        "Betway",
        Decimal(200),
        Decimal(380),
        led.BetOutcome.WON,
        ts_utc=at(4),
        funding=led.BetFunding.BONUS,
    )

    assert ledger.balance(led.venue_bonus_code("Betway")).base_amount == Decimal("680.00")
    assert ledger.balance(led.venue_cash_code("Betway")).base_amount == Decimal("1000.00")
    assert ledger.trial_balance().is_balanced


def test_exchange_bets_are_priced_at_crypto_cost_basis(ledger: led.Ledger) -> None:
    ledger.record_crypto_purchase(
        "FNB",
        "Ledger",
        money.USDC,
        Decimal(1000),
        Decimal("18.00"),
        ts_utc=at(1),
    )
    ledger.record_crypto_transfer(
        led.wallet_code("Ledger", money.USDC),
        led.exchange_code("SXbet"),
        money.USDC,
        Decimal(500),
        Decimal("18.50"),
        ts_utc=at(2),
    )
    ledger.record_bet_placed(
        "SXbet",
        Decimal(100),
        ts_utc=at(3),
        funding=led.BetFunding.EXCHANGE,
    )

    exchange = ledger.balance(led.exchange_code("SXbet"))
    assert exchange.currency == money.USDC
    assert exchange.amount == Decimal("400.000000")
    assert exchange.base_amount == Decimal("7200.00")
    assert ledger.trial_balance().is_balanced


# -- append-only and reversal ---------------------------------------------------


def test_posted_entries_cannot_be_updated_or_deleted(ledger: led.Ledger) -> None:
    entry_id = ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(ledger).execute(
            "UPDATE journal_entry SET description = 'tampered' WHERE id = ?",
            (entry_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(ledger).execute("DELETE FROM journal_entry WHERE id = ?", (entry_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(ledger).execute("UPDATE posting SET amount = '1' WHERE entry_id = ?", (entry_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(ledger).execute("DELETE FROM posting WHERE entry_id = ?", (entry_id,))

    with pytest.raises(led.AppendOnlyViolation):
        ledger.edit_entry(entry_id, description="tampered")

    assert ledger.balance(led.venue_cash_code("Betway")).base_amount == Decimal("1000.00")


def test_reversal_is_linked_balanced_and_restores_the_book(ledger: led.Ledger) -> None:
    original = ledger.record_fiat_deposit(
        "FNB",
        "Betway",
        Decimal(1000),
        ts_utc=at(1),
        fee=Decimal("15.00"),
        external_ref="eft-9931",
    )
    reversal = ledger.reverse_entry(original, ts_utc=at(2), description="wrong venue")

    row = (
        raw(ledger)
        .execute(
            "SELECT kind, reversal_of, external_ref, description FROM journal_entry WHERE id = ?",
            (reversal,),
        )
        .fetchone()
    )
    assert (row[0], row[1], row[2], row[3]) == ("reversal", original, "eft-9931", "wrong venue")

    assert_entry_balances(ledger, reversal)
    assert entry_base_amounts(ledger, reversal) == [
        -a for a in entry_base_amounts(ledger, original)
    ]
    assert ledger.balance(led.venue_cash_code("Betway")).base_amount == Decimal("0.00")
    assert ledger.balance(led.bank_code("FNB")).base_amount == Decimal("0.00")
    assert ledger.balance(led.EXPENSE_FEES).base_amount == Decimal("0.00")
    assert ledger.trial_balance().is_balanced

    with pytest.raises(led.EntryAlreadyReversed):
        ledger.reverse_entry(original, ts_utc=at(3))


def test_reversing_an_unknown_entry_is_rejected(ledger: led.Ledger) -> None:
    with pytest.raises(led.LedgerError):
        ledger.reverse_entry(9999, ts_utc=at(1))


# -- statements and opening balances -------------------------------------------


def test_statement_reports_a_running_base_balance(ledger: led.Ledger) -> None:
    ledger.record_fiat_deposit("FNB", "Betway", Decimal(1000), ts_utc=at(1))
    ledger.record_bet_placed("Betway", Decimal(250), ts_utc=at(2), external_ref="bet-1")
    ledger.record_bet_settled(
        "Betway",
        Decimal(250),
        Decimal(500),
        led.BetOutcome.WON,
        ts_utc=at(3),
        external_ref="bet-1",
    )

    lines = ledger.statement(led.venue_cash_code("Betway"))
    assert [line.kind for line in lines] == ["fiat_deposit", "bet_placed", "bet_settled"]
    assert [line.running_base for line in lines] == [
        Decimal("1000.00"),
        Decimal("750.00"),
        Decimal("1250.00"),
    ]
    assert [line.external_ref for line in lines] == [None, "bet-1", "bet-1"]
    assert ledger.statement(led.venue_cash_code("Betway"), as_of=at(2))[-1].running_base == Decimal(
        "750.00",
    )


def test_opening_balance_posts_against_equity(ledger: led.Ledger) -> None:
    ledger.ensure_account(led.bank_code("FNB"), "FNB", led.AccountType.ASSET, money.ZAR)
    ledger.record_opening_balance(led.bank_code("FNB"), Decimal(25000), ts_utc=at(0))

    assert ledger.balance(led.bank_code("FNB")).base_amount == Decimal("25000.00")
    assert ledger.balance(led.EQUITY_OPENING).base_amount == Decimal("-25000.00")
    assert ledger.trial_balance().is_balanced


# -- money primitives -----------------------------------------------------------


def test_money_rejects_floats_and_unknown_currencies() -> None:
    with pytest.raises(money.MoneyError):
        money.dec(1.1)  # type: ignore[arg-type]
    with pytest.raises(money.MoneyError):
        # bool passes the int annotation but is never a valid amount.
        money.dec(True)
    with pytest.raises(money.UnknownCurrency):
        money.quantize(Decimal(1), "XXX")
    with pytest.raises(money.InvalidRate):
        money.apply_rate(Decimal(1), Decimal(-1))


def test_naive_timestamps_are_rejected(ledger: led.Ledger) -> None:
    with pytest.raises(led.LedgerError):
        ledger.record_fiat_deposit(
            "FNB",
            "Betway",
            Decimal(100),
            ts_utc=datetime(2026, 7, 1, 9, 0),  # noqa: DTZ001 - naive on purpose
        )
