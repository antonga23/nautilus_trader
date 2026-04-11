// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

//! Parsing utilities for Binance API responses.
//!
//! Provides conversion functions to transform raw Binance exchange data
//! into Nautilus domain objects such as instruments and market data.

use std::str::FromStr;

use anyhow::Context;
use nautilus_core::nanos::UnixNanos;
use nautilus_model::{
    data::TradeTick,
    enums::{
        AggressorSide, LiquiditySide, OrderSide, OrderStatus, OrderType, TimeInForce, TriggerType,
    },
    identifiers::{
        AccountId, ClientOrderId, InstrumentId, OrderListId, Symbol, TradeId, Venue, VenueOrderId,
    },
    instruments::{
        Instrument, any::InstrumentAny, crypto_perpetual::CryptoPerpetual,
        currency_pair::CurrencyPair,
    },
    reports::{FillReport, OrderStatusReport},
    types::{Currency, Money, Price, Quantity},
};
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde_json::Value;

use crate::{
    common::{
        enums::BinanceTradingStatus,
        sbe::spot::{
            order_side::OrderSide as SbeOrderSide, order_status::OrderStatus as SbeOrderStatus,
            order_type::OrderType as SbeOrderType, time_in_force::TimeInForce as SbeTimeInForce,
        },
    },
    http::models::{BinanceFuturesUsdSymbol, BinanceSpotSymbol},
    spot::http::models::{
        BinanceAccountTrade, BinanceNewOrderResponse, BinanceOrderResponse, BinanceSymbolSbe,
        BinanceTrades,
    },
};

const BINANCE_VENUE: &str = "BINANCE";
const CONTRACT_TYPE_PERPETUAL: &str = "PERPETUAL";

/// Returns a currency from the internal map or creates a new crypto currency.
pub fn get_currency(code: &str) -> Currency {
    Currency::get_or_create_crypto(code)
}

/// Extracts filter values from Binance symbol filters array.
fn get_filter<'a>(filters: &'a [Value], filter_type: &str) -> Option<&'a Value> {
    filters.iter().find(|f| {
        f.get("filterType")
            .and_then(|v| v.as_str())
            .is_some_and(|t| t == filter_type)
    })
}

/// Parses a string field from a JSON value.
fn parse_filter_string(filter: &Value, field: &str) -> anyhow::Result<String> {
    filter
        .get(field)
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| anyhow::anyhow!("Missing field '{field}' in filter"))
}

/// Parses a Price from a filter field.
fn parse_filter_price(filter: &Value, field: &str) -> anyhow::Result<Price> {
    let value = parse_filter_string(filter, field)?;
    Price::from_str(&value).map_err(|e| anyhow::anyhow!("Failed to parse {field}='{value}': {e}"))
}

/// Parses a Quantity from a filter field.
fn parse_filter_quantity(filter: &Value, field: &str) -> anyhow::Result<Quantity> {
    let value = parse_filter_string(filter, field)?;
    Quantity::from_str(&value)
        .map_err(|e| anyhow::anyhow!("Failed to parse {field}='{value}': {e}"))
}

