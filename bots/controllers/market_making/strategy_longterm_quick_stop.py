"""
Strategy 2: Long-term and Quick Stop loss

Characteristics:
- Long-term profit taking (profit_skew = 0)
- Quick stop loss (stop_loss_skew = 0)
- Progressive accumulation (accumulate_skew = 1)
"""
from decimal import Decimal

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class LongtermQuickStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_longterm_quick_stop"
    controller_type: str = "market_making"

    # Strategy-specific skew parameters
    accumulate_skew: Decimal = Decimal("1")  # Progressive accumulation
    profit_skew: Decimal = Decimal("0")      # Long-term profit taking
    stop_loss_skew: Decimal = Decimal("0")   # Quick stop loss


class LongtermQuickStop(StrategyControllerBase):
    """
    Long-term and Quick Stop loss strategy.

    Features:
    - Patient profit taking at final level
    - Quick stop loss at final level
    - Progressive accumulation entries
    - Ideal for trending markets
    """