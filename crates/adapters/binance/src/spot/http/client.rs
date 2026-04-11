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

//! Binance Spot HTTP client with SBE encoding.
//!
//! This client communicates with Binance Spot REST API using SBE (Simple Binary
//! Encoding) for all request/response payloads, providing microsecond timestamp
//! precision and reduced latency compared to JSON.
//!
//! ## Architecture
//!
//! Two-layer client pattern:
//! - [`BinanceRawSpotHttpClient`]: Low-level API methods returning raw bytes.
//! - [`BinanceSpotHttpClient`]: High-level methods with SBE decoding.
//!
//! ## SBE Headers
//!
//! All requests include:
//! - `Accept: application/sbe`
//! - `X-MBX-SBE: 3:2` (schema ID:version)

use std::{collections::HashMap, fmt::Debug, num::NonZeroU32, sync::Arc};

use chrono::Utc;
use dashmap::DashMap;
use nautilus_core::{consts::NAUTILUS_USER_AGENT, nanos::UnixNanos};
use nautilus_model::{
    data::TradeTick,
    enums::{OrderSide, OrderType, TimeInForce},
    identifiers::{AccountId, ClientOrderId, InstrumentId, VenueOrderId},
    instruments::{Instrument, any::InstrumentAny},
    reports::{FillReport, OrderStatusReport},
    types::{Price, Quantity},
};
use nautilus_network::{
    http::{HttpClient, HttpResponse, Method},
    ratelimiter::quota::Quota,
};
use serde::Serialize;
use ustr::Ustr;

use super::{
    error::{BinanceSpotHttpError, BinanceSpotHttpResult},
    models::{
        BinanceAccountInfo, BinanceAccountTrade, BinanceCancelOrderResponse, BinanceDepth,
        BinanceNewOrderResponse, BinanceOrderResponse, BinanceTrades,
    },
    parse,
    query::{
        AccountInfoParams, AccountTradesParams, AllOrdersParams, CancelOpenOrdersParams,
        CancelOrderParams, CancelReplaceOrderParams, DepthParams, NewOrderParams, OpenOrdersParams,
        QueryOrderParams, TradesParams,
    },
};
use crate::{
    common::{
        consts::BINANCE_SPOT_RATE_LIMITS,
        credential::Credential,
        enums::{BinanceEnvironment, BinanceProductType, BinanceSide, BinanceTimeInForce},
        models::BinanceErrorResponse,
        sbe::spot::{SBE_SCHEMA_ID, SBE_SCHEMA_VERSION},
        urls::get_http_base_url,
    },
    spot::enums::BinanceSpotOrderType,
};

/// SBE schema header value for Spot API.
pub const SBE_SCHEMA_HEADER: &str = "3:2";

/// Binance Spot API path.
const SPOT_API_PATH: &str = "/api/v3";

/// Global rate limit key.
const BINANCE_GLOBAL_RATE_KEY: &str = "binance:spot:global";

/// Orders rate limit key prefix.
const BINANCE_ORDERS_RATE_KEY: &str = "binance:spot:orders";

/// Low-level HTTP client for Binance Spot REST API with SBE encoding.
///
/// Handles:
/// - Base URL resolution by environment.
/// - Optional HMAC SHA256 signing for private endpoints.
/// - Rate limiting using Spot API quotas.
/// - SBE decoding to Binance-specific response types.
///
/// Methods are named to match Binance API endpoints and return
/// venue-specific types (decoded from SBE).
#[derive(Debug, Clone)]
pub struct BinanceRawSpotHttpClient {
    client: HttpClient,
    base_url: String,
    credential: Option<Credential>,
    recv_window: Option<u64>,
    order_rate_keys: Vec<String>,
}

