"""
Dynamic BB-Grid Strategy Controller Base

Advanced hybrid strategy combining signal-based trading with dynamic grid management:

1. Signal-triggered entry level adjustment (signal + 2 spread as entry_price)
2. Dynamic level management (keep filled, update unfilled on new signals)
3. No accumulation when all levels are filled
4. Hourly time limit for incomplete accumulation levels
5. Final profit/stop loss with position closure and signal waiting
6. No cooldown time between signals

Key Logic:
- Each signal sets new entry level = signal_price + 2*spread
- Unfilled levels update to new entry level, filled levels remain
- All levels filled = wait for profit/stop loss, ignore new signals
- Time limit triggers closure of incomplete levels
- Stop loss hit = wait for stop_loss_waiting_time before next signal
"""

import logging
from decimal import Decimal
from typing import Dict, List, Optional, Set
from pydantic import Field, field_validator

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType, PriceType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.core.data_type.common import MarketDict


class DynamicBBGridControllerConfigBase(ControllerConfigBase):
    """
    Configuration for Dynamic BB-Grid strategy controllers.
    """
    controller_type: str = "dynamic_bb_grid"
    candles_config: List[CandlesConfig] = []

    # Trading pair configuration
    connector_name: str = Field(
        default="hyperliquid_perpetual",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the connector name (e.g., hyperliquid_perpetual):",
        }
    )
    trading_pair: str = Field(
        default="BTC-USD",
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the trading pair to trade on (e.g., BTC-USD):",
        }
    )

    # Level configuration (no entry_price in config)
    level_number: int = Field(
        default=5,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the number of accumulation levels:",
        }
    )
    level_size: Decimal = Field(
        default=Decimal("100"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the quote asset amount for each level:",
        }
    )
    accumulate_pct: Decimal = Field(
        default=Decimal("0.01"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage between accumulation levels (e.g., 0.01 for 1%):",
        }
    )
    profit_pct: Decimal = Field(
        default=Decimal("0.02"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for profit taking (e.g., 0.02 for 2%):",
        }
    )

    # Spread configuration for entry level calculation
    spread_multiplier: Decimal = Field(
        default=Decimal("2.0"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the spread multiplier for entry level calculation (e.g., 2.0):",
        }
    )

    # Time limits
    time_limit_hours: int = Field(
        default=1,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the time limit for incomplete levels in hours (e.g., 1):",
        }
    )

    # Final profit/stop loss levels
    final_profit_pct: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the final profit percentage (e.g., 0.05 for 5%):",
        }
    )
    final_stop_loss_pct: Decimal = Field(
        default=Decimal("0.03"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the final stop loss percentage (e.g., 0.03 for 3%):",
        }
    )

    # Stop loss waiting time
    stop_loss_waiting_time_hours: int = Field(
        default=4,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the waiting time after stop loss in hours (e.g., 4):",
        }
    )

    # Perpetual trading parameters
    leverage: int = Field(
        default=10,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the leverage to use for trading (e.g., 10 for 10x leverage):",
        }
    )
    position_mode: PositionMode = Field(
        default="HEDGE",
        json_schema_extra={"prompt": "Enter the position mode (HEDGE/ONEWAY):"}
    )

    @field_validator('accumulate_pct', 'profit_pct', 'final_profit_pct', 'final_stop_loss_pct')
    def validate_percentages(cls, v):
        if v < 0 or v > 1:
            raise ValueError("Percentage values must be between 0 and 1")
        return v

    @field_validator('position_mode', mode="before")
    @classmethod
    def validate_position_mode(cls, v: str) -> PositionMode:
        if isinstance(v, str):
            if v.upper() in PositionMode.__members__:
                return PositionMode[v.upper()]
            raise ValueError(f"Invalid position mode: {v}. Valid options are: {', '.join(PositionMode.__members__)}")
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class DynamicBBGridControllerBase(ControllerBase):
    """
    Base class for Dynamic BB-Grid trading strategies.
    """

    def __init__(self, config: DynamicBBGridControllerConfigBase, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # Strategy state
        self.processed_data = {"signal": 0}
        self.current_entry_price = None  # Set dynamically by signals
        self.filled_levels: Set[int] = set()  # Track which levels are filled
        self.stop_loss_waiting_until = float('inf')  # Timestamp until which to wait after stop loss

        # Final level prices - updated only when orders change
        self.final_take_profit_price = None
        self.final_stop_loss_price = None

        # Initialize market data provider
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair
            )
        ])

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Main strategy logic with dynamic level management.
        """
        actions = []

        # Check if we're in stop loss waiting period
        current_time = self.market_data_provider.time()
        if current_time < self.stop_loss_waiting_until:
            return actions  # Wait and do nothing
        else:
            self.stop_loss_waiting_until = float('inf')

        # Check final profit/stop loss if we have positions
        if self._get_total_position() != Decimal("0"):
            if self._check_and_handle_final_levels():
                return []  # Final levels handled, return empty actions

        # Process signals
        signal = self.processed_data.get("signal", 0)
        return self._handle_signal(signal)


    def _handle_signal(self, signal: int) -> List[ExecutorAction]:
        """
        Handle new signal with dynamic level management.
        """
        actions = []

        if signal == 0:
            return actions 

        # Determine trade direction
        trade_side = TradeType.BUY if signal > 0 else TradeType.SELL

        # Check if all levels are filled
        if len(self.filled_levels) >= self.config.level_number:
            return actions  # All levels filled, wait for profit/stop loss

        # Calculate new entry price based on signal direction
        current_price = self._get_current_price()
        spread = self._get_spread()

        if signal > 0:  # BUY signal
            # Entry above current price for BUY accumulation
            new_entry_price = current_price + (spread * self.config.spread_multiplier)
        else:  # SELL signal
            # Entry below current price for SELL accumulation
            new_entry_price = current_price - (spread * self.config.spread_multiplier)

        # Update entry price and reset timer
        self.current_entry_price = new_entry_price

        # Create/update unfilled levels
        for level_index in range(self.config.level_number):
            if level_index in self.filled_levels:
                continue  # Skip filled levels

            level_id = f"level_{level_index}"

            # Stop existing unfilled level executor
            existing_executor = self._get_level_executor(level_id)
            if existing_executor and existing_executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=existing_executor.id
                ))

            # Create new level executor with updated entry price
            action = self._create_level_executor(level_index, new_entry_price, trade_side)
            if action:
                actions.append(action)

        # Update final level prices after creating new levels
        self._update_final_level_prices()

        return actions

    def _create_level_executor(self, level_index: int, entry_price: Decimal, trade_side: TradeType) -> Optional[CreateExecutorAction]:
        """
        Create executor for specific level with given entry price and trade direction.
        """
        try:
            # Calculate accumulation price for this level based on trade direction
            if trade_side == TradeType.BUY:
                # BUY: accumulate at lower prices (entry_price - level_offset)
                accumulate_price = entry_price * (1 - self.config.accumulate_pct * (level_index + 1))
            else:
                # SELL: accumulate at higher prices (entry_price + level_offset)
                accumulate_price = entry_price * (1 + self.config.accumulate_pct * (level_index + 1))

            # Calculate amount
            amount = self.config.level_size / accumulate_price

            # Create triple barrier config (profit only)
            triple_barrier = TripleBarrierConfig(
                take_profit=self.config.profit_pct,
                stop_loss=None,  # No individual stop loss
                time_limit=self.config.time_limit_hours * 3600,
                open_order_type=OrderType.LIMIT,
                take_profit_order_type=OrderType.LIMIT,
                time_limit_order_type=OrderType.MARKET
            )

            # Create executor config
            executor_config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=trade_side,
                entry_price=accumulate_price,
                amount=amount,
                triple_barrier_config=triple_barrier,
                leverage=self.config.leverage,
                level_id=f"level_{level_index}"
            )

            return CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=executor_config
            )

        except Exception as e:
            self.logger().error(f"Error creating level executor for level {level_index}: {e}")
            return None

    def _get_level_executor(self, level_id: str):
        """Get executor for specific level"""
        for executor in self.executors_info:
            if executor.custom_info.get("level_id") == level_id:
                return executor
        return None

    def _check_and_handle_final_levels(self) -> bool:
        """Check if final profit or stop loss levels have been hit and handle them"""
        if self.final_take_profit_price is None and self.final_stop_loss_price is None:
            return False

        current_price = self._get_current_price()
        if current_price == Decimal("0"):
            return False

        # Check if current price hits final levels
        # For BUY accumulation: TP when price >= final_take_profit_price, SL when price <= final_stop_loss_price
        # For SELL accumulation: TP when price <= final_take_profit_price, SL when price >= final_stop_loss_price
        take_profit_hit = False
        stop_loss_hit = False

        if self._is_buy_accumulation():
            # BUY accumulation: price goes UP for profit, DOWN for stop loss
            take_profit_hit = (self.final_take_profit_price and
                              current_price >= self.final_take_profit_price)
            stop_loss_hit = (self.final_stop_loss_price and
                            current_price <= self.final_stop_loss_price)
        else:
            # SELL accumulation: price goes DOWN for profit, UP for stop loss
            take_profit_hit = (self.final_take_profit_price and
                              current_price <= self.final_take_profit_price)
            stop_loss_hit = (self.final_stop_loss_price and
                            current_price >= self.final_stop_loss_price)

        if not take_profit_hit and not stop_loss_hit:
            return False

        self._close_all_positions_and_stop()

        # If stop loss hit, set waiting time
        if stop_loss_hit:
            self.stop_loss_waiting_until = (
                self.market_data_provider.time() +
                self.config.stop_loss_waiting_time_hours * 3600
            )
            self.logger().info(f"Stop loss hit at {current_price} (final SL: {self.final_stop_loss_price}). Waiting {self.config.stop_loss_waiting_time_hours} hours before next signal.")
        else:
            self.logger().info(f"Final profit hit at {current_price} (final TP: {self.final_take_profit_price}). Ready for next signal.")

        self._reset_strategy_state()

        return True

    def _close_incomplete_levels(self) -> List[ExecutorAction]:
        """Close all incomplete (unfilled) levels due to time limit"""
        actions = []

        # Close all active unfilled level executors
        for executor in self.executors_info:
            if executor.is_active and "level_id" in executor.custom_info:
                level_num = int(executor.custom_info["level_id"].split("_")[1])
                if level_num not in self.filled_levels:
                    actions.append(StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor.id
                    ))

        # Reset state for new signal
        self._reset_strategy_state()

        return actions

    def _close_all_positions_and_stop(self) -> List[ExecutorAction]:
        """Close all active positions"""
        actions = []
        for executor in self.executors_info:
            if executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=executor.id
                ))
        return actions

    def _reset_strategy_state(self):
        """Reset strategy state for new signal"""
        self.current_entry_price = None
        self.filled_levels.clear()
        self.final_take_profit_price = None
        self.final_stop_loss_price = None

    def _get_current_price(self) -> Decimal:
        """Get current market price"""
        try:
            return self.market_data_provider.get_price_by_type(
                self.config.connector_name,
                self.config.trading_pair,
                PriceType.MidPrice
            )
        except:
            connector = self.connectors.get(self.config.connector_name)
            if connector and connector.ready:
                return Decimal(str(connector.get_mid_price(self.config.trading_pair)))
            return Decimal("0")

    def _get_spread(self) -> Decimal:
        """Get current bid-ask spread"""
        try:
            connector = self.connectors.get(self.config.connector_name)
            if connector and connector.ready:
                bid_price = Decimal(str(connector.get_price(self.config.trading_pair, False)))
                ask_price = Decimal(str(connector.get_price(self.config.trading_pair, True)))
                return ask_price - bid_price
        except:
            pass
        return self._get_current_price() * Decimal("0.001")  # Fallback: 0.1% of price

    def _get_total_position(self) -> Decimal:
        """Get total position size"""
        position_held = next(
            (position for position in self.positions_held if
             position.trading_pair == self.config.trading_pair and
             position.connector_name == self.config.connector_name),
            None
        )
        return position_held.amount if position_held else Decimal("0")

    def _get_average_entry_price(self) -> Decimal:
        """Calculate average entry price of active positions"""
        total_value = Decimal("0")
        total_amount = Decimal("0")

        for executor in self.executors_info:
            if executor.is_active and executor.side == TradeType.BUY:
                total_value += executor.entry_price * executor.amount
                total_amount += executor.amount

        return total_value / total_amount if total_amount > 0 else Decimal("0")

    def _update_final_level_prices(self):
        """Update final level prices based on active executors"""
        highest_take_profit_price = None
        lowest_stop_loss_price = None

        for executor in self.executors_info:
            if executor.is_active and executor.side == TradeType.BUY:
                # Calculate take profit price for this level
                take_profit_price = executor.entry_price * (1 + self.config.profit_pct)

                # Calculate stop loss price for this level
                stop_loss_price = executor.entry_price * (1 - self.config.final_stop_loss_pct)

                # Track highest take profit price
                if highest_take_profit_price is None or take_profit_price > highest_take_profit_price:
                    highest_take_profit_price = take_profit_price

                # Track lowest stop loss price
                if lowest_stop_loss_price is None or stop_loss_price < lowest_stop_loss_price:
                    lowest_stop_loss_price = stop_loss_price

        self.highest_take_profit_price = highest_take_profit_price
        self.lowest_stop_loss_price = lowest_stop_loss_price

    async def update_processed_data(self):
        """
        Update processed data with signal information.
        This method should be overridden by subclasses to implement specific signal logic.
        """
        self.processed_data = {"signal": 0}

    def to_format_status(self) -> List[str]:
        """Get formatted status information"""
        status = []

        # Header
        header = f"Dynamic BB-Grid: {self.config.controller_name} | {self.config.connector_name}:{self.config.trading_pair}"
        status.append(header)
        status.append("=" * len(header))

        # Strategy state
        current_time = self.market_data_provider.time()
        if current_time < self.stop_loss_waiting_until:
            remaining_wait = (self.stop_loss_waiting_until - current_time) / 3600
            status.append(f"Status: WAITING (Stop loss cooldown: {remaining_wait:.1f}h remaining)")

        # Current signal and entry price
        signal = self.processed_data.get("signal", 0)
        signal_str = "BUY" if signal > 0 else "SELL" if signal < 0 else "HOLD"
        status.append(f"Signal: {signal_str} ({signal})")

        if self.current_entry_price:
            status.append(f"Entry Price: {self.current_entry_price}")

        # Level info
        status.append(f"Filled Levels: {len(self.filled_levels)} / {self.config.level_number}")
        status.append(f"Total Position: {self._get_total_position()}")

        return status