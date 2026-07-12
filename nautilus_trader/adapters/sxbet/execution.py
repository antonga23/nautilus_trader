# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
SX.bet execution client.
"""

import asyncio
import re
from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.common.settlement import BET_SETTLEMENTS_TOPIC
from nautilus_trader.adapters.betting.common.settlement import BetSettlement
from nautilus_trader.adapters.betting.common.settlement import SettlementResult
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetExecClientConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage
from nautilus_trader.adapters.sxbet.signing import from_wei
from nautilus_trader.adapters.sxbet.signing import generate_salt
from nautilus_trader.adapters.sxbet.signing import get_expiry
from nautilus_trader.adapters.sxbet.signing import percentage_to_decimal_odds
from nautilus_trader.adapters.sxbet.signing import sign_eip712_fill_order
from nautilus_trader.adapters.sxbet.signing import sign_eip712_order
from nautilus_trader.adapters.sxbet.signing import to_wei
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import CurrencyType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events.order import OrderAccepted
from nautilus_trader.model.events.order import OrderCanceled
from nautilus_trader.model.events.order import OrderRejected
from nautilus_trader.model.events.order import OrderSubmitted
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order


# SX.bet settles USDC bets with 6-decimal wallet precision (see signing.to_wei).
SXBET_USDC = Currency(
    code="USDC",
    precision=6,
    iso4217=0,
    name="USD Coin",
    currency_type=CurrencyType.CRYPTO,
)

# CryptoBettingInstrument size/price precision used when no instrument is resolvable.
_SXBET_BETTING_PRECISION = 2


class SXBetExecutionError(ValueError):
    """
    Base class for SX.bet execution validation errors.
    """


class SXBetMissingOrderHashError(SXBetExecutionError):
    """
    Raised when the venue order response does not include a usable order hash.
    """

    def __init__(self) -> None:
        super().__init__("SX.bet response missing a valid orderHash")


class SXBetUnsupportedBaseCurrencyError(SXBetExecutionError):
    """
    Raised when the configured execution base currency is unsupported.
    """

    def __init__(self, base_currency: str) -> None:
        super().__init__(
            "SX.bet execution currently supports only USDC base_currency; "
            f"received {base_currency!r}",
        )


class SXBetInvalidConfigError(SXBetExecutionError):
    """
    Raised when required SX.bet execution credentials are missing or malformed.
    """

    def __init__(self, field_name: str, reason: str) -> None:
        super().__init__(f"Invalid SX.bet execution config for {field_name}: {reason}")


class SXBetMissingFillHashError(SXBetExecutionError):
    """
    Raised when the venue fill response does not include a usable fill hash.
    """

    def __init__(self) -> None:
        super().__init__("SX.bet response missing a valid fillHash")


class SXBetExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for the SX.bet venue.

    Uses EIP712 signed orders for blockchain-based betting.

    """

    _SUPPORTED_BASE_CURRENCY = "USDC"

    def __init__(  # pylint: disable=too-many-arguments
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: SXBetHttpClient,
        instrument_provider: SXBetInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: SXBetExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SXBET_VENUE.value),
            venue=SXBET_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=AccountType.BETTING,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        self._http_client = http_client
        self._config = config
        self._instrument_provider = instrument_provider
        self._logger = logger
        self._validate_config(config)
        self._wallet_address = self._normalize_wallet_address(config.wallet_address)
        self._private_key = self._normalize_private_key(config.private_key)
        self._set_account_id(AccountId(f"{SXBET_VENUE.value}-{self._wallet_address[:10]}"))
        self._base_token = self._resolve_base_token(config.base_currency)
        # Order tracking
        self._orders: dict[ClientOrderId, dict] = {}
        self._venue_order_ids: dict[ClientOrderId, VenueOrderId] = {}
        # Cumulative matched size (wei) last emitted per order, for idempotent fills
        self._last_matched_wei: dict[ClientOrderId, int] = {}
        # Orders whose settlement was already published, so grading never re-emits
        self._settled_order_ids: set[ClientOrderId] = set()
        self._fill_poll_task: asyncio.Task | None = None
        self._account_state_task: asyncio.Task | None = None
        self._settlement_poll_task: asyncio.Task | None = None

    async def _connect(self) -> None:
        """
        Connect to the execution venue.
        """
        self._log.info("Connecting SXBetExecutionClient...")
        await self._http_client.connect()

        # Publish the initial account state so the portfolio sees SX.bet funds
        await self._update_account_state()

        # SX.bet exposes no authenticated user fill push feed, so fills and the
        # account balance are reconciled by polling.
        self._fill_poll_task = self.create_task(
            self._fill_poll_loop(),
            log_msg="sxbet_fill_poll",
        )
        self._account_state_task = self.create_task(
            self._account_state_loop(),
            log_msg="sxbet_account_state_poll",
        )
        self._settlement_poll_task = self.create_task(
            self._settlement_poll_loop(),
            log_msg="sxbet_settlement_poll",
        )

        self._log.info("SXBetExecutionClient connected")

    async def _disconnect(self) -> None:
        """
        Disconnect from the execution venue.
        """
        self._log.info("Disconnecting SXBetExecutionClient...")
        for task in (self._fill_poll_task, self._account_state_task, self._settlement_poll_task):
            if task is not None and not task.done():
                task.cancel()
        self._fill_poll_task = None
        self._account_state_task = None
        self._settlement_poll_task = None
        await self._http_client.disconnect()
        self._log.info("SXBetExecutionClient disconnected")

    async def _fill_poll_loop(self) -> None:
        interval = float(getattr(self._config, "fill_poll_interval_secs", 3.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reconcile_order_fills()
            except asyncio.CancelledError:
                raise
            except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
                self._log.error(f"SX.bet fill reconciliation failed: {e}")

    async def _account_state_loop(self) -> None:
        interval = float(getattr(self._config, "account_state_interval_secs", 30.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._update_account_state()
            except asyncio.CancelledError:
                raise
            except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
                self._log.error(f"SX.bet account state refresh failed: {e}")

    async def _settlement_poll_loop(self) -> None:
        interval = float(getattr(self._config, "settlement_poll_interval_secs", 30.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reconcile_settlements()
            except asyncio.CancelledError:
                raise
            except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
                self._log.error(f"SX.bet settlement reconciliation failed: {e}")

    async def _reconcile_settlements(self) -> None:
        """
        Poll graded SX.bet trades and publish one ``BetSettlement`` per graded order.

        Only runs while tracked orders remain unsettled; queries the ``/trades`` feed
        with ``settled=true`` and matches rows to tracked orders by ``orderHash`` (maker
        posts) or ``fillHash`` (taker fills). Idempotent: each order settles the bus
        exactly once, across polls and across multiple graded trade rows (partial
        matches all grade with the market, so the first graded row decides).

        A grading pays out or releases the wallet balance, so the account state is
        refreshed immediately after publishing settlements rather than waiting for the
        slower account-state poll.

        """
        pending = {
            str(venue_order_id): client_order_id
            for client_order_id, venue_order_id in self._venue_order_ids.items()
            if client_order_id not in self._settled_order_ids
        }
        if not pending:
            return

        trades = await self._http_client.get_user_trades(self._wallet_address, settled=True)
        emitted = 0
        for trade in trades.get("data", {}).get("trades", []):
            client_order_id = pending.get(str(trade.get("orderHash"))) or pending.get(
                str(trade.get("fillHash")),
            )
            if client_order_id is None or client_order_id in self._settled_order_ids:
                continue

            result = self._settlement_result(trade)
            if result is None:
                continue

            self._settled_order_ids.add(client_order_id)
            self._publish_settlement(client_order_id, result, trade)
            emitted += 1

        if emitted:
            await self._update_account_state()

    @staticmethod
    def _settlement_result(trade: dict) -> SettlementResult | None:
        """
        Derive WON / LOST / VOID from a graded ``/trades`` row.

        Grounded in the SX.bet API schema: ``settled`` flags grading, ``outcome`` is the
        market's final outcome (``1`` outcome one, ``2`` outcome two, ``0`` void — a
        voided market returns all stakes), and ``bettingOutcomeOne`` is the side this
        bet backed. ``settleValue`` is not used because it does not encode the bettor's
        result.

        """
        if trade.get("settled") is not True:
            return None
        if trade.get("tradeStatus") == "FAILED":
            return None
        outcome = trade.get("outcome")
        if outcome == 0:
            return SettlementResult.VOID
        if outcome not in (1, 2):
            return None
        betting_outcome_one = trade.get("bettingOutcomeOne")
        if not isinstance(betting_outcome_one, bool):
            return None
        won = (outcome == 1) == betting_outcome_one
        return SettlementResult.WON if won else SettlementResult.LOST

    def _publish_settlement(
        self,
        client_order_id: ClientOrderId,
        result: SettlementResult,
        trade: dict,
    ) -> None:
        order = self._cache.order(client_order_id)
        settle_value = trade.get("settleValue")
        settlement = BetSettlement(
            venue=SXBET_VENUE.value,
            client_order_id=client_order_id.value,
            instrument_id=str(order.instrument_id) if order is not None else None,
            result=result,
            settle_value=float(settle_value) if isinstance(settle_value, (int, float)) else None,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.info(
            f"SX.bet bet settled: {client_order_id} {result} "
            f"(marketHash={trade.get('marketHash')}, settleValue={settle_value})",
        )
        self._msgbus.publish(topic=BET_SETTLEMENTS_TOPIC, msg=settlement)

    async def _update_account_state(self) -> None:
        """
        Fetch the SX.bet wallet balance and publish an account state.

        SX.bet does not currently expose wallet balance through the public REST API
        (``get_balance`` raises), so an account state is only published when the HTTP
        client yields a usable balance result. ``locked`` is reported as zero; modelling
        open-order stake as locked funds is left to a follow-up.

        """
        try:
            balance = await self._http_client.get_balance(
                self._wallet_address,
                self._base_token,
            )
        except SXBetHttpClientError as e:
            self._log.warning(f"Could not get balance: {e}")
            return

        balance_wei = self._parse_balance_wei(balance)
        if balance_wei is None:
            return

        total = Money(from_wei(balance_wei, decimals=SXBET_USDC.precision), SXBET_USDC)
        account_balance = AccountBalance(
            total=total,
            locked=Money(0, SXBET_USDC),
            free=total,
        )
        self.generate_account_state(
            balances=[account_balance],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

    @staticmethod
    def _parse_balance_wei(balance: object) -> int | None:
        """
        Extract the raw wallet balance (6-decimal USDC subunits) from a balance result.
        """
        value: object = balance
        if isinstance(balance, dict):
            for key in ("balance", "available", "total"):
                if key in balance:
                    value = balance[key]
                    break
            else:
                return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, (str, float)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return None

    async def _reconcile_order_fills(self) -> None:
        """
        Poll SX.bet order status and emit fills for newly matched size.

        Idempotent: the cumulative matched size (``fillAmount``) last seen per order is
        tracked, so repeated polls of the same matched size never re-emit, and a larger
        matched size emits only the incremental delta.

        """
        if not self._venue_order_ids:
            return

        orders = await self._http_client.get_user_orders(self._wallet_address)
        by_hash = {
            str(venue_order_id): client_order_id
            for client_order_id, venue_order_id in self._venue_order_ids.items()
        }
        for order_data in orders.get("data", {}).get("orders", []):
            order_hash = order_data.get("orderHash")
            client_order_id = by_hash.get(str(order_hash))
            if client_order_id is None:
                continue

            matched_wei = self._matched_wei(order_data)
            last_wei = self._last_matched_wei.get(client_order_id, 0)
            if matched_wei <= last_wei:
                continue

            self._emit_order_filled(
                client_order_id=client_order_id,
                order_data=order_data,
                delta_wei=matched_wei - last_wei,
                cumulative_wei=matched_wei,
            )
            self._last_matched_wei[client_order_id] = matched_wei

    @staticmethod
    def _matched_wei(order_data: dict) -> int:
        try:
            return int(order_data.get("fillAmount", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _emit_order_filled(
        self,
        client_order_id: ClientOrderId,
        order_data: dict,
        delta_wei: int,
        cumulative_wei: int,
    ) -> None:
        order = self._cache.order(client_order_id)
        if order is None:
            self._log.warning(
                f"Cannot emit SX.bet fill; order not in cache ({client_order_id})",
            )
            return

        venue_order_id = self._venue_order_ids.get(client_order_id)
        if venue_order_id is None:
            return

        instrument = self._instrument_provider.find(order.instrument_id)
        last_qty = self._make_qty(instrument, from_wei(delta_wei, decimals=SXBET_USDC.precision))
        last_px = self._fill_price(instrument, order, order_data)
        # Deterministic per cumulative matched level keeps repeated polls idempotent.
        trade_id = TradeId(f"{venue_order_id.value}-{cumulative_wei}")

        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=trade_id,
            order_side=order.side,
            order_type=order.order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=SXBET_USDC,
            commission=Money(0, SXBET_USDC),
            liquidity_side=self._liquidity_side(),
            ts_event=self._clock.timestamp_ns(),
            info=order_data,
        )

    def _liquidity_side(self) -> LiquiditySide:
        execution_mode = getattr(self._config, "execution_mode", "maker_post")
        return LiquiditySide.TAKER if execution_mode == "taker_fill" else LiquiditySide.MAKER

    @staticmethod
    def _make_qty(instrument: object, value: float) -> Quantity:
        if isinstance(instrument, CryptoBettingInstrument):
            return instrument.make_qty(value)
        return Quantity(value, precision=_SXBET_BETTING_PRECISION)

    def _status_quantity(
        self,
        instrument: object,
        order_data: dict,
        cached_order: Order | None,
    ) -> Quantity:
        try:
            total_wei = int(order_data.get("totalBetSize", 0) or 0)
        except (TypeError, ValueError):
            total_wei = 0
        if total_wei:
            return self._make_qty(
                instrument,
                from_wei(total_wei, decimals=SXBET_USDC.precision),
            )
        if cached_order is not None:
            return cached_order.quantity
        return Quantity.from_int(1)

    @staticmethod
    def _fill_price(instrument: object, order: Order, order_data: dict) -> Price:
        percentage_odds = order_data.get("percentageOdds")
        decimal_odds: float | None = None
        if percentage_odds is not None:
            try:
                decimal_odds = percentage_to_decimal_odds(int(percentage_odds))
            except (TypeError, ValueError):
                decimal_odds = None
        if decimal_odds is None:
            order_price = getattr(order, "price", None)
            decimal_odds = float(order_price) if order_price is not None else 0.0
        if isinstance(instrument, CryptoBettingInstrument):
            return instrument.make_price(decimal_odds)
        return Price(decimal_odds, precision=_SXBET_BETTING_PRECISION)

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submit an order to the venue.
        """
        execution_mode = getattr(self._config, "execution_mode", "maker_post")
        if execution_mode == "taker_fill":
            await self._submit_taker_fill(command)
            return
        await self._submit_maker_order(command)

    async def _submit_maker_order(self, command: SubmitOrder) -> None:
        """
        Post a maker order to the SX.bet order book.
        """
        order = command.order
        instrument_id = order.instrument_id

        # Get instrument
        instrument = self._instrument_provider.find(instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {instrument_id}",
            )
            return

        # SX.bet orders use the market-level hash; event_id is fixture-level.
        market_hash = instrument.market_id or instrument.event_id

        # Determine outcome
        is_outcome_one = self._instrument_is_outcome_one(instrument)

        # Get price from order
        if order.order_type == OrderType.LIMIT and order.price:
            decimal_odds = float(order.price)
        else:
            decimal_odds = instrument.price

        # Convert to SX.bet format
        percentage_odds = decimal_odds_to_percentage(decimal_odds)
        stake_wei = to_wei(Decimal(str(order.quantity)), decimals=6)  # USDC has 6 decimals

        # Build order
        salt = generate_salt()
        expiry = get_expiry(hours=24)

        order_data = {
            "marketHash": market_hash,
            "maker": self._wallet_address,
            "totalBetSize": stake_wei,
            "percentageOdds": percentage_odds,
            "expiry": expiry,
            "baseToken": self._base_token,
            "salt": salt,
            "isMakerBettingOutcomeOne": is_outcome_one,
        }

        self._log.info(f"Submitting SX.bet order for {instrument_id}")

        try:
            # Sign the order
            signature = sign_eip712_order(
                order=order_data,
                private_key=self._private_key,
            )

            # Submit order event once the payload is signed and ready for venue submission.
            self._generate_order_submitted(order)

            if getattr(self._config, "dry_run", False):
                self._log.info(
                    "SX.bet dry-run execution enabled; order payload was signed but not submitted",
                )
                self._generate_order_rejected(order=order, reason="dry_run_no_submit")
                return

            # Place order via API
            result = await self._http_client.place_order(
                market_hash=market_hash,
                total_bet_size=stake_wei,
                percentage_odds=percentage_odds,
                expiry=expiry,
                salt=salt,
                is_maker_betting_outcome_one=is_outcome_one,
                signature=signature,
                base_token=self._base_token,
            )

            # Parse result
            order_hash = self._extract_order_hash(result)

            # Store order info
            self._orders[order.client_order_id] = {
                "order_hash": order_hash,
                "result": result,
            }
            venue_order_id = VenueOrderId(order_hash)
            self._venue_order_ids[order.client_order_id] = venue_order_id

            # Order accepted
            self._generate_order_accepted(order, venue_order_id)

        except (ImportError, ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to submit order: {e}"
            self._log.error(msg)
            self._generate_order_rejected(order=order, reason=str(e))

    async def _submit_taker_fill(self, command: SubmitOrder) -> None:
        """
        Fill displayed SX.bet order-book liquidity as a taker.
        """
        order = command.order
        instrument_id = order.instrument_id

        instrument = self._instrument_provider.find(instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {instrument_id}",
            )
            return

        market = instrument.market_id or instrument.event_id
        is_taker_betting_outcome_one = self._instrument_is_outcome_one(instrument)
        if order.order_type == OrderType.LIMIT and order.price:
            decimal_odds = float(order.price)
        else:
            decimal_odds = instrument.price
        desired_odds = decimal_odds_to_percentage(decimal_odds)
        stake_wei = to_wei(Decimal(str(order.quantity)), decimals=6)
        fill_salt = generate_salt()
        odds_slippage = int(getattr(self._config, "odds_slippage", 5))
        message = "Nautilus live arbitrage taker fill"

        fill_data = {
            "market": market,
            "taker": self._wallet_address,
            "baseToken": self._base_token,
            "isTakerBettingOutcomeOne": is_taker_betting_outcome_one,
            "stakeWei": stake_wei,
            "desiredOdds": desired_odds,
            "oddsSlippage": odds_slippage,
            "fillSalt": fill_salt,
            "message": message,
        }

        self._log.info(f"Filling SX.bet order-book liquidity for {instrument_id}")

        try:
            taker_sig = sign_eip712_fill_order(
                fill=fill_data,
                private_key=self._private_key,
            )
            self._generate_order_submitted(order)

            if getattr(self._config, "dry_run", False):
                self._log.info(
                    "SX.bet dry-run execution enabled; taker fill payload was signed "
                    "but not submitted",
                )
                self._generate_order_rejected(order=order, reason="dry_run_no_submit")
                return

            result = await self._http_client.fill_order(
                market=market,
                taker=self._wallet_address,
                base_token=self._base_token,
                is_taker_betting_outcome_one=is_taker_betting_outcome_one,
                stake_wei=stake_wei,
                desired_odds=desired_odds,
                odds_slippage=odds_slippage,
                taker_sig=taker_sig,
                fill_salt=fill_salt,
                message=message,
            )
            fill_hash = self._extract_fill_hash(result)
            self._orders[order.client_order_id] = {
                "fill_hash": fill_hash,
                "result": result,
            }
            venue_order_id = VenueOrderId(fill_hash)
            self._venue_order_ids[order.client_order_id] = venue_order_id
            self._generate_order_accepted(order, venue_order_id)

        except (ImportError, ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to fill order: {e}"
            self._log.error(msg)
            self._generate_order_rejected(order=order, reason=str(e))

    @staticmethod
    def _resolve_base_token(base_currency: str) -> str:
        normalized_currency = base_currency.upper()
        if normalized_currency != SXBetExecutionClient._SUPPORTED_BASE_CURRENCY:
            raise SXBetUnsupportedBaseCurrencyError(base_currency)
        return SXBET_TOKENS[normalized_currency]

    @staticmethod
    def _validate_config(config: SXBetExecClientConfig) -> None:
        required_fields = {
            "api_key": config.api_key,
            "private_key": config.private_key,
            "wallet_address": config.wallet_address,
        }
        for field_name, value in required_fields.items():
            if not isinstance(value, str) or not value.strip():
                raise SXBetInvalidConfigError(field_name, "must be a non-empty string")

        SXBetExecutionClient._normalize_wallet_address(config.wallet_address)
        SXBetExecutionClient._normalize_private_key(config.private_key)
        execution_mode = getattr(config, "execution_mode", "maker_post")
        if execution_mode not in {"taker_fill", "maker_post"}:
            raise SXBetInvalidConfigError(
                "execution_mode",
                "must be 'taker_fill' or 'maker_post'",
            )
        odds_slippage = int(getattr(config, "odds_slippage", 5))
        if odds_slippage < 0 or odds_slippage > 100:
            raise SXBetInvalidConfigError(
                "odds_slippage",
                "must be between 0 and 100",
            )

    @staticmethod
    def _normalize_wallet_address(wallet_address: str) -> str:
        normalized = wallet_address.strip()
        if normalized.startswith("0X"):
            normalized = f"0x{normalized[2:]}"
        if not normalized.startswith("0x") and re.fullmatch(r"[0-9a-fA-F]{40}", normalized):
            normalized = f"0x{normalized}"
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", normalized):
            raise SXBetInvalidConfigError(
                "wallet_address",
                "must be a 42-character 0x-prefixed address",
            )
        return normalized

    @staticmethod
    def _normalize_private_key(private_key: str) -> str:
        normalized = private_key.strip()
        if normalized.startswith("0X"):
            normalized = f"0x{normalized[2:]}"
        if not normalized.startswith("0x") and re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
            normalized = f"0x{normalized}"
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", normalized):
            raise SXBetInvalidConfigError(
                "private_key",
                "must be a 66-character 0x-prefixed private key",
            )
        return normalized

    @staticmethod
    def _extract_order_hash(result: dict) -> str:
        order_hash = result.get("data", {}).get("orderHash")
        if not isinstance(order_hash, str) or not order_hash.strip():
            raise SXBetMissingOrderHashError
        return order_hash

    @staticmethod
    def _extract_fill_hash(result: dict) -> str:
        fill_hash = result.get("data", {}).get("fillHash")
        if not isinstance(fill_hash, str) or not fill_hash.strip():
            raise SXBetMissingFillHashError
        return fill_hash

    @staticmethod
    def _instrument_is_outcome_one(instrument: CryptoBettingInstrument) -> bool:
        info = getattr(instrument, "info", None)
        if isinstance(info, dict) and "outcome_one" in info:
            return bool(info["outcome_one"])

        outcome = Outcome.from_string(instrument.outcome)
        if outcome in {Outcome.HOME, Outcome.OVER, Outcome.YES}:
            return True
        if outcome in {Outcome.AWAY, Outcome.UNDER, Outcome.NO}:
            return False

        params = instrument.params or ""
        if not isinstance(params, str):
            params = str(params)
        return "outcome_one=True" in params

    def _resolved_account_id(self) -> AccountId:
        account_id = self.account_id
        if account_id is None:  # pragma: no cover - constructor sets this defensively
            raise RuntimeError("SX.bet execution account_id is not initialized")
        return account_id

    def _generate_order_submitted(self, order: Order) -> None:
        """
        Generate and send order submitted event.
        """
        account_id = self._resolved_account_id()
        event = OrderSubmitted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_accepted(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
    ) -> None:
        """
        Generate and send order accepted event.
        """
        account_id = self._resolved_account_id()
        event = OrderAccepted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            account_id=account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_rejected(
        self,
        order: Order,
        reason: str,
    ) -> None:
        """
        Generate and send order rejected event.
        """
        account_id = self._resolved_account_id()
        event = OrderRejected(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=account_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_canceled(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
    ) -> None:
        """
        Generate and send order canceled event.
        """
        account_id = self._resolved_account_id()
        event = OrderCanceled(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            account_id=account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _send_order_event(self, event: OrderEvent) -> None:
        """
        Send an order event via the message bus.
        """
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)

    async def _cancel_order(self, command: CancelOrder) -> None:
        """
        Cancel an order.
        """
        client_order_id = command.client_order_id

        if client_order_id not in self._orders:
            msg = f"Order not found: {client_order_id}"
            self._log.warning(msg)
            return

        order_info = self._orders[client_order_id]
        order_hash = order_info.get("order_hash")
        if order_hash is None:
            msg = f"Order hash missing for {client_order_id}"
            self._log.error(msg)
            return

        try:
            await self._http_client.cancel_order(order_hash)

            order = self._cache.order(client_order_id)
            if order:
                venue_order_id = self._venue_order_ids.get(client_order_id)
                if venue_order_id:
                    self._generate_order_canceled(order, venue_order_id)

        except (ValueError, TypeError, SXBetHttpClientError) as e:
            msg = f"Failed to cancel order: {e}"
            self._log.error(msg)

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        """
        Cancel all locally tracked SX.bet maker orders.
        """
        cancelable = tuple(
            (client_order_id, order_info.get("order_hash"))
            for client_order_id, order_info in self._orders.items()
            if order_info.get("order_hash")
        )
        for client_order_id, order_hash in cancelable:
            try:
                await self._http_client.cancel_order(str(order_hash))
            except (ValueError, TypeError, SXBetHttpClientError) as e:
                self._log.error(f"Failed to cancel SX.bet order {client_order_id}: {e}")
                continue

            order = self._cache.order(client_order_id)
            venue_order_id = self._venue_order_ids.get(client_order_id)
            if order and venue_order_id:
                self._generate_order_canceled(order, venue_order_id)

    async def _modify_order(self, command: ModifyOrder) -> None:
        """
        Modify an order (cancel and replace).
        """
        # SX.bet doesn't support order modification, must cancel and replace
        self._log.warning(
            f"Order modification requires cancel/replace on SX.bet: {command.client_order_id}",
        )

    def _resolve_status_query(
        self,
        command: GenerateOrderStatusReport,
    ) -> tuple[VenueOrderId | None, Order | None, InstrumentId | None]:
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id
        instrument_id = command.instrument_id
        cached_order = self._cache.order(client_order_id) if client_order_id is not None else None

        if venue_order_id is None and client_order_id is not None:
            venue_order_id = self._venue_order_ids.get(client_order_id)
        if instrument_id is None and cached_order is not None:
            instrument_id = cached_order.instrument_id

        return venue_order_id, cached_order, instrument_id

    @staticmethod
    def _map_order_status(status: str) -> OrderStatus:
        status = status.upper()
        if status == "ACTIVE":
            return OrderStatus.ACCEPTED
        if status == "FILLED":
            return OrderStatus.FILLED
        if status == "PARTIALLY_FILLED":
            return OrderStatus.PARTIALLY_FILLED
        if status in {"CANCELLED", "EXPIRED"}:
            return OrderStatus.CANCELED
        return OrderStatus.SUBMITTED

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """
        Generate an order status report.
        """
        venue_order_id, cached_order, instrument_id = self._resolve_status_query(command)
        if venue_order_id is None:
            self._log.warning(
                "Cannot query SX.bet order status without a venue order id "
                f"(client_order_id={command.client_order_id})",
            )
            return None

        if instrument_id is None:
            self._log.warning(
                "Cannot build SX.bet order status report without an instrument id "
                f"(venue_order_id={venue_order_id})",
            )
            return None

        try:
            orders = await self._http_client.get_user_orders(
                self._wallet_address,
            )

            instrument = self._instrument_provider.find(instrument_id)
            for order_data in orders.get("data", {}).get("orders", []):
                if order_data.get("orderHash") != str(venue_order_id):
                    continue

                filled_qty = self._make_qty(
                    instrument,
                    from_wei(self._matched_wei(order_data), decimals=SXBET_USDC.precision),
                )
                quantity = self._status_quantity(instrument, order_data, cached_order)
                if filled_qty > quantity:
                    quantity = filled_qty
                # SX.bet returns "orderStatus"; older code read "status".
                raw_status = order_data.get("orderStatus") or order_data.get("status", "")

                return OrderStatusReport(
                    account_id=self._resolved_account_id(),
                    instrument_id=instrument_id,
                    venue_order_id=venue_order_id,
                    client_order_id=command.client_order_id,
                    order_side=cached_order.side if cached_order else OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    order_status=self._map_order_status(raw_status),
                    quantity=quantity,
                    filled_qty=filled_qty,
                    report_id=UUID4(),
                    ts_accepted=self._clock.timestamp_ns(),
                    ts_last=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                )

        except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to get order status: {e}"
            self._log.error(msg)

        return None

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        """
        Generate order status reports for locally tracked SX.bet orders.

        Nautilus calls this during startup reconciliation before this client has
        necessarily submitted any orders in the current process. SX.bet does not expose
        a safe unbounded "all order statuses for this account" query for our startup
        path, so an empty local order set is a valid no-op.

        """
        instrument_id = command.instrument_id
        if command.start is not None or command.end is not None:
            self._log.debug(
                "SX.bet bulk order status reconciliation ignores time range filters "
                "and reports locally tracked orders only",
            )

        if instrument_id is not None:
            client_order_ids = list(
                self._cache.client_order_ids(venue=SXBET_VENUE, instrument_id=instrument_id),
            )
        else:
            client_order_ids = list(self._venue_order_ids)

        if not client_order_ids:
            self._log.debug(
                "No locally tracked SX.bet orders available for bulk status reconciliation",
            )
            return []

        reports: list[OrderStatusReport] = []
        for client_order_id in client_order_ids:
            cached_order = self._cache.order(client_order_id)
            venue_order_id = self._venue_order_ids.get(client_order_id)
            if venue_order_id is None and cached_order is not None:
                venue_order_id = cached_order.venue_order_id
            if venue_order_id is None:
                self._log.warning(
                    "Cannot query SX.bet order status without a venue order id "
                    f"(client_order_id={client_order_id})",
                )
                continue

            report = await self.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=instrument_id
                    or (cached_order.instrument_id if cached_order is not None else None),
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    command_id=UUID4(),
                    ts_init=self._clock.timestamp_ns(),
                    params=command.params,
                ),
            )
            if report is None:
                continue
            if command.open_only and report.order_status not in {
                OrderStatus.SUBMITTED,
                OrderStatus.ACCEPTED,
            }:
                continue
            reports.append(report)

        return reports

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        """
        Generate SX.bet fill reports from the authenticated user trades feed.

        Reports are bounded to locally tracked orders (matched by ``orderHash``). During
        startup reconciliation, before this process has submitted any orders and without
        a ``venue_order_id`` filter, there is nothing to reconcile, so an empty list is
        returned without querying the venue.

        """
        venue_filter = command.venue_order_id
        if not self._venue_order_ids and venue_filter is None:
            self._log.debug(
                "No locally tracked SX.bet orders for fill reconciliation; returning none",
            )
            return []

        by_hash = {
            str(venue_order_id): client_order_id
            for client_order_id, venue_order_id in self._venue_order_ids.items()
        }
        trades = await self._http_client.get_user_trades(self._wallet_address)
        reports: list[FillReport] = []
        for trade in trades.get("data", {}).get("trades", []):
            order_hash = trade.get("orderHash")
            if order_hash is None:
                continue
            venue_order_id = VenueOrderId(str(order_hash))
            if venue_filter is not None and venue_order_id != venue_filter:
                continue
            client_order_id = by_hash.get(str(order_hash))
            if client_order_id is None and venue_filter is None:
                continue

            report = self._build_fill_report(command, trade, venue_order_id, client_order_id)
            if report is not None:
                reports.append(report)

        return reports

    def _build_fill_report(
        self,
        command: GenerateFillReports,
        trade: dict,
        venue_order_id: VenueOrderId,
        client_order_id: ClientOrderId | None,
    ) -> FillReport | None:
        cached_order = self._cache.order(client_order_id) if client_order_id is not None else None
        instrument_id = command.instrument_id
        if instrument_id is None and cached_order is not None:
            instrument_id = cached_order.instrument_id
        if instrument_id is None:
            return None

        instrument = self._instrument_provider.find(instrument_id)
        try:
            stake_wei = int(trade.get("stake", 0) or 0)
        except (TypeError, ValueError):
            stake_wei = 0
        if stake_wei <= 0:
            return None

        last_qty = self._make_qty(instrument, from_wei(stake_wei, decimals=SXBET_USDC.precision))
        odds = trade.get("odds")
        try:
            decimal_odds = percentage_to_decimal_odds(int(odds)) if odds is not None else 0.0
        except (TypeError, ValueError):
            decimal_odds = 0.0
        last_px = (
            instrument.make_price(decimal_odds)
            if isinstance(instrument, CryptoBettingInstrument)
            else Price(decimal_odds, precision=_SXBET_BETTING_PRECISION)
        )
        order_side = cached_order.side if cached_order is not None else OrderSide.BUY
        fill_hash = trade.get("fillHash") or f"{venue_order_id.value}-{stake_wei}"
        liquidity_side = (
            LiquiditySide.MAKER if trade.get("maker") is True else self._liquidity_side()
        )

        return FillReport(
            account_id=self._resolved_account_id(),
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            trade_id=TradeId(str(fill_hash)),
            order_side=order_side,
            last_qty=last_qty,
            last_px=last_px,
            commission=Money(0, SXBET_USDC),
            liquidity_side=liquidity_side,
            report_id=UUID4(),
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        """
        Generate SX.bet position status reports.

        The live arbitrage pilot does not reconstruct SX.bet positions from account
        history during startup. Orders submitted by this process are tracked via order
        status reports and order lifecycle events.

        """
        self._log.debug(
            "SX.bet position status reconciliation is local-only; returning no reports "
            f"(instrument_id={command.instrument_id})",
        )
        return []
