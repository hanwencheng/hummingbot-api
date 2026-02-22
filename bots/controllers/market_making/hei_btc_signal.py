"""
HEI/USDT Market Making Strategy with Signal Control

Based on hei_btc_mm.py but adds SignalManager integration for:
- Pause/resume control: When paused, the strategy stops placing new orders
- Market regime control: BULLISH = buy-only mode, BEARISH/NEUTRAL = allow sells
"""

import json
import os
import random
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
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

from ..signal_manager import SignalManager


class HEIBTCSignalConfig(ControllerConfigBase):
    controller_type: str = "market_making"
    controller_name: str = "hei_btc_signal"
    candles_config: List[CandlesConfig] = []

    connector_name: str = Field(default="binance")
    trading_pair: str = Field(default="HEI-USDT")

    signal_server_url: str = Field(default="http://host.docker.internal:8005")
    signal_user_id: str = Field(default="614270688")

    buy_order_min_size: Decimal = Field(default=Decimal("100"))
    buy_order_max_size: Decimal = Field(default=Decimal("200"))
    min_tick: Decimal = Field(default=Decimal("0.0001"))
    new_order_cooldown: int = Field(default=300)
    deep_buy_update_cooldown: int = Field(default=30)

    hourly_sell_limit: Decimal = Field(default=Decimal("10000"))
    market_sell_threshold: Decimal = Field(default=Decimal("1000"))

    deep_order_start_offset: int = Field(default=50)
    deep_order_max_per_level: Decimal = Field(default=Decimal("20000"))
    deep_order_usdt_ratio: Decimal = Field(default=Decimal("0.5"))

    state_file_name: str = Field(default="hei_usdt_signal_state.json")
    default_buy_only: bool = Field(default=True, description="Default buy-only mode when no market regime signal")
    min_sell_interval: int = Field(default=300, description="Minimum seconds between market sells")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class HEIBTCSignalController(ControllerBase):
    STATES = {
        "INITIAL": "INITIAL",
        "BUY_1_ACTIVE": "BUY_1_ACTIVE",
        "BUY_2_ACTIVE": "BUY_2_ACTIVE",
        "SELL_ACTIVE": "SELL_ACTIVE"
    }

    def __init__(self, config: HEIBTCSignalConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

        self._manager = SignalManager.instance(config.signal_server_url, config.signal_user_id)
        self._sub = self._manager.subscribe(config.id)
        self._was_paused = False
        self._last_regime: Optional[str] = None

        self.current_state = self.STATES["INITIAL"]
        self.buy_1_order_id: Optional[str] = None
        self.buy_2_order_id: Optional[str] = None
        self.active_sell_order_id: Optional[str] = None
        self.followup_buy_order_id: Optional[str] = None
        self.buy_1_fill_time: float = 0
        self.current_order_price: Decimal = Decimal("0")
        self.last_sell_1_price: Decimal = Decimal("0")

        self.hourly_sell_amounts: Dict[int, Decimal] = {}
        self.total_usdt_from_sales: Decimal = Decimal("0")

        self.deep_order_ids: List[str] = []
        self.last_deep_order_update_time: float = 0
        self.last_deep_order_sell_1_price: Decimal = Decimal("0")

        self.pending_followup_buy: bool = False
        self.sell_filled_amount: Decimal = Decimal("0")
        self.last_market_sell_time: float = 0

        self._state_loaded = False
        self.processed_data = {}

    def on_stop(self):
        self._save_state()
        self.logger().info("Controller stopped, state saved")

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        current_time = self.market_data_provider.time()

        if not self._state_loaded:
            self._load_state()
            self._state_loaded = True

        is_paused = self._sub.is_paused()

        if is_paused and not self._was_paused:
            self.logger().info("=" * 50)
            self.logger().info("PAUSED: Strategy paused via signal server")
            self.logger().info(f"  Instance: {self.config.id}")
            self.logger().info(f"  State: {self.current_state}")
            self.logger().info("  All new orders suspended until resumed")
            self.logger().info("=" * 50)
            self._was_paused = True

        if not is_paused and self._was_paused:
            self.logger().info("=" * 50)
            self.logger().info("RESUMED: Strategy resumed via signal server")
            self.logger().info(f"  Instance: {self.config.id}")
            self.logger().info(f"  State: {self.current_state}")
            self.logger().info("  Resuming normal operation")
            self.logger().info("=" * 50)
            self._was_paused = False

        current_regime = self._manager.market_regime(self.config.id)
        if current_regime != self._last_regime:
            buy_only = self._is_buy_only_mode()
            self.logger().info("=" * 50)
            self.logger().info(f"REGIME: Market regime changed to {current_regime or 'DEFAULT'}")
            self.logger().info(f"  Instance: {self.config.id}")
            self.logger().info(f"  Buy-only mode: {buy_only}")
            self.logger().info(f"  Sell orders: {'DISABLED' if buy_only else 'ENABLED'}")
            self.logger().info("=" * 50)
            self._last_regime = current_regime

        if is_paused:
            return []

        if not self.processed_data.get("order_book_valid", False):
            self.logger().warning("Order book data not available")
            return actions

        sell_1_price = self.processed_data["best_ask"]
        buy_1_price = self.processed_data["best_bid"]
        spread = sell_1_price - buy_1_price
        price_changed = (sell_1_price != self.last_sell_1_price)

        self._update_executor_states()

        if self.current_state == self.STATES["INITIAL"]:
            if self._should_trigger_sell(buy_1_price, spread):
                actions.extend(self._place_active_sell_order(buy_1_price))
            elif spread > self.config.min_tick:
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price

        elif self.current_state == self.STATES["BUY_1_ACTIVE"]:
            if self._is_order_filled(self.buy_1_order_id):
                self.buy_1_order_id = None
                actions.extend(self._place_buy_2_order(sell_1_price))
                self.buy_1_fill_time = current_time
                self.last_sell_1_price = sell_1_price

            elif self._should_trigger_sell(buy_1_price, spread):
                actions.extend(self._cancel_order(self.buy_1_order_id))
                self.buy_1_order_id = None
                actions.extend(self._place_active_sell_order(buy_1_price))

            elif price_changed and spread > self.config.min_tick:
                actions.extend(self._cancel_order(self.buy_1_order_id))
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price

        elif self.current_state == self.STATES["BUY_2_ACTIVE"]:
            time_elapsed = current_time - self.buy_1_fill_time
            cooldown_done = time_elapsed >= self.config.new_order_cooldown

            if self._is_order_filled(self.buy_2_order_id) and cooldown_done:
                self.buy_2_order_id = None
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price

            elif cooldown_done:
                actions.extend(self._cancel_order(self.buy_2_order_id))
                self.buy_2_order_id = None
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price

            elif price_changed:
                actions.extend(self._cancel_order(self.buy_2_order_id))
                actions.extend(self._place_buy_2_order(sell_1_price))
                self.last_sell_1_price = sell_1_price

        elif self.current_state == self.STATES["SELL_ACTIVE"]:
            actions.extend(self._cancel_order(self.active_sell_order_id))

            if self._was_sell_filled_or_partial():
                actions.extend(self._place_followup_buy_order())

            self.active_sell_order_id = None
            actions.extend(self._place_buy_1_order(sell_1_price))
            self.last_sell_1_price = sell_1_price

        time_since_deep_update = current_time - self.last_deep_order_update_time
        deep_price_changed = (sell_1_price != self.last_deep_order_sell_1_price)

        if time_since_deep_update >= self.config.deep_buy_update_cooldown:
            if deep_price_changed and self.total_usdt_from_sales > Decimal("0"):
                actions.extend(self._update_deep_orders(buy_1_price))
                self.last_deep_order_sell_1_price = sell_1_price
            self._save_state()
            self.last_deep_order_update_time = current_time

        return actions

    def _get_executor_by_id(self, executor_id: str) -> Optional[ExecutorInfo]:
        return next((e for e in self.executors_info if e.id == executor_id), None)

    def _has_executed_amount(self, executor: ExecutorInfo) -> bool:
        if hasattr(executor, '_order') and executor._order:
            return getattr(executor._order, 'executed_amount_base', Decimal("0")) > Decimal("0")
        return False

    def _get_executor_amounts(self, executor: ExecutorInfo) -> tuple[Decimal, Decimal]:
        if hasattr(executor, '_order') and executor._order:
            quote = Decimal(str(getattr(executor._order, 'executed_amount_quote', Decimal("0"))))
            base = Decimal(str(getattr(executor._order, 'executed_amount_base', Decimal("0"))))
            return quote, base
        return Decimal("0"), Decimal("0")

    def _update_executor_states(self):
        active_sell_executors = self.filter_executors(
            self.executors_info,
            lambda x: x.is_active and x.custom_info.get("level_id", "").startswith("active_sell_")
        )

        for executor in active_sell_executors:
            executed_quote, executed_base = self._get_executor_amounts(executor)
            self.sell_filled_amount = executed_quote
            if executed_quote > Decimal("0"):
                self.total_usdt_from_sales += executed_quote
                self._record_hourly_sell(executed_base)

    def _is_order_filled(self, order_id: Optional[str]) -> bool:
        if not order_id:
            return False
        executor = self._get_executor_by_id(order_id)
        if not executor:
            return False
        if executor.close_type == CloseType.POSITION_HOLD:
            return True
        if hasattr(executor, '_order') and executor._order:
            executed_base = getattr(executor._order, 'executed_amount_base', Decimal("0"))
            order_is_filled = getattr(executor._order, 'is_filled', False)
            return executed_base > Decimal("0") or order_is_filled
        return False

    def _is_buy_only_mode(self) -> bool:
        regime = self._manager.market_regime(self.config.id)
        if regime == "BULLISH":
            return True
        if regime in ("BEARISH", "NEUTRAL"):
            return False
        return self.config.default_buy_only

    def _should_trigger_sell(self, best_bid: Decimal, spread: Decimal) -> bool:
        if self._is_buy_only_mode():
            return False
        try:
            if spread > self.config.min_tick or best_bid <= Decimal("0"):
                return False
            elapsed = self.market_data_provider.time() - self.last_market_sell_time
            if elapsed < self.config.min_sell_interval:
                return False
            volume_at_bid = self._get_volume_at_price(best_bid, is_buy=False)
            if volume_at_bid <= Decimal("0"):
                return False
            return volume_at_bid >= self.config.market_sell_threshold
        except Exception:
            return False

    def _was_sell_filled_or_partial(self) -> bool:
        if not self.active_sell_order_id:
            return False
        executor = self._get_executor_by_id(self.active_sell_order_id)
        if not executor:
            return False
        return self._has_executed_amount(executor) or executor.close_type == CloseType.POSITION_HOLD

    def _random_amount(self) -> Decimal:
        return Decimal(str(round(random.uniform(
            float(self.config.buy_order_min_size),
            float(self.config.buy_order_max_size)
        ), 2)))

    def _create_order_config(self, side: TradeType, amount: Decimal, price: Decimal,
                             strategy: ExecutionStrategy, level_id: str) -> OrderExecutorConfig:
        return OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            amount=amount,
            price=price,
            execution_strategy=strategy,
            level_id=level_id
        )

    def _create_executor_action(self, config: OrderExecutorConfig) -> CreateExecutorAction:
        return CreateExecutorAction(controller_id=self.config.id, executor_config=config)

    def _place_buy_order(self, sell_1_price: Decimal, ticks: int, order_type: str) -> List[ExecutorAction]:
        price = sell_1_price - (ticks * self.config.min_tick)
        level_id = f"{order_type}_{int(time.time())}"
        executor_config = self._create_order_config(TradeType.BUY, self._random_amount(), price,
                                                    ExecutionStrategy.LIMIT, level_id)

        if order_type == "buy_1":
            self.buy_1_order_id = executor_config.id
            self.current_state = self.STATES["BUY_1_ACTIVE"]
        else:
            self.buy_2_order_id = executor_config.id
            self.current_state = self.STATES["BUY_2_ACTIVE"]

        self.current_order_price = price
        return [self._create_executor_action(executor_config)]

    def _place_buy_1_order(self, sell_1_price: Decimal) -> List[ExecutorAction]:
        return self._place_buy_order(sell_1_price, 1, "buy_1")

    def _place_buy_2_order(self, sell_1_price: Decimal) -> List[ExecutorAction]:
        return self._place_buy_order(sell_1_price, 2, "buy_2")

    def _place_active_sell_order(self, best_bid: Decimal) -> List[ExecutorAction]:
        volume_at_bid = self._get_volume_at_price(best_bid, is_buy=False)
        sell_amount = volume_at_bid / Decimal("4")

        if not self._check_hourly_limit(sell_amount):
            return []

        executor_config = self._create_order_config(
            TradeType.SELL, sell_amount, best_bid,
            ExecutionStrategy.MARKET, f"active_sell_{int(time.time())}"
        )
        self.active_sell_order_id = executor_config.id
        self.last_market_sell_time = self.market_data_provider.time()
        self.current_state = self.STATES["SELL_ACTIVE"]
        return [self._create_executor_action(executor_config)]

    def _place_followup_buy_order(self) -> List[ExecutorAction]:
        executor_config = self._create_order_config(
            TradeType.BUY, self._random_amount(), Decimal("0"),
            ExecutionStrategy.MARKET, f"followup_buy_{int(time.time())}"
        )
        self.followup_buy_order_id = executor_config.id
        return [self._create_executor_action(executor_config)]

    def _update_deep_orders(self, buy_1_price: Decimal) -> List[ExecutorAction]:
        actions = []

        for order_id in self.deep_order_ids:
            actions.extend(self._cancel_order(order_id))
        self.deep_order_ids.clear()

        available_usdt = self.total_usdt_from_sales * self.config.deep_order_usdt_ratio
        current_price = buy_1_price - (self.config.deep_order_start_offset * self.config.min_tick)
        level_index = 0

        while available_usdt > Decimal("0") and current_price > Decimal("0"):
            max_hei = self.config.deep_order_max_per_level
            usdt_needed = max_hei * current_price
            hei_amount = available_usdt / current_price if usdt_needed > available_usdt else max_hei

            if hei_amount < Decimal("1"):
                break

            executor_config = self._create_order_config(
                TradeType.BUY, hei_amount, current_price,
                ExecutionStrategy.LIMIT, f"deep_buy_{level_index}_{int(time.time())}"
            )
            actions.append(self._create_executor_action(executor_config))
            self.deep_order_ids.append(executor_config.id)

            available_usdt -= hei_amount * current_price
            current_price -= self.config.min_tick
            level_index += 1

        return actions

    def _cancel_order(self, order_id: Optional[str]) -> List[ExecutorAction]:
        if not order_id:
            return []
        executor = self._get_executor_by_id(order_id)
        if executor and executor.is_active:
            return [StopExecutorAction(controller_id=self.config.id, executor_id=order_id)]
        return []

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
                "current_state": self.current_state,
                "last_sell_1_price": str(self.last_sell_1_price),
                "buy_1_fill_time": self.buy_1_fill_time,
                "current_order_price": str(self.current_order_price),
                "hourly_sell_amounts": {str(k): str(v) for k, v in self.hourly_sell_amounts.items()},
                "total_usdt_from_sales": str(self.total_usdt_from_sales),
                "last_deep_order_update_time": self.last_deep_order_update_time,
                "last_deep_order_sell_1_price": str(self.last_deep_order_sell_1_price),
                "last_consumed_timestamp": self._sub.last_timestamp,
                "last_market_sell_time": self.last_market_sell_time,
                "last_updated": time.time()
            }
            with open(self._get_state_file_path(), 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger().error(f"Save state error: {e}")

    def _load_state(self):
        try:
            state_path = self._get_state_file_path()
            if not os.path.exists(state_path):
                self._save_state()
                return

            with open(state_path, 'r') as f:
                state = json.load(f)

            self.last_sell_1_price = Decimal(state.get("last_sell_1_price", "0"))
            self.buy_1_fill_time = state.get("buy_1_fill_time", 0)
            self.current_order_price = Decimal(state.get("current_order_price", "0"))
            self.hourly_sell_amounts = {int(k): Decimal(v) for k, v in state.get("hourly_sell_amounts", {}).items()}
            self.total_usdt_from_sales = Decimal(state.get("total_usdt_from_sales", "0"))
            self.last_deep_order_update_time = state.get("last_deep_order_update_time", 0)
            self.last_deep_order_sell_1_price = Decimal(state.get("last_deep_order_sell_1_price", "0"))
            self._sub.last_timestamp = state.get("last_consumed_timestamp", 0)
            self.last_market_sell_time = state.get("last_market_sell_time", 0)
            self.current_state = self.STATES["INITIAL"]

            self.logger().info(f"State loaded, last_consumed_timestamp={self._sub.last_timestamp}")
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
            "current_state": self.current_state,
            "is_paused": self._sub.is_paused(),
            "total_usdt_from_sales": self.total_usdt_from_sales,
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

    def _get_volume_at_price(self, price: Decimal, is_buy: bool = False) -> Decimal:
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            result = connector.get_volume_for_price(self.config.trading_pair, is_buy, price)
            return result.result_volume if result.result_volume else Decimal("0")
        except Exception:
            return Decimal("0")

    def to_format_status(self) -> List[str]:
        is_paused = self._sub.is_paused()
        regime = self._manager.market_regime(self.config.id) or "DEFAULT"
        buy_only = self._is_buy_only_mode()
        header = f"HEI/USDT Signal MM | {self.config.connector_name}:{self.config.trading_pair}"
        status = [
            header,
            "=" * len(header),
            f"Paused: {is_paused}",
            f"State: {self.current_state}",
            f"Regime: {regime} (buy-only: {buy_only})"
        ]

        if self.processed_data.get("order_book_valid", False):
            status.extend([
                f"Best bid: {self.processed_data.get('best_bid', 'N/A')}",
                f"Best ask: {self.processed_data.get('best_ask', 'N/A')}"
            ])

        status.extend([
            f"Hourly sold: {self._get_hourly_sold()} / {self.config.hourly_sell_limit} HEI",
            f"Total USDT from sales: {self.total_usdt_from_sales}",
            f"Deep orders: {len(self.deep_order_ids)}"
        ])

        status.extend(self._manager.status(self.config.id))
        return status
