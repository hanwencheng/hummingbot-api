from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Union
from pydantic import Field, validator

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType


class LevelGroup:
    """Represents a group of levels: accumulate, profit, and stop_loss"""
    def __init__(self, level_index: int):
        self.level_index = level_index
        self.accumulate_level: Optional[dict] = None
        self.profit_level: Optional[dict] = None
        self.stop_loss_level: Optional[dict] = None
        self.accumulate_active = True
        self.profit_active = False
        self.stop_loss_active = False
        self.accumulate_executor_id: Optional[str] = None
        self.profit_executor_id: Optional[str] = None
        self.stop_loss_executor_id: Optional[str] = None


class StrategyControllerConfigBase(ControllerConfigBase):
    """
    Base configuration for strategy controllers with level-based trading.
    """
    controller_type: str = "market_making"
    candles_config: List[CandlesConfig] = []

    # Trading pair configuration
    connector_name: str = Field(
        default="binance",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the name of the connector to use (e.g., binance):",
        }
    )
    trading_pair: str = Field(
        default="BTC-USDT",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the trading pair to trade on (e.g., BTC-USDT):",
        }
    )

    # Strategy parameters
    ticker: str = Field(
        default="BTC-USDT",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the ticker symbol:",
        }
    )
    direction_buy: bool = Field(
        default=True,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Is the direction buy? (True for buy, False for sell):",
        }
    )
    entry_price: Decimal = Field(
        default=Decimal("50000"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the entry price:",
        }
    )
    level_number: int = Field(
        default=5,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the number of levels:",
        }
    )
    level_pct: Decimal = Field(
        default=Decimal("0.01"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage difference between levels (e.g., 0.01 for 1%):",
        }
    )
    level_size: Decimal = Field(
        default=Decimal("100"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the quote asset amount for each level:",
        }
    )
    time_limit: int = Field(
        default=24,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the maximum strategy duration in hours:",
        }
    )

    # Strategy-specific parameters (to be set by subclasses)
    final_profit_level: Optional[Decimal] = None
    final_stop_loss_level: Optional[Decimal] = None
    accumulate_skew: Decimal = Field(default=Decimal("1"))
    profit_skew: Decimal = Field(default=Decimal("1"))
    stop_loss_skew: Decimal = Field(default=Decimal("0"))

    @validator('level_pct', 'accumulate_skew', 'profit_skew', 'stop_loss_skew')
    def validate_percentages(cls, v):
        if v < 0 or v > 1:
            raise ValueError("Percentage values must be between 0 and 1")
        return v