/// Parses a USD-M Futures symbol definition into a Nautilus CryptoPerpetual instrument.
///
/// # Errors
///
/// Returns an error if:
/// - Required filter values are missing (PRICE_FILTER, LOT_SIZE).
/// - Price or quantity values cannot be parsed.
/// - The contract type is not PERPETUAL.
pub fn parse_usdm_instrument(
    symbol: &BinanceFuturesUsdSymbol,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    // Only handle perpetual contracts for now
    if symbol.contract_type != CONTRACT_TYPE_PERPETUAL {
        anyhow::bail!(
            "Unsupported contract type '{}' for symbol '{}', expected '{}'",
            symbol.contract_type,
            symbol.symbol,
            CONTRACT_TYPE_PERPETUAL
        );
    }

    let base_currency = get_currency(symbol.base_asset.as_str());
    let quote_currency = get_currency(symbol.quote_asset.as_str());
    let settlement_currency = get_currency(symbol.margin_asset.as_str());

    let instrument_id = InstrumentId::new(
        Symbol::from_str_unchecked(format!("{}-PERP", symbol.symbol)),
        Venue::new(BINANCE_VENUE),
    );
    let raw_symbol = Symbol::new(symbol.symbol.as_str());

    let price_filter = get_filter(&symbol.filters, "PRICE_FILTER")
        .context("Missing PRICE_FILTER in symbol filters")?;

    let tick_size = parse_filter_price(price_filter, "tickSize")?;
    let max_price = parse_filter_price(price_filter, "maxPrice").ok();
    let min_price = parse_filter_price(price_filter, "minPrice").ok();

    let lot_filter =
        get_filter(&symbol.filters, "LOT_SIZE").context("Missing LOT_SIZE in symbol filters")?;

    let step_size = parse_filter_quantity(lot_filter, "stepSize")?;
    let max_quantity = parse_filter_quantity(lot_filter, "maxQty").ok();
    let min_quantity = parse_filter_quantity(lot_filter, "minQty").ok();

    // Default margin (0.1 = 10x leverage)
    let default_margin = Decimal::new(1, 1);

    let instrument = CryptoPerpetual::new(
        instrument_id,
        raw_symbol,
        base_currency,
        quote_currency,
        settlement_currency,
        false, // is_inverse
        tick_size.precision,
        step_size.precision,
        tick_size,
        step_size,
        None, // multiplier
        Some(step_size),
        max_quantity,
        min_quantity,
        None, // max_notional
        None, // min_notional
        max_price,
        min_price,
        Some(default_margin),
        Some(default_margin),
        None, // maker_fee
        None, // taker_fee
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::CryptoPerpetual(instrument))
}

/// SBE status value for Trading.
const SBE_STATUS_TRADING: u8 = 0;

/// Parses a Binance Spot SBE symbol into a Nautilus CurrencyPair instrument.
///
/// # Errors
///
/// Returns an error if:
/// - Required filter values are missing (PRICE_FILTER, LOT_SIZE).
/// - Price or quantity values cannot be parsed.
/// - The symbol is not actively trading.
pub fn parse_spot_instrument_sbe(
    symbol: &BinanceSymbolSbe,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    if symbol.status != SBE_STATUS_TRADING {
        anyhow::bail!(
            "Symbol '{}' is not trading (status: {})",
            symbol.symbol,
            symbol.status
        );
    }

    let base_currency = get_currency(&symbol.base_asset);
    let quote_currency = get_currency(&symbol.quote_asset);

    let instrument_id = InstrumentId::new(
        Symbol::from_str_unchecked(&symbol.symbol),
        Venue::new(BINANCE_VENUE),
    );
    let raw_symbol = Symbol::new(&symbol.symbol);

    let price_filter = get_filter(&symbol.filters, "PRICE_FILTER")
        .context("Missing PRICE_FILTER in symbol filters")?;

    let tick_size = parse_filter_price(price_filter, "tickSize")?;
    let max_price = parse_filter_price(price_filter, "maxPrice").ok();
    let min_price = parse_filter_price(price_filter, "minPrice").ok();

    let lot_filter =
        get_filter(&symbol.filters, "LOT_SIZE").context("Missing LOT_SIZE in symbol filters")?;

    let step_size = parse_filter_quantity(lot_filter, "stepSize")?;
    let max_quantity = parse_filter_quantity(lot_filter, "maxQty").ok();
    let min_quantity = parse_filter_quantity(lot_filter, "minQty").ok();

    // Spot has no leverage, use 1.0 margin
    let default_margin = Decimal::new(1, 0);

    let instrument = CurrencyPair::new(
        instrument_id,
        raw_symbol,
        base_currency,
        quote_currency,
        tick_size.precision,
        step_size.precision,
        tick_size,
        step_size,
        None, // multiplier
        Some(step_size),
        max_quantity,
        min_quantity,
        None, // max_notional
        None, // min_notional
        max_price,
        min_price,
        Some(default_margin),
        Some(default_margin),
        None, // maker_fee
        None, // taker_fee
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::CurrencyPair(instrument))
}

