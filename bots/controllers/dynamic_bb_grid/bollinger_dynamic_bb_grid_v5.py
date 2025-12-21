"""
Bollinger Bands Dynamic BB-Grid Strategy V4

Advanced Bollinger Bands strategy with dynamic grid management:
- Signal-based entry level adjustment (signal + 2*profit_pct)
- Keep filled levels, update unfilled levels on new signals
- BUY signal when BBP < bb_long_threshold (oversold condition)
- Dynamic level management with no cooldown
- Final profit/stop loss with waiting period after stop loss
"""

from decimal import Decimal
from typing import List
from hummingbot.strategy_v2.controllers.controller_base import ExecutorAction
from datetime import datetime, timedelta, timezone

from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from ..bb_stage_v6 import BBStage
from ..dynamic_bb_grid_controller_base import (
    DynamicBBGridController,
    DynamicBBGridControllerConfig
)

def get_readable_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=0))).strftime('%Y-%m-%d %H:%M:%S')


class BollingerDynamicBBGridV4Config(DynamicBBGridControllerConfig):
    controller_name: str = "bollinger_dynamic_bb_grid_v4"


class BollingerDynamicBBGridV4Controller(DynamicBBGridController):
    """
    Bollinger Bands Dynamic BB-Grid Strategy Controller.

    Dynamic Grid Logic:
    - Unfilled levels update to new entry prices, filled levels remain unchanged
    - All levels filled = wait for profit/stop loss targets
    """

    def __init__(self, config: BollingerDynamicBBGridV4Config, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.stage = BBStage(0.008, 0.03)
        self.config.controller_name = config.controller_name
        self.max_records = self.config.bb_length + 20 # Extra buffer for BB calculation
        # Set up candles config if not provided
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [
                CandlesConfig(
                    connector=config.candles_connector,
                    trading_pair=config.candles_trading_pair,
                    interval=config.interval,
                    max_records=self.max_records 
                )
            ]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Main strategy logic with dynamic level management.
        """
        actions = []

        self.logger().debug(f":: start action in v4")

        # Update filled executor states
        self._update_executor_fill_states()

        # Check and close expired executors
        expired_actions = self._check_and_close_expired_executors()
        if expired_actions:
            self.logger().debug(f":: Found {len(expired_actions)} expired executors, returning early")
            return expired_actions

        has_stop_loss_hit = self._check_if_hit_final_stop_loss()
        if has_stop_loss_hit and not self.reverse_order_open:
            self.stage.to_normal_stage()
            actions.extend(self.handle_stop_loss_hit())
            return actions
        elif not has_stop_loss_hit and self.reverse_order_open:
            self.logger().debug(f"stop loss period ended, close reverse order")
            actions.extend(self._close_all_positions_and_stop())
            self.reverse_order_open = False
            return actions
        elif has_stop_loss_hit and self.reverse_order_open:
            actions = self._check_reverse_open_order_time()
            return actions

        if not self._check_cooldown_time():
            return actions
            
        signal = self._calculate_signal(self.processed_data)
        signal_actions = self._handle_signal(signal)
        actions.extend(signal_actions)

        return actions

    def _check_reverse_open_order_time(self) -> List[ExecutorAction]:
        actions = []
        current_time = self.market_data_provider.time()
        reverse_order_creation_time = current_time - self.most_recent_stop_loss_time
        if reverse_order_creation_time > self.config.cooldown_time:
            actions = self._stop_unfilled_executor()
        
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
            self.logger().debug(f":: No Signal Check, still in cooldown period. {remaining_cooldown:.1f}s remaining")
            return False

    def _calculate_signal(self, processed_data: any):
        signal = 0
        
        if "bbu" not in processed_data:
            return signal

        bbu = processed_data['bbu']
        bbl = processed_data['bbl']
        close_bt = processed_data['close']
        passed_bbp = processed_data["bbp"]
        current_price = float(self._get_current_price())
        macd = processed_data["macd"]
        
        readable_time = get_readable_time(self.market_data_provider.time())
        bbp = (float(current_price) - bbl)/(bbu - bbl)
        bb_relative_width = (bbu - bbl) / bbu
        bb_width_offset = -0.1
        bb_width_threshold = 0.04
        bb_width_multiplier_base = max(0.2, bb_relative_width / bb_width_threshold + bb_width_offset)
        bb_width_multiplier_max = 1.5
        bb_width_multiplier = min(bb_width_multiplier_base, bb_width_multiplier_max)

        signal = self.stage.get_signal(current_price, passed_bbp, bbu, bbl, bb_width_multiplier, macd);
            
        current_seconds = datetime.fromtimestamp(self.market_data_provider.time()).second
        if current_seconds == 0 and signal != 0:          
            self.logger().info(f"Current Stage is {self.stage.name}, Signal is {signal:.4f}, and bbu is {bbu:.4f}, and bbl is {bbl:.4f}")
            self.logger().info(f"price upper diff is {(current_price - bbu)/current_price:.4f}, lower diff is {(current_price - bbl)/current_price:.4f}, macd is {macd/current_price:.4f}")
            self.logger().info(f"Time: {readable_time} | Signal: {signal} | BBP: {bbp:.4f} | price: {close_bt:.4f}")
        return signal

    def _handle_signal(self, signal: int) -> List[ExecutorAction]:
        """
        Handle new signal with dynamic level management.
        """
        actions = []

        entry_price = 0

        if signal == 0:
            actions.extend(self._stop_unfilled_executor())
            return actions

        trade_side = TradeType.BUY if signal > 0 else TradeType.SELL
        direction_buy = True if signal > 0 else False

        self.logger().debug(f"current position is {self._get_total_position()}, and direction {self.direction_buy} - {direction_buy}, current stage is {self.stage.name}")
        if self._get_total_position() != Decimal("0") and self.direction_buy != direction_buy:
            self.logger().debug(f"Important! Closing all positions and stopping due to direction change")
            self.direction_buy = direction_buy
            actions.extend(self._close_all_positions_and_stop())
            return actions
        else:
            # Check if all levels are filled
            if len(self.filled_executor_ids) >= self.config.level_number:
                self.logger().debug(f":: All levels filled ({len(self.filled_executor_ids)} >= {self.config.level_number}), waiting for profit/stop loss")
                return actions  # All levels filled, wait for profit/stop loss

            current_price = self._get_current_price()
            if signal > 0:  # BUY signal
                entry_price = current_price * (1 - self.config.accumulate_pct)
            else:  # SELL signal
                entry_price = current_price * (1 + self.config.accumulate_pct)

            # Stop all unfilled executors and create new ones
            stopped_actions = self._stop_unfilled_executor()
            self.logger().debug(f":: Stopped {len(stopped_actions)} unfilled executors")
            actions.extend(stopped_actions)

        self.direction_buy = direction_buy

        #========= Create new unfilled levels
        unfilled_levels_number = self.config.level_number - len(self.filled_executor_ids)
        self.logger().debug(f":: Creating {unfilled_levels_number} new executors")

        for level_index in range(unfilled_levels_number):
            action = self._create_level_executor(level_index, entry_price, trade_side, abs(signal), False)
            if action:
                self.logger().debug(f":: Created executor for level {level_index} and trade side is: {trade_side} from {entry_price:.4f} ")
                actions.append(action)

        # Update last signal time when executors are created
        if actions:
            self.last_executor_creation_time = self.market_data_provider.time()
            self.logger().debug(f":: Updated last_signal_time, next signal allowed after {self.config.cooldown_time}s cooldown")

        return actions

    def handle_stop_loss_hit(self) -> List[ExecutorAction]:
        actions = self._close_all_positions_and_stop()
        current_price = self._get_current_price()
        trade_side = TradeType.SELL if self.direction_buy else TradeType.BUY
        
        enable_reverse_order = False
        if enable_reverse_order:
            for level_index in range(self.config.level_number):
                action = self._create_level_executor(level_index, current_price, trade_side, 1, True)
                if action:
                    self.logger().debug(f"Important! Reverse executor for level {level_index} and trade side is: {trade_side} at {current_price:.4f}")
                    actions.append(action)   
            self.reverse_order_open = True
        return actions    
