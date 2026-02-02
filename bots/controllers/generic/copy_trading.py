"""
Copy Trading Controller

A generic copy trading strategy that copies trades from a followed account
via the Wildmeta Signal Server. Supports full pause handling that closes
positions and resets state on resume.

Features:
- Copies BUY/SELL signals from a specific address
- Configurable order amount modes: fixed, proportional, mirror
- Full pause handling with position cleanup
- State persistence across restarts
- Signal TTL expiration
"""

import json
import os
import time
from decimal import Decimal
from typing import List, Literal, Optional, Set

from pydantic import Field

from hummingbot import data_path
from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

from .copy_trading_manager import CopyTradeSignal, CopyTradingManager


class CopyTradingConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "copy_trading"
    candles_config: List[CandlesConfig] = []

    copy_trading_server_url: str = Field(
        default="http://host.docker.internal:8006",
        description="Signal server URL"
    )
    copy_trading_user_id: str = Field(
        default="",
        description="User identifier for the signal server"
    )
    following_address: str = Field(
        default="",
        description="Address to copy trades from"
    )
    connector_name: str = Field(
        default="binance_perpetual",
        description="Exchange connector"
    )
    trading_pair: str = Field(
        default="BTC-USDT",
        description="Trading pair"
    )
    amount_mode: Literal["fixed", "proportional", "mirror"] = Field(
        default="fixed",
        description="How to calculate order amount: fixed=use fixed_order_amount, "
                    "proportional=scale signal amount by ratio, mirror=use signal amount directly"
    )
    fixed_order_amount: Decimal = Field(
        default=Decimal("100"),
        description="Fixed order amount in quote currency (when mode=fixed)"
    )
    proportional_ratio: Decimal = Field(
        default=Decimal("1.0"),
        description="Scale ratio for signal amount (when mode=proportional)"
    )
    max_order_amount: Decimal = Field(
        default=Decimal("1000"),
        description="Maximum order amount limit"
    )
    signal_ttl: int = Field(
        default=60,
        description="Signal expiration in seconds"
    )
    leverage: int = Field(
        default=1,
        description="Leverage for perpetual trading"
    )
    state_file_name: str = Field(
        default="copy_trading_state.json",
        description="State persistence file name"
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class CopyTradingController(ControllerBase):

    def __init__(self, config: CopyTradingConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

        self._manager = CopyTradingManager.instance(
            config.copy_trading_server_url,
            config.copy_trading_user_id,
            config.following_address
        )
        self._sub = self._manager.subscribe(config.id)
        self._state_loaded = False
        self._processed_signal_ids: Set[str] = set()
        self._was_paused = False
        self._pause_cleanup_done = False

    def stop(self):
        self._save_state()
        self.logger().info("Copy trading controller stopping, state saved")
        super().stop()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if not self._state_loaded:
            self._load_state()
            self._state_loaded = True

        is_paused = self._sub.is_paused()

        if is_paused and not self._pause_cleanup_done:
            self.logger().info("PAUSED - executing cleanup: closing positions and discarding signals")
            actions = self._create_pause_cleanup_actions()
            self._pause_cleanup_done = True
            self._was_paused = True
            self._save_state()
            return actions

        if is_paused:
            pending = self._sub.pending(self.config.trading_pair)
            if pending:
                self.logger().info(f"Discarding {len(pending)} signal(s) while paused")
                self._sub.consume_all(self.config.trading_pair, reason="paused")
                self._save_state()
            return []

        if self._was_paused and not is_paused:
            self.logger().info("RESUMED - resetting state for fresh start")
            self._reset_state()
            self._was_paused = False
            self._pause_cleanup_done = False
            self._save_state()

        pending = self._sub.pending(self.config.trading_pair)
        actions = []
        now = self.market_data_provider.time()

        for signal in pending:
            if signal.signal_id in self._processed_signal_ids:
                self._sub.consume(signal, reason="already_processed")
                continue

            if now - signal.timestamp > self.config.signal_ttl:
                self.logger().warning(f"Expired signal {signal.signal_id}")
                self._sub.consume(signal, reason="expired")
                self._processed_signal_ids.add(signal.signal_id)
            else:
                self.logger().info(f"Executing {signal.signal_type} signal {signal.signal_id} from {signal.copy_account}")
                actions.extend(self._create_order(signal))
                self._sub.consume(signal, reason="executed")
                self._processed_signal_ids.add(signal.signal_id)
            self._save_state()

        return actions

    def _create_pause_cleanup_actions(self) -> List[ExecutorAction]:
        actions = []

        for executor in self.executors_info:
            if executor.is_active:
                self.logger().info(f"Stopping executor {executor.id} (keep_position=False)")
                actions.append(StopExecutorAction(
                    controller_id=self.config.id,
                    executor_id=executor.id,
                    keep_position=False
                ))

        for position in self.positions_held:
            if (position.connector_name == self.config.connector_name and
                    position.trading_pair == self.config.trading_pair):
                close_side = TradeType.SELL if position.amount > 0 else TradeType.BUY
                close_amount = abs(position.amount)

                if close_amount > 0:
                    self.logger().info(
                        f"Closing position: {close_side.name} {close_amount} {self.config.trading_pair}"
                    )
                    close_config = OrderExecutorConfig(
                        timestamp=self.market_data_provider.time(),
                        connector_name=self.config.connector_name,
                        trading_pair=self.config.trading_pair,
                        side=close_side,
                        amount=close_amount,
                        price=Decimal("0"),
                        execution_strategy=ExecutionStrategy.MARKET,
                        level_id="pause_position_close"
                    )
                    actions.append(CreateExecutorAction(
                        controller_id=self.config.id,
                        executor_config=close_config
                    ))

        self._sub.consume_all(self.config.trading_pair, reason="paused")

        return actions

    def _reset_state(self):
        self._sub.clear_consumed()
        self._processed_signal_ids.clear()
        self.logger().info("State reset: cleared consumed signals and processed IDs")

    def _create_order(self, signal: CopyTradeSignal) -> List[ExecutorAction]:
        amount = self._calculate_order_amount(signal)
        side = TradeType.BUY if signal.signal_type == "BUY" else TradeType.SELL

        config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            amount=amount,
            price=Decimal("0"),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"copy_{signal.signal_id}"
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

    def _calculate_order_amount(self, signal: CopyTradeSignal) -> Decimal:
        if self.config.amount_mode == "fixed":
            amount = self.config.fixed_order_amount
        elif self.config.amount_mode == "mirror":
            amount = Decimal(str(signal.amount)) if signal.amount else self.config.fixed_order_amount
        elif self.config.amount_mode == "proportional":
            base_amount = Decimal(str(signal.amount)) if signal.amount else self.config.fixed_order_amount
            amount = base_amount * self.config.proportional_ratio
        else:
            amount = self.config.fixed_order_amount

        return min(amount, self.config.max_order_amount)

    async def update_processed_data(self):
        await self._manager.update(self._sub.last_timestamp)

        if self._manager.state_error:
            self.logger().warning(f"State error: {self._manager.state_error}")
        if self._manager.events_error:
            self.logger().warning(f"Events error: {self._manager.events_error}")

        pending = self._sub.pending(self.config.trading_pair)
        if pending:
            self.logger().info(f"Received {len(pending)} signal(s) for {self.config.trading_pair}")

    def _save_state(self):
        try:
            state = {
                "last_consumed_timestamp": self._sub.last_timestamp,
                "processed_signal_ids": list(self._processed_signal_ids),
                "was_paused": self._was_paused,
                "last_updated": time.time()
            }
            state_path = os.path.join(data_path(), self.config.state_file_name)
            with open(state_path, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            self.logger().error(f"Save state error: {e}")

    def _load_state(self):
        try:
            state_path = os.path.join(data_path(), self.config.state_file_name)
            if os.path.exists(state_path):
                with open(state_path) as f:
                    state = json.load(f)
                self._sub.last_timestamp = state.get(
                    "last_consumed_timestamp",
                    state.get("last_processed_timestamp", 0)
                )
                self._processed_signal_ids = set(state.get("processed_signal_ids", []))
                self._was_paused = state.get("was_paused", False)
                self.logger().info(
                    f"State loaded: last_timestamp={self._sub.last_timestamp}, "
                    f"processed={len(self._processed_signal_ids)}, was_paused={self._was_paused}"
                )
        except Exception as e:
            self.logger().error(f"Load state error: {e}")

    def is_paused(self) -> bool:
        return self._sub.is_paused()

    def to_format_status(self) -> List[str]:
        status = [
            f"Copy Trading | {self.config.connector_name}:{self.config.trading_pair}",
            f"Following: {self.config.following_address}",
            f"Amount Mode: {self.config.amount_mode}",
        ]
        status.extend(self._manager.status(self.config.id))

        active_executors = [e for e in self.executors_info if e.is_active]
        if active_executors:
            status.append(f"Active Executors: {len(active_executors)}")

        pending = self._sub.pending(self.config.trading_pair)
        if pending:
            s = pending[-1]
            status.append(f"Latest: {s.signal_type} {s.trading_pair} ({s.signal_id})")

        return status