/// Parses a Binance Spot symbol definition into a Nautilus CurrencyPair instrument.
///
/// # Errors
///
/// Returns an error if:
/// - Required filter values are missing (PRICE_FILTER, LOT_SIZE).
/// - Price or quantity values cannot be parsed.
/// - The symbol is not actively trading.
pub fn parse_spot_instrument(
    symbol: &BinanceSpotSymbol,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    if symbol.status != BinanceTradingStatus::Trading {
        anyhow::bail!(
            "Symbol '{}' is not trading (status: {:?})",
            symbol.symbol,
            symbol.status
        );
    }

    let base_currency = get_currency(symbol.base_asset.as_str());
    let quote_currency = get_currency(symbol.quote_asset.as_str());

    let instrument_id = InstrumentId::new(
        Symbol::from_str_unchecked(symbol.symbol.as_str()),
        Venue::new(BINANCE_VENUE),
    );
    let raw_symbol = Symbol::new(symbol.symbol.as_str());

    let price_filter = get_filter(&symbol.filters, "PRICE_FILTER")
        .context("Missing PRICE_FILTER in symbol filters")?;

    let tick_size = parse_filter_price(price_filter, "tickSize")?;
    let max_price = parse_filter_price(price_filter, "maxPrice").ok();
    let min_price = parse_filter_price(price_filter, "minPrice").ok();

    let lot_filter =
        get_filter(&symbol.filters, "LOT_SIZE").context("Missing LOT_SIZE in symbol filters")?;

    let step_size = parse_filter_quantity(lot_filter, "stepSize")?;
    let max_quantity = parse_filter_quantity(lot_filter, "maxQty").ok();
    let min_quantity = parse_filter_quantity(lot_filter, "minQty").ok();

    // Spot has no leverage, use 1.0 margin
    let default_margin = Decimal::new(1, 0);

    let instrument = CurrencyPair::new(
        instrument_id,
        raw_symbol,
        base_currency,
        quote_currency,
        tick_size.precision,
        step_size.precision,
        tick_size,
        step_size,
        None, // multiplier
        Some(step_size),
        max_quantity,
        min_quantity,
        None, // max_notional
        None, // min_notional
        max_price,
        min_price,
        Some(default_margin),
        Some(default_margin),
        None, // maker_fee
        None, // taker_fee
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::CurrencyPair(instrument))
}

/// Parses Binance SBE trades into Nautilus TradeTick objects.
///
/// Uses mantissa/exponent encoding from SBE to construct proper Price and Quantity.
///
/// # Errors
///
/// Returns an error if any trade cannot be parsed.
pub fn parse_spot_trades_sbe(
    trades: &BinanceTrades,
    instrument: &InstrumentAny,
    ts_init: UnixNanos,
) -> anyhow::Result<Vec<TradeTick>> {
    let instrument_id = instrument.id();
    let price_precision = instrument.price_precision();
    let size_precision = instrument.size_precision();

    let mut result = Vec::with_capacity(trades.trades.len());

    for trade in &trades.trades {
        // Convert mantissa + exponent to decimal value
        let price_exp = trades.price_exponent as i32;
        let qty_exp = trades.qty_exponent as i32;

        let price_dec = Decimal::new(trade.price_mantissa, (-price_exp) as u32);
        let qty_dec = Decimal::new(trade.qty_mantissa, (-qty_exp) as u32);

        let price = Price::new(price_dec.to_f64().unwrap_or(0.0), price_precision);
        let size = Quantity::new(qty_dec.to_f64().unwrap_or(0.0), size_precision);

        // is_buyer_maker means the buyer was the maker, so the aggressor was selling
        let aggressor_side = if trade.is_buyer_maker {
            AggressorSide::Seller
        } else {
            AggressorSide::Buyer
        };

        // SBE trade timestamps are in microseconds
        let ts_event = UnixNanos::from(trade.time as u64 * 1_000);

        let tick = TradeTick::new(
            instrument_id,
            price,
            size,
            aggressor_side,
            TradeId::new(trade.id.to_string()),
            ts_event,
            ts_init,
        );

        result.push(tick);
    }

    Ok(result)
}

