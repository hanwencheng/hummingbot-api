"""
Strategy 1: Dynamic and Quick Stop loss

Characteristics:
- Dynamic profit taking (profit_skew = 1)
- Quick stop loss (stop_loss_skew = 0)
- Progressive accumulation (accumulate_skew = 1)
"""
from decimal import Decimal

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class DynamicQuickStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_dynamic_quick_stop"
    controller_type: str = "market_making"

    # Strategy-specific skew parameters
    accumulate_skew: Decimal = Decimal("1")  # Progressive accumulation
    profit_skew: Decimal = Decimal("1")      # Dynamic profit taking
    stop_loss_skew: Decimal = Decimal("0")   # Quick stop loss


class DynamicQuickStop(StrategyControllerBase):
    """
    Dynamic and Quick Stop loss strategy.

    Features:
    - Dynamic profit taking at varying levels
    - Quick stop loss at final level
    - Progressive accumulation entries
    - Responsive to market movements
    """