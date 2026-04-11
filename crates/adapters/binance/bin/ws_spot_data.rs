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

//! Test binary for Binance Spot WebSocket SBE market data streams.
//!
//! Tests trades and quotes (best bid/ask) streams against the live Binance SBE endpoint.
//!
//! # Usage
//!
//! ```bash
//! cargo run --bin binance-spot-ws-data --package nautilus-binance
//! ```
//!
//! # Environment Variables
//!
//! Ed25519 authentication is **required** for SBE streams:
//! - `BINANCE_API_KEY`: Ed25519 API key (required)
//! - `BINANCE_API_SECRET`: Ed25519 private key in PEM format (required)

use futures_util::StreamExt;
use nautilus_binance::{
    common::{
        enums::{BinanceEnvironment, BinanceProductType},
        parse::parse_spot_instrument,
        sbe::stream::mantissa_to_f64,
    },
    http::client::{BinanceHttpClient, BinanceInstrument},
    spot::websocket::{
        client::BinanceSpotWebSocketClient,
        handler::{MarketDataMessage, decode_market_data},
        messages::NautilusWsMessage,
    },
};
use nautilus_core::nanos::UnixNanos;
use nautilus_model::{data::Data, instruments::InstrumentAny};
use tracing_subscriber::{EnvFilter, fmt, layer::SubscriberExt, util::SubscriberInitExt};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(fmt::layer())
        .with(EnvFilter::from_default_env().add_directive("info".parse()?))
        .init();

    // Read credentials from environment (required for SBE streams)
    let api_key = std::env::var("BINANCE_API_KEY").ok();
    let api_secret = std::env::var("BINANCE_API_SECRET").ok();

    if api_key.is_none() || api_secret.is_none() {
        tracing::error!("Ed25519 credentials required for Binance SBE streams");
        tracing::error!("Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables");
        anyhow::bail!("Missing required Ed25519 credentials");
    }
    tracing::info!("Using Ed25519 authentication for SBE streams");

    // Fetch instruments via HTTP (JSON API)
    tracing::info!("Fetching instruments from Binance Spot API...");
    let http_client = BinanceHttpClient::new(
        BinanceProductType::Spot,
        BinanceEnvironment::Mainnet,
        None, // api_key (not needed for public endpoints)
        None, // api_secret
        None, // base_url_override
        None, // recv_window
        None, // timeout_secs
        None, // proxy_url
    )?;
    http_client.exchange_info().await?;

    // Get cached symbols and parse to instruments
    let instruments = parse_spot_instruments(&http_client);
    tracing::info!(count = instruments.len(), "Parsed instruments");

    tracing::info!("Creating Binance Spot WebSocket client...");
    let mut ws_client = BinanceSpotWebSocketClient::new(
        None, // url (default SBE endpoint)
        api_key, api_secret, None, // heartbeat
    )?;

    ws_client.cache_instruments(instruments);

    tracing::info!("Connecting to Binance Spot SBE WebSocket...");
    ws_client.connect().await?;

    // Subscribe to trades and quotes for BTC and ETH
    let streams = vec![
        "btcusdt@trade".to_string(),
        "ethusdt@trade".to_string(),
        "btcusdt@bestBidAsk".to_string(),
        "ethusdt@bestBidAsk".to_string(),
    ];
    tracing::info!("Subscribing to streams: {:?}", streams);
    ws_client.subscribe(streams).await?;

    tracing::info!("Listening for messages (Ctrl+C to stop)...");

    let stream = ws_client.stream();
    tokio::pin!(stream);

    let mut message_count = 0u64;
    let mut trade_count = 0u64;
    let mut quote_count = 0u64;
    let start_time = std::time::Instant::now();

    loop {
        tokio::select! {
            Some(msg) = stream.next() => {
                message_count += 1;

                match msg {
                    NautilusWsMessage::Data(data_vec) => {
                        for data in &data_vec {
                            match data {
                                Data::Trade(trade) => {
                                    trade_count += 1;
                                    tracing::info!(
                                        msg = message_count,
                                        instrument = %trade.instrument_id,
                                        price = %trade.price,
                                        size = %trade.size,
                                        side = ?trade.aggressor_side,
                                        trade_id = %trade.trade_id,
                                        "Trade"
                                    );
                                }
                                Data::Quote(quote) => {
                                    quote_count += 1;
                                    tracing::info!(
                                        msg = message_count,
                                        instrument = %quote.instrument_id,
                                        bid = %quote.bid_price,
                                        ask = %quote.ask_price,
                                        bid_size = %quote.bid_size,
                                        ask_size = %quote.ask_size,
                                        "Quote"
                                    );
                                }
                                _ => {
                                    tracing::debug!(msg = message_count, "Other data: {data:?}");
                                }
                            }
                        }
                    }
                    NautilusWsMessage::Deltas(deltas) => {
                        tracing::info!(
                            msg = message_count,
                            instrument = %deltas.instrument_id,
                            num_deltas = deltas.deltas.len(),
                            "OrderBook deltas"
                        );
                    }
                    NautilusWsMessage::RawBinary(data) => {
                        // Try to decode and display SBE data
                        match decode_and_display_sbe(&data) {
                            Ok(()) => {}
                            Err(e) => {
                                tracing::warn!(
                                    msg = message_count,
                                    len = data.len(),
                                    error = %e,
                                    "Raw binary (decode failed)"
                                );
                            }
                        }
                    }
                    NautilusWsMessage::RawJson(json) => {
                        tracing::debug!(msg = message_count, "Raw JSON: {json}");
                    }
                    NautilusWsMessage::Error(err) => {
                        tracing::error!(code = err.code, msg = %err.msg, "WebSocket error");
                    }
                    NautilusWsMessage::Reconnected => {
                        tracing::warn!("WebSocket reconnected");
                    }
                    NautilusWsMessage::Instrument(inst) => {
                        tracing::info!("Instrument: {inst:?}");
                    }
                }

                if message_count.is_multiple_of(50) {
                    let elapsed = start_time.elapsed().as_secs_f64();
                    let rate = message_count as f64 / elapsed;
                    tracing::info!(
                        messages = message_count,
                        trades = trade_count,
                        quotes = quote_count,
                        elapsed_secs = format!("{elapsed:.1}"),
                        rate = format!("{rate:.1}/s"),
                        "Statistics"
                    );
                }
            }
            _ = tokio::signal::ctrl_c() => {
                tracing::info!("Received Ctrl+C, shutting down...");
                break;
            }
        }
    }

    ws_client.close().await?;

    let elapsed = start_time.elapsed().as_secs_f64();
    tracing::info!(
        total_messages = message_count,
        trades = trade_count,
        quotes = quote_count,
        elapsed_secs = format!("{elapsed:.1}"),
        avg_rate = format!("{:.1}/s", message_count as f64 / elapsed),
        "Final statistics"
    );

    Ok(())
}

