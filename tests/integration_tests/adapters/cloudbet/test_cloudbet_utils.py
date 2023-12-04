from unittest.mock import patch, AsyncMock
import pandas as pd
import pytest
from nautilus_trader.adapters.cloudbet.client.util import make_symbol, extract_cloudbet_symbol, cloudbet_instrument_id, \
    cloudbet_timestamp_to_unix_nanos, datetime_to_cloudbet_timestamp
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.identifiers import Symbol


@pytest.fixture(autouse=False)
def mock_symbol() -> Symbol:
    return Symbol("19662638|tennis.set_handicap|away|handicap=1.5.CLOUDBET")

@pytest.fixture(autouse=False)
def mock_event_id() -> int:
    return 19662638


@pytest.fixture(autouse=False)
def mock_submarket_name() -> str:
    return "tennis.set_handicap"


@pytest.fixture(autouse=False)
def mock_submarket_outcome() -> str:
    return "away"

@pytest.fixture(autouse=False)
def mock_submarket_params() -> str:
    return "handicap=1.5.CLOUDBET"

class TestSymbolGeneration:

    def test_normal_input_values(self, mock_symbol):
        event_id = 19662638
        submarket_name = 'tennis.set_handicap'
        outcome = 'away'
        params = 'handicap=1.5.CLOUDBET'

        result = make_symbol(event_id=event_id, submarket_name=submarket_name, outcome=outcome, params=params)

        assert result == mock_symbol

    @pytest.mark.parametrize("event_id,submarket_name,outcome,params", [
        (None, "submarket_name", "outcome", "params"),
        (1, None, "outcome", "params"),
        (1, "submarket_name", None, "params")
    ])
    def test_make_symbol(self, event_id, submarket_name, outcome, params):
        with pytest.raises(TypeError):
            symbol = make_symbol(event_id, submarket_name, outcome, params)

    @pytest.mark.parametrize("event_id,submarket_name,outcome", [(19662638, "submarket_name", "outcome")])
    def test_minimum_input_values(self, event_id, submarket_name, outcome):
        symbol = make_symbol(event_id, submarket_name, outcome)
        assert symbol == Symbol("19662638|submarket_name|outcome|")

    def test_extract_none_symbol(self):
        with pytest.raises(TypeError):
            extract_cloudbet_symbol(None)

    def test_extract_valid_symbol(self, mock_symbol):
        result = extract_cloudbet_symbol(mock_symbol)
        assert result == (19662638, "tennis.set_handicap", "away", "handicap=1.5.CLOUDBET")

    @patch('nautilus_trader.adapters.cloudbet.client.util.make_symbol')
    def test_extract_symbol_missing_params(self, mock_make_symbol):
        mock_make_symbol.return_value = Symbol('19662638|submarket_name|outcome|')
        symbol = mock_make_symbol(event_id=19662638, submarket_name='submarket_name', outcome='outcome')
        result = extract_cloudbet_symbol(symbol)
        assert result == (19662638, "submarket_name", "outcome", '')


@patch('nautilus_trader.adapters.cloudbet.client.util.make_symbol')
def test_create_valid_instrument_id(mock_make_symbol, mock_symbol, mock_event_id, mock_submarket_name, mock_submarket_outcome, mock_submarket_params):
    mock_make_symbol.return_value = mock_symbol # mock the make_symbol function which is called by cloudbet_instrument_id
    instrument_id : InstrumentId = cloudbet_instrument_id(mock_event_id, mock_submarket_name, mock_submarket_outcome, mock_submarket_params)
    expected_venue = Venue("CLOUDBET")
    expected_instrument_id = InstrumentId(symbol=mock_symbol, venue=expected_venue)

    assert instrument_id == expected_instrument_id

@pytest.mark.parametrize("event_id,submarket_name,outcome,params", [
    (None, "submarket_name", "outcome", "params"),
    ("not a int", "submarket_name", "outcome", "params"),
    (1, None, "outcome", "params"),
    (1, "submarket_name", None, "params"),
    (1, "submarket_name", None, None)
])
def test_create_invalid_instrument_id(event_id, submarket_name, outcome, params):
    with pytest.raises(TypeError):
        instrument_id: InstrumentId = cloudbet_instrument_id(event_id, submarket_name, outcome, params)


class TestTimestampConversion:
    def test_valid_timestamp_conversion(self):
        cloudbet_timestamp = "2023-09-19T09:51:00Z"
        expected_result = 1695109860000 * 1e6
        assert cloudbet_timestamp_to_unix_nanos(cloudbet_timestamp) == expected_result

    def test_non_string_input(self):
        cloudbet_timestamp = 12345
        with pytest.raises(TypeError):
            cloudbet_timestamp_to_unix_nanos(cloudbet_timestamp)

    def test_empty_string_input(self):
        cloudbet_timestamp = ""
        with pytest.raises(ValueError):
            cloudbet_timestamp_to_unix_nanos(cloudbet_timestamp)

    def test_invalid_format_input(self):
        cloudbet_timestamp = "2023-09-19 12:51:11"
        with pytest.raises(ValueError):
            cloudbet_timestamp_to_unix_nanos(cloudbet_timestamp)

    def test_valid_timestamp_conversion(self):
        # Arrange
        timestamp = pd.Timestamp('2023-09-19 12:51:11')

        # Act
        result = datetime_to_cloudbet_timestamp(timestamp)
        # Assert
        assert result == '2023-09-19T12:51:11Z'

    def test_none_input_raises_value_error(self):
        # Arrange
        timestamp = None

        # Act and Assert
        with pytest.raises(TypeError):
            datetime_to_cloudbet_timestamp(timestamp)

    def test_invalid_type_input_raises_type_error(self):
        # Arrange
        timestamp = '2023-09-19 12:51:11'

        # Act and Assert
        with pytest.raises(TypeError):
            datetime_to_cloudbet_timestamp(timestamp)

    def test_timezone_conversion(self):
        # Arrange
        timestamp = pd.Timestamp('2023-09-19 12:51:11', tz='UTC')

        # Act
        result = datetime_to_cloudbet_timestamp(timestamp)

        # Assert
        assert result == '2023-09-19T12:51:11Z'