/// Maps Binance SBE order status to Nautilus order status.
#[must_use]
pub const fn map_order_status_sbe(status: SbeOrderStatus) -> OrderStatus {
    match status {
        SbeOrderStatus::New => OrderStatus::Accepted,
        SbeOrderStatus::PendingNew => OrderStatus::Submitted,
        SbeOrderStatus::PartiallyFilled => OrderStatus::PartiallyFilled,
        SbeOrderStatus::Filled => OrderStatus::Filled,
        SbeOrderStatus::Canceled => OrderStatus::Canceled,
        SbeOrderStatus::PendingCancel => OrderStatus::PendingCancel,
        SbeOrderStatus::Rejected => OrderStatus::Rejected,
        SbeOrderStatus::Expired | SbeOrderStatus::ExpiredInMatch => OrderStatus::Expired,
        SbeOrderStatus::Unknown | SbeOrderStatus::NonRepresentable | SbeOrderStatus::NullVal => {
            OrderStatus::Initialized
        }
    }
}

/// Maps Binance SBE order type to Nautilus order type.
#[must_use]
pub const fn map_order_type_sbe(order_type: SbeOrderType) -> OrderType {
    match order_type {
        SbeOrderType::Market => OrderType::Market,
        SbeOrderType::Limit | SbeOrderType::LimitMaker => OrderType::Limit,
        SbeOrderType::StopLoss | SbeOrderType::TakeProfit => OrderType::StopMarket,
        SbeOrderType::StopLossLimit | SbeOrderType::TakeProfitLimit => OrderType::StopLimit,
        SbeOrderType::NonRepresentable | SbeOrderType::NullVal => OrderType::Market,
    }
}

/// Maps Binance SBE order side to Nautilus order side.
#[must_use]
pub const fn map_order_side_sbe(side: SbeOrderSide) -> OrderSide {
    match side {
        SbeOrderSide::Buy => OrderSide::Buy,
        SbeOrderSide::Sell => OrderSide::Sell,
        SbeOrderSide::NonRepresentable | SbeOrderSide::NullVal => OrderSide::NoOrderSide,
    }
}

/// Maps Binance SBE time in force to Nautilus time in force.
#[must_use]
pub const fn map_time_in_force_sbe(tif: SbeTimeInForce) -> TimeInForce {
    match tif {
        SbeTimeInForce::Gtc => TimeInForce::Gtc,
        SbeTimeInForce::Ioc => TimeInForce::Ioc,
        SbeTimeInForce::Fok => TimeInForce::Fok,
        SbeTimeInForce::NonRepresentable | SbeTimeInForce::NullVal => TimeInForce::Gtc,
    }
}