impl BinanceRawSpotHttpClient {
    /// Creates a new Binance Spot raw HTTP client.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying [`HttpClient`] fails to build.
    pub fn new(
        environment: BinanceEnvironment,
        api_key: Option<String>,
        api_secret: Option<String>,
        base_url_override: Option<String>,
        recv_window: Option<u64>,
        timeout_secs: Option<u64>,
        proxy_url: Option<String>,
    ) -> BinanceSpotHttpResult<Self> {
        let RateLimitConfig {
            default_quota,
            keyed_quotas,
            order_keys,
        } = Self::rate_limit_config();

        let credential = match (api_key, api_secret) {
            (Some(key), Some(secret)) => Some(Credential::new(key, secret)),
            (None, None) => None,
            _ => return Err(BinanceSpotHttpError::MissingCredentials),
        };

        let base_url = base_url_override.unwrap_or_else(|| {
            get_http_base_url(BinanceProductType::Spot, environment).to_string()
        });

        let headers = Self::default_headers(&credential);

        let client = HttpClient::new(
            headers,
            vec!["X-MBX-APIKEY".to_string()],
            keyed_quotas,
            default_quota,
            timeout_secs,
            proxy_url,
        )?;

        Ok(Self {
            client,
            base_url,
            credential,
            recv_window,
            order_rate_keys: order_keys,
        })
    }

    /// Returns the SBE schema ID.
    #[must_use]
    pub const fn schema_id() -> u16 {
        SBE_SCHEMA_ID
    }

    /// Returns the SBE schema version.
    #[must_use]
    pub const fn schema_version() -> u16 {
        SBE_SCHEMA_VERSION
    }

    /// Performs a GET request and returns raw response bytes.
    pub async fn get<P>(&self, path: &str, params: Option<&P>) -> BinanceSpotHttpResult<Vec<u8>>
    where
        P: Serialize + ?Sized,
    {
        self.request(Method::GET, path, params, false, false).await
    }

    /// Performs a signed GET request and returns raw response bytes.
    pub async fn get_signed<P>(
        &self,
        path: &str,
        params: Option<&P>,
    ) -> BinanceSpotHttpResult<Vec<u8>>
    where
        P: Serialize + ?Sized,
    {
        self.request(Method::GET, path, params, true, false).await
    }

    /// Performs a signed POST request for order operations.
    pub async fn post_order<P>(
        &self,
        path: &str,
        params: Option<&P>,
    ) -> BinanceSpotHttpResult<Vec<u8>>
    where
        P: Serialize + ?Sized,
    {
        self.request(Method::POST, path, params, true, true).await
    }

    /// Performs a signed DELETE request for cancel operations.
    pub async fn delete_order<P>(
        &self,
        path: &str,
        params: Option<&P>,
    ) -> BinanceSpotHttpResult<Vec<u8>>
    where
        P: Serialize + ?Sized,
    {
        self.request(Method::DELETE, path, params, true, true).await
    }

