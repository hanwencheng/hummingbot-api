"""
HEI Signal-Based Trading Controller

Fetches signals from wildmeta-signal-server and executes BUY/SELL orders.
Uses SignalManager singleton with per-subscriber consumption tracking.
"""

import json
import os
import time
from decimal import Decimal
from typing import List

from pydantic import Field

from hummingbot import data_path
from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction

from ..signal_manager import SignalManager, TradeSignal


class HEISignalConfig(ControllerConfigBase):
    controller_type: str = "market_making"
    controller_name: str = "hei_signal"
    candles_config: List[CandlesConfig] = []

    connector_name: str = Field(default="binance")
    trading_pair: str = Field(default="HEI-USDT")
    signal_server_url: str = Field(default="http://localhost:8001")
    signal_user_id: str = Field(default="614270688")
    default_order_amount: Decimal = Field(default=Decimal("100"))
    event_ttl: int = Field(default=60)
    state_file_name: str = Field(default="hei_signal_state.json")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class HEISignalController(ControllerBase):

    def __init__(self, config: HEISignalConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

        self._manager = SignalManager.instance(config.signal_server_url, config.signal_user_id)
        self._sub = self._manager.subscribe(config.id)
        self._state_loaded = False

    def stop(self):
        self._save_state()
        super().stop()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if not self._state_loaded:
            self._load_state()
            self._state_loaded = True

        pending = self._sub.pending(self.config.trading_pair)

        if self._sub.is_paused():
            if pending:
                self.logger().info(f"Discarding {len(pending)} signal(s) while paused")
                self._sub.consume_all(self.config.trading_pair, reason="paused")
                self._save_state()
            return []

        actions = []
        now = self.market_data_provider.time()

        for signal in pending:
            if now - signal.timestamp > self.config.event_ttl:
                self.logger().warning(f"Expired signal {signal.signal_id}")
                self._sub.consume(signal, reason="expired")
            else:
                self.logger().info(f"Executing {signal.signal_type} {signal.signal_id}")
                actions.extend(self._create_order(signal))
                self._sub.consume(signal, reason="executed")
            self._save_state()

        return actions

    def _create_order(self, signal: TradeSignal) -> List[ExecutorAction]:
        amount = Decimal(str(signal.amount)) if signal.amount else self.config.default_order_amount
        side = TradeType.BUY if signal.signal_type == "BUY" else TradeType.SELL

        config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            amount=amount,
            price=Decimal("0"),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"signal_{signal.signal_id}"
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

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
            state = {"last_consumed_timestamp": self._sub.last_timestamp, "last_updated": time.time()}
            with open(os.path.join(data_path(), self.config.state_file_name), 'w') as f:
                json.dump(state, f)
        except Exception as e:
            self.logger().error(f"Save state error: {e}")

    def _load_state(self):
        try:
            path = os.path.join(data_path(), self.config.state_file_name)
            if os.path.exists(path):
                with open(path) as f:
                    state = json.load(f)
                self._sub.last_timestamp = state.get("last_consumed_timestamp", state.get("last_processed_timestamp", 0))
                self.logger().info(f"State loaded: {self._sub.last_timestamp}")
        except Exception as e:
            self.logger().error(f"Load state error: {e}")

    def is_paused(self) -> bool:
        return self._sub.is_paused()

    def to_format_status(self) -> List[str]:
        status = [f"HEI Signal | {self.config.connector_name}:{self.config.trading_pair}"]
        status.extend(self._manager.status(self.config.id))
        pending = self._sub.pending(self.config.trading_pair)
        if pending:
            s = pending[-1]
            status.append(f"Latest: {s.signal_type} {s.trading_pair} ({s.signal_id})")
        return status