/// Parses a Binance SBE order response into a Nautilus `OrderStatusReport`.
///
/// # Errors
///
/// Returns an error if any field cannot be parsed.
#[allow(clippy::too_many_arguments)]
pub fn parse_order_status_report_sbe(
    order: &BinanceOrderResponse,
    account_id: AccountId,
    instrument: &InstrumentAny,
    ts_init: UnixNanos,
) -> anyhow::Result<OrderStatusReport> {
    let instrument_id = instrument.id();
    let price_precision = instrument.price_precision();
    let size_precision = instrument.size_precision();

    // Convert mantissa + exponent to values
    let price_exp = order.price_exponent as i32;
    let qty_exp = order.qty_exponent as i32;

    let price_dec = Decimal::new(order.price_mantissa, (-price_exp) as u32);
    let qty_dec = Decimal::new(order.orig_qty_mantissa, (-qty_exp) as u32);
    let filled_dec = Decimal::new(order.executed_qty_mantissa, (-qty_exp) as u32);

    let price = if order.price_mantissa != 0 {
        Some(Price::new(
            price_dec.to_f64().unwrap_or(0.0),
            price_precision,
        ))
    } else {
        None
    };

    let quantity = Quantity::new(qty_dec.to_f64().unwrap_or(0.0), size_precision);
    let filled_qty = Quantity::new(filled_dec.to_f64().unwrap_or(0.0), size_precision);

    // Calculate average price from cumulative quote qty / executed qty
    // Quote qty = price * qty, so its exponent is (price_exp + qty_exp)
    let avg_px = if order.executed_qty_mantissa > 0 {
        let quote_exp = price_exp + qty_exp;
        let cum_quote_dec = Decimal::new(order.cummulative_quote_qty_mantissa, (-quote_exp) as u32);
        let avg_dec = cum_quote_dec / filled_dec;
        Some(Price::new(avg_dec.to_f64().unwrap_or(0.0), price_precision))
    } else {
        None
    };

    // Parse trigger price for stop orders
    let trigger_price = order.stop_price_mantissa.and_then(|mantissa| {
        if mantissa != 0 {
            let stop_dec = Decimal::new(mantissa, (-price_exp) as u32);
            Some(Price::new(
                stop_dec.to_f64().unwrap_or(0.0),
                price_precision,
            ))
        } else {
            None
        }
    });

    // Map enums
    let order_status = map_order_status_sbe(order.status);
    let order_type = map_order_type_sbe(order.order_type);
    let order_side = map_order_side_sbe(order.side);
    let time_in_force = map_time_in_force_sbe(order.time_in_force);

    // Determine trigger type for stop orders
    let trigger_type = if trigger_price.is_some() {
        Some(TriggerType::LastPrice)
    } else {
        None
    };

    // Parse timestamps (SBE uses microseconds)
    let ts_event = UnixNanos::from(order.update_time as u64 * 1000);

    // Build order list ID if present
    let order_list_id = order.order_list_id.and_then(|id| {
        if id > 0 {
            Some(OrderListId::new(id.to_string()))
        } else {
            None
        }
    });

    // Determine post-only (limit maker orders are post-only)
    let post_only = order.order_type == SbeOrderType::LimitMaker;

    // Parse order creation time (SBE uses microseconds)
    let ts_accepted = UnixNanos::from(order.time as u64 * 1000);

    let mut report = OrderStatusReport::new(
        account_id,
        instrument_id,
        Some(ClientOrderId::new(order.client_order_id.clone())),
        VenueOrderId::new(order.order_id.to_string()),
        order_side,
        order_type,
        time_in_force,
        order_status,
        quantity,
        filled_qty,
        ts_accepted,
        ts_event,
        ts_init,
        None, // report_id (auto-generated)
    );

    // Apply optional fields using builder methods
    if let Some(p) = price {
        report = report.with_price(p);
    }
    if let Some(ap) = avg_px {
        report = report.with_avg_px(ap.as_f64())?;
    }
    if let Some(tp) = trigger_price {
        report = report.with_trigger_price(tp);
    }
    if let Some(tt) = trigger_type {
        report = report.with_trigger_type(tt);
    }
    if let Some(oli) = order_list_id {
        report = report.with_order_list_id(oli);
    }
    if post_only {
        report = report.with_post_only(true);
    }

    Ok(report)
}

