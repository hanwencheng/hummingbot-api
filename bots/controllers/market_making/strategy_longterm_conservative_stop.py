"""
Strategy 4: Long-term and Conservative Stop loss

Characteristics:
- Long-term profit taking (profit_skew = 0)
- Conservative stop loss (stop_loss_skew = 1)
- Progressive accumulation (accumulate_skew = 1)
"""
from decimal import Decimal

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class LongtermConservativeStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_longterm_conservative_stop"
    controller_type: str = "market_making"

    # Strategy-specific skew parameters
    accumulate_skew: Decimal = Decimal("1")  # Progressive accumulation
    profit_skew: Decimal = Decimal("0")      # Long-term profit taking
    stop_loss_skew: Decimal = Decimal("1")   # Conservative stop loss


class LongtermConservativeStop(StrategyControllerBase):
    """
    Long-term and Conservative Stop loss strategy.

    Features:
    - Patient profit taking at final level
    - Conservative progressive stop losses
    - Progressive accumulation entries
    - Ideal for trending markets with volatility
    """