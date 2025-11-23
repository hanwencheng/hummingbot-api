"""
Strategy 3: Dynamic and Conservative Stop loss
- final_profit_level = entry_price * (1 + direction_buy * level_number * level_pct)
- final_stop_loss_level = entry_price * (1 - direction_buy * 2 * level_number * level_pct)
- accumulate_skew = 1
- profit_skew = 1
- stop_loss_skew = 1
"""
from decimal import Decimal
from typing import List

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction

from ..strategy_controller_base import StrategyControllerBase, StrategyControllerConfigBase


class DynamicConservativeStopConfig(StrategyControllerConfigBase):
    controller_name: str = "strategy_dynamic_conservative_stop"
    controller_type: str = "market_making"

    def __init__(self, **data):
        super().__init__(**data)
        # Set strategy-specific parameters
        direction_multiplier = 1 if self.direction_buy else -1

        self.final_profit_level = self.entry_price * (
            1 + direction_multiplier * self.level_number * self.level_pct
        )
        self.final_stop_loss_level = self.entry_price * (
            1 - direction_multiplier * 2 * self.level_number * self.level_pct
        )
        self.accumulate_skew = Decimal("1")
        self.profit_skew = Decimal("1")
        self.stop_loss_skew = Decimal("1")


class DynamicConservativeStop(StrategyControllerBase):
    """
    Strategy 3: Dynamic and Conservative Stop loss

    This strategy uses:
    - Dynamic profit taking (profit_skew = 1) - takes profit at varying levels
    - Conservative stop loss (stop_loss_skew = 1) - progressive stop loss levels
    - Aggressive accumulation (accumulate_skew = 1) - accumulates at varying entry levels

    Characteristics:
    - Flexible profit taking and stop loss management
    - More tolerant to market volatility
    - Progressive risk management
    """

    def __init__(self, config: DynamicConservativeStopConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

    def _calculate_final_levels(self):
        """Override to use strategy-specific level calculations"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        self.config.final_profit_level = self.config.entry_price * (
            1 + direction_multiplier * self.config.level_number * self.config.level_pct
        )
        self.config.final_stop_loss_level = self.config.entry_price * (
            1 - direction_multiplier * 2 * self.config.level_number * self.config.level_pct
        )

        # Ensure skews are set correctly
        self.config.accumulate_skew = Decimal("1")
        self.config.profit_skew = Decimal("1")
        self.config.stop_loss_skew = Decimal("1")

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Override to add strategy-specific logic if needed.
        This strategy uses dynamic management for both profit and stop loss.
        """
        return super().determine_executor_actions()