/// Parses a Binance new order response (SBE) into a Nautilus `OrderStatusReport`.
///
/// # Errors
///
/// Returns an error if any field cannot be parsed.
pub fn parse_new_order_response_sbe(
    response: &BinanceNewOrderResponse,
    account_id: AccountId,
    instrument: &InstrumentAny,
    ts_init: UnixNanos,
) -> anyhow::Result<OrderStatusReport> {
    let instrument_id = instrument.id();
    let price_precision = instrument.price_precision();
    let size_precision = instrument.size_precision();

    let price_exp = response.price_exponent as i32;
    let qty_exp = response.qty_exponent as i32;

    let price_dec = Decimal::new(response.price_mantissa, (-price_exp) as u32);
    let qty_dec = Decimal::new(response.orig_qty_mantissa, (-qty_exp) as u32);
    let filled_dec = Decimal::new(response.executed_qty_mantissa, (-qty_exp) as u32);

    let price = if response.price_mantissa != 0 {
        Some(Price::new(
            price_dec.to_f64().unwrap_or(0.0),
            price_precision,
        ))
    } else {
        None
    };

    let quantity = Quantity::new(qty_dec.to_f64().unwrap_or(0.0), size_precision);
    let filled_qty = Quantity::new(filled_dec.to_f64().unwrap_or(0.0), size_precision);

    // Quote qty = price * qty, so exponent is (price_exp + qty_exp)
    let avg_px = if response.executed_qty_mantissa > 0 {
        let quote_exp = price_exp + qty_exp;
        let cum_quote_dec =
            Decimal::new(response.cummulative_quote_qty_mantissa, (-quote_exp) as u32);
        let avg_dec = cum_quote_dec / filled_dec;
        Some(Price::new(avg_dec.to_f64().unwrap_or(0.0), price_precision))
    } else {
        None
    };

    let trigger_price = response.stop_price_mantissa.and_then(|mantissa| {
        if mantissa != 0 {
            let stop_dec = Decimal::new(mantissa, (-price_exp) as u32);
            Some(Price::new(
                stop_dec.to_f64().unwrap_or(0.0),
                price_precision,
            ))
        } else {
            None
        }
    });

    let order_status = map_order_status_sbe(response.status);
    let order_type = map_order_type_sbe(response.order_type);
    let order_side = map_order_side_sbe(response.side);
    let time_in_force = map_time_in_force_sbe(response.time_in_force);

    let trigger_type = if trigger_price.is_some() {
        Some(TriggerType::LastPrice)
    } else {
        None
    };

    // SBE uses microseconds; for new orders transact_time is both creation and event time
    let ts_event = UnixNanos::from(response.transact_time as u64 * 1000);
    let ts_accepted = ts_event;

    let order_list_id = response.order_list_id.and_then(|id| {
        if id > 0 {
            Some(OrderListId::new(id.to_string()))
        } else {
            None
        }
    });

    // Limit maker orders are post-only
    let post_only = response.order_type == SbeOrderType::LimitMaker;

    let mut report = OrderStatusReport::new(
        account_id,
        instrument_id,
        Some(ClientOrderId::new(response.client_order_id.clone())),
        VenueOrderId::new(response.order_id.to_string()),
        order_side,
        order_type,
        time_in_force,
        order_status,
        quantity,
        filled_qty,
        ts_accepted,
        ts_event,
        ts_init,
        None,
    );

    if let Some(p) = price {
        report = report.with_price(p);
    }
    if let Some(ap) = avg_px {
        report = report.with_avg_px(ap.as_f64())?;
    }
    if let Some(tp) = trigger_price {
        report = report.with_trigger_price(tp);
    }
    if let Some(tt) = trigger_type {
        report = report.with_trigger_type(tt);
    }
    if let Some(oli) = order_list_id {
        report = report.with_order_list_id(oli);
    }
    if post_only {
        report = report.with_post_only(true);
    }

    Ok(report)
}

