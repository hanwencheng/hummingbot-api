"""
High Funding Rate Trading Strategy Controller

A perpetual trading strategy that shorts high-volume altcoins when funding rates are high (≤-0.5%).

Strategy:
1. Monitor funding rate continuously
2. When rate ≤ threshold AND 30 seconds before funding charge, enter short position (once per cycle)
3. Place short order at best_ask + 10 ticks with 2x leverage
4. Exit with dual take-profit: TP1 at 4% (50%), TP2 at 6% (50%)
5. Auto-cancel unfilled entry orders after funding is charged
6. Continuous execution every funding period (4 hours for Binance)

Protections:
- Auto-close if funding rate becomes unfavorable (> threshold)
- Auto-close if position held for max funding cycles (default: 1 cycle)
- Track and log funding fees charged while holding position
- Emergency close after max holding time (24 hours)

State Machine:
INITIAL → WAITING_FOR_ENTRY → POSITION_OPEN → POSITION_CLOSED → INITIAL
"""

import time
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class HighFundingRateConfig(ControllerConfigBase):
    """Configuration for High Funding Rate Trading Strategy"""

    controller_type: str = "market_making"
    controller_name: str = "high_funding_rate"
    candles_config: List = []

    connector_name: str = Field(
        default="binance_perpetual",
        json_schema_extra={"prompt": "Enter the perpetual connector name (e.g., binance_perpetual):", "prompt_on_new": True}
    )
    trading_pair: str = Field(
        default="BTC-USDT",
        json_schema_extra={"prompt": "Enter the trading pair (e.g., BTC-USDT):", "prompt_on_new": True}
    )

    min_funding_rate: Decimal = Field(
        default=Decimal("-0.005"),
        json_schema_extra={"prompt": "Enter minimum funding rate to trigger (negative, e.g., -0.02 for -2%):", "prompt_on_new": True, "is_updatable": True}
    )
    entry_timing_before_funding: int = Field(
        default=30,
        json_schema_extra={"prompt": "Enter seconds before funding to enter position:", "prompt_on_new": True, "is_updatable": True}
    )

    position_size_usd: Decimal = Field(
        default=Decimal("1000"),
        json_schema_extra={"prompt": "Enter position size in USD:", "prompt_on_new": True, "is_updatable": True}
    )
    leverage: int = Field(
        default=2,
        json_schema_extra={"prompt": "Enter leverage (e.g., 2 for 2x leverage):", "prompt_on_new": True, "is_updatable": True}
    )
    entry_price_tick_offset: int = Field(
        default=10,
        json_schema_extra={"prompt": "Enter tick offset for entry price (ticks above best ask):", "prompt_on_new": True, "is_updatable": True}
    )

    take_profit_1_pct: Decimal = Field(
        default=Decimal("0.03"),
        json_schema_extra={"prompt": "Enter first take profit percentage (e.g., 0.04 for 4%):", "prompt_on_new": True, "is_updatable": True}
    )
    take_profit_2_pct: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={"prompt": "Enter second take profit percentage (e.g., 0.06 for 6%):", "prompt_on_new": True, "is_updatable": True}
    )
    max_holding_time: int = Field(
        default=86400,
        json_schema_extra={"prompt": "Enter max holding time in seconds:", "prompt_on_new": True, "is_updatable": True}
    )

    recheck_funding_before_entry: bool = Field(
        default=True,
        json_schema_extra={"prompt": "Re-check funding rate before order placement (recommended: true):", "prompt_on_new": True, "is_updatable": True}
    )

    close_on_unfavorable_funding: bool = Field(
        default=True,
        json_schema_extra={"prompt": "Close position if funding rate becomes unfavorable (recommended: true):", "prompt_on_new": True, "is_updatable": True}
    )

    max_funding_cycles_to_hold: int = Field(
        default=1,
        json_schema_extra={"prompt": "Max funding cycles to hold position (default 1 = 4 hours):", "prompt_on_new": True, "is_updatable": True}
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class HighFundingRateController(ControllerBase):
    """High Funding Rate Trading Strategy Controller"""

    STATES = {
        "INITIAL": "INITIAL",
        "WAITING_FOR_ENTRY": "WAITING_FOR_ENTRY",
        "POSITION_OPEN": "POSITION_OPEN",
        "POSITION_CLOSED": "POSITION_CLOSED"
    }

    def __init__(self, config: HighFundingRateConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=config.trading_pair)
        ])

        # State
        self.current_state = self.STATES["INITIAL"]

        # Order tracking
        self.entry_order_id: Optional[str] = None
        self.active_tp_orders: List[str] = []  # Track all active TP orders

        # Funding
        self.current_funding_rate: Decimal = Decimal("0")
        self.next_funding_time: float = 0
        self.last_funding_times: List[float] = []
        self.entry_placed_for_current_cycle: bool = False

        # Position
        self.entry_price: Decimal = Decimal("0")
        self.position_size: Decimal = Decimal("0")
        self.position_entry_time: float = 0
        self.entry_funding_rate: Decimal = Decimal("0")
        self.funding_cycles_held: int = 0

        # Flags
        self._leverage_configured = False
        self._tp1_logged = False
        self._tp2_logged = False
        self.last_funding_log_time: float = 0
        self.processed_data = {}

        self.logger().info(f"Initialized for {config.trading_pair} on {config.connector_name}")

    def _configure_leverage(self):
        """Configure leverage for perpetual trading"""
        if self._leverage_configured:
            return

        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            current = connector.get_leverage(self.config.trading_pair)

            if current == self.config.leverage:
                self.logger().info(f"Leverage already {self.config.leverage}x")
            else:
                connector.set_leverage(self.config.trading_pair, self.config.leverage)
                self.logger().info(f"Leverage set to {self.config.leverage}x (was {current}x)")

            self._leverage_configured = True
        except Exception as e:
            self.logger().error(f"Error configuring leverage: {e}")

    async def update_processed_data(self):
        """Update market data and funding info"""
        try:
            if not self._leverage_configured:
                self._configure_leverage()

            connector = self.market_data_provider.get_connector(self.config.connector_name)
            order_book = self.market_data_provider.get_order_book(self.config.connector_name, self.config.trading_pair)
            funding_info = connector.get_funding_info(self.config.trading_pair)

            self.current_funding_rate = Decimal(str(funding_info.get("rate", 0)))
            new_funding_time = float(funding_info.get("nextFundingTime", 0))
            current_time = self.market_data_provider.time()

            # Detect funding cycle change
            if new_funding_time != self.next_funding_time and self.next_funding_time > 0:
                self.logger().info(f"New funding cycle: {self.next_funding_time} → {new_funding_time}")
                self.entry_placed_for_current_cycle = False

                # Track funding cycles while holding position
                if self.current_state == self.STATES["POSITION_OPEN"]:
                    self.funding_cycles_held += 1
                    self.logger().warning(
                        f"Funding charged while holding position! Cycles held: {self.funding_cycles_held}, "
                        f"Entry rate: {self.entry_funding_rate*100:.4f}%, Current rate: {self.current_funding_rate*100:.4f}%"
                    )

            self.next_funding_time = new_funding_time

            self.processed_data = {
                "order_book_valid": order_book is not None,
                "best_bid": Decimal(str(order_book.get_price(False))) if order_book else Decimal("0"),
                "best_ask": Decimal(str(order_book.get_price(True))) if order_book else Decimal("0"),
                "funding_rate": self.current_funding_rate,
                "next_funding_time": self.next_funding_time,
                "time_until_funding": new_funding_time - current_time,
                "current_time": current_time
            }

            self._monitor_funding_frequency(current_time)
            self._log_funding_info_periodic(current_time)

        except Exception as e:
            self.logger().error(f"Error updating data: {e}")
            self.processed_data = {"order_book_valid": False}

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Main strategy logic - state machine"""
        if not self.processed_data.get("order_book_valid", False):
            return []

        actions = []
        funding_rate = self.processed_data["funding_rate"]
        time_until_funding = self.processed_data["time_until_funding"]
        current_time = self.processed_data["current_time"]

        # Auto-cancel entry order if funding occurred
        if self.current_state == self.STATES["WAITING_FOR_ENTRY"] and time_until_funding < 0:
            self.logger().warning("Funding occurred, cancelling entry order")
            actions.extend(self._cancel_order(self.entry_order_id))
            self.current_state = self.STATES["INITIAL"]
            self.entry_placed_for_current_cycle = False
            return actions

        if self.current_state == self.STATES["INITIAL"]:
            if funding_rate <= self.config.min_funding_rate:
                timing_window = self.config.entry_timing_before_funding
                in_window = timing_window - 2 < time_until_funding < timing_window + 2

                if in_window and not self.entry_placed_for_current_cycle:
                    actions.extend(self._place_entry_order())
                    self.entry_placed_for_current_cycle = True
                    self.logger().info(f"Entry: rate={funding_rate}, time_until={time_until_funding:.1f}s")

        elif self.current_state == self.STATES["WAITING_FOR_ENTRY"]:
            if self._is_order_filled(self.entry_order_id):
                actions.extend(self._place_take_profit_orders())
                self.position_entry_time = current_time
                self.logger().info(f"Entry filled at {self.entry_price}")
            elif self.entry_order_id and (current_time - self.position_entry_time) > 60:
                actions.extend(self._cancel_order(self.entry_order_id))
                self.current_state = self.STATES["INITIAL"]
                self.logger().warning("Entry timeout")

        elif self.current_state == self.STATES["POSITION_OPEN"]:
            # Check which TPs are filled and remove from active list
            filled_tps = []
            for tp_id in self.active_tp_orders[:]:  # Copy list to iterate
                if self._is_order_filled(tp_id):
                    filled_tps.append(tp_id)
                    self.active_tp_orders.remove(tp_id)
                    if not self._tp1_logged:
                        self.logger().info(f"TP filled: {tp_id}")
                        self._tp1_logged = True
                    elif not self._tp2_logged:
                        self._tp2_logged = True

            # All TPs filled = position closed normally
            if len(self.active_tp_orders) == 0 and len(filled_tps) > 0:
                self.current_state = self.STATES["POSITION_CLOSED"]
                self._log_trade_results()
                return actions

            # Check if position should be force-closed
            force_close_reason = self._should_force_close_position(funding_rate, current_time)
            if force_close_reason:
                actions.extend(self._force_close_position(force_close_reason))
                return actions

        elif self.current_state == self.STATES["POSITION_CLOSED"]:
            self._reset_state()
            self.current_state = self.STATES["INITIAL"]

        return actions

    def _place_entry_order(self) -> List[ExecutorAction]:
        """Place short entry order"""
        if self.config.recheck_funding_before_entry:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            current_rate = Decimal(str(connector.get_funding_info(self.config.trading_pair).get("rate", 0)))
            if current_rate > self.config.min_funding_rate:
                self.logger().warning(f"Rate changed: {current_rate} > {self.config.min_funding_rate}")
                return []

        best_ask = self.processed_data["best_ask"]
        min_tick = self._get_min_tick()
        entry_price = best_ask + (self.config.entry_price_tick_offset * min_tick)
        position_size = self.config.position_size_usd / entry_price

        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.SELL,
            amount=position_size,
            price=entry_price,
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"entry_{int(time.time())}"
        )

        self.entry_order_id = executor_config.id
        self.entry_price = entry_price
        self.position_size = position_size
        self.position_entry_time = self.market_data_provider.time()
        self.entry_funding_rate = self.current_funding_rate  # Track funding rate at entry
        self.funding_cycles_held = 0
        self.current_state = self.STATES["WAITING_FOR_ENTRY"]

        return [CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config)]

    def _place_take_profit_orders(self) -> List[ExecutorAction]:
        """Place TP1 and TP2 orders"""
        half_size = self.position_size / Decimal("2")
        tp1_price = self.entry_price * (Decimal("1") - self.config.take_profit_1_pct)
        tp2_price = self.entry_price * (Decimal("1") - self.config.take_profit_2_pct)

        tp1_config = self._create_tp_config(tp1_price, half_size, "tp1")
        tp2_config = self._create_tp_config(tp2_price, half_size, "tp2")

        # Track active TP orders
        self.active_tp_orders = [tp1_config.id, tp2_config.id]
        self.current_state = self.STATES["POSITION_OPEN"]

        self.logger().info(f"TPs: {tp1_price} ({self.config.take_profit_1_pct*100}%), {tp2_price} ({self.config.take_profit_2_pct*100}%)")

        return [
            CreateExecutorAction(controller_id=self.config.id, executor_config=tp1_config),
            CreateExecutorAction(controller_id=self.config.id, executor_config=tp2_config)
        ]

    def _create_tp_config(self, price: Decimal, amount: Decimal, level_prefix: str) -> OrderExecutorConfig:
        """Create take profit order config"""
        return OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,
            amount=amount,
            price=price,
            execution_strategy=ExecutionStrategy.LIMIT,
            level_id=f"{level_prefix}_{int(time.time())}"
        )

    def _should_force_close_position(self, funding_rate: Decimal, current_time: float) -> Optional[str]:
        """Check if position should be force-closed, return reason if yes"""
        # Check 1: Funding rate became unfavorable
        if self.config.close_on_unfavorable_funding and funding_rate > self.config.min_funding_rate:
            return f"funding_rate_unfavorable: {self.entry_funding_rate*100:.4f}% → {funding_rate*100:.4f}% (threshold: {self.config.min_funding_rate*100:.2f}%)"

        # Check 2: Exceeded max funding cycles
        if self.funding_cycles_held >= self.config.max_funding_cycles_to_hold:
            return f"max_funding_cycles: held {self.funding_cycles_held} cycles (max: {self.config.max_funding_cycles_to_hold})"

        # Check 3: Exceeded max holding time
        holding_time = current_time - self.position_entry_time
        if holding_time > self.config.max_holding_time:
            return f"max_holding_time: {holding_time:.0f}s ({holding_time/3600:.2f}h)"

        return None

    def _force_close_position(self, reason: str) -> List[ExecutorAction]:
        """Force close position at market price"""
        actions = []

        # Step 1: Cancel all active TP orders
        if self.active_tp_orders:
            self.logger().info(f"Cancelling {len(self.active_tp_orders)} active TP orders: {self.active_tp_orders}")
            for tp_order_id in self.active_tp_orders[:]:  # Copy list
                cancel_actions = self._cancel_order(tp_order_id)
                if cancel_actions:
                    actions.extend(cancel_actions)

        # Step 2: Place market order to close entire position immediately
        close_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=TradeType.BUY,  # Close short
            amount=self.position_size,
            price=self.processed_data.get("best_ask", Decimal("0")),
            execution_strategy=ExecutionStrategy.MARKET,
            level_id=f"force_close_{int(time.time())}"
        )

        actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=close_config))

        self.logger().warning(f"Force closing position at market: {reason}")
        self.active_tp_orders.clear()  # Clear the list
        self.current_state = self.STATES["POSITION_CLOSED"]

        return actions

    def _monitor_funding_frequency(self, current_time: float):
        """Monitor funding frequency changes"""
        if self.next_funding_time > current_time:
            if not self.last_funding_times or self.next_funding_time > self.last_funding_times[-1]:
                self.last_funding_times.append(self.next_funding_time)
                if len(self.last_funding_times) > 3:
                    self.last_funding_times.pop(0)

        if len(self.last_funding_times) >= 2:
            interval = self.last_funding_times[-1] - self.last_funding_times[-2]
            if abs(interval - 14400) > 300:  # 4 hours ± 5 min
                self.logger().warning(f"Funding interval changed: {interval/3600:.1f}h")

    def _log_funding_info_periodic(self, current_time: float):
        """Log funding info every 5 minutes"""
        if current_time - self.last_funding_log_time < 300:
            return

        time_until = self.processed_data.get("time_until_funding", 0)
        hours, remainder = divmod(int(time_until), 3600)
        minutes, seconds = divmod(remainder, 60)

        frequency = "Unknown"
        if len(self.last_funding_times) >= 2:
            interval = self.last_funding_times[-1] - self.last_funding_times[-2]
            frequency = f"{interval/3600:.1f}h"

        will_trigger = "YES" if self.current_funding_rate <= self.config.min_funding_rate else "NO"

        self.logger().info("=" * 60)
        self.logger().info("FUNDING SNAPSHOT")
        self.logger().info(f"Rate: {self.current_funding_rate} ({self.current_funding_rate*100:.4f}%) | Threshold: {self.config.min_funding_rate*100:.2f}%")
        self.logger().info(f"Frequency: {frequency} | Next: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(self.next_funding_time))}")
        self.logger().info(f"Time until: {hours}h {minutes}m {seconds}s | Will trigger: {will_trigger}")
        self.logger().info("=" * 60)

        self.last_funding_log_time = current_time

    def _get_min_tick(self) -> Decimal:
        """Get minimum price tick"""
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            rules = connector.trading_rules.get(self.config.trading_pair)
            return Decimal(str(rules.min_price_increment)) if rules else Decimal("0.01")
        except Exception as e:
            self.logger().error(f"Error getting min tick: {e}")
            return Decimal("0.01")

    def _get_executor_by_id(self, executor_id: str) -> Optional[ExecutorInfo]:
        """Get executor by ID"""
        return next((e for e in self.executors_info if e.id == executor_id), None)

    def _is_order_filled(self, order_id: Optional[str]) -> bool:
        """Check if order is filled"""
        if not order_id:
            return False

        executor = self._get_executor_by_id(order_id)
        if not executor:
            return False

        if executor.close_type == CloseType.POSITION_HOLD:
            return True

        if hasattr(executor, '_order') and executor._order:
            tracked = executor._order
            return getattr(tracked, 'executed_amount_base', Decimal("0")) > 0 or getattr(tracked, 'is_filled', False)

        return False

    def _cancel_order(self, order_id: Optional[str]) -> List[ExecutorAction]:
        """Cancel active order"""
        if not order_id:
            return []

        executor = self._get_executor_by_id(order_id)
        if executor and executor.is_active:
            return [StopExecutorAction(controller_id=self.config.id, executor_id=order_id)]
        return []

    def _log_trade_results(self):
        """Log final P&L and statistics"""
        try:
            duration = self.market_data_provider.time() - self.position_entry_time
            tp1_pnl = self.config.take_profit_1_pct * 50  # 50% at TP1
            tp2_pnl = self.config.take_profit_2_pct * 50  # 50% at TP2
            total_pnl = tp1_pnl + tp2_pnl

            self.logger().info("=" * 60)
            self.logger().info("TRADE COMPLETED")
            self.logger().info(f"Entry: {self.entry_price} | Size: {self.position_size} (${self.position_size*self.entry_price:.2f})")
            self.logger().info(f"TP1: {self.entry_price*(1-self.config.take_profit_1_pct):.2f} (50% @ {self.config.take_profit_1_pct*100:.1f}%)")
            self.logger().info(f"TP2: {self.entry_price*(1-self.config.take_profit_2_pct):.2f} (50% @ {self.config.take_profit_2_pct*100:.1f}%)")
            self.logger().info(f"Duration: {duration:.0f}s ({duration/3600:.2f}h) | P&L: {total_pnl:.2f}%")
            self.logger().info(f"Funding cycles held: {self.funding_cycles_held} (Entry rate: {self.entry_funding_rate*100:.4f}%, Exit rate: {self.current_funding_rate*100:.4f}%)")
            self.logger().info("=" * 60)
        except Exception as e:
            self.logger().error(f"Error logging results: {e}")

    def _reset_state(self):
        """Reset all tracking variables"""
        self.entry_order_id = None
        self.active_tp_orders.clear()
        self.entry_price = Decimal("0")
        self.position_size = Decimal("0")
        self.position_entry_time = 0
        self.entry_placed_for_current_cycle = False
        self.entry_funding_rate = Decimal("0")
        self.funding_cycles_held = 0
        self._tp1_logged = False
        self._tp2_logged = False

    def to_format_status(self) -> List[str]:
        """Format status for display"""
        header = f"High Funding Rate | {self.config.connector_name}:{self.config.trading_pair}"
        status = [header, "=" * len(header), f"State: {self.current_state}"]

        if self.processed_data.get("order_book_valid"):
            status.extend([
                f"Bid/Ask: {self.processed_data['best_bid']}/{self.processed_data['best_ask']}",
                f"Funding: {self.processed_data['funding_rate']} ({self.processed_data['time_until_funding']:.0f}s until)"
            ])
        else:
            status.append("Order book: N/A")

        if self.current_state == self.STATES["POSITION_OPEN"]:
            status.extend([
                f"Position: {self.position_size} @ {self.entry_price}",
                f"Active TPs: {len(self.active_tp_orders)} | Cycles held: {self.funding_cycles_held}"
            ])
        elif self.current_state != self.STATES["INITIAL"]:
            status.append(f"Position: {self.position_size} @ {self.entry_price}")

        status.extend([
            "",
            f"Threshold: {self.config.min_funding_rate} | Entry: {self.config.entry_timing_before_funding}s before",
            f"Size: ${self.config.position_size_usd} | Leverage: {self.config.leverage}x"
        ])

        return status
