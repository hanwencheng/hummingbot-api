"""
Strategy 3: Dynamic and Conservative Stop loss

Characteristics:
- Dynamic profit taking (profit_skew = 1)
- Conservative stop loss (stop_loss_skew = 1)
- Progressive accumulation (accumulate_skew = 1)
"""
from decimal import Decimal

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class DynamicConservativeStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_dynamic_conservative_stop"
    controller_type: str = "market_making"

    # Strategy-specific skew parameters
    accumulate_skew: Decimal = Decimal("1")  # Progressive accumulation
    profit_skew: Decimal = Decimal("1")      # Dynamic profit taking
    stop_loss_skew: Decimal = Decimal("1")   # Conservative stop loss


class DynamicConservativeStop(StrategyControllerBase):
    """
    Dynamic and Conservative Stop loss strategy.

    Features:
    - Dynamic profit taking at varying levels
    - Conservative progressive stop losses
    - Progressive accumulation entries
    - Tolerant to market volatility
    """