/// Parses a Binance SBE account trade into a Nautilus `FillReport`.
///
/// # Errors
///
/// Returns an error if any field cannot be parsed.
pub fn parse_fill_report_sbe(
    trade: &BinanceAccountTrade,
    account_id: AccountId,
    instrument: &InstrumentAny,
    commission_currency: Currency,
    ts_init: UnixNanos,
) -> anyhow::Result<FillReport> {
    let instrument_id = instrument.id();
    let price_precision = instrument.price_precision();
    let size_precision = instrument.size_precision();

    // Convert mantissa + exponent to values
    let price_exp = trade.price_exponent as i32;
    let qty_exp = trade.qty_exponent as i32;
    let comm_exp = trade.commission_exponent as i32;

    let price_dec = Decimal::new(trade.price_mantissa, (-price_exp) as u32);
    let qty_dec = Decimal::new(trade.qty_mantissa, (-qty_exp) as u32);
    let comm_dec = Decimal::new(trade.commission_mantissa, (-comm_exp) as u32);

    let last_px = Price::new(price_dec.to_f64().unwrap_or(0.0), price_precision);
    let last_qty = Quantity::new(qty_dec.to_f64().unwrap_or(0.0), size_precision);
    let commission = Money::new(comm_dec.to_f64().unwrap_or(0.0), commission_currency);

    // Determine order side from is_buyer
    let order_side = if trade.is_buyer {
        OrderSide::Buy
    } else {
        OrderSide::Sell
    };

    // Determine liquidity side from is_maker
    let liquidity_side = if trade.is_maker {
        LiquiditySide::Maker
    } else {
        LiquiditySide::Taker
    };

    // Parse timestamp (SBE uses microseconds)
    let ts_event = UnixNanos::from(trade.time as u64 * 1000);

    Ok(FillReport::new(
        account_id,
        instrument_id,
        VenueOrderId::new(trade.order_id.to_string()),
        TradeId::new(trade.id.to_string()),
        order_side,
        last_qty,
        last_px,
        commission,
        liquidity_side,
        None, // client_order_id (not in account trades response)
        None, // venue_position_id
        ts_event,
        ts_init,
        None, // report_id
    ))
}

#[cfg(test)]
mod tests {
    use rstest::rstest;
    use serde_json::json;
    use ustr::Ustr;

    use super::*;
    use crate::http::models::BinanceSpotSymbol;

    fn sample_usdm_symbol() -> BinanceFuturesUsdSymbol {
        BinanceFuturesUsdSymbol {
            symbol: Ustr::from("BTCUSDT"),
            pair: Ustr::from("BTCUSDT"),
            contract_type: "PERPETUAL".to_string(),
            delivery_date: 4133404800000,
            onboard_date: 1569398400000,
            status: BinanceTradingStatus::Trading,
            maint_margin_percent: "2.5000".to_string(),
            required_margin_percent: "5.0000".to_string(),
            base_asset: Ustr::from("BTC"),
            quote_asset: Ustr::from("USDT"),
            margin_asset: Ustr::from("USDT"),
            price_precision: 2,
            quantity_precision: 3,
            base_asset_precision: 8,
            quote_precision: 8,
            underlying_type: Some("COIN".to_string()),
            underlying_sub_type: vec!["PoW".to_string()],
            settle_plan: None,
            trigger_protect: Some("0.0500".to_string()),
            liquidation_fee: Some("0.012500".to_string()),
            market_take_bound: Some("0.05".to_string()),
            order_types: vec!["LIMIT".to_string(), "MARKET".to_string()],
            time_in_force: vec!["GTC".to_string(), "IOC".to_string()],
            filters: vec![
                json!({
                    "filterType": "PRICE_FILTER",
                    "tickSize": "0.10",
                    "maxPrice": "4529764",
                    "minPrice": "556.80"
                }),
                json!({
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.001",
                    "maxQty": "1000",
                    "minQty": "0.001"
                }),
            ],
        }
    }

    #[rstest]
    fn test_parse_usdm_perpetual() {
        let symbol = sample_usdm_symbol();
        let ts = UnixNanos::from(1_700_000_000_000_000_000u64);

        let result = parse_usdm_instrument(&symbol, ts, ts);
        assert!(result.is_ok(), "Failed: {:?}", result.err());

        let instrument = result.unwrap();
        match instrument {
            InstrumentAny::CryptoPerpetual(perp) => {
                assert_eq!(perp.id.to_string(), "BTCUSDT-PERP.BINANCE");
                assert_eq!(perp.raw_symbol.to_string(), "BTCUSDT");
                assert_eq!(perp.base_currency.code.as_str(), "BTC");
                assert_eq!(perp.quote_currency.code.as_str(), "USDT");
                assert_eq!(perp.settlement_currency.code.as_str(), "USDT");
                assert!(!perp.is_inverse);
                assert_eq!(perp.price_increment, Price::from_str("0.10").unwrap());
                assert_eq!(perp.size_increment, Quantity::from_str("0.001").unwrap());
            }
            other => panic!("Expected CryptoPerpetual, got {other:?}"),
        }
    }

