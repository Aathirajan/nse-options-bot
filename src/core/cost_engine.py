"""
core/cost_engine.py

Computes the real, all-in cost (in basis points of notional) of taking a
trade on Indian exchanges, so the Arbiter can net it against a signal's
raw alpha. This is intentionally kept as a pure, dependency-free module
so it's trivial to unit test and to keep in sync with current statutory
rates (these DO change — recheck against current NSE/SEBI circulars
before relying on this for real capital).

Formula (see settings.yaml for the actual configured rates):

    total_cost_bps = half_spread_bps
                    + slippage_bps
                    + stt_bps            (options: on premium, sell side only)
                    + turnover_bps
                    + brokerage_bps      (flat INR converted to bps of notional)
                    + gst_bps            (18% on brokerage + turnover charges)
                    + stamp_duty_bps     (buy side only)
                    + sebi_charges_bps
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.signals import Direction


@dataclass(frozen=True)
class CostConfig:
    stt_options_sell_pct: float = 0.125
    nse_turnover_pct: float = 0.035
    gst_pct: float = 18.0
    sebi_charges_pct: float = 0.0001
    stamp_duty_pct: float = 0.003
    brokerage_per_order_inr: float = 5.0


@dataclass(frozen=True)
class CostBreakdown:
    half_spread_bps: float
    slippage_bps: float
    stt_bps: float
    turnover_bps: float
    brokerage_bps: float
    gst_bps: float
    stamp_duty_bps: float
    sebi_bps: float

    @property
    def total_bps(self) -> float:
        return (
            self.half_spread_bps
            + self.slippage_bps
            + self.stt_bps
            + self.turnover_bps
            + self.brokerage_bps
            + self.gst_bps
            + self.stamp_duty_bps
            + self.sebi_bps
        )


class CostEngine:
    def __init__(self, config: CostConfig | None = None):
        self.config = config or CostConfig()

    def estimate_cost_bps(
        self,
        *,
        direction: Direction,
        notional_inr: float,
        bid_ask_spread_pct: float,
        expected_slippage_pct: float,
        is_option: bool,
        is_closing_leg: bool = False,
    ) -> CostBreakdown:
        """
        Estimate total transaction cost in bps of notional for one leg
        (entry OR exit — call twice and sum for a full round trip).

        Args:
            direction: BUY or SELL for this leg.
            notional_inr: price * quantity * lot_size, in INR.
            bid_ask_spread_pct: current bid-ask spread as % of mid price.
            expected_slippage_pct: expected slippage beyond spread, as %.
            is_option: whether this is an options instrument (affects STT).
            is_closing_leg: True if this is exiting a position bought earlier
                in the same trade (affects which side STT applies to).
        """
        cfg = self.config

        half_spread_bps = (bid_ask_spread_pct / 2) * 100
        slippage_bps = expected_slippage_pct * 100

        # STT on options applies on premium value, on the SELL side only
        # (whether that's an opening short or a closing sale of a long).
        stt_bps = 0.0
        if is_option and direction == Direction.SELL:
            stt_bps = cfg.stt_options_sell_pct * 100

        turnover_bps = cfg.nse_turnover_pct * 100
        sebi_bps = cfg.sebi_charges_pct * 100

        # Stamp duty applies on buy side only.
        stamp_duty_bps = cfg.stamp_duty_pct * 100 if direction == Direction.BUY else 0.0

        brokerage_bps = (
            (cfg.brokerage_per_order_inr / notional_inr) * 10_000
            if notional_inr > 0
            else 0.0
        )

        # GST is charged on (brokerage + turnover charges), not on the
        # whole notional.
        gst_base_bps = brokerage_bps + turnover_bps
        gst_bps = gst_base_bps * (cfg.gst_pct / 100)

        return CostBreakdown(
            half_spread_bps=half_spread_bps,
            slippage_bps=slippage_bps,
            stt_bps=stt_bps,
            turnover_bps=turnover_bps,
            brokerage_bps=brokerage_bps,
            gst_bps=gst_bps,
            stamp_duty_bps=stamp_duty_bps,
            sebi_bps=sebi_bps,
        )

    def estimate_round_trip_cost_bps(
        self,
        *,
        entry_direction: Direction,
        notional_inr: float,
        bid_ask_spread_pct: float,
        expected_slippage_pct: float,
        is_option: bool,
    ) -> float:
        """Convenience: entry leg + exit leg (opposite direction) combined."""
        exit_direction = (
            Direction.SELL if entry_direction == Direction.BUY else Direction.BUY
        )
        entry_cost = self.estimate_cost_bps(
            direction=entry_direction,
            notional_inr=notional_inr,
            bid_ask_spread_pct=bid_ask_spread_pct,
            expected_slippage_pct=expected_slippage_pct,
            is_option=is_option,
        )
        exit_cost = self.estimate_cost_bps(
            direction=exit_direction,
            notional_inr=notional_inr,
            bid_ask_spread_pct=bid_ask_spread_pct,
            expected_slippage_pct=expected_slippage_pct,
            is_option=is_option,
            is_closing_leg=True,
        )
        return entry_cost.total_bps + exit_cost.total_bps