/// Parse instruments from HTTP client cache.
fn parse_spot_instruments(http_client: &BinanceHttpClient) -> Vec<InstrumentAny> {
    let ts = UnixNanos::default();
    let mut instruments = Vec::new();

    for entry in http_client.instruments() {
        if let BinanceInstrument::Spot(symbol) = entry.value() {
            match parse_spot_instrument(symbol, ts, ts) {
                Ok(instrument) => instruments.push(instrument),
                Err(e) => tracing::trace!(symbol = %symbol.symbol, error = %e, "Skipped symbol"),
            }
        }
    }

    instruments
}

/// Decode and display raw SBE binary data.
fn decode_and_display_sbe(data: &[u8]) -> anyhow::Result<()> {
    match decode_market_data(data)? {
        MarketDataMessage::Trades(event) => {
            for trade in &event.trades {
                let price = mantissa_to_f64(trade.price_mantissa, event.price_exponent);
                let qty = mantissa_to_f64(trade.qty_mantissa, event.qty_exponent);
                let side = if trade.is_buyer_maker { "SELL" } else { "BUY" };
                let ts = chrono::DateTime::from_timestamp_micros(event.transact_time_us)
                    .map_or_else(
                        || "?".to_string(),
                        |dt| dt.format("%H:%M:%S%.6f").to_string(),
                    );

                tracing::info!(
                    symbol = %event.symbol,
                    side = side,
                    price = format!("{price:.2}"),
                    qty = format!("{qty:.6}"),
                    id = trade.id,
                    time = ts,
                    "Trade (raw SBE)"
                );
            }
        }
        MarketDataMessage::BestBidAsk(event) => {
            let bid = mantissa_to_f64(event.bid_price_mantissa, event.price_exponent);
            let ask = mantissa_to_f64(event.ask_price_mantissa, event.price_exponent);
            let bid_size = mantissa_to_f64(event.bid_qty_mantissa, event.qty_exponent);
            let ask_size = mantissa_to_f64(event.ask_qty_mantissa, event.qty_exponent);
            let ts = chrono::DateTime::from_timestamp_micros(event.event_time_us).map_or_else(
                || "?".to_string(),
                |dt| dt.format("%H:%M:%S%.6f").to_string(),
            );

            tracing::info!(
                symbol = %event.symbol,
                bid = format!("{bid:.2}"),
                ask = format!("{ask:.2}"),
                bid_size = format!("{bid_size:.6}"),
                ask_size = format!("{ask_size:.6}"),
                time = ts,
                "Quote (raw SBE)"
            );
        }
        MarketDataMessage::DepthSnapshot(event) => {
            tracing::info!(
                symbol = %event.symbol,
                bids = event.bids.len(),
                asks = event.asks.len(),
                "Depth snapshot (raw SBE)"
            );
        }
        MarketDataMessage::DepthDiff(event) => {
            tracing::info!(
                symbol = %event.symbol,
                bids = event.bids.len(),
                asks = event.asks.len(),
                first_update_id = event.first_book_update_id,
                last_update_id = event.last_book_update_id,
                "Depth diff (raw SBE)"
            );
        }
    }

    Ok(())
}