    /// Tests connectivity to the API.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn ping(&self) -> BinanceSpotHttpResult<()> {
        let bytes = self.get("ping", None::<&()>).await?;
        parse::decode_ping(&bytes)?;
        Ok(())
    }

    /// Returns the server time in **microseconds** since epoch.
    ///
    /// Note: SBE provides microsecond precision vs JSON's milliseconds.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn server_time(&self) -> BinanceSpotHttpResult<i64> {
        let bytes = self.get("time", None::<&()>).await?;
        let timestamp = parse::decode_server_time(&bytes)?;
        Ok(timestamp)
    }

    /// Returns exchange information including trading symbols.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn exchange_info(
        &self,
    ) -> BinanceSpotHttpResult<super::models::BinanceExchangeInfoSbe> {
        let bytes = self.get("exchangeInfo", None::<&()>).await?;
        let info = parse::decode_exchange_info(&bytes)?;
        Ok(info)
    }

    /// Returns order book depth for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn depth(&self, params: &DepthParams) -> BinanceSpotHttpResult<BinanceDepth> {
        let bytes = self.get("depth", Some(params)).await?;
        let depth = parse::decode_depth(&bytes)?;
        Ok(depth)
    }

    /// Returns recent trades for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn trades(&self, params: &TradesParams) -> BinanceSpotHttpResult<BinanceTrades> {
        let bytes = self.get("trades", Some(params)).await?;
        let trades = parse::decode_trades(&bytes)?;
        Ok(trades)
    }

    /// Creates a new order.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn new_order(
        &self,
        params: &NewOrderParams,
    ) -> BinanceSpotHttpResult<BinanceNewOrderResponse> {
        let bytes = self.post_order("order", Some(params)).await?;
        let response = parse::decode_new_order_full(&bytes)?;
        Ok(response)
    }

    /// Cancels an existing order.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_order(
        &self,
        params: &CancelOrderParams,
    ) -> BinanceSpotHttpResult<BinanceCancelOrderResponse> {
        let bytes = self.delete_order("order", Some(params)).await?;
        let response = parse::decode_cancel_order(&bytes)?;
        Ok(response)
    }

    /// Cancels all open orders for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_open_orders(
        &self,
        params: &CancelOpenOrdersParams,
    ) -> BinanceSpotHttpResult<Vec<BinanceCancelOrderResponse>> {
        let bytes = self.delete_order("openOrders", Some(params)).await?;
        let response = parse::decode_cancel_open_orders(&bytes)?;
        Ok(response)
    }

    /// Cancels an existing order and places a new order atomically.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_replace_order(
        &self,
        params: &CancelReplaceOrderParams,
    ) -> BinanceSpotHttpResult<BinanceNewOrderResponse> {
        let bytes = self.post_order("order/cancelReplace", Some(params)).await?;
        let response = parse::decode_new_order_full(&bytes)?;
        Ok(response)
    }

    /// Queries an order's status.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn query_order(
        &self,
        params: &QueryOrderParams,
    ) -> BinanceSpotHttpResult<BinanceOrderResponse> {
        let bytes = self.get_signed("order", Some(params)).await?;
        let response = parse::decode_order(&bytes)?;
        Ok(response)
    }

    /// Returns all open orders for a symbol or all symbols.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn open_orders(
        &self,
        params: &OpenOrdersParams,
    ) -> BinanceSpotHttpResult<Vec<BinanceOrderResponse>> {
        let bytes = self.get_signed("openOrders", Some(params)).await?;
        let response = parse::decode_orders(&bytes)?;
        Ok(response)
    }

    /// Returns all orders (including closed) for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn all_orders(
        &self,
        params: &AllOrdersParams,
    ) -> BinanceSpotHttpResult<Vec<BinanceOrderResponse>> {
        let bytes = self.get_signed("allOrders", Some(params)).await?;
        let response = parse::decode_orders(&bytes)?;
        Ok(response)
    }

    /// Returns account information including balances.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn account(
        &self,
        params: &AccountInfoParams,
    ) -> BinanceSpotHttpResult<BinanceAccountInfo> {
        let bytes = self.get_signed("account", Some(params)).await?;
        let response = parse::decode_account(&bytes)?;
        Ok(response)
    }

    /// Returns account trade history for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn account_trades(
        &self,
        params: &AccountTradesParams,
    ) -> BinanceSpotHttpResult<Vec<BinanceAccountTrade>> {
        let bytes = self.get_signed("myTrades", Some(params)).await?;
        let response = parse::decode_account_trades(&bytes)?;
        Ok(response)
    }

    async fn request<P>(
        &self,
        method: Method,
        path: &str,
        params: Option<&P>,
        signed: bool,
        use_order_quota: bool,
    ) -> BinanceSpotHttpResult<Vec<u8>>
    where
        P: Serialize + ?Sized,
    {
        let mut query = params
            .map(serde_urlencoded::to_string)
            .transpose()
            .map_err(|e| BinanceSpotHttpError::ValidationError(e.to_string()))?
            .unwrap_or_default();

        let mut headers = HashMap::new();
        if signed {
            let cred = self
                .credential
                .as_ref()
                .ok_or(BinanceSpotHttpError::MissingCredentials)?;

            if !query.is_empty() {
                query.push('&');
            }

            let timestamp = Utc::now().timestamp_millis();
            query.push_str(&format!("timestamp={timestamp}"));

            if let Some(recv_window) = self.recv_window {
                query.push_str(&format!("&recvWindow={recv_window}"));
            }

            let signature = cred.sign(&query);
            query.push_str(&format!("&signature={signature}"));
            headers.insert("X-MBX-APIKEY".to_string(), cred.api_key().to_string());
        }

        let url = self.build_url(path, &query);
        let keys = self.rate_limit_keys(use_order_quota);

        let response = self
            .client
            .request(
                method,
                url,
                None::<&HashMap<String, Vec<String>>>,
                Some(headers),
                None,
                None,
                Some(keys),
            )
            .await?;

        if !response.status.is_success() {
            return self.parse_error_response(response);
        }

        Ok(response.body.to_vec())
    }

    fn build_url(&self, path: &str, query: &str) -> String {
        let normalized_path = if path.starts_with('/') {
            path.to_string()
        } else {
            format!("/{path}")
        };

        let mut url = format!("{}{}{}", self.base_url, SPOT_API_PATH, normalized_path);
        if !query.is_empty() {
            url.push('?');
            url.push_str(query);
        }
        url
    }

    fn rate_limit_keys(&self, use_orders: bool) -> Vec<String> {
        if use_orders {
            let mut keys = Vec::with_capacity(1 + self.order_rate_keys.len());
            keys.push(BINANCE_GLOBAL_RATE_KEY.to_string());
            keys.extend(self.order_rate_keys.iter().cloned());
            keys
        } else {
            vec![BINANCE_GLOBAL_RATE_KEY.to_string()]
        }
    }

    fn parse_error_response<T>(&self, response: HttpResponse) -> BinanceSpotHttpResult<T> {
        let status = response.status.as_u16();
        let body_hex = hex::encode(&response.body);

        // Binance may return JSON errors even when SBE was requested
        if let Ok(body_str) = std::str::from_utf8(&response.body)
            && let Ok(err) = serde_json::from_str::<BinanceErrorResponse>(body_str)
        {
            return Err(BinanceSpotHttpError::BinanceError {
                code: err.code,
                message: err.msg,
            });
        }

        Err(BinanceSpotHttpError::UnexpectedStatus {
            status,
            body: body_hex,
        })
    }

    fn default_headers(credential: &Option<Credential>) -> HashMap<String, String> {
        let mut headers = HashMap::new();
        headers.insert("User-Agent".to_string(), NAUTILUS_USER_AGENT.to_string());
        headers.insert("Accept".to_string(), "application/sbe".to_string());
        headers.insert("X-MBX-SBE".to_string(), SBE_SCHEMA_HEADER.to_string());
        if let Some(cred) = credential {
            headers.insert("X-MBX-APIKEY".to_string(), cred.api_key().to_string());
        }
        headers
    }

    fn rate_limit_config() -> RateLimitConfig {
        let quotas = BINANCE_SPOT_RATE_LIMITS;
        let mut keyed = Vec::new();
        let mut order_keys = Vec::new();
        let mut default = None;

        for quota in quotas {
            if let Some(q) = Self::quota_from(quota) {
                if quota.rate_limit_type == "REQUEST_WEIGHT" && default.is_none() {
                    default = Some(q);
                } else if quota.rate_limit_type == "ORDERS" {
                    let key = format!("{}:{}", BINANCE_ORDERS_RATE_KEY, quota.interval);
                    order_keys.push(key.clone());
                    keyed.push((key, q));
                }
            }
        }

        let default_quota =
            default.unwrap_or_else(|| Quota::per_second(NonZeroU32::new(10).unwrap()));

        keyed.push((BINANCE_GLOBAL_RATE_KEY.to_string(), default_quota));

        RateLimitConfig {
            default_quota: Some(default_quota),
            keyed_quotas: keyed,
            order_keys,
        }
    }

    fn quota_from(quota: &crate::common::consts::BinanceRateLimitQuota) -> Option<Quota> {
        let burst = NonZeroU32::new(quota.limit)?;
        match quota.interval {
            "SECOND" => Some(Quota::per_second(burst)),
            "MINUTE" => Some(Quota::per_minute(burst)),
            "DAY" => Quota::with_period(std::time::Duration::from_secs(86_400))
                .map(|q| q.allow_burst(burst)),
            _ => None,
        }
    }
}

