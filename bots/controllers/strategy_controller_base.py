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
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.core.data_type.common import PriceType, MarketDict


class StrategyControllerConfigBase(ControllerConfigBase):
    """
    Base configuration for strategy controllers with level-based trading using TripleBarrierConfig.
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
    profit_level_pct: Decimal = Field(
        default=Decimal("0.02"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for take profit orders (e.g., 0.02 for 2%):",
        }
    )
    stop_loss_pct: Decimal = Field(
        default=Decimal("0.015"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for stop loss orders (e.g., 0.015 for 1.5%):",
        }
    )
    accumulate_pct: Decimal = Field(
        default=Decimal("0.01"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for accumulate orders (e.g., 0.01 for 1%):",
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
        default=168,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the maximum strategy duration in hours:",
        }
    )
    leverage: int = Field(
        default=10,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the leverage to use for trading (e.g., 10 for 10x leverage):",
        }
    )

    # Strategy-specific parameters (to be set by subclasses)
    final_profit_level: Optional[Decimal] = None
    final_stop_loss_level: Optional[Decimal] = None
    final_accumulate_price: Optional[Decimal] = None
    accumulate_skew: Decimal = Field(default=Decimal("1"))
    profit_skew: Decimal = Field(default=Decimal("1"))
    stop_loss_skew: Decimal = Field(default=Decimal("0"))

    @field_validator('level_pct', 'profit_level_pct', 'stop_loss_pct', 'accumulate_pct', 'accumulate_skew', 'profit_skew', 'stop_loss_skew')
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
    Base class for level-based trading strategies using TripleBarrierConfig.
    Implements the core logic for managing levels with automatic stop-loss/take-profit handling.
    """

    def __init__(self, config: StrategyControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.strategy_start_time = None  # Will be set in first update cycle
        self.processed_data = {}  # Initialize processed data
        self.level_states = {}  # Track which levels are active
        self.strategy_stopped = False  # Track if strategy has been permanently stopped

        # Initialize market data provider (same as PMM strategy)
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair
            )
        ])

    def update_config(self, new_config: StrategyControllerConfigBase):
        """
        Update controller configuration and refresh all cached/calculated values.
        This ensures that config changes are properly reflected in trading behavior.
        """
        old_config = self.config
        self.config = new_config

        # Log the config update for debugging
        self.logger().info(f"Updating config for controller {self.config.id}")
        self.logger().info(f"Old entry_price: {old_config.entry_price}, New entry_price: {new_config.entry_price}")
        self.logger().info(f"Old level_size: {old_config.level_size}, New level_size: {new_config.level_size}")
        self.logger().info(f"Old leverage: {old_config.leverage}, New leverage: {new_config.leverage}")

        # Clear cached data that depends on configuration
        self.processed_data.clear()
        self.level_states.clear()
        self.strategy_stopped = False  # Reset strategy stop state on config update

        # Recalculate levels with new configuration
        self._calculate_final_levels()

        # Update market data provider if trading pair changed
        if (old_config.connector_name != new_config.connector_name or
            old_config.trading_pair != new_config.trading_pair):
            self.market_data_provider.initialize_rate_sources([
                ConnectorPair(
                    connector_name=new_config.connector_name,
                    trading_pair=new_config.trading_pair
                )
            ])

        # Call parent update_config to handle framework-level updates
        super().update_config(new_config)

        self.logger().info(f"Config update completed for controller {self.config.id}")

    @property
    def total_accumulated_position(self) -> Decimal:
        """
        Get total accumulated position from framework's position tracking.
        Replaces manual position tracking from original implementation.
        """
        position_held = next(
            (position for position in self.positions_held if
             position.trading_pair == self.config.trading_pair and
             position.connector_name == self.config.connector_name),
            None
        )
        return position_held.amount if position_held else Decimal("0")

    def _calculate_final_levels(self):
        """Calculate final profit and stop loss levels - to be overridden by subclasses"""
        direction_multiplier = 1 if self.config.direction_buy else -1

        self.config.final_profit_level = self.config.entry_price * (
            1 + direction_multiplier * self.config.level_number * self.config.profit_level_pct
        )

        self.config.final_accumulate_price = self.config.entry_price * (
            1 - self.config.level_number * self.config.accumulate_pct *
            direction_multiplier
        )

        self.config.final_stop_loss_level = self.config.final_accumulate_price - (self.config.entry_price * direction_multiplier * self.config.level_number * self.config.stop_loss_pct)
        

    def _calculate_level_prices(self, level_index: int) -> Dict[str, Decimal]:
        """
        Calculate prices for a specific level.
        Returns dict with 'accumulate_price', 'profit_price', 'stop_loss_price'
        """
        direction_multiplier = 1 if self.config.direction_buy else -1

        # Calculate accumulate price (entry point for this level)
        accumulate_price = self.config.entry_price - (
            level_index * self.config.accumulate_pct * self.config.entry_price *
            direction_multiplier * self.config.accumulate_skew
        )

        # Calculate profit level price
        profit_price = self.config.final_profit_level - (
            level_index * self.config.profit_level_pct * self.config.entry_price *
            direction_multiplier * self.config.profit_skew
        )

        # Calculate stop loss level price
        stop_loss_price = self.config.final_stop_loss_level + (
            level_index * self.config.stop_loss_pct * self.config.entry_price *
            direction_multiplier * self.config.stop_loss_skew
        )

        return {
            'accumulate_price': accumulate_price,
            'profit_price': profit_price,
            'stop_loss_price': stop_loss_price
        }

    def _create_triple_barrier_config(self, level_index: int, accumulate_price: Decimal,
                                      profit_price: Decimal, stop_loss_price: Decimal) -> TripleBarrierConfig:
        """
        Create TripleBarrierConfig for a specific level.
        Converts price-based levels to percentage-based barriers.
        """
        # Calculate take profit percentage from accumulate price
        if self.config.direction_buy:
            take_profit_pct = (profit_price - accumulate_price) / accumulate_price
        else:
            take_profit_pct = (accumulate_price - profit_price) / accumulate_price

        # Calculate stop loss percentage from accumulate price
        if self.config.direction_buy:
            stop_loss_pct = (accumulate_price - stop_loss_price) / accumulate_price
        else:
            stop_loss_pct = (stop_loss_price - accumulate_price) / accumulate_price

        # Ensure percentages are positive
        take_profit_pct = abs(take_profit_pct)
        stop_loss_pct = abs(stop_loss_pct)

        return TripleBarrierConfig(
            take_profit=take_profit_pct,
            stop_loss=stop_loss_pct,
            time_limit=self.config.time_limit * 3600,  # Convert hours to seconds
            open_order_type=OrderType.LIMIT,  # Entry order is limit order
            take_profit_order_type=OrderType.LIMIT,  # Profit at specific price level
            stop_loss_order_type=OrderType.MARKET,  # Stop loss triggers market order
            time_limit_order_type=OrderType.MARKET  # Time limit triggers market order
        )

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Main strategy logic - determine what actions to take"""
        actions = []

        # Initialize start time and level calculations on first call
        if self.strategy_start_time is None:
            self.strategy_start_time = self.market_data_provider.time()
            # Calculate final levels
            self._calculate_final_levels()

        # If strategy has been stopped, only return stop actions for any remaining active executors
        if self.strategy_stopped:
            return actions

        # Check time limit
        if self._check_time_limit_exceeded():
            self.strategy_stopped = True
            return self._close_all_positions_and_stop()

        # Check final profit/stop loss levels regardless of current position
        if self.total_accumulated_position != Decimal("0"):
            current_price = self._get_current_price()
            if self._check_final_levels_hit(current_price):
                self.strategy_stopped = True
                return self._close_all_positions_and_stop()

        # Create executors for levels that need to be active (only if strategy not stopped)
        for level_index in range(self.config.level_number):
            level_id = f"level_{level_index}"

            # Check if this level already has an active executor
            if not self._has_active_executor(level_id):
                action = self._create_level_executor(level_index)
                if action:
                    actions.append(action)

        return actions

    def _has_active_executor(self, level_id: str) -> bool:
        """Check if there's an active executor for this level"""
        return any(
            executor.is_active and executor.custom_info.get("level_id") == level_id
            for executor in self.executors_info
        )

    def _create_level_executor(self, level_index: int) -> Optional[CreateExecutorAction]:
        """
        Create a PositionExecutor with TripleBarrierConfig for a specific level.
        This replaces the original _create_accumulate_executor logic.
        """
        try:
            # Calculate level prices
            prices = self._calculate_level_prices(level_index)
            accumulate_price = prices['accumulate_price']
            profit_price = prices['profit_price']
            stop_loss_price = prices['stop_loss_price']

            # Create triple barrier configuration
            triple_barrier = self._create_triple_barrier_config(
                level_index, accumulate_price, profit_price, stop_loss_price
            )

            # Calculate amount (convert from quote to base amount)
            amount = self.config.level_size / accumulate_price

            # Create position executor config
            executor_config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=TradeType.BUY if self.config.direction_buy else TradeType.SELL,
                entry_price=accumulate_price,
                amount=amount,
                triple_barrier_config=triple_barrier,
                leverage=self.config.leverage,
                level_id=f"level_{level_index}"
            )

            action = CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=executor_config
            )

            return action

        except Exception as e:
            self.logger().error(f"Error creating level executor for level {level_index}: {e}")
            return None

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

    def _close_all_positions_and_stop(self) -> List[ExecutorAction]:
        """Close all active positions and stop the strategy"""
        actions = []

        # Stop all active executors
        for executor in self.executors_info:
            if executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=executor.id
                ))

        return actions

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
        if getattr(self, 'strategy_stopped', False):
            status.append("Status: STOPPED (Final levels hit)")
        elif hasattr(self, 'strategy_start_time') and self.strategy_start_time:
            elapsed = (self.market_data_provider.time() - self.strategy_start_time) / 3600
            status.append(f"Running Time: {elapsed:.1f}h / {self.config.time_limit}h")
            status.append("Status: ACTIVE")
        else:
            status.append("Status: Initializing...")

        status.append(f"Total Position: {self.total_accumulated_position}")
        status.append("")

        # Active executors status
        active_levels = []
        for executor in self.executors_info:
            if executor.is_active and "level_id" in executor.custom_info:
                level_id = executor.custom_info["level_id"]
                active_levels.append(level_id)

        status.append("Active Levels:")
        if active_levels:
            status.append(f"  {', '.join(sorted(active_levels))}")
        else:
            status.append("  None")

        # TripleBarrier configuration summary
        if hasattr(self, 'config') and self.config.final_profit_level:
            status.append("")
            status.append("Level Configuration:")
            status.append(f"  Final Profit: {self.config.final_profit_level}")
            status.append(f"  Last Accumulate Price: {self.config.final_accumulate_price}")
            status.append(f"  Final Stop Loss: {self.config.final_stop_loss_level}")
            status.append(f"  Skews - Acc:{self.config.accumulate_skew}, Prof:{self.config.profit_skew}, SL:{self.config.stop_loss_skew}")

        return status