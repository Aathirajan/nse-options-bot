"""
adapters/instrument_builder.py

Converts our InstrumentRecord (from mapper.py, sourced from the real
INDstocks instrument master) into actual nautilus_trader Instrument
objects — IndexInstrument for NIFTY/SENSEX, OptionContract for
CE/PE contracts. Confirmed against nautilus_trader==1.231.0's real
constructors (inspected directly, not guessed):

    IndexInstrument(instrument_id, raw_symbol, currency, price_precision,
                     size_precision, price_increment, size_increment,
                     ts_event, ts_init, ...)

    OptionContract(instrument_id, raw_symbol, asset_class, currency,
                    price_precision, price_increment, multiplier,
                    lot_size, underlying, option_kind, strike_price,
                    activation_ns, expiration_ns, ts_event, ts_init, ...)

These are the objects you register with a BacktestEngine via
add_instrument(), and what QuoteTick/TradeTick instrument_id fields must
match.
"""

from __future__ import annotations

import datetime as dt

from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import AssetClass, OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import IndexInstrument, Instrument, OptionContract
from nautilus_trader.model.objects import Price, Quantity

from src.adapters.mapper import InstrumentRecord

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _venue_for(exchange: str) -> Venue:
    # NFO/BFO option contracts still settle/clear under the NSE/BSE
    # venue for Nautilus's purposes — our own nautilus_id already
    # encodes "-NFO"/"-NSE" etc. in the symbol portion, so the Venue
    # itself just needs to be a valid, consistent tag.
    return Venue(exchange)


def build_instrument(record: InstrumentRecord, ts_now_ns: int | None = None) -> Instrument:
    """
    Builds a real nautilus_trader Instrument from our InstrumentRecord.
    Raises ValueError for instrument_types we don't yet map (extend as
    you add FUTCUR/equity support).
    """
    if ts_now_ns is None:
        ts_now_ns = dt_to_unix_nanos(dt.datetime.now(tz=IST))

    symbol_part, _, venue_part = record.nautilus_id.partition("-")
    # Only options encode a real exchange venue in the nautilus_id suffix
    # (e.g. "...-NFO"/"...-BFO", from mapper.build_nautilus_id()). For
    # indices the suffix is our own "-INDEX" marker, not a venue — always
    # fall back to record.exchange (the real NSE/BSE) in that case.
    known_venue_suffixes = {"NFO", "BFO", "NSE", "BSE"}
    venue = venue_part if venue_part in known_venue_suffixes else record.exchange
    instrument_id = InstrumentId(Symbol(symbol_part), _venue_for(venue))

    if record.instrument_type == "INDEX":
        return IndexInstrument(
            instrument_id=instrument_id,
            raw_symbol=Symbol(record.symbol),
            currency=INR,
            price_precision=2,
            size_precision=0,
            price_increment=Price.from_str(f"{record.tick_size:.2f}"),
            size_increment=Quantity.from_int(1),
            ts_event=ts_now_ns,
            ts_init=ts_now_ns,
        )

    if record.instrument_type == "OPTION":
        if record.option_type is None or record.strike is None or record.expiry is None:
            raise ValueError(
                f"OPTION record for '{record.nautilus_id}' is missing "
                f"option_type/strike/expiry — cannot build OptionContract"
            )
        y, m, d = (int(x) for x in record.expiry.split("-"))
        # Options expire at market close (15:30 IST) on expiry day.
        expiry_dt = dt.datetime(y, m, d, 15, 30, tzinfo=IST)

        return OptionContract(
            instrument_id=instrument_id,
            raw_symbol=Symbol(record.nautilus_id),
            asset_class=AssetClass.INDEX,
            currency=INR,
            price_precision=2,
            price_increment=Price.from_str(f"{record.tick_size:.2f}"),
            multiplier=Quantity.from_int(1),
            lot_size=Quantity.from_int(record.lot_size),
            underlying=record.symbol,
            option_kind=OptionKind.CALL if record.option_type == "CE" else OptionKind.PUT,
            strike_price=Price.from_str(f"{record.strike:.2f}"),
            activation_ns=ts_now_ns,
            expiration_ns=dt_to_unix_nanos(expiry_dt),
            ts_event=ts_now_ns,
            ts_init=ts_now_ns,
        )

    raise ValueError(
        f"Don't know how to build a Nautilus instrument for "
        f"instrument_type='{record.instrument_type}' (record: {record.nautilus_id}). "
        f"Extend build_instrument() — e.g. add an Equity branch — as needed."
    )


def find_record_by_real_instrument_id(mapper, real_instrument_id) -> InstrumentRecord | None:
    """
    Reverse lookup: given a REAL nautilus_trader InstrumentId (e.g.
    "NIFTY.NSE"), find the InstrumentRecord that produces it.

    Needed because our mapper's own `nautilus_id` field (e.g.
    "NIFTY-INDEX", used as the join key across WS/REST identifier
    formats — see mapper.py's docstring) is NOT the same string as the
    real Nautilus InstrumentId build_instrument() constructs. Both the
    live data client and live execution client need this same reverse
    mapping, so it lives here rather than being duplicated in each.

    O(n) over the instrument master — fine for a watchlist-sized set;
    cache the result at the call site if this ever needs to run on a
    hot path with a large instrument universe.
    """
    for record in mapper._by_nautilus_id.values():
        try:
            if build_instrument(record).id == real_instrument_id:
                return record
        except ValueError:
            continue
    return None
