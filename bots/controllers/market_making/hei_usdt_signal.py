"""
HEI/USDT Signal-Controlled Market Making Strategy

- Places buy2/buy3/buy4/buy5 limit orders (5000 HEI each) below the best ask
- Sell trigger: bid volume at buy1 >= 10k → market sell 5k HEI, 3 min cooldown
- Buy-all trigger: ask volume at sell1 < 500 → market buy all, 3 min cooldown
- Hourly sell limit: 100,000 HEI
- Two states: START / PAUSE (controlled by signal server)
"""

import json
import os
import time
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot import data_path
from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

from ..signal_manager import SignalManager


class HEIUSDTSignalConfig(ControllerConfigBase):
    controller_type: str = "market_making"
    controller_name: str = "hei_usdt_signal"
    candles_config: List[CandlesConfig] = []

    connector_name: str = Field(default="binance")
    trading_pair: str = Field(default="HEI-USDT")
    signal_server_url: str = Field(default="http://host.docker.internal:8005")
    signal_user_id: str = Field(default="614270688")

    tier_order_size: Decimal = Field(default=Decimal("5000"))
    min_tick: Decimal = Field(default=Decimal("0.0001"))

    sell_volume_threshold: Decimal = Field(default=Decimal("10000"))
    sell_amount: Decimal = Field(default=Decimal("5000"))
    buy_all_volume_threshold: Decimal = Field(default=Decimal("500"))

    action_cooldown: int = Field(default=180)
    hourly_sell_limit: Decimal = Field(default=Decimal("100000"))

    state_file_name: str = Field(default="hei_usdt_signal_state.json")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class HEIUSDTSignalController(ControllerBase):

    def __init__(self, config: HEIUSDTSignalConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

        self._manager = SignalManager.instance(config.signal_server_url, config.signal_user_id)
        self._sub = self._manager.subscribe(config.id)
        self._was_paused = False

        self.tier_order_ids: Dict[int, Optional[str]] = {2: None, 3: None, 4: None, 5: None}
        self.tier_order_prices: Dict[int, Decimal] = {2: Decimal("0"), 3: Decimal("0"), 4: Decimal("0"), 5: Decimal("0")}

        self.last_sell_time: float = 0
        self.last_buy_all_time: float = 0
        self.hourly_sell_amounts: Dict[int, Decimal] = {}

        self._state_loaded = False
        self.processed_data = {}

    def stop(self):
        self._save_state()
        super().stop()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        current_time = self.market_data_provider.time()

        if not self._state_loaded:
            self._load_state()
            self._state_loaded = True

        is_paused = self._sub.is_paused()

        if is_paused and not self._was_paused:
            self.logger().info("PAUSED via signal server — suspending all orders")
            actions.extend(self._cancel_all_tier_orders())
            self._was_paused = True
            return actions

        if not is_paused and self._was_paused:
            self.logger().info("RESUMED via signal server")
            self._was_paused = False

        if is_paused:
            return []

        if not self.processed_data.get("order_book_valid", False):
            self.logger().warning("Order book not available")
            return actions

        best_ask = self.processed_data["best_ask"]
        best_bid = self.processed_data["best_bid"]

        actions.extend(self._refresh_tier_orders(best_ask))

        sell_cooldown_ok = (current_time - self.last_sell_time) >= self.config.action_cooldown
        if sell_cooldown_ok:
            bid_volume = self._get_volume_at_price(best_bid, is_buy=False)
            if bid_volume >= self.config.sell_volume_threshold:
                if self._check_hourly_limit(self.config.sell_amount):
                    actions.extend(self._place_market_sell(self.config.sell_amount))
                    self.last_sell_time = current_time
                    self._record_hourly_sell(self.config.sell_amount)
                    self.logger().info(f"Sell triggered: bid_volume={bid_volume} at buy1={best_bid}")

        buy_cooldown_ok = (current_time - self.last_buy_all_time) >= self.config.action_cooldown
        if buy_cooldown_ok:
            ask_volume = self._get_volume_at_price(best_ask, is_buy=True)
            if Decimal("0") < ask_volume < self.config.buy_all_volume_threshold:
                actions.extend(self._place_market_buy(ask_volume))
                self.last_buy_all_time = current_time
                self.logger().info(f"Buy-all triggered: ask_volume={ask_volume} at sell1={best_ask}")

        self._save_state()
        return actions

    def _refresh_tier_orders(self, best_ask: Decimal) -> List[ExecutorAction]:
        actions = []
        for tick_offset in [2, 3, 4, 5]:
            target_price = best_ask - (tick_offset * self.config.min_tick)
            current_order_id = self.tier_order_ids[tick_offset]
            current_price = self.tier_order_prices[tick_offset]

            if current_order_id is not None:
                executor = self._get_executor_by_id(current_order_id)
                if executor is not None and executor.is_active and current_price == target_price:
                    continue
                if executor is not None and executor.is_active:
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=current_order_id))
                self.tier_order_ids[tick_offset] = None
                self.tier_order_prices[tick_offset] = Decimal("0")

            executor_config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=TradeType.BUY,
                amount=self.config.tier_order_size,
                price=target_price,
                execution_strategy=ExecutionStrategy.LIMIT,
                level_id=f"tier_buy_{tick_offset}_{int(time.time())}"
            )
            self.tier_order_ids[tick_offset] = executor_config.id
            self.tier_order_prices[tick_offset] = target_price
            actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config))
        return actions

    def _cancel_all_tier_orders(self) -> List[ExecutorAction]:
        actions = []
        for tick_offset in self.tier_order_ids:
            order_id = self.tier_order_ids[tick_offset]
            if order_id:
                executor = self._get_executor_by_id(order_id)
                if executor and executor.is_active:
                    actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=order_id))
            self.tier_order_ids[tick_offset] = None
            self.tier_order_prices[tick_offset] = Decimal("0")
        return actions

    def _place_market_sell(self, amount: Decimal) -> List[ExecutorAction]:
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.SELL,
            amount=amount,
            price=Decimal("0"),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"market_sell_{int(time.time())}"
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config)]

    def _place_market_buy(self, amount: Decimal) -> List[ExecutorAction]:
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=amount,
            price=Decimal("0"),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"market_buy_{int(time.time())}"
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config)]

    def _get_executor_by_id(self, executor_id: str) -> Optional[ExecutorInfo]:
        return next((e for e in self.executors_info if e.id == executor_id), None)

    def _get_volume_at_price(self, price: Decimal, is_buy: bool = False) -> Decimal:
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            result = connector.get_volume_for_price(self.config.trading_pair, is_buy, price)
            return result.result_volume if result.result_volume else Decimal("0")
        except Exception:
            return Decimal("0")

    def _get_current_hour(self) -> int:
        return int(self.market_data_provider.time() // 3600) * 3600

    def _get_hourly_sold(self) -> Decimal:
        return self.hourly_sell_amounts.get(self._get_current_hour(), Decimal("0"))

    def _check_hourly_limit(self, amount: Decimal) -> bool:
        return (self._get_hourly_sold() + amount) <= self.config.hourly_sell_limit

    def _record_hourly_sell(self, amount: Decimal):
        current_hour = self._get_current_hour()
        self.hourly_sell_amounts[current_hour] = self._get_hourly_sold() + amount
        cutoff = current_hour - 86400
        self.hourly_sell_amounts = {h: v for h, v in self.hourly_sell_amounts.items() if h >= cutoff}

    def _get_state_file_path(self) -> str:
        return os.path.join(data_path(), self.config.state_file_name)

    def _save_state(self):
        try:
            state = {
                "last_sell_time": self.last_sell_time,
                "last_buy_all_time": self.last_buy_all_time,
                "hourly_sell_amounts": {str(k): str(v) for k, v in self.hourly_sell_amounts.items()},
                "last_consumed_timestamp": self._sub.last_timestamp,
                "last_updated": time.time()
            }
            with open(self._get_state_file_path(), 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger().error(f"Save state error: {e}")

    def _load_state(self):
        try:
            path = self._get_state_file_path()
            if not os.path.exists(path):
                self._save_state()
                return
            with open(path) as f:
                state = json.load(f)
            self.last_sell_time = state.get("last_sell_time", 0)
            self.last_buy_all_time = state.get("last_buy_all_time", 0)
            self.hourly_sell_amounts = {int(k): Decimal(v) for k, v in state.get("hourly_sell_amounts", {}).items()}
            self._sub.last_timestamp = state.get("last_consumed_timestamp", 0)
            self.logger().info(f"State loaded from {path}")
        except Exception as e:
            self.logger().error(f"Load state error: {e}")

    async def update_processed_data(self):
        await self._manager.update(self._sub.last_timestamp)

        if self._manager.state_error:
            self.logger().warning(f"Signal state error: {self._manager.state_error}")
        if self._manager.events_error:
            self.logger().warning(f"Signal events error: {self._manager.events_error}")

        order_book_data = self._fetch_order_book_data()
        is_valid = order_book_data is not None

        self.processed_data = {
            "is_paused": self._sub.is_paused(),
            "timestamp": self.market_data_provider.time(),
            "order_book_valid": is_valid,
            "best_bid": order_book_data["best_bid"] if is_valid else Decimal("0"),
            "best_ask": order_book_data["best_ask"] if is_valid else Decimal("0")
        }

    def _fetch_order_book_data(self) -> Optional[Dict]:
        try:
            order_book = self.market_data_provider.get_order_book(
                self.config.connector_name, self.config.trading_pair
            )
            if order_book is None:
                return None
            return {
                "best_bid": Decimal(str(order_book.get_price(False))),
                "best_ask": Decimal(str(order_book.get_price(True)))
            }
        except Exception as e:
            self.logger().error(f"Order book error: {e}")
            return None

    def to_format_status(self) -> List[str]:
        is_paused = self._sub.is_paused()
        header = f"HEI/USDT Signal MM | {self.config.connector_name}:{self.config.trading_pair}"
        active_tiers = sum(1 for oid in self.tier_order_ids.values() if oid is not None)
        status = [
            header,
            "=" * len(header),
            f"State: {'PAUSED' if is_paused else 'START'}",
        ]

        if self.processed_data.get("order_book_valid", False):
            status.extend([
                f"Best bid: {self.processed_data.get('best_bid', 'N/A')}",
                f"Best ask: {self.processed_data.get('best_ask', 'N/A')}"
            ])

        status.extend([
            f"Tier orders active: {active_tiers}/4",
            f"Hourly sold: {self._get_hourly_sold()} / {self.config.hourly_sell_limit} HEI",
        ])

        status.extend(self._manager.status(self.config.id))
        return status
