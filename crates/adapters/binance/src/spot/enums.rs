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

//! Binance Spot-specific enumerations.

use serde::{Deserialize, Serialize};

/// Spot order type enumeration.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BinanceSpotOrderType {
    /// Limit order.
    Limit,
    /// Market order.
    Market,
    /// Stop loss (triggers market sell when price drops to stop price).
    StopLoss,
    /// Stop loss limit (triggers limit sell when price drops to stop price).
    StopLossLimit,
    /// Take profit (triggers market sell when price rises to stop price).
    TakeProfit,
    /// Take profit limit (triggers limit sell when price rises to stop price).
    TakeProfitLimit,
    /// Limit maker (post-only, rejected if would match immediately).
    LimitMaker,
    /// Unknown or undocumented value.
    #[serde(other)]
    Unknown,
}

/// Spot order response type.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BinanceOrderResponseType {
    /// Acknowledge only (fastest).
    Ack,
    /// Result with order details.
    Result,
    /// Full response with fills.
    #[default]
    Full,
}

/// Cancel/replace mode for order modification.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BinanceCancelReplaceMode {
    /// Stop if cancel fails.
    StopOnFailure,
    /// Continue with new order even if cancel fails.
    AllowFailure,
}