class StrategyControllerBase(ControllerBase):
    """
    Base class for level-based trading strategies.
    Implements the core logic for managing level groups with accumulate, profit, and stop-loss levels.
    """

    def __init__(self, config: StrategyControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.level_groups: List[LevelGroup] = []
        self.strategy_start_time = self.current_timestamp
        self.strategy_active = True
        self.total_accumulated_position = Decimal("0")

        # Calculate final levels based on strategy type
        self._calculate_final_levels()

        # Initialize level groups
        self._initialize_level_groups()

    def _calculate_final_levels(self):
        """Calculate final profit and stop loss levels - to be overridden by subclasses"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        self.config.final_profit_level = self.config.entry_price * (
            1 + direction_multiplier * self.config.level_number * self.config.level_pct
        )
        self.config.final_stop_loss_level = self.config.entry_price * (
            1 - direction_multiplier * 2 * self.config.level_number * self.config.level_pct
        )

    def _initialize_level_groups(self):
        """Initialize all level groups with their prices and sizes"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        for i in range(self.config.level_number):
            level_group = LevelGroup(i)

            # Calculate accumulate level
            accumulate_price = self.config.entry_price - (
                i * self.config.level_pct * direction_multiplier * self.config.accumulate_skew
            )
            level_group.accumulate_level = {
                "price": accumulate_price,
                "size": self.config.level_size,
                "side": TradeType.BUY if self.config.direction_buy else TradeType.SELL
            }

            # Calculate profit level
            profit_price = self.config.final_profit_level - (
                i * self.config.level_pct * direction_multiplier * self.config.profit_skew
            )
            profit_size = self.config.level_size / accumulate_price * profit_price
            level_group.profit_level = {
                "price": profit_price,
                "size": profit_size,
                "side": TradeType.SELL if self.config.direction_buy else TradeType.BUY
            }

            # Calculate stop loss level
            stop_loss_price = self.config.final_stop_loss_level + (
                (self.config.level_number - i + 1) * self.config.level_pct *
                direction_multiplier * self.config.stop_loss_skew
            )
            stop_loss_size = self.config.level_size / accumulate_price * stop_loss_price
            level_group.stop_loss_level = {
                "price": stop_loss_price,
                "size": stop_loss_size,
                "side": TradeType.SELL if self.config.direction_buy else TradeType.BUY
            }

            self.level_groups.append(level_group)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Main strategy logic - determine what actions to take"""
        actions = []

        if not self.strategy_active:
            return actions

        # Check time limit
        if self._check_time_limit_exceeded():
            return self._close_all_positions_and_stop()

        # Check final profit/stop loss levels
        current_price = self._get_current_price()
        if self._check_final_levels_hit(current_price):
            return self._close_all_positions_and_stop()

        # Manage active level groups
        for level_group in self.level_groups:
            actions.extend(self._manage_level_group(level_group))

        return actions

    def _get_current_price(self) -> Decimal:
        """Get current market price"""
        connector = self.connectors.get(self.config.connector_name)
        if connector and connector.ready:
            mid_price = connector.get_mid_price(self.config.trading_pair)
            return Decimal(str(mid_price))
        return self.config.entry_price

    def _check_time_limit_exceeded(self) -> bool:
        """Check if strategy has exceeded time limit"""
        time_elapsed_hours = (self.current_timestamp - self.strategy_start_time) / 3600
        return time_elapsed_hours > self.config.time_limit

    def _check_final_levels_hit(self, current_price: Decimal) -> bool:
        """Check if final profit or stop loss levels have been hit"""
        if self.config.direction_buy:
            return (current_price >= self.config.final_profit_level or
                   current_price <= self.config.final_stop_loss_level)
        else:
            return (current_price <= self.config.final_profit_level or
                   current_price >= self.config.final_stop_loss_level)

    def _manage_level_group(self, level_group: LevelGroup) -> List[ExecutorAction]:
        """Manage a single level group"""
        actions = []

        # Check if accumulate level should be active and create executor if needed
        if level_group.accumulate_active and not level_group.accumulate_executor_id:
            actions.append(self._create_accumulate_executor(level_group))

        # Check if profit level should be active and create executor if needed
        if level_group.profit_active and not level_group.profit_executor_id:
            actions.append(self._create_profit_executor(level_group))

        # Check if stop loss level should be active and create executor if needed
        if level_group.stop_loss_active and not level_group.stop_loss_executor_id:
            actions.append(self._create_stop_loss_executor(level_group))

        return actions

    def _create_accumulate_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create executor for accumulate level"""
        accumulate_level = level_group.accumulate_level
        executor_config = PositionExecutorConfig(
            timestamp=self.current_timestamp,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=accumulate_level["side"],
            amount_quote=accumulate_level["size"],
            entry_price=accumulate_level["price"],
            triple_barrier_config=TripleBarrierConfig(
                take_profit=None,
                stop_loss=None,
                trailing_stop=None,
                time_limit=None
            )
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        level_group.accumulate_executor_id = action.executor_config.id
        return action

    def _create_profit_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create executor for profit level"""
        profit_level = level_group.profit_level
        executor_config = PositionExecutorConfig(
            timestamp=self.current_timestamp,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=profit_level["side"],
            amount_quote=profit_level["size"],
            entry_price=profit_level["price"],
            triple_barrier_config=TripleBarrierConfig(
                take_profit=None,
                stop_loss=None,
                trailing_stop=None,
                time_limit=None
            )
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        level_group.profit_executor_id = action.executor_config.id
        return action

    def _create_stop_loss_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create executor for stop loss level"""
        stop_loss_level = level_group.stop_loss_level
        executor_config = PositionExecutorConfig(
            timestamp=self.current_timestamp,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=stop_loss_level["side"],
            amount_quote=stop_loss_level["size"],
            entry_price=stop_loss_level["price"],
            triple_barrier_config=TripleBarrierConfig(
                take_profit=None,
                stop_loss=None,
                trailing_stop=None,
                time_limit=None
            )
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        level_group.stop_loss_executor_id = action.executor_config.id
        return action

    def _close_all_positions_and_stop(self) -> List[ExecutorAction]:
        """Close all active positions and stop the strategy"""
        actions = []
        self.strategy_active = False

        # Stop all active executors
        for level_group in self.level_groups:
            if level_group.accumulate_executor_id:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=level_group.accumulate_executor_id
                ))
            if level_group.profit_executor_id:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=level_group.profit_executor_id
                ))
            if level_group.stop_loss_executor_id:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=level_group.stop_loss_executor_id
                ))

        return actions

    def did_trade(self, executor_id: str, side: TradeType, amount: Decimal, price: Decimal):
        """Handle trade events and update level group states"""
        for level_group in self.level_groups:
            if level_group.accumulate_executor_id == executor_id:
                # Accumulate level filled
                level_group.accumulate_active = False
                level_group.profit_active = True
                level_group.stop_loss_active = True
                self.total_accumulated_position += amount
                break
            elif level_group.profit_executor_id == executor_id:
                # Profit level filled
                level_group.profit_active = False
                level_group.stop_loss_active = False
                level_group.accumulate_active = True
                self.total_accumulated_position -= amount
                break
            elif level_group.stop_loss_executor_id == executor_id:
                # Stop loss level hit
                level_group.profit_active = False
                level_group.stop_loss_active = False
                level_group.accumulate_active = False
                self.total_accumulated_position -= amount
                break

    def on_stop(self):
        """Clean up when controller stops"""
        self.strategy_active = False