struct RateLimitConfig {
    default_quota: Option<Quota>,
    keyed_quotas: Vec<(String, Quota)>,
    order_keys: Vec<String>,
}

/// High-level HTTP client for Binance Spot API.
///
/// Wraps [`BinanceRawSpotHttpClient`] and provides domain-level methods:
/// - Simple types (ping, server_time): Pass through from raw client.
/// - Complex types (instruments, orders): Transform to Nautilus domain types.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_trader.core.nautilus_pyo3.binance")
)]
pub struct BinanceSpotHttpClient {
    inner: Arc<BinanceRawSpotHttpClient>,
    instruments_cache: Arc<DashMap<Ustr, InstrumentAny>>,
}

impl Clone for BinanceSpotHttpClient {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            instruments_cache: self.instruments_cache.clone(),
        }
    }
}

impl Debug for BinanceSpotHttpClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BinanceSpotHttpClient")
            .field("inner", &self.inner)
            .field("instruments_cached", &self.instruments_cache.len())
            .finish()
    }
}

impl BinanceSpotHttpClient {
    /// Creates a new Binance Spot HTTP client.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying HTTP client cannot be created.
    pub fn new(
        environment: BinanceEnvironment,
        api_key: Option<String>,
        api_secret: Option<String>,
        base_url_override: Option<String>,
        recv_window: Option<u64>,
        timeout_secs: Option<u64>,
        proxy_url: Option<String>,
    ) -> BinanceSpotHttpResult<Self> {
        let inner = BinanceRawSpotHttpClient::new(
            environment,
            api_key,
            api_secret,
            base_url_override,
            recv_window,
            timeout_secs,
            proxy_url,
        )?;

        Ok(Self {
            inner: Arc::new(inner),
            instruments_cache: Arc::new(DashMap::new()),
        })
    }

