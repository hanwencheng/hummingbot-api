"""
HEI/BTC Market Making Strategy Controller

A market making strategy for HEI/BTC pair on Binance Spot that:
1. Improves trading volume
2. Sells more HEI than buying
3. Maintains order book bid-side depth

State Machine:
- INITIAL -> BUY_1_ACTIVE (place buy_1 order 1 tick below ask)
- BUY_1_ACTIVE -> BUY_2_ACTIVE (when filled, place buy_2 order 2 ticks below ask)
- BUY_2_ACTIVE -> BUY_1_ACTIVE (after 5 min cooldown)
- BUY_1_ACTIVE -> SELL_ACTIVE (when >5000 HEI at buy_1 price, market sell half)
- SELL_ACTIVE -> BUY_1_ACTIVE (next cycle, with followup buy)
"""

import json
import os
import random
import time
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

class HEIBTCMMConfig(ControllerConfigBase):
    """Configuration for HEI/BTC Market Making Strategy"""

    controller_type: str = "market_making"
    controller_name: str = "hei_btc_mm"
    candles_config: List[CandlesConfig] = []

    connector_name: str = Field(
        default="binance",
        json_schema_extra={
            "prompt": "Enter the connector name:",
            "prompt_on_new": True
        }
    )
    trading_pair: str = Field(
        default="HEI-BTC",
        json_schema_extra={
            "prompt": "Enter the trading pair:",
            "prompt_on_new": True
        }
    )

    buy_order_min_size: Decimal = Field(
        default=Decimal("100"),
        json_schema_extra={
            "prompt": "Enter minimum HEI per buy order:",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )
    buy_order_max_size: Decimal = Field(
        default=Decimal("200"),
        json_schema_extra={
            "prompt": "Enter maximum HEI per buy order:",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    min_tick: Decimal = Field(
        default=Decimal("0.00000001"),
        json_schema_extra={
            "prompt": "Enter minimum price tick:",
            "prompt_on_new": True
        }
    )

    new_order_cooldown: int = Field(
        default=300,
        json_schema_extra={
            "prompt": "Enter cooldown after buy_1 fills (seconds):",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )
    deep_buy_update_cooldown: int = Field(
        default=30,
        json_schema_extra={
            "prompt": "Enter deep order update interval (seconds):",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    hourly_sell_limit: Decimal = Field(
        default=Decimal("20000"),
        json_schema_extra={
            "prompt": "Enter max HEI to sell per hour:",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )
    market_sell_threshold: Decimal = Field(
        default=Decimal("5000"),
        json_schema_extra={
            "prompt": "Enter HEI volume threshold to trigger sell:",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    deep_order_start_offset: int = Field(
        default=20,
        json_schema_extra={
            "prompt": "Enter deep order start offset (ticks below buy_1):",
            "prompt_on_new": True
        }
    )
    deep_order_max_per_level: Decimal = Field(
        default=Decimal("20000"),
        json_schema_extra={
            "prompt": "Enter max HEI per deep order level:",
            "prompt_on_new": True
        }
    )
    deep_order_btc_ratio: Decimal = Field(
        default=Decimal("0.5"),
        json_schema_extra={
            "prompt": "Enter ratio of BTC from sales to use for deep orders:",
            "prompt_on_new": True
        }
    )

    state_file_name: str = Field(default="hei_btc_mm_state.json")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class HEIBTCMMController(ControllerBase):
    """
    HEI/BTC Market Making Strategy Controller

    Manages a state machine for market making with:
    - buy_1_order: 1 tick below best ask
    - buy_2_order: 2 ticks below best ask (after buy_1 fills)
    - active_sell_order: Market sell when volume threshold met
    - deep_orders: Build order book depth with BTC from sales
    """

    STATES = {
        "INITIAL": "INITIAL",
        "BUY_1_ACTIVE": "BUY_1_ACTIVE",
        "BUY_2_ACTIVE": "BUY_2_ACTIVE",
        "SELL_ACTIVE": "SELL_ACTIVE"
    }

    def __init__(self, config: HEIBTCMMConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair
            )
        ])

        self.current_state = self.STATES["INITIAL"]
        self.buy_1_order_id: Optional[str] = None
        self.buy_2_order_id: Optional[str] = None
        self.active_sell_order_id: Optional[str] = None
        self.followup_buy_order_id: Optional[str] = None
        self.buy_1_fill_time: float = 0
        self.current_order_price: Decimal = Decimal("0")

        self.last_sell_1_price: Decimal = Decimal("0")

        self.hourly_sell_amounts: Dict[int, Decimal] = {}
        self.total_btc_from_sales: Decimal = Decimal("0")

        self.deep_order_ids: List[str] = []
        self.last_deep_order_update_time: float = 0
        self.last_deep_order_sell_1_price: Decimal = Decimal("0")

        self.pending_followup_buy: bool = False
        self.sell_filled_amount: Decimal = Decimal("0")

        self._state_loaded = False
        self.processed_data = {}

        self.debug_logging_enabled: bool = True
        self.buy_only_enabled: bool = True

    def _log_debug(self, message: str):
        if self.debug_logging_enabled:
            self.logger().info(message)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Main strategy loop implementing the state machine"""
        actions = []
        current_time = self.market_data_provider.time()

        if not self._state_loaded:
            self._load_state()
            self._state_loaded = True

        if not self.processed_data.get("order_book_valid", False):
            self.logger().warning("Order book data not available in processed_data")
            return actions

        sell_1_price = self.processed_data["best_ask"]
        buy_1_price = self.processed_data["best_bid"]
        spread = sell_1_price - buy_1_price
        price_changed = (sell_1_price != self.last_sell_1_price)

        self._log_debug(f"[determine_executor_actions] state={self.current_state}, sell_1={sell_1_price}, buy_1={buy_1_price}, spread={spread}, price_changed={price_changed}")

        self._update_executor_states()

        if self.current_state == self.STATES["INITIAL"]:
            if self._should_trigger_sell(buy_1_price, spread):
                actions.extend(self._place_active_sell_order(buy_1_price))
                self.logger().info(f"INITIAL -> SELL_ACTIVE: Volume threshold met at minimum spread, selling")
            elif spread > self.config.min_tick:
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().info(f"INITIAL -> BUY_1_ACTIVE: Placing buy_1 at {sell_1_price - self.config.min_tick}")

        elif self.current_state == self.STATES["BUY_1_ACTIVE"]:
            if self._is_order_filled(self.buy_1_order_id):
                self.buy_1_order_id = None
                actions.extend(self._place_buy_2_order(sell_1_price))
                self.buy_1_fill_time = current_time
                self.last_sell_1_price = sell_1_price
                self.logger().info(f"BUY_1_ACTIVE -> BUY_2_ACTIVE: buy_1 filled, placing buy_2")

            elif self._should_trigger_sell(buy_1_price, spread):
                actions.extend(self._cancel_order(self.buy_1_order_id))
                self.buy_1_order_id = None  # Don't track anymore
                actions.extend(self._place_active_sell_order(buy_1_price))
                self.logger().info(f"BUY_1_ACTIVE -> SELL_ACTIVE: Volume threshold met, selling")

            elif price_changed and spread > self.config.min_tick:
                actions.extend(self._cancel_order(self.buy_1_order_id))
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().debug(f"BUY_1_ACTIVE: Price changed, replacing buy_1")

        elif self.current_state == self.STATES["BUY_2_ACTIVE"]:
            time_elapsed = current_time - self.buy_1_fill_time
            cooldown_done = time_elapsed >= self.config.new_order_cooldown

            if self._is_order_filled(self.buy_2_order_id) and cooldown_done:
                self.buy_2_order_id = None
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().info(f"BUY_2_ACTIVE -> BUY_1_ACTIVE: buy_2 filled, cooldown done")

            elif cooldown_done:
                actions.extend(self._cancel_order(self.buy_2_order_id))
                self.buy_2_order_id = None
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().info(f"BUY_2_ACTIVE -> BUY_1_ACTIVE: Cooldown done, canceling buy_2")

            elif price_changed:
                actions.extend(self._cancel_order(self.buy_2_order_id))
                actions.extend(self._place_buy_2_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().debug(f"BUY_2_ACTIVE: Price changed, replacing buy_2")

        elif self.current_state == self.STATES["SELL_ACTIVE"]:
            actions.extend(self._cancel_order(self.active_sell_order_id))

            if self._was_sell_filled_or_partial():
                actions.extend(self._place_followup_buy_order())
                self.logger().info(f"SELL_ACTIVE: Sell filled/partial, placing followup buy")

            self.active_sell_order_id = None
            actions.extend(self._place_buy_1_order(sell_1_price))
            self.last_sell_1_price = sell_1_price
            self.logger().info(f"SELL_ACTIVE -> BUY_1_ACTIVE: Back to buy_1")

        time_since_deep_update = current_time - self.last_deep_order_update_time
        deep_price_changed = (sell_1_price != self.last_deep_order_sell_1_price)

        if (time_since_deep_update >= self.config.deep_buy_update_cooldown and
            deep_price_changed and
            self.total_btc_from_sales > Decimal("0")):
            actions.extend(self._update_deep_orders(buy_1_price))
            self.last_deep_order_update_time = current_time
            self.last_deep_order_sell_1_price = sell_1_price

        self._save_state()

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
                self.total_btc_from_sales += executed_quote
                self._record_hourly_sell(executed_base)
                self._log_debug(f"[_update_executor_states] Sell filled: executed_quote={executed_quote}, executed_base={executed_base}")

    def _is_order_filled(self, order_id: Optional[str]) -> bool:
        if not order_id:
            return False

        executor = self._get_executor_by_id(order_id)
        if not executor:
            self._log_debug(f"[_is_order_filled] order_id={order_id} not found in executors_info")
            return False

        if executor.close_type == CloseType.POSITION_HOLD:
            return True

        if hasattr(executor, '_order') and executor._order:
            tracked_order = executor._order
            executed_base = getattr(tracked_order, 'executed_amount_base', Decimal("0"))
            order_is_filled = getattr(tracked_order, 'is_filled', False)
            self._log_debug(f"[_is_order_filled] order_id={order_id}, executed_amount_base={executed_base}, is_filled={order_is_filled}, close_type={executor.close_type}")
            return executed_base > Decimal("0") or order_is_filled

        self._log_debug(f"[_is_order_filled] order_id={order_id}, no _order attribute, close_type={executor.close_type}")
        return False

    def _should_trigger_sell(self, best_bid: Decimal, spread: Decimal) -> bool:
        if self.buy_only_enabled:
            return False

        try:
            if spread > self.config.min_tick or best_bid <= Decimal("0"):
                return False

            volume_at_bid = self._get_volume_at_price(best_bid, is_buy=False)
            if volume_at_bid <= Decimal("0"):
                return False

            should_sell = volume_at_bid >= self.config.market_sell_threshold
            self._log_debug(f"[_should_trigger_sell] best_bid={best_bid}, spread={spread}, volume={volume_at_bid}, threshold={self.config.market_sell_threshold}, should_sell={should_sell}")
            return should_sell
        except Exception as e:
            self.logger().error(f"[_should_trigger_sell] Error: {e}")
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
        self._log_debug(f"[_place_{order_type}_order] sell_1_price={sell_1_price}, order_price={price}")

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
        self._log_debug(f"[_place_active_sell_order] best_bid={best_bid}, volume_at_bid={volume_at_bid}, sell_amount={sell_amount}")

        if not self._check_hourly_limit(sell_amount):
            self.logger().warning("[_place_active_sell_order] Hourly sell limit exceeded, skipping sell")
            return []

        executor_config = self._create_order_config(
            TradeType.SELL, sell_amount, best_bid,
            ExecutionStrategy.MARKET, f"active_sell_{int(time.time())}"
        )
        self.active_sell_order_id = executor_config.id
        self.current_state = self.STATES["SELL_ACTIVE"]
        return [self._create_executor_action(executor_config)]

    def _place_followup_buy_order(self) -> List[ExecutorAction]:
        amount = self._random_amount()
        self._log_debug(f"[_place_followup_buy_order] amount={amount}")

        executor_config = self._create_order_config(
            TradeType.BUY, amount, Decimal("0"),
            ExecutionStrategy.MARKET, f"followup_buy_{int(time.time())}"
        )
        self.followup_buy_order_id = executor_config.id
        return [self._create_executor_action(executor_config)]

    def _update_deep_orders(self, buy_1_price: Decimal) -> List[ExecutorAction]:
        actions = []

        for order_id in self.deep_order_ids:
            actions.extend(self._cancel_order(order_id))
        self.deep_order_ids.clear()

        available_btc = self.total_btc_from_sales * self.config.deep_order_btc_ratio
        current_price = buy_1_price - (self.config.deep_order_start_offset * self.config.min_tick)
        level_index = 0

        while available_btc > Decimal("0") and current_price > Decimal("0"):
            max_hei = self.config.deep_order_max_per_level
            btc_needed = max_hei * current_price
            hei_amount = available_btc / current_price if btc_needed > available_btc else max_hei

            if hei_amount < Decimal("1"):
                break

            executor_config = self._create_order_config(
                TradeType.BUY, hei_amount, current_price,
                ExecutionStrategy.LIMIT, f"deep_buy_{level_index}_{int(time.time())}"
            )
            actions.append(self._create_executor_action(executor_config))
            self.deep_order_ids.append(executor_config.id)

            available_btc -= hei_amount * current_price
            current_price -= self.config.min_tick
            level_index += 1

        self._log_debug(f"Updated {level_index} deep orders")
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
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        data_dir = os.path.join(base_path, "instances", "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, self.config.state_file_name)

    def _save_state(self):
        try:
            state = {
                "current_state": self.current_state,
                "last_sell_1_price": str(self.last_sell_1_price),
                "buy_1_fill_time": self.buy_1_fill_time,
                "current_order_price": str(self.current_order_price),
                "hourly_sell_amounts": {str(k): str(v) for k, v in self.hourly_sell_amounts.items()},
                "total_btc_from_sales": str(self.total_btc_from_sales),
                "last_deep_order_update_time": self.last_deep_order_update_time,
                "last_deep_order_sell_1_price": str(self.last_deep_order_sell_1_price),
                "last_updated": time.time()
            }
            with open(self._get_state_file_path(), 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger().error(f"Error saving state: {e}")

    def _load_state(self):
        try:
            state_path = self._get_state_file_path()
            if not os.path.exists(state_path):
                return

            with open(state_path, 'r') as f:
                state = json.load(f)

            self.last_sell_1_price = Decimal(state.get("last_sell_1_price", "0"))
            self.buy_1_fill_time = state.get("buy_1_fill_time", 0)
            self.current_order_price = Decimal(state.get("current_order_price", "0"))
            self.hourly_sell_amounts = {int(k): Decimal(v) for k, v in state.get("hourly_sell_amounts", {}).items()}
            self.total_btc_from_sales = Decimal(state.get("total_btc_from_sales", "0"))
            self.last_deep_order_update_time = state.get("last_deep_order_update_time", 0)
            self.last_deep_order_sell_1_price = Decimal(state.get("last_deep_order_sell_1_price", "0"))
            self.current_state = self.STATES["INITIAL"]

            self._log_debug(f"Loaded state: total_btc_from_sales={self.total_btc_from_sales}")
        except Exception as e:
            self.logger().error(f"Error loading state: {e}")

    async def update_processed_data(self):
        order_book_data = self._fetch_order_book_data()
        is_valid = order_book_data is not None

        self.processed_data = {
            "current_state": self.current_state,
            "total_btc_from_sales": self.total_btc_from_sales,
            "timestamp": self.market_data_provider.time(),
            "order_book_valid": is_valid,
            "best_bid": order_book_data["best_bid"] if is_valid else Decimal("0"),
            "best_ask": order_book_data["best_ask"] if is_valid else Decimal("0")
        }

        if is_valid:
            self._log_debug(f"[update_processed_data] best_bid={order_book_data['best_bid']}, best_ask={order_book_data['best_ask']}")
        else:
            self._log_debug("[update_processed_data] Order book data NOT available")

    def _fetch_order_book_data(self) -> Optional[Dict]:
        try:
            order_book = self.market_data_provider.get_order_book(
                self.config.connector_name, self.config.trading_pair
            )
            if order_book is None:
                self._log_debug(f"[_fetch_order_book_data] Order book is None for {self.config.trading_pair}")
                return None

            return {
                "best_bid": Decimal(str(order_book.get_price(False))),
                "best_ask": Decimal(str(order_book.get_price(True)))
            }
        except Exception as e:
            self.logger().error(f"[_fetch_order_book_data] Error fetching order book: {e}")
            return None

    def _get_volume_at_price(self, price: Decimal, is_buy: bool = False) -> Decimal:
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            result = connector.get_volume_for_price(self.config.trading_pair, is_buy, price)
            volume = result.result_volume
            self._log_debug(f"[_get_volume_at_price] price={price}, is_buy={is_buy}, volume={volume}")
            return volume if volume else Decimal("0")
        except Exception as e:
            self._log_debug(f"[_get_volume_at_price] Error getting volume at price {price}: {e}")
            return Decimal("0")

    def to_format_status(self) -> List[str]:
        header = f"HEI/BTC MM | {self.config.connector_name}:{self.config.trading_pair}"
        status = [header, "=" * len(header), f"State: {self.current_state}"]

        if self.processed_data.get("order_book_valid", False):
            status.extend([
                f"Best bid: {self.processed_data.get('best_bid', 'N/A')}",
                f"Best ask: {self.processed_data.get('best_ask', 'N/A')}"
            ])
        else:
            status.append("Order book: Not available")

        status.extend([
            f"Last sell_1 price: {self.last_sell_1_price}",
            f"Current order price: {self.current_order_price}",
            "",
            f"Hourly sold: {self._get_hourly_sold()} / {self.config.hourly_sell_limit} HEI",
            f"Total BTC from sales: {self.total_btc_from_sales}",
            f"Deep orders: {len(self.deep_order_ids)}"
        ])
        return status