    #[rstest]
    fn test_parse_non_perpetual_fails() {
        let mut symbol = sample_usdm_symbol();
        symbol.contract_type = "CURRENT_QUARTER".to_string();
        let ts = UnixNanos::from(1_700_000_000_000_000_000u64);

        let result = parse_usdm_instrument(&symbol, ts, ts);
        assert!(result.is_err());
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("Unsupported contract type")
        );
    }

    #[rstest]
    fn test_parse_missing_price_filter_fails() {
        let mut symbol = sample_usdm_symbol();
        symbol.filters = vec![json!({
            "filterType": "LOT_SIZE",
            "stepSize": "0.001",
            "maxQty": "1000",
            "minQty": "0.001"
        })];
        let ts = UnixNanos::from(1_700_000_000_000_000_000u64);

        let result = parse_usdm_instrument(&symbol, ts, ts);
        assert!(result.is_err());
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("Missing PRICE_FILTER")
        );
    }

    fn sample_spot_symbol() -> BinanceSpotSymbol {
        BinanceSpotSymbol {
            symbol: Ustr::from("BTCUSDT"),
            status: BinanceTradingStatus::Trading,
            base_asset: Ustr::from("BTC"),
            base_asset_precision: 8,
            quote_asset: Ustr::from("USDT"),
            quote_precision: 8,
            quote_asset_precision: Some(8),
            order_types: vec!["LIMIT".to_string(), "MARKET".to_string()],
            iceberg_allowed: true,
            oco_allowed: Some(true),
            quote_order_qty_market_allowed: Some(true),
            allow_trailing_stop: Some(true),
            is_spot_trading_allowed: Some(true),
            is_margin_trading_allowed: Some(false),
            filters: vec![
                json!({
                    "filterType": "PRICE_FILTER",
                    "tickSize": "0.01",
                    "maxPrice": "1000000.00",
                    "minPrice": "0.01"
                }),
                json!({
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.00001",
                    "maxQty": "9000.00000",
                    "minQty": "0.00001"
                }),
            ],
            permissions: vec!["SPOT".to_string()],
            permission_sets: vec![],
            default_self_trade_prevention_mode: Some("EXPIRE_MAKER".to_string()),
            allowed_self_trade_prevention_modes: vec!["EXPIRE_MAKER".to_string()],
        }
    }

    #[rstest]
    fn test_parse_spot_instrument() {
        let symbol = sample_spot_symbol();
        let ts = UnixNanos::from(1_700_000_000_000_000_000u64);

        let result = parse_spot_instrument(&symbol, ts, ts);
        assert!(result.is_ok(), "Failed: {:?}", result.err());

        let instrument = result.unwrap();
        match instrument {
            InstrumentAny::CurrencyPair(pair) => {
                assert_eq!(pair.id.to_string(), "BTCUSDT.BINANCE");
                assert_eq!(pair.raw_symbol.to_string(), "BTCUSDT");
                assert_eq!(pair.base_currency.code.as_str(), "BTC");
                assert_eq!(pair.quote_currency.code.as_str(), "USDT");
                assert_eq!(pair.price_increment, Price::from_str("0.01").unwrap());
                assert_eq!(pair.size_increment, Quantity::from_str("0.00001").unwrap());
            }
            other => panic!("Expected CurrencyPair, got {other:?}"),
        }
    }

    #[rstest]
    fn test_parse_spot_non_trading_fails() {
        let mut symbol = sample_spot_symbol();
        symbol.status = BinanceTradingStatus::Break;
        let ts = UnixNanos::from(1_700_000_000_000_000_000u64);

        let result = parse_spot_instrument(&symbol, ts, ts);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("is not trading"));
    }
}
