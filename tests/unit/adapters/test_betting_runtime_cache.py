from nautilus_trader.adapters.betting.runtime_cache import ActiveVenueInstrumentIndex
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import decode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index


def test_active_venue_instrument_index_key_normalizes_venue() -> None:
    assert active_venue_instrument_index_key(" cloudbet ") == (
        "betting:active_venue_instruments:CLOUDBET"
    )


def test_active_venue_instrument_index_round_trip() -> None:
    raw = encode_active_venue_instrument_index(
        venue="sxbet",
        instrument_ids=["b", "a", "a"],
        updated_at_ns=123,
    )

    payload = decode_active_venue_instrument_index(raw)

    assert payload == ActiveVenueInstrumentIndex(
        venue="SXBET",
        instrument_ids=("a", "b"),
        updated_at_ns=123,
    )


def test_active_venue_instrument_index_decode_rejects_invalid_payload() -> None:
    assert decode_active_venue_instrument_index(None) is None
    assert decode_active_venue_instrument_index(b"not-json") is None
