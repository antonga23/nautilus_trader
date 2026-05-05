import json
from collections.abc import Iterable
from dataclasses import dataclass


ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX = "betting:active_venue_instruments"


@dataclass(frozen=True)
class ActiveVenueInstrumentIndex:
    venue: str
    instrument_ids: tuple[str, ...]
    updated_at_ns: int


def active_venue_instrument_index_key(venue: str) -> str:
    return f"{ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX}:{venue.strip().upper()}"


def encode_active_venue_instrument_index(
    *,
    venue: str,
    instrument_ids: Iterable[str],
    updated_at_ns: int,
) -> bytes:
    payload = {
        "venue": venue.strip().upper(),
        "instrument_ids": sorted(
            {str(instrument_id) for instrument_id in instrument_ids if instrument_id}
        ),
        "updated_at_ns": int(updated_at_ns),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_active_venue_instrument_index(raw: bytes | None) -> ActiveVenueInstrumentIndex | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    venue = str(payload.get("venue") or "").strip().upper()
    instrument_ids_raw = payload.get("instrument_ids")
    updated_at_ns = int(payload.get("updated_at_ns") or 0)
    if not venue or not isinstance(instrument_ids_raw, list):
        return None
    instrument_ids = tuple(
        str(instrument_id) for instrument_id in instrument_ids_raw if instrument_id
    )
    return ActiveVenueInstrumentIndex(
        venue=venue,
        instrument_ids=instrument_ids,
        updated_at_ns=updated_at_ns,
    )
