import logging
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Union
from pydantic import Field, field_validator, validator

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig, ExecutionStrategy
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.core.data_type.common import PriceType, MarketDict
from hummingbot.strategy_v2.executors.data_types import ConnectorPair

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
        default="hyperliquid_perpetual",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the name of the connector to use (e.g., binance):",
        }
    )
    trading_pair: str = Field(
        default="BTC-USD",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the trading pair to trade on (e.g., BTC-USD):",
        }
    )

    # Strategy parameters
    ticker: str = Field(
        default="BTC-USD",
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

    @field_validator('level_pct', 'accumulate_skew', 'profit_skew', 'stop_loss_skew')
    def validate_percentages(cls, v):
        if v < 0 or v > 1:
            raise ValueError("Percentage values must be between 0 and 1")
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        """
        Update markets dictionary with the connector and trading pair this strategy needs.
        This method in the config class is called during framework initialization.
        """
        return markets.add_or_update(self.connector_name, self.trading_pair)


class StrategyControllerBase(ControllerBase):
    """
    Base class for level-based trading strategies.
    Implements the core logic for managing level groups with accumulate, profit, and stop-loss levels.
    """

    def __init__(self, config: StrategyControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.level_groups: List[LevelGroup] = []
        self.strategy_start_time = None  # Will be set in first update cycle
        self.total_accumulated_position = Decimal("0")
        self.processed_data = {}  # Initialize processed data

        # Initialize market data provider (same as PMM strategy)
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair
            )
        ])

    def _calculate_final_levels(self):
        """Calculate final profit and stop loss levels - to be overridden by subclasses"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        self.config.final_profit_level = self.config.entry_price * (
            1 + direction_multiplier * self.config.level_number * self.config.level_pct * self.config.entry_price
        )
        self.config.final_stop_loss_level = self.config.entry_price * (
            1 - direction_multiplier * 2 * self.config.level_number * self.config.level_pct * self.config.entry_price
        )

    def _initialize_level_groups(self):
        """Initialize all level groups with their prices and sizes"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        for i in range(self.config.level_number):
            level_group = LevelGroup(i)

            # Calculate accumulate level
            accumulate_price = self.config.entry_price - (
                i * self.config.level_pct * self.config.entry_price* direction_multiplier * self.config.accumulate_skew
            )
            level_group.accumulate_level = {
                "price": accumulate_price,
                "size": self.config.level_size,
                "side": TradeType.BUY if self.config.direction_buy else TradeType.SELL
            }

            # Calculate profit level
            profit_price = self.config.final_profit_level - (
                i * self.config.level_pct * self.config.entry_price * direction_multiplier * self.config.profit_skew
            )
            # Size calculation: level_size / price of accumulate_level * price of profit_level
            profit_size = self.config.level_size / accumulate_price * profit_price
            level_group.profit_level = {
                "price": profit_price,
                "size": profit_size,
                "side": TradeType.SELL if self.config.direction_buy else TradeType.BUY
            }

            # Calculate stop loss level
            stop_loss_price = self.config.final_stop_loss_level + (
                (self.config.level_number - i - 1) * self.config.level_pct * self.config.entry_price *
                direction_multiplier * self.config.stop_loss_skew
            )
            # Size calculation: level_size / price of accumulate_level * price of stop_loss_level
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

        # Initialize start time and level groups on first call
        if self.strategy_start_time is None:
            self.strategy_start_time = self.market_data_provider.time()
            # Calculate final levels and initialize level groups
            self._calculate_final_levels()
            self._initialize_level_groups()

        # Check time limit
        if self._check_time_limit_exceeded():
            return self._close_all_positions_and_stop()

        # Check final profit/stop loss levels only if we have accumulated positions
        if self.total_accumulated_position != Decimal("0"):
            current_price = self._get_current_price()
            if self._check_final_levels_hit(current_price):
                return self._close_all_positions_and_stop()

        # Manage active level groups
        for level_group in self.level_groups:
            actions.extend(self._manage_level_group(level_group))

        return actions

    async def update_processed_data(self):
        """
        Update the processed data for the controller.
        Gets current market price and position information.
        """
        # Get current market price with fallback for when connector isn't ready
        try:
            reference_price = self.market_data_provider.get_price_by_type(
                self.config.connector_name,
                self.config.trading_pair,
                PriceType.MidPrice
            )
        except (ValueError, AttributeError, KeyError) as e:
            # Connector not ready yet, use entry price as fallback and return early
            self.processed_data = {
                "reference_price": self.config.entry_price,
                "position_amount": Decimal("0"),
                "unrealized_pnl_pct": Decimal("0"),
                "current_timestamp": self.market_data_provider.time()
            }
            return

        # Get current position if any
        position_held = None
        if hasattr(self, 'positions_held') and self.positions_held:
            position_held = next(
                (position for position in self.positions_held if
                 position.trading_pair == self.config.trading_pair and
                 position.connector_name == self.config.connector_name),
                None
            )

        # Calculate position metrics
        if position_held is not None:
            position_amount = position_held.amount
            unrealized_pnl_pct = (
                position_held.unrealized_pnl_quote / position_held.amount_quote
                if position_held.amount_quote != 0 else Decimal("0")
            )
        else:
            position_amount = Decimal("0")
            unrealized_pnl_pct = Decimal("0")

        # Update processed data
        self.processed_data = {
            "reference_price": reference_price,
            "position_amount": position_amount,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "current_timestamp": self.market_data_provider.time()
        }

    def _get_current_price(self) -> Decimal:
        """Get current market price"""
        # Try to get from processed data first
        if self.processed_data and "reference_price" in self.processed_data:
            return Decimal(str(self.processed_data["reference_price"]))

        # Fallback to direct connector access
        connector = self.connectors.get(self.config.connector_name)
        if connector and connector.ready:
            mid_price = connector.get_mid_price(self.config.trading_pair)
            return Decimal(str(mid_price))

        return self.config.entry_price

    def _check_time_limit_exceeded(self) -> bool:
        """Check if strategy has exceeded time limit"""
        if self.strategy_start_time is None:
            return False
        time_elapsed_hours = (self.market_data_provider.time() - self.strategy_start_time) / 3600
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
        # if level_group.stop_loss_active and not level_group.stop_loss_executor_id:
            # actions.append(self._create_stop_loss_executor(level_group))

        return actions

    def _create_accumulate_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create limit order executor for accumulate level"""
        accumulate_level = level_group.accumulate_level

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=accumulate_level["side"],
            amount=accumulate_level["size"],
            price=accumulate_level["price"],
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"accumulate_{level_group.level_index}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        level_group.accumulate_executor_id = action.executor_config.id
        return action

    def _create_profit_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create limit order executor for profit level"""
        profit_level = level_group.profit_level
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=profit_level["side"],
            amount=profit_level["size"],
            price=profit_level["price"],
            leverage=10,
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"profit_{level_group.level_index}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        level_group.profit_executor_id = action.executor_config.id
        return action

    def _create_stop_loss_executor(self, level_group: LevelGroup) -> CreateExecutorAction:
        """Create market order executor for stop loss level - triggers immediately when stop loss hit"""
        stop_loss_level = level_group.stop_loss_level
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=stop_loss_level["side"],
            amount=stop_loss_level["size"],
            price=stop_loss_level["price"],
            execution_strategy=ExecutionStrategy.MARKET,  # Stop loss should be market order
            level_id=f"stop_loss_{level_group.level_index}"
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

    def to_format_status(self) -> List[str]:
        """
        Get the status of the controller in a formatted way.
        Returns strategy-specific information for level-based trading.
        """
        status = []

        # Header
        header = f"Strategy: {self.config.controller_name} | {self.config.connector_name}:{self.config.trading_pair}"
        status.append(header)
        status.append("=" * len(header))

        # Basic strategy info
        direction = "BUY" if self.config.direction_buy else "SELL"
        status.append(f"Direction: {direction}")
        status.append(f"Entry Price: {self.config.entry_price}")
        status.append(f"Levels: {self.config.level_number}")
        status.append(f"Level Size: {self.config.level_size}")
        status.append(f"Level %: {self.config.level_pct:.2%}")
        status.append("")

        # Strategy state
        if hasattr(self, 'strategy_start_time') and self.strategy_start_time:
            elapsed = (self.market_data_provider.time() - self.strategy_start_time) / 3600
            status.append(f"Running Time: {elapsed:.1f}h / {self.config.time_limit}h")
        else:
            status.append("Status: Initializing...")

        status.append(f"Total Position: {self.total_accumulated_position}")
        status.append("")

        # Level group status
        if hasattr(self, 'level_groups') and self.level_groups:
            status.append("Level Groups Status:")
            for i, lg in enumerate(self.level_groups):
                acc_status = "✓" if lg.accumulate_active else "✗"
                prof_status = "✓" if lg.profit_active else "✗"
                stop_status = "✓" if lg.stop_loss_active else "✗"
                status.append(f"  L{i}: Acc:{acc_status} Prof:{prof_status} Stop:{stop_status}")

        return status