    /// Returns a reference to the inner raw client.
    #[must_use]
    pub fn inner(&self) -> &BinanceRawSpotHttpClient {
        &self.inner
    }

    /// Returns the SBE schema ID.
    #[must_use]
    pub const fn schema_id() -> u16 {
        SBE_SCHEMA_ID
    }

    /// Returns the SBE schema version.
    #[must_use]
    pub const fn schema_version() -> u16 {
        SBE_SCHEMA_VERSION
    }

    /// Generates a timestamp for initialization.
    fn generate_ts_init(&self) -> UnixNanos {
        UnixNanos::from(chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0) as u64)
    }

    /// Retrieves an instrument from the cache.
    fn instrument_from_cache(&self, symbol: Ustr) -> anyhow::Result<InstrumentAny> {
        self.instruments_cache
            .get(&symbol)
            .map(|entry| entry.value().clone())
            .ok_or_else(|| anyhow::anyhow!("Instrument {symbol} not in cache"))
    }

    /// Caches multiple instruments.
    pub fn cache_instruments(&self, instruments: Vec<InstrumentAny>) {
        for inst in instruments {
            self.instruments_cache
                .insert(inst.raw_symbol().inner(), inst);
        }
    }

    /// Caches a single instrument.
    pub fn cache_instrument(&self, instrument: InstrumentAny) {
        self.instruments_cache
            .insert(instrument.raw_symbol().inner(), instrument);
    }

    /// Gets an instrument from the cache by symbol.
    #[must_use]
    pub fn get_instrument(&self, symbol: &Ustr) -> Option<InstrumentAny> {
        self.instruments_cache
            .get(symbol)
            .map(|entry| entry.value().clone())
    }

    /// Tests connectivity to the API.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn ping(&self) -> BinanceSpotHttpResult<()> {
        self.inner.ping().await
    }

    /// Returns the server time in **microseconds** since epoch.
    ///
    /// Note: SBE provides microsecond precision vs JSON's milliseconds.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn server_time(&self) -> BinanceSpotHttpResult<i64> {
        self.inner.server_time().await
    }

    /// Returns exchange information including trading symbols.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn exchange_info(
        &self,
    ) -> BinanceSpotHttpResult<super::models::BinanceExchangeInfoSbe> {
        self.inner.exchange_info().await
    }

    /// Requests Nautilus instruments for all trading symbols.
    ///
    /// Fetches exchange info via SBE and parses each symbol into a CurrencyPair.
    /// Non-trading symbols are skipped with a debug log.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn request_instruments(&self) -> BinanceSpotHttpResult<Vec<InstrumentAny>> {
        let info = self.exchange_info().await?;
        let ts_init = self.generate_ts_init();

        let mut instruments = Vec::with_capacity(info.symbols.len());
        for symbol in &info.symbols {
            match crate::common::parse::parse_spot_instrument_sbe(symbol, ts_init, ts_init) {
                Ok(instrument) => instruments.push(instrument),
                Err(e) => {
                    tracing::debug!(
                        symbol = %symbol.symbol,
                        error = %e,
                        "Skipping symbol during instrument parsing"
                    );
                }
            }
        }

        // Cache instruments for use by other domain methods
        self.cache_instruments(instruments.clone());

        tracing::info!(count = instruments.len(), "Loaded spot instruments");
        Ok(instruments)
    }

    /// Requests recent trades for an instrument.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails, the instrument is not cached,
    /// or trade parsing fails.
    pub async fn request_trades(
        &self,
        instrument_id: InstrumentId,
        limit: Option<u32>,
    ) -> anyhow::Result<Vec<TradeTick>> {
        let symbol = instrument_id.symbol.inner();
        let instrument = self.instrument_from_cache(symbol)?;
        let ts_init = self.generate_ts_init();

        let params = TradesParams {
            symbol: symbol.to_string(),
            limit,
        };

        let trades = self
            .inner
            .trades(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        crate::common::parse::parse_spot_trades_sbe(&trades, &instrument, ts_init)
    }

    /// Submits a new order to the venue.
    ///
    /// Converts Nautilus domain types to Binance-specific parameters
    /// and returns an `OrderStatusReport`.
    ///
    /// # Errors
    ///
    /// Returns an error if:
    /// - The instrument is not cached.
    /// - The order type or time-in-force is unsupported.
    /// - Stop orders are submitted without a trigger price.
    /// - The request fails or SBE decoding fails.
    #[allow(clippy::too_many_arguments)]
    pub async fn submit_order(
        &self,
        account_id: AccountId,
        instrument_id: InstrumentId,
        client_order_id: ClientOrderId,
        order_side: OrderSide,
        order_type: OrderType,
        quantity: Quantity,
        time_in_force: TimeInForce,
        price: Option<Price>,
        trigger_price: Option<Price>,
        post_only: bool,
    ) -> anyhow::Result<OrderStatusReport> {
        let symbol = instrument_id.symbol.inner();
        let instrument = self.instrument_from_cache(symbol)?;
        let ts_init = self.generate_ts_init();

        let binance_side = match order_side {
            OrderSide::Buy => BinanceSide::Buy,
            OrderSide::Sell => BinanceSide::Sell,
            _ => anyhow::bail!("Invalid order side: {order_side:?}"),
        };

        let is_stop_order = matches!(order_type, OrderType::StopMarket | OrderType::StopLimit);
        if is_stop_order && trigger_price.is_none() {
            anyhow::bail!("Stop orders require a trigger price");
        }

        let binance_order_type = match (order_type, post_only) {
            (OrderType::Market, _) => BinanceSpotOrderType::Market,
            (OrderType::Limit, true) => BinanceSpotOrderType::LimitMaker,
            (OrderType::Limit, false) => BinanceSpotOrderType::Limit,
            (OrderType::StopMarket, _) => BinanceSpotOrderType::StopLoss,
            (OrderType::StopLimit, _) => BinanceSpotOrderType::StopLossLimit,
            _ => anyhow::bail!("Unsupported order type: {order_type:?}"),
        };

        let binance_tif = match time_in_force {
            TimeInForce::Gtc => BinanceTimeInForce::Gtc,
            TimeInForce::Ioc => BinanceTimeInForce::Ioc,
            TimeInForce::Fok => BinanceTimeInForce::Fok,
            TimeInForce::Gtd => BinanceTimeInForce::Gtd,
            _ => anyhow::bail!("Unsupported time in force: {time_in_force:?}"),
        };

        let params = NewOrderParams {
            symbol: symbol.to_string(),
            side: binance_side,
            order_type: binance_order_type,
            time_in_force: Some(binance_tif),
            quantity: Some(quantity.to_string()),
            quote_order_qty: None,
            price: price.map(|p| p.to_string()),
            new_client_order_id: Some(client_order_id.to_string()),
            stop_price: trigger_price.map(|p| p.to_string()),
            trailing_delta: None,
            iceberg_qty: None,
            new_order_resp_type: None,
            self_trade_prevention_mode: None,
        };

        let response = self
            .inner
            .new_order(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        crate::common::parse::parse_new_order_response_sbe(
            &response,
            account_id,
            &instrument,
            ts_init,
        )
    }

    /// Cancels an existing order on the venue by venue order ID.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_order(
        &self,
        instrument_id: InstrumentId,
        venue_order_id: VenueOrderId,
    ) -> anyhow::Result<VenueOrderId> {
        let symbol = instrument_id.symbol.inner().to_string();

        let order_id: i64 = venue_order_id
            .inner()
            .parse()
            .map_err(|_| anyhow::anyhow!("Invalid venue order ID: {venue_order_id}"))?;

        let params = CancelOrderParams::by_order_id(&symbol, order_id);

        let response = self
            .inner
            .cancel_order(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        Ok(VenueOrderId::new(response.order_id.to_string()))
    }

    /// Cancels an existing order on the venue by client order ID.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_order_by_client_id(
        &self,
        instrument_id: InstrumentId,
        client_order_id: ClientOrderId,
    ) -> anyhow::Result<VenueOrderId> {
        let symbol = instrument_id.symbol.inner().to_string();
        let params = CancelOrderParams::by_client_order_id(&symbol, client_order_id.to_string());

        let response = self
            .inner
            .cancel_order(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        Ok(VenueOrderId::new(response.order_id.to_string()))
    }

    /// Cancels all open orders for a symbol.
    ///
    /// Returns the venue order IDs of all canceled orders.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn cancel_all_orders(
        &self,
        instrument_id: InstrumentId,
    ) -> anyhow::Result<Vec<VenueOrderId>> {
        let symbol = instrument_id.symbol.inner().to_string();
        let params = CancelOpenOrdersParams::new(symbol);

        let responses = self
            .inner
            .cancel_open_orders(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        Ok(responses
            .into_iter()
            .map(|r| VenueOrderId::new(r.order_id.to_string()))
            .collect())
    }

    /// Modifies an existing order (cancel and replace atomically).
    ///
    /// # Errors
    ///
    /// Returns an error if:
    /// - The instrument is not cached.
    /// - The order type or time-in-force is unsupported.
    /// - The request fails or SBE decoding fails.
    #[allow(clippy::too_many_arguments)]
    pub async fn modify_order(
        &self,
        account_id: AccountId,
        instrument_id: InstrumentId,
        venue_order_id: VenueOrderId,
        client_order_id: ClientOrderId,
        order_side: OrderSide,
        order_type: OrderType,
        quantity: Quantity,
        time_in_force: TimeInForce,
        price: Option<Price>,
    ) -> anyhow::Result<OrderStatusReport> {
        let symbol = instrument_id.symbol.inner();
        let instrument = self.instrument_from_cache(symbol)?;
        let ts_init = self.generate_ts_init();

        let binance_side = match order_side {
            OrderSide::Buy => BinanceSide::Buy,
            OrderSide::Sell => BinanceSide::Sell,
            _ => anyhow::bail!("Invalid order side: {order_side:?}"),
        };

        let binance_order_type = match order_type {
            OrderType::Market => BinanceSpotOrderType::Market,
            OrderType::Limit => BinanceSpotOrderType::Limit,
            _ => anyhow::bail!("Unsupported order type for modify: {order_type:?}"),
        };

        let binance_tif = match time_in_force {
            TimeInForce::Gtc => BinanceTimeInForce::Gtc,
            TimeInForce::Ioc => BinanceTimeInForce::Ioc,
            TimeInForce::Fok => BinanceTimeInForce::Fok,
            TimeInForce::Gtd => BinanceTimeInForce::Gtd,
            _ => anyhow::bail!("Unsupported time in force: {time_in_force:?}"),
        };

        let cancel_order_id: i64 = venue_order_id
            .inner()
            .parse()
            .map_err(|_| anyhow::anyhow!("Invalid venue order ID: {venue_order_id}"))?;

        let params = CancelReplaceOrderParams {
            symbol: symbol.to_string(),
            side: binance_side,
            order_type: binance_order_type,
            cancel_replace_mode: crate::spot::enums::BinanceCancelReplaceMode::StopOnFailure,
            time_in_force: Some(binance_tif),
            quantity: Some(quantity.to_string()),
            quote_order_qty: None,
            price: price.map(|p| p.to_string()),
            cancel_order_id: Some(cancel_order_id),
            cancel_orig_client_order_id: None,
            new_client_order_id: Some(client_order_id.to_string()),
            stop_price: None,
            trailing_delta: None,
            iceberg_qty: None,
            new_order_resp_type: None,
            self_trade_prevention_mode: None,
        };

        let response = self
            .inner
            .cancel_replace_order(&params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        crate::common::parse::parse_new_order_response_sbe(
            &response,
            account_id,
            &instrument,
            ts_init,
        )
    }

    /// Requests the status of a specific order.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails, instrument is not cached,
    /// or parsing fails.
    pub async fn request_order_status(
        &self,
        account_id: AccountId,
        instrument_id: InstrumentId,
        params: &QueryOrderParams,
    ) -> anyhow::Result<OrderStatusReport> {
        let symbol = instrument_id.symbol.inner();
        let instrument = self.instrument_from_cache(symbol)?;
        let ts_init = self.generate_ts_init();

        let order = self
            .inner
            .query_order(params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        crate::common::parse::parse_order_status_report_sbe(
            &order,
            account_id,
            &instrument,
            ts_init,
        )
    }

    /// Requests all open orders for a symbol or all symbols.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails, any order's instrument is not cached,
    /// or parsing fails.
    pub async fn request_open_orders(
        &self,
        account_id: AccountId,
        params: &OpenOrdersParams,
    ) -> anyhow::Result<Vec<OrderStatusReport>> {
        let ts_init = self.generate_ts_init();

        let orders = self
            .inner
            .open_orders(params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        orders
            .iter()
            .map(|order| {
                let symbol = Ustr::from(&order.symbol);
                let instrument = self.instrument_from_cache(symbol)?;
                crate::common::parse::parse_order_status_report_sbe(
                    order,
                    account_id,
                    &instrument,
                    ts_init,
                )
            })
            .collect()
    }

    /// Requests order history (including closed orders) for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails, any order's instrument is not cached,
    /// or parsing fails.
    pub async fn request_order_history(
        &self,
        account_id: AccountId,
        params: &AllOrdersParams,
    ) -> anyhow::Result<Vec<OrderStatusReport>> {
        let ts_init = self.generate_ts_init();

        let orders = self
            .inner
            .all_orders(params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        orders
            .iter()
            .map(|order| {
                let symbol = Ustr::from(&order.symbol);
                let instrument = self.instrument_from_cache(symbol)?;
                crate::common::parse::parse_order_status_report_sbe(
                    order,
                    account_id,
                    &instrument,
                    ts_init,
                )
            })
            .collect()
    }

    /// Requests account state including balances.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails or SBE decoding fails.
    pub async fn request_account_state(
        &self,
        params: &AccountInfoParams,
    ) -> BinanceSpotHttpResult<BinanceAccountInfo> {
        self.inner.account(params).await
    }

    /// Requests fill reports (trade history) for a symbol.
    ///
    /// # Errors
    ///
    /// Returns an error if the request fails, any trade's instrument is not cached,
    /// or parsing fails.
    pub async fn request_fill_reports(
        &self,
        account_id: AccountId,
        params: &AccountTradesParams,
    ) -> anyhow::Result<Vec<FillReport>> {
        let ts_init = self.generate_ts_init();

        let trades = self
            .inner
            .account_trades(params)
            .await
            .map_err(|e| anyhow::anyhow!(e))?;

        trades
            .iter()
            .map(|trade| {
                let symbol = Ustr::from(&trade.symbol);
                let instrument = self.instrument_from_cache(symbol)?;
                let commission_currency =
                    crate::common::parse::get_currency(&trade.commission_asset);
                crate::common::parse::parse_fill_report_sbe(
                    trade,
                    account_id,
                    &instrument,
                    commission_currency,
                    ts_init,
                )
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    #[rstest]
    fn test_schema_constants() {
        assert_eq!(BinanceRawSpotHttpClient::schema_id(), 3);
        assert_eq!(BinanceRawSpotHttpClient::schema_version(), 2);
        assert_eq!(BinanceSpotHttpClient::schema_id(), 3);
        assert_eq!(BinanceSpotHttpClient::schema_version(), 2);
    }

    #[rstest]
    fn test_sbe_schema_header() {
        assert_eq!(SBE_SCHEMA_HEADER, "3:2");
    }

    #[rstest]
    fn test_default_headers_include_sbe() {
        let headers = BinanceRawSpotHttpClient::default_headers(&None);

        assert_eq!(headers.get("Accept"), Some(&"application/sbe".to_string()));
        assert_eq!(headers.get("X-MBX-SBE"), Some(&"3:2".to_string()));
    }

    #[rstest]
    fn test_rate_limit_config() {
        let config = BinanceRawSpotHttpClient::rate_limit_config();

        assert!(config.default_quota.is_some());
        // Spot has 2 ORDERS quotas (SECOND and DAY)
        assert_eq!(config.order_keys.len(), 2);
    }
}
