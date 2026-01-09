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

        self.logger().info(f"[determine_executor_actions] state={self.current_state}, sell_1={sell_1_price}, buy_1={buy_1_price}, spread={spread}, price_changed={price_changed}")

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
                actions.extend(self._place_buy_1_order(sell_1_price))
                self.last_sell_1_price = sell_1_price
                self.logger().info(f"BUY_2_ACTIVE -> BUY_1_ACTIVE: buy_2 filled, cooldown done")

            elif cooldown_done:
                actions.extend(self._cancel_order(self.buy_2_order_id))
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
        """Efficient lookup using generator - stops at first match"""
        return next(
            (e for e in self.executors_info if e.id == executor_id),
            None
        )

    def _update_executor_states(self):
        """Update tracking of executor states based on executors_info (optimized)"""
        active_sell_executors = self.filter_executors(
            self.executors_info,
            lambda x: x.is_active and x.custom_info.get("level_id", "").startswith("active_sell_")
        )

        for executor in active_sell_executors:
            executed_quote = Decimal("0")
            executed_base = Decimal("0")

            if hasattr(executor, '_order') and executor._order:
                executed_quote = Decimal(str(getattr(executor._order, 'executed_amount_quote', Decimal("0"))))
                executed_base = Decimal(str(getattr(executor._order, 'executed_amount_base', Decimal("0"))))

            self.sell_filled_amount = executed_quote
            if executed_quote > Decimal("0"):
                self.total_btc_from_sales += executed_quote
                self._record_hourly_sell(executed_base)
                self.logger().info(f"[_update_executor_states] Sell filled: executed_quote={executed_quote}, executed_base={executed_base}")

    def _is_order_filled(self, order_id: Optional[str]) -> bool:
        """Check if an order executor is filled by accessing the underlying TrackedOrder (optimized)"""
        if not order_id:
            return False

        executor = self._get_executor_by_id(order_id)
        if not executor:
            self.logger().info(f"[_is_order_filled] order_id={order_id} not found in executors_info")
            return False

        is_filled = False

        if hasattr(executor, '_order') and executor._order:
            tracked_order = executor._order
            executed_base = getattr(tracked_order, 'executed_amount_base', Decimal("0"))
            order_is_filled = getattr(tracked_order, 'is_filled', False)

            self.logger().info(f"[_is_order_filled] order_id={order_id}, executed_amount_base={executed_base}, is_filled={order_is_filled}, close_type={executor.close_type}")

            if executed_base > Decimal("0"):
                is_filled = True
            elif order_is_filled:
                is_filled = True
        else:
            self.logger().info(f"[_is_order_filled] order_id={order_id}, no _order attribute, close_type={executor.close_type}")

        if executor.close_type == CloseType.POSITION_HOLD:
            is_filled = True

        return is_filled

    def _should_trigger_sell(self, best_bid: Decimal, spread: Decimal) -> bool:
        """Check if sell should be triggered based on volume at best bid when spread is minimum"""
        try:
            if spread > self.config.min_tick:
                return False

            if best_bid <= Decimal("0"):
                return False

            volume_at_bid = self._get_volume_at_price(best_bid, is_buy=False)
            threshold = self.config.market_sell_threshold

            self.logger().info(f"[_should_trigger_sell] volume_at_bid type={type(volume_at_bid)}, value={volume_at_bid}")
            self.logger().info(f"[_should_trigger_sell] threshold type={type(threshold)}, value={threshold}")

            if volume_at_bid <= Decimal("0"):
                return False

            should_sell = volume_at_bid >= threshold
            self.logger().info(f"[_should_trigger_sell] best_bid={best_bid}, spread={spread}, volume={volume_at_bid}, threshold={threshold}, should_sell={should_sell}")
            return should_sell
        except Exception as e:
            self.logger().error(f"[_should_trigger_sell] Error: {e}")
            return False

    def _was_sell_filled_or_partial(self) -> bool:
        """Check if active sell order was filled or partially filled (optimized)"""
        if not self.active_sell_order_id:
            return False

        executor = self._get_executor_by_id(self.active_sell_order_id)
        if not executor:
            return False

        if hasattr(executor, '_order') and executor._order:
            executed_base = getattr(executor._order, 'executed_amount_base', Decimal("0"))
            if executed_base > Decimal("0"):
                return True
        if executor.close_type == CloseType.POSITION_HOLD:
            return True
        return False

    def _random_amount(self) -> Decimal:
        """Generate random order amount between min and max size"""
        min_size = float(self.config.buy_order_min_size)
        max_size = float(self.config.buy_order_max_size)
        return Decimal(str(round(random.uniform(min_size, max_size), 2)))

    def _place_buy_1_order(self, sell_1_price: Decimal) -> List[ExecutorAction]:
        """Place buy_1 order: 1 tick below best ask"""
        price = sell_1_price - self.config.min_tick
        amount = self._random_amount()
        self.logger().info(f"[_place_buy_1_order] sell_1_price={sell_1_price}, order_price={price}, amount={amount}")

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=amount,
            price=price,
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"buy_1_{int(time.time())}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        self.buy_1_order_id = executor_config.id
        self.current_order_price = price
        self.current_state = self.STATES["BUY_1_ACTIVE"]

        return [action]

    def _place_buy_2_order(self, sell_1_price: Decimal) -> List[ExecutorAction]:
        """Place buy_2 order: 2 ticks below best ask"""
        price = sell_1_price - (2 * self.config.min_tick)
        amount = self._random_amount()
        self.logger().info(f"[_place_buy_2_order] sell_1_price={sell_1_price}, order_price={price}, amount={amount}")

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=amount,
            price=price,
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"buy_2_{int(time.time())}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        self.buy_2_order_id = executor_config.id
        self.current_order_price = price
        self.current_state = self.STATES["BUY_2_ACTIVE"]

        return [action]

    def _place_active_sell_order(self, best_bid: Decimal) -> List[ExecutorAction]:
        """Place market sell order when volume threshold is met at best bid"""
        volume_at_bid = self._get_volume_at_price(best_bid, is_buy=False)
        sell_amount = volume_at_bid / Decimal("2")
        self.logger().info(f"[_place_active_sell_order] best_bid={best_bid}, volume_at_bid={volume_at_bid}, sell_amount={sell_amount}")

        if not self._check_hourly_limit(sell_amount):
            self.logger().warning(f"[_place_active_sell_order] Hourly sell limit exceeded, skipping sell")
            return []

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.SELL,
            amount=sell_amount,
            price=best_bid,
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"active_sell_{int(time.time())}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        self.active_sell_order_id = executor_config.id
        self.current_state = self.STATES["SELL_ACTIVE"]

        return [action]

    def _place_followup_buy_order(self) -> List[ExecutorAction]:
        """Place followup market buy order after sell (Design #7)"""
        amount = self._random_amount()
        self.logger().info(f"[_place_followup_buy_order] amount={amount}")

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=amount,
            price=Decimal("0"),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"followup_buy_{int(time.time())}"
        )

        action = CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

        self.followup_buy_order_id = executor_config.id

        return [action]

    def _update_deep_orders(self, buy_1_price: Decimal) -> List[ExecutorAction]:
        """Update deep buy orders to build order book depth (Design #6)"""
        actions = []

        for order_id in self.deep_order_ids:
            actions.extend(self._cancel_order(order_id))
        self.deep_order_ids.clear()

        available_btc = self.total_btc_from_sales * self.config.deep_order_btc_ratio
        starting_price = buy_1_price - (self.config.deep_order_start_offset * self.config.min_tick)

        current_price = starting_price
        level_index = 0

        while available_btc > Decimal("0") and current_price > Decimal("0"):
            max_hei = self.config.deep_order_max_per_level
            btc_needed = max_hei * current_price

            if btc_needed > available_btc:
                hei_amount = available_btc / current_price
            else:
                hei_amount = max_hei

            if hei_amount < Decimal("1"):
                break

            executor_config = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=TradeType.BUY,
                amount=hei_amount,
                price=current_price,
                execution_strategy=ExecutionStrategy.LIMIT,
                level_id=f"deep_buy_{level_index}_{int(time.time())}"
            )

            action = CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=executor_config
            )

            actions.append(action)
            self.deep_order_ids.append(executor_config.id)

            available_btc -= hei_amount * current_price
            current_price -= self.config.min_tick
            level_index += 1

        self.logger().info(f"Updated {level_index} deep orders")
        return actions

    def _cancel_order(self, order_id: Optional[str]) -> List[ExecutorAction]:
        """Cancel an order by executor ID (optimized)"""
        if not order_id:
            return []

        executor = self._get_executor_by_id(order_id)
        if executor and executor.is_active:
            return [StopExecutorAction(
                controller_id=self.config.id,
                executor_id=order_id
            )]
        return []

    def _check_hourly_limit(self, amount: Decimal) -> bool:
        """Check if sell amount would exceed hourly limit (Design #5)"""
        current_hour = int(self.market_data_provider.time() // 3600) * 3600
        current_sold = self.hourly_sell_amounts.get(current_hour, Decimal("0"))
        return (current_sold + amount) <= self.config.hourly_sell_limit

    def _record_hourly_sell(self, amount: Decimal):
        """Record sell amount for hourly tracking"""
        current_hour = int(self.market_data_provider.time() // 3600) * 3600
        current_sold = self.hourly_sell_amounts.get(current_hour, Decimal("0"))
        self.hourly_sell_amounts[current_hour] = current_sold + amount

        old_hours = [h for h in self.hourly_sell_amounts.keys() if h < current_hour - 86400]
        for h in old_hours:
            del self.hourly_sell_amounts[h]

    def _get_state_file_path(self) -> str:
        """Get path to state file"""
        base_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        data_dir = os.path.join(base_path, "instances", "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, self.config.state_file_name)

    def _save_state(self):
        """Save strategy state to file (Design #8)"""
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
            state_path = self._get_state_file_path()
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger().error(f"Error saving state: {e}")

    def _load_state(self):
        """Load strategy state from file"""
        try:
            state_path = self._get_state_file_path()
            if os.path.exists(state_path):
                with open(state_path, 'r') as f:
                    state = json.load(f)

                    self.current_state = state.get("current_state", self.STATES["INITIAL"])
                    self.last_sell_1_price = Decimal(state.get("last_sell_1_price", "0"))
                    self.buy_1_fill_time = state.get("buy_1_fill_time", 0)
                    self.current_order_price = Decimal(state.get("current_order_price", "0"))
                    self.hourly_sell_amounts = {
                        int(k): Decimal(v) for k, v in state.get("hourly_sell_amounts", {}).items()
                    }
                    self.total_btc_from_sales = Decimal(state.get("total_btc_from_sales", "0"))
                    self.last_deep_order_update_time = state.get("last_deep_order_update_time", 0)
                    self.last_deep_order_sell_1_price = Decimal(state.get("last_deep_order_sell_1_price", "0"))

                    self.current_state = self.STATES["INITIAL"]

                    self.logger().info(f"Loaded state: total_btc_from_sales={self.total_btc_from_sales}")
        except Exception as e:
            self.logger().error(f"Error loading state: {e}")

    async def update_processed_data(self):
        """Update processed data including order book snapshot (called every cycle)"""
        order_book_data = self._fetch_order_book_data()

        self.processed_data = {
            "current_state": self.current_state,
            "total_btc_from_sales": self.total_btc_from_sales,
            "timestamp": self.market_data_provider.time(),
            "order_book_valid": order_book_data is not None,
            "best_bid": order_book_data["best_bid"] if order_book_data else Decimal("0"),
            "best_ask": order_book_data["best_ask"] if order_book_data else Decimal("0")
        }

        if order_book_data:
            self.logger().info(f"[update_processed_data] best_bid={order_book_data['best_bid']}, best_ask={order_book_data['best_ask']}")
        else:
            self.logger().info("[update_processed_data] Order book data NOT available")

    def _fetch_order_book_data(self) -> Optional[Dict]:
        """Fetch order book data from market_data_provider"""
        try:
            self.logger().info(f"[_fetch_order_book_data] Fetching order book for {self.config.connector_name}:{self.config.trading_pair}")

            order_book = self.market_data_provider.get_order_book(
                self.config.connector_name,
                self.config.trading_pair
            )

            if order_book is None:
                self.logger().info(f"[_fetch_order_book_data] Order book is None for {self.config.trading_pair}")
                return None

            best_bid = Decimal(str(order_book.get_price(False)))
            best_ask = Decimal(str(order_book.get_price(True)))

            self.logger().info(f"[_fetch_order_book_data] Success: best_bid={best_bid}, best_ask={best_ask}")

            return {
                "best_bid": best_bid,
                "best_ask": best_ask
            }
        except Exception as e:
            self.logger().error(f"[_fetch_order_book_data] Error fetching order book: {e}")
            return None

    def _get_volume_at_price(self, price: Decimal, is_buy: bool = False) -> Decimal:
        """Get cumulative volume at a specific price level using connector directly (like VWAP script)"""
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            result = connector.get_volume_for_price(
                self.config.trading_pair,
                is_buy,
                price
            )
            volume = result.result_volume
            self.logger().info(f"[_get_volume_at_price] price={price}, is_buy={is_buy}, volume={volume}")
            return volume if volume else Decimal("0")
        except Exception as e:
            self.logger().info(f"[_get_volume_at_price] Error getting volume at price {price}: {e}")
            return Decimal("0")

    def to_format_status(self) -> List[str]:
        """Get formatted status for display"""
        status = []

        header = f"HEI/BTC MM | {self.config.connector_name}:{self.config.trading_pair}"
        status.append(header)
        status.append("=" * len(header))

        status.append(f"State: {self.current_state}")

        if self.processed_data.get("order_book_valid", False):
            status.append(f"Best bid: {self.processed_data.get('best_bid', 'N/A')}")
            status.append(f"Best ask: {self.processed_data.get('best_ask', 'N/A')}")
        else:
            status.append("Order book: Not available")

        status.append(f"Last sell_1 price: {self.last_sell_1_price}")
        status.append(f"Current order price: {self.current_order_price}")
        status.append("")

        current_hour = int(self.market_data_provider.time() // 3600) * 3600
        hourly_sold = self.hourly_sell_amounts.get(current_hour, Decimal("0"))
        status.append(f"Hourly sold: {hourly_sold} / {self.config.hourly_sell_limit} HEI")
        status.append(f"Total BTC from sales: {self.total_btc_from_sales}")
        status.append(f"Deep orders: {len(self.deep_order_ids)}")

        return status
