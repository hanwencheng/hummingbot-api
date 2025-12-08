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

from decimal import Decimal
from typing import Dict, List, Optional
from pydantic import Field, field_validator
from datetime import datetime

import pandas as pd
import pandas_ta as ta 

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType, PriceType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.core.data_type.common import MarketDict
from sqlalchemy.sql.operators import OperatorType


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
        default=Decimal("500"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the quote asset amount for each level:",
        }
    )
    cooldown_time: int = Field(
        default=60 * 5, gt=0,
        json_schema_extra={
            "prompt": "Enter the cooldown time in seconds after executing a signal (e.g., 300 for 5 minutes): ",
            "prompt_on_new": True, "is_updatable": True},
    )
    accumulate_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage between accumulation levels (e.g., 0.01 for 1%):",
        }
    )
    profit_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for profit taking (e.g., 0.02 for 2%):",
        }
    )
    stop_loss_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the percentage for stop loss (e.g., 0.015 for 1.5%):",
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

    # Skew parameters for level price adjustments
    profit_skew: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the profit skew multiplier (e.g., 1.0 for equal spacing):",
        }
    )
    stop_loss_skew: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the stop loss skew multiplier (e.g., 0.0 for equal pricing):",
        }
    )
    bb_long_threshold: float = Field(
        default=0.1,
        json_schema_extra={
            "prompt": "Enter the BBP threshold for BUY signals (e.g., 0.2 for oversold): ",
            "prompt_on_new": True
        }
    )
    bb_short_threshold: float = Field(
        default=0.9,
        json_schema_extra={
            "prompt": "Enter the BBP threshold for SELL signals (e.g., 0.8 for overbought): ",
            "prompt_on_new": True
        }
    )


    @field_validator('accumulate_pct', 'profit_pct', 'stop_loss_pct', 'profit_skew', 'stop_loss_skew')
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
        self.filled_executor_ids = set()  # Set of filled executor IDs
        self.stop_loss_waiting_until = 0  # Large timestamp until which to wait after stop loss
        self.last_executor_creation_time = 0  # Track last time executors were created for cooldown
        self.most_recent_stop_loss_time = 0

        # Final level prices - updated only when orders change
        self.final_stop_loss_price = None
        self.direction_buy = True
        self.reverse_order_open = False

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

        # Update filled executor states
        self._update_executor_fill_states()

        # Check and close expired executors
        expired_actions = self._check_and_close_expired_executors()
        if expired_actions:
            self.logger().info(f":: Found {len(expired_actions)} expired executors, returning early")
            return expired_actions

        has_stop_loss_hit = self._check_if_hit_final_stop_loss()
        if has_stop_loss_hit and not self.reverse_order_open:
            actions.extend(self.handle_stop_loss_hit())
            return actions
        elif not has_stop_loss_hit and self.reverse_order_open:
            self.logger().info(f"stop loss period ended, close reverse order")
            actions.extend(self._close_all_positions_and_stop())
            self.reverse_order_open = False
            return actions
        elif has_stop_loss_hit and self.reverse_order_open:
            self.logger().info(f"reverse order opening now")
            return actions
            

        #========= Check cooldown time before creating new executors
        if not self._check_cooldown_time():
            return actions
        
        signal = self._calculate_signal(self.processed_data)
        signal_actions = self._handle_signal(signal)
        actions.extend(signal_actions)

        return actions

    def _check_cooldown_time(self) -> bool:
        """
        Check if enough time has passed since last executor creation.

        Returns:
            bool: True if cooldown period has passed, False otherwise
        """
        current_time = self.market_data_provider.time()
        time_since_last_signal = current_time - self.last_executor_creation_time

        if time_since_last_signal >= self.config.cooldown_time:
            return True
        else:
            remaining_cooldown = self.config.cooldown_time - time_since_last_signal
            self.logger().info(f":: No Signal Check, still in cooldown period. {remaining_cooldown:.1f}s remaining")
            return False

    def _calculate_signal(self, processed_data: any):
        signal = 0

        bbu = processed_data['bbu']
        bbl = processed_data['bbl']
        close_bt = processed_data['close']
        rsi = processed_data["rsi"]
        passed_bbp = processed_data["bbp"]
        avg_gain = processed_data["avg_gain"]
        prev_price = processed_data["prev_price"]
        avg_loss = processed_data["avg_loss"]
        readable_time = datetime.fromtimestamp(processed_data["timestamp"]).strftime('%Y-%m-%d %H:%M:%S')
        bbp = (close_bt - bbl)/(bbu - bbl)

        # Calculate real-time RSI using current price and stored averages
        realtime_rsi = self._calculate_realtime_rsi(
            close_bt, prev_price, avg_gain, avg_loss
        )

        # not accurate if it is lower than 4% of the bbu

        if bbp > self.config.bb_short_threshold:
            signal = -1 
        if bbp < self.config.bb_long_threshold:
            signal = 1
            
        if not signal == 0:
            self.logger().info(f"close_bt is {close_bt:.4f}, bbp is {bbp:.4f}, and bbu is {bbu:.4f}, and bbl is {bbl:.4f}")
            self.logger().info(f"Time: {readable_time} | Signal: {signal} | BBP: {bbp:.4f} | price: {close_bt:.4f} | rsi: {realtime_rsi:.2f}")
        return signal

    def _calculate_realtime_rsi(self, current_price: Decimal, prev_price: float, prev_avg_gain: float, prev_avg_loss: float, period: int = 14) -> float:
        # Calculate price change
        price_change = float(current_price) - prev_price

        # Separate gains and losses
        gain = max(price_change, 0)
        loss = max(-price_change, 0)

        # Update averages using Wilder's method: new_avg = (old_avg * (n-1) + new_value) / n
        new_avg_gain = (prev_avg_gain * (period - 1) + gain) / period
        new_avg_loss = (prev_avg_loss * (period - 1) + loss) / period

        # Calculate RSI
        if new_avg_loss == 0:
            rsi = 100.0
        else:
            rs = new_avg_gain / new_avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    def _handle_signal(self, signal: int) -> List[ExecutorAction]:
        """
        Handle new signal with dynamic level management.
        """
        actions = []

        entry_price = 0

        if signal == 0:
            return actions

        trade_side = TradeType.BUY if signal > 0 else TradeType.SELL
        direction_buy = True if signal > 0 else False

        #========= close unfilled levels
        if self._get_total_position() != Decimal("0") and self.direction_buy != direction_buy:
            self.logger().info(f"Important! Closing all positions and stopping due to direction change")
            actions.extend(self._close_all_positions_and_stop())
        else:
            # Check if all levels are filled
            if len(self.filled_executor_ids) >= self.config.level_number:
                self.logger().info(f":: All levels filled ({len(self.filled_executor_ids)} >= {self.config.level_number}), waiting for profit/stop loss")
                return actions  # All levels filled, wait for profit/stop loss

            current_price = self._get_current_price()
            if signal > 0:  # BUY signal
                entry_price = current_price * (1 - self.config.accumulate_pct)
            else:  # SELL signal
                entry_price = current_price * (1 + self.config.accumulate_pct)

            # Stop all unfilled executors and create new ones
            stopped_actions = self._stop_unfilled_executor()
            self.logger().info(f":: Stopped {len(stopped_actions)} unfilled executors")
            actions.extend(stopped_actions)

        self.direction_buy = direction_buy

        #========= Create new unfilled levels
        unfilled_levels_number = self.config.level_number - len(self.filled_executor_ids)
        self.logger().info(f":: Creating {unfilled_levels_number} new executors")

        for level_index in range(unfilled_levels_number):
            action = self._create_level_executor(level_index, entry_price, trade_side, False)
            if action:
                self.logger().info(f":: Created executor for level {level_index} and trade side is: {trade_side} from {entry_price:.4f} ")
                actions.append(action)

        # Update last signal time when executors are created
        if actions:
            self.last_executor_creation_time = self.market_data_provider.time()
            self.logger().info(f":: Updated last_signal_time, next signal allowed after {self.config.cooldown_time}s cooldown")

        # Update final level prices after creating new levels
        # self._update_final_level_prices()

        self.logger().info(f":: _handle_signal returning {len(actions)} actions")
        return actions

    def handle_stop_loss_hit(self) -> List[ExecutorAction]:
        actions = []
        actions.extend(self._close_all_positions_and_stop())
        current_price = self._get_current_price()
        trade_side = TradeType.SELL if self.direction_buy else TradeType.BUY
        
        enable_reverse_order = True
        if enable_reverse_order:
            for level_index in range(self.config.level_number):
                action = self._create_level_executor(level_index, current_price, trade_side, True)
                if action:
                    self.logger().info(f"Important! Reverse executor for level {level_index} and trade side is: {trade_side} at {current_price:.4f}")
                    actions.append(action)   
            self.reverse_order_open = True
        return actions    

    def _create_triple_barrier_config(self, direction_buy: bool, accumulate_price: Decimal,
                                      profit_price: Decimal, stop_loss_price: Decimal) -> TripleBarrierConfig:
        """
        Create TripleBarrierConfig for a specific level.
        Converts price-based levels to percentage-based barriers.
        """
        # Calculate take profit percentage from accumulate price
        if direction_buy:
            take_profit_pct = (profit_price - accumulate_price) / accumulate_price
        else:
            take_profit_pct = (accumulate_price - profit_price) / accumulate_price

        # Calculate stop loss percentage from accumulate price
        if direction_buy:
            stop_loss_pct = (accumulate_price - stop_loss_price) / accumulate_price
        else:
            stop_loss_pct = (stop_loss_price - accumulate_price) / accumulate_price

        # Ensure percentages are positive
        take_profit_pct = abs(take_profit_pct)
        stop_loss_pct = abs(stop_loss_pct)

        return TripleBarrierConfig(
            take_profit=take_profit_pct,
            stop_loss=stop_loss_pct,
            time_limit=self.config.time_limit_hours * 3600,  # Convert hours to seconds
            open_order_type=OrderType.LIMIT,  # Entry order is limit order
            take_profit_order_type=OrderType.LIMIT,  # Profit at specific price level
            stop_loss_order_type=OrderType.MARKET,  # Stop loss triggers market order
            time_limit_order_type=OrderType.MARKET  # Time limit triggers market order
        )

    def _create_level_executor(self, level_index: int, entry_price: Decimal, trade_side: TradeType, is_reverse: bool = False) -> Optional[CreateExecutorAction]:
        """
        Create executor for specific level with given entry price and trade direction.
        """
        try:
            direction_multiplier = 1 if trade_side == TradeType.BUY else -1
            reverse_direction_multiplier = 0 if is_reverse else 1
            # Calculate amount
            final_profit_level = entry_price * (
                1 + direction_multiplier * self.config.level_number * self.config.profit_pct
            )

            final_accumulate_price = entry_price * (
                1 - self.config.level_number * self.config.accumulate_pct *
                direction_multiplier
            )

            final_stop_loss_level = final_accumulate_price - (entry_price * direction_multiplier * self.config.level_number * self.config.stop_loss_pct)
    
            accumulate_price = entry_price - (
                level_index * self.config.accumulate_pct * entry_price *
                direction_multiplier * reverse_direction_multiplier
            )

            # Calculate profit level price
            profit_price = final_profit_level - (
                level_index * self.config.profit_pct * entry_price *
                direction_multiplier * self.config.profit_skew
            )

            # Calculate stop loss level price
            # stop_loss_price = final_stop_loss_level + (
            #     level_index * self.config.stop_loss_pct * entry_price *
            #     direction_multiplier * self.config.stop_loss_skew
            # )
            stop_loss_price = final_stop_loss_level

            # Create triple barrier configuration
            triple_barrier = self._create_triple_barrier_config(trade_side == TradeType.BUY, 
                accumulate_price, profit_price, stop_loss_price
            )

            amount = self.config.level_size / accumulate_price

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
            )

            return CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=executor_config
            )

        except Exception as e:
            self.logger().error(f"Error creating level executor for level {level_index}: {e}")
            return None

    def _get_executor_by_id(self, executor_id: str):
        """Get executor by executor ID"""
        for executor in self.executors_info:
            if executor.id == executor_id:
                return executor
        return None

    def _get_filled_executor_ids(self) -> List[str]:
        """Get list of unfilled (active) executor IDs"""
        unfilled_ids = []
        for executor in self.executors_info:
            if executor.is_trading and executor.id in self.filled_executor_ids:
                unfilled_ids.append(executor.id)
        return unfilled_ids
    
    def _get_unfilled_executor_ids(self) -> List[str]:
        """Get list of unfilled (active) executor IDs"""
        unfilled_ids = []
        for executor in self.executors_info:
            if executor.is_active and executor.id not in self.filled_executor_ids:
                unfilled_ids.append(executor.id)
        return unfilled_ids

    def _update_executor_fill_states(self):
        """Update filled executor states based on accumulation order fill status"""
        self.filled_executor_ids.clear()
        for executor in self.executors_info:
            if executor.is_trading:
                # Accumulation order has started filling
                self.filled_executor_ids.add(executor.id)

    def _check_and_close_expired_executors(self) -> List[ExecutorAction]:
        """Check for expired executors and close them"""
        actions = []
        current_time = self.market_data_provider.time()
        time_limit_seconds = self.config.time_limit_hours * 3600

        expired_executor_ids = []
        for executor in self.executors_info:
            if (executor.is_active and
                executor.id not in self.filled_executor_ids):

                # Check if executor has exceeded time limit
                executor_age = current_time - executor.timestamp
                if executor_age > time_limit_seconds:
                    expired_executor_ids.append(executor.id)
                    actions.append(StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor.id
                    ))

        # TODO to be deleted after test
        if expired_executor_ids:
            self.logger().info(f"Closing expired executors: {expired_executor_ids}")

        return actions

    def _get_executor_time_remaining(self, executor_id: str) -> float:
        """Get remaining time for executor in hours"""
        executor = self._get_executor_by_id(executor_id)
        if not executor:
            return 0.0

        current_time = self.market_data_provider.time()
        time_limit_seconds = self.config.time_limit_hours * 3600
        executor_age = current_time - executor.timestamp
        remaining_seconds = time_limit_seconds - executor_age
        return max(0.0, remaining_seconds / 3600)

    def _check_if_hit_final_stop_loss(self) -> bool:
        """Check if we should enter stop loss waiting period based on recent executor closures"""
        current_time = self.market_data_provider.time()

        # Find the most recent stop loss closure
        for executor in self.executors_info:
            if executor.close_type == CloseType.STOP_LOSS:
                # Use close_timestamp if available, otherwise fall back to timestamp
                executor_close_time = getattr(executor, 'close_timestamp', 0)

                if executor_close_time > self.most_recent_stop_loss_time:
                    self.logger().info(f"new top loss find, Stop loss waiting period active.")
                    self.most_recent_stop_loss_time = executor_close_time

        # Check if enough time has passed since the most recent stop loss
        time_since_stop_loss = current_time - self.most_recent_stop_loss_time
        waiting_time_seconds = self.config.stop_loss_waiting_time_hours * 3600

        if time_since_stop_loss < waiting_time_seconds:
            return True  # Still in waiting period

        return False  # Waiting period over, can proceed

    def _close_all_positions_and_stop(self) -> List[ExecutorAction]:
        """Close all active positions"""
        actions = []
        for executor in self.executors_info:
            if executor.is_trading or executor.is_active:
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    keep_position=False,
                    executor_id=executor.id
                ))
        self._reset_strategy_state()
        return actions

    def _stop_unfilled_executor(self) -> List[ExecutorAction]:
        actions = []
        unfilled_executor_ids = self._get_unfilled_executor_ids()
        for executor_id in unfilled_executor_ids:
            actions.append(StopExecutorAction(
                controller_id=self.config.id,
                executor_id=executor_id
            ))
        return actions

    def _reset_strategy_state(self):
        """Reset strategy state for new signal"""
        self.filled_executor_ids.clear()
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

        # Level info
        status.append(f"Filled Levels: {len(self.filled_executor_ids)} / {self.config.level_number}")
        status.append(f"Total Position: {self._get_total_position()}")

        # Show active executor info
        active_executors = [e for e in self.executors_info if e.is_active and e.id not in self.filled_executor_ids]
        if active_executors:
            status.append("")
            status.append("Active Executors:")
            for executor in active_executors[:3]:  # Show up to 3 for brevity
                time_remaining = self._get_executor_time_remaining(executor.id)
                status.append(f"  {executor.id[:8]}... | {time_remaining:.1f}h remaining | Price: {executor.entry_price}")

            if len(active_executors) > 3:
                status.append(f"  ... and {len(active_executors) - 3} more")

        # Show filled executor IDs if any
        if self.filled_executor_ids:
            status.append(f"Filled Executor IDs: {sorted(list([id[:8] + '...' for id in self.filled_executor_ids]))}")

        return status