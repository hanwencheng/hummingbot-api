"""
Strategy 2: Long-term and Quick Stop loss
- final_profit_level = entry_price * (1 + direction_buy * level_number * level_pct)
- final_stop_loss_level = entry_price * (1 - direction_buy * 2 * level_number * level_pct)
- accumulate_skew = 1
- profit_skew = 0
- stop_loss_skew = 0
"""
from decimal import Decimal
from typing import List

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class LongtermQuickStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_longterm_quick_stop"
    controller_type: str = "market_making"

    # Strategy-specific parameters (set in the strategy controller)
    accumulate_skew: Decimal = Decimal("1")
    profit_skew: Decimal = Decimal("0")
    stop_loss_skew: Decimal = Decimal("0")


class LongtermQuickStop(StrategyControllerBase):
    """
    Strategy 2: Long-term and Quick Stop loss

    This strategy uses:
    - Long-term profit taking (profit_skew = 0) - always takes profit at the highest level
    - Quick stop loss (stop_loss_skew = 0) - always stops at the final stop loss price
    - Aggressive accumulation (accumulate_skew = 1) - accumulates at varying entry levels

    Characteristics:
    - Patient profit taking - waits for maximum profit
    - Strict stop loss protection
    - Good for trending markets
    """

    def __init__(self, config: LongtermQuickStopConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config


    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Override to add strategy-specific logic if needed.
        This strategy focuses on long-term profit taking.
        """
        return super().determine_executor_actions()