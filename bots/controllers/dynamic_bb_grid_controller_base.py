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
from typing import List, Optional
from pydantic import Field, field_validator
from datetime import datetime
from pydantic_core.core_schema import ValidationInfo

import pandas as pd
import pandas_ta as ta 

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType, PriceType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TrailingStop, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.core.data_type.common import MarketDict

class DynamicBBGridControllerConfig(ControllerConfigBase):
    """
    Configuration for Dynamic BB-Grid strategy controllers.
    """
    controller_type: str = "dynamic_bb_grid"
    candles_config: List[CandlesConfig] = []
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
            "prompt_on_new": True
        }
    )
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
            "prompt_on_new": True
        }
    )
    interval: str = Field(
        default="15m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True
        }
    )
    # Bollinger Bands parameters
    bb_length: int = Field(
        default=20,
        json_schema_extra={
            "prompt": "Enter the Bollinger Bands length: ",
            "prompt_on_new": True
        }
    )
    bb_std: float = Field(
        default=2.0,
        json_schema_extra={
            "prompt": "Enter the Bollinger Bands standard deviation: ",
            "prompt_on_new": True
        }
    )
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
    time_limit_hours: float = Field(
        default=1,
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the time limit for incomplete levels in hours (e.g., 1):",
        }
    )

    # Stop loss waiting time
    stop_loss_waiting_time_hours: float = Field(
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
    reverse_skew: Decimal = Field(
        default=Decimal("0.2"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "The skew multiplier for reverse order(e.g., 1.0 for equal spacing):",
        }
    )
    profit_skew: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the take profit skew multiplier (e.g., 0.0 for equal pricing):",
        }
    )
    stop_loss_skew: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={
            "prompt_on_new": True,
            "prompt": "Enter the stop loss skew multiplier (e.g., 0.0 for equal pricing):",
        }
    )
    bb_weak_threshold: float = Field(
        default=0.04,
        json_schema_extra={
            "prompt": "The Bollinger Band width threshold for weak trend, a percentage calculated by (BBU-BBL)/BBU",
            "prompt_on_new": True
        }
    )
    bb_strong_threshold: float = Field(
        default=0.08,
        json_schema_extra={
            "prompt": "The Bollinger Band width threshold for strong trend, a percentage calculated by (BBU-BBL)/BBU",
            "prompt_on_new": True
        }
    )


    @field_validator('accumulate_pct', 'profit_pct', 'stop_loss_pct', 'profit_skew', 'stop_loss_skew', 'reverse_skew', 'bb_weak_threshold', 'bb_strong_threshold')
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

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class DynamicBBGridController(ControllerBase):
    """
    Base class for Dynamic BB-Grid trading strategies.
    """

    def __init__(self, config: DynamicBBGridControllerConfig, *args, **kwargs):
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
        self.enable_reverse_order = False

        # Initialize market data provider
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair
            )
        ])

    async def update_processed_data(self):
        """
        Update processed data with Bollinger Bands signal.
        Generates both BUY and SELL signals based on Bollinger Bands position.
        """
        try:
            # Get candles data
            df = self.market_data_provider.get_candles_df(
                connector_name=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.interval,
                max_records=self.max_records + 1100
            )

            if df is None or len(df) < self.config.bb_length:
                # Not enough data for BB calculation
                self.logger().warning(f"Backtesting Debug: Insufficient data for BB calculation. Need {self.config.bb_length}, got {len(df) if df is not None else 0}")
                self.processed_data = {
                    "signal": 0,
                    "features": df if df is not None else pd.DataFrame()
                }
                return

            # Calculate Bollinger Bands indicators
            df.ta.bbands(
                length=self.config.bb_length,
                lower_std=self.config.bb_std,
                upper_std=self.config.bb_std,
                append=True
            )
            macd_fast = 720
            macd_slow = 1560
            macd_signal = 540
            df.ta.macd(fast=macd_fast, slow=macd_slow, signal=macd_signal, append=True)
            df.ta.rsi(length=14, append=True)
            # current_price = self.market_data_provider.get_price_by_type(self.config.connector_name,self.config.trading_pair,PriceType.MidPrice)
            bb_suffix = f"{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"
            macdh = df[f"MACDh_{macd_fast}_{macd_slow}_{macd_signal}"]
            macd = df[f"MACD_{macd_fast}_{macd_slow}_{macd_signal}"]
            bbp_col = f"BBP_{bb_suffix}"
            bbu_col = f"BBU_{bb_suffix}"
            bbl_col = f"BBL_{bb_suffix}"
            df["bbp"] = bbp = (df["close"] - df[bbl_col]) / (df[bbu_col] - df[bbl_col])
            df["bbu"] = df[bbu_col]
            df["bbl"] = df[bbl_col]
            df["rsi"] = rsi = df["RSI_14"]
            df["macd"] = macd
            df["macdh"] = macdh
            # Calculate Wilder's EMA components for real-time RSI calculation
            # Calculate price changes and store previous price
            df['prev_price'] = df['close'].shift(1)
            # df['price_change'] = df['close'].diff()
            # df['gain'] = df['price_change'].where(df['price_change'] > 0, 0)
            # df['loss'] = -df['price_change'].where(df['price_change'] < 0, 0)

            # Initialize avg_gain and avg_loss using Wilder's EMA method
            alpha = 1.0 / 14  # Wilder's smoothing factor for 14-period RSI
            # df['avg_gain'] = df['gain'].ewm(alpha=alpha, adjust=False).mean()
            # df['avg_loss'] = df['loss'].ewm(alpha=alpha, adjust=False).mean()
        

            # Store processed data
            self.processed_data = {
                "features": df,
                "bbp": df[bbp_col].iloc[-1],
                "bbu": df[bbu_col].iloc[-1],
                "bbl": df[bbl_col].iloc[-1],
                "rsi": df["RSI_14"].iloc[-1],
                "close": df["close"].iloc[-1],
                "prev_price": df["prev_price"].iloc[-1],
                "macd": macd.iloc[-1],
                "macdh": macdh.iloc[-1]
                # "avg_gain": df["avg_gain"].iloc[-1],
                # "avg_loss": df["avg_loss"].iloc[-1]
            }

        except Exception as e:
            self.logger().error(f"Error updating processed data: {e}")
            import traceback
            self.logger().error(f"Backtesting Debug: Full traceback: {traceback.format_exc()}")
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}

    def _create_triple_barrier_config(self, direction_buy: bool, accumulate_price: Decimal,
                                      profit_price: Decimal, stop_loss_price: Decimal, is_reverse: bool = False) -> TripleBarrierConfig:
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

        time_limit = self.config.stop_loss_waiting_time_hours * 3600 if is_reverse else self.config.time_limit_hours * 3600 


        return TripleBarrierConfig(
            take_profit=take_profit_pct,
            stop_loss=stop_loss_pct,
            # trailing_stop=TrailingStop(
            #     activation_price = Decimal(0.004),
            #     trailing_delta = Decimal(0.001)),
            time_limit=time_limit,  # Convert hours to seconds
            open_order_type=OrderType.LIMIT,  # Entry order is limit order
            take_profit_order_type=OrderType.LIMIT,  # Profit at specific price level
            stop_loss_order_type=OrderType.MARKET,  # Stop loss triggers market order
            time_limit_order_type=OrderType.MARKET  # Time limit triggers market order
        )

    def _create_level_executor(self, level_index: int, entry_price: Decimal, trade_side: TradeType, level_size_multiplier: float, is_reverse: bool = False) -> Optional[CreateExecutorAction]:
        """
        Create executor for specific level with given entry price and trade direction.
        """
        try:
            direction_multiplier = 1 if trade_side == TradeType.BUY else -1
            reverse_direction_multiplier = self.config.reverse_skew if is_reverse else 1
            # Calculate amount
            final_profit_level = entry_price * (
                1 + direction_multiplier * self.config.level_number * self.config.profit_pct
            )

            final_accumulate_price = entry_price * (
                1 - self.config.level_number * self.config.accumulate_pct *
                direction_multiplier * reverse_direction_multiplier
            )

            final_stop_loss_level = final_accumulate_price - (
                entry_price * direction_multiplier *
                self.config.level_number * self.config.stop_loss_pct)
    
            accumulate_price = entry_price - (
                min(level_index, 3) * self.config.accumulate_pct * entry_price *
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
                accumulate_price, profit_price, stop_loss_price, is_reverse
            )

            amount = self.config.level_size / accumulate_price

            self.logger().debug(f"level size is: {self.config.level_size}, and accumulate_price is {accumulate_price}, and level_size_multiplier is {Decimal(level_size_multiplier)} and leverage is {self.config.leverage}, and amount is {amount * Decimal(level_size_multiplier)}, and profit price is {profit_price}, stop loss is{stop_loss_price}")

            # Create executor config
            executor_config = PositionExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=trade_side,
                entry_price=accumulate_price,
                amount=amount * Decimal(level_size_multiplier),
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
            if executor.id in self.filled_executor_ids:
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
            if executor.is_trading and executor.close_type == None and executor.is_active:
                # Accumulation order has started filling
                self.filled_executor_ids.add(executor.id)
                self.logger().debug(f'filled executor id with closeType: {executor.close_type} type {executor.type}, status {executor.status}, is_active {executor.is_active}, id: {executor.id}, close at: {executor.close_timestamp}')
        self.logger().debug(f'find {len(self.filled_executor_ids)} filled exectuors')

    def _check_and_close_expired_executors(self) -> List[ExecutorAction]:
        """Check for expired executors and close them"""
        actions = []
        current_time = self.market_data_provider.time()
        time_limit_seconds = self.config.time_limit_hours * 3600

        expired_executor_ids = []
        for executor in self.executors_info:
            if (executor.is_active and executor.id not in self.filled_executor_ids):
                # Check if executor has exceeded time limit
                executor_age = current_time - executor.timestamp
                self.logger().debug(f'current time {current_time} - executor.timestamp{executor.timestamp} = {executor_age}')
                if executor_age > time_limit_seconds:
                    expired_executor_ids.append(executor.id)
                    actions.append(StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor.id
                    ))

        # TODO to be deleted after test
        if expired_executor_ids:
            self.logger().debug(f"Closing expired executors: {expired_executor_ids}")

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
                    self.logger().info(f"new stop loss find {executor_close_time}, Stop loss waiting period active.")
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
            if executor.is_active:
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
        total_position = 0;
        for executor in self.executors_info:
            if executor.id in self.filled_executor_ids:
                total_position = total_position + executor.filled_amount_quote
        return total_position

    def _get_average_entry_price(self) -> Decimal:
        """Calculate average entry price of active positions"""
        total_value = Decimal("0")
        total_amount = Decimal("0")

        for executor in self.executors_info:
            if executor.is_active and executor.side == TradeType.BUY:
                total_value += executor.entry_price * executor.amount
                total_amount += executor.amount

        return total_value / total_amount if total_amount > 0 else Decimal("0")


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