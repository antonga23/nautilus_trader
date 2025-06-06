// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
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

//! Represents a valid position ID.

use std::{
    fmt::{Debug, Display, Formatter},
    hash::Hash,
};

use nautilus_core::correctness::{FAILED, check_valid_string};
use ustr::Ustr;

/// Represents a valid position ID.
#[repr(C)]
#[derive(Clone, Copy, Hash, PartialEq, Eq, PartialOrd, Ord)]
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_trader.core.nautilus_pyo3.model")
)]
pub struct PositionId(Ustr);

impl PositionId {
    /// Creates a new [`PositionId`] instance with correctness checking.
    ///
    /// # Errors
    ///
    /// This function returns an error:
    /// - If `value` is not a valid string.
    ///
    /// # Notes
    ///
    /// PyO3 requires a `Result` type for proper error handling and stacktrace printing in Python.
    pub fn new_checked<T: AsRef<str>>(value: T) -> anyhow::Result<Self> {
        let value = value.as_ref();
        check_valid_string(value, stringify!(value))?;
        Ok(Self(Ustr::from(value)))
    }

    /// Creates a new [`PositionId`] instance.
    ///
    /// # Panics
    ///
    /// This function panics:
    /// - If `value` is not a valid string.
    pub fn new<T: AsRef<str>>(value: T) -> Self {
        Self::new_checked(value).expect(FAILED)
    }

    /// Sets the inner identifier value.
    #[allow(dead_code)]
    pub(crate) fn set_inner(&mut self, value: &str) {
        self.0 = Ustr::from(value);
    }

    /// Returns the inner identifier value.
    #[must_use]
    pub fn inner(&self) -> Ustr {
        self.0
    }

    /// Returns the inner identifier value as a string slice.
    #[must_use]
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }

    /// Checks if the position ID is virtual.
    ///
    /// Returns `true` if the position ID starts with "P-", otherwise `false`.
    #[must_use]
    pub fn is_virtual(&self) -> bool {
        self.0.starts_with("P-")
    }
}

impl Debug for PositionId {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}", self.0)
    }
}
impl Display for PositionId {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

////////////////////////////////////////////////////////////////////////////////
// Tests
////////////////////////////////////////////////////////////////////////////////
#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::PositionId;
    use crate::identifiers::stubs::*;

    #[rstest]
    fn test_string_reprs(position_id_test: PositionId) {
        assert_eq!(position_id_test.as_str(), "P-123456789");
        assert_eq!(format!("{position_id_test}"), "P-123456789");
    }
}
