"""
Signal Manager Module - Singleton with per-subscriber consumption tracking.

Usage:
    manager = SignalManager.instance(url, user_id)
    subscription = manager.subscribe("my_controller")

    await manager.update()

    for signal in subscription.pending(pair):
        process(signal)
        subscription.consume(signal)

    # Or discard all when paused
    subscription.consume_all(pair, reason="paused")
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, List, Literal, Optional, Set

import aiohttp


@dataclass
class TradeSignal:
    signal_id: str
    signal_type: Literal["BUY", "SELL"]
    timestamp: float
    trading_pair: str
    source_id: str = ""
    price: Optional[float] = None
    amount: Optional[float] = None


class SignalSubscription:
    def __init__(self, manager: "SignalManager", subscriber_id: str):
        self._manager = manager
        self.subscriber_id = subscriber_id
        self._consumed: Set[str] = set()
        self._last_ts: float = 0
        self._on_consume: Optional[Callable[[TradeSignal, str], None]] = None

    def pending(self, trading_pair: Optional[str] = None) -> List[TradeSignal]:
        signals = self._manager.signals_for(trading_pair) if trading_pair else self._manager.signals
        return [s for s in signals if s.signal_id not in self._consumed]

    def consume(self, signal: TradeSignal, reason: str = "processed"):
        if signal.signal_id not in self._consumed:
            self._consumed.add(signal.signal_id)
            self._last_ts = max(self._last_ts, signal.timestamp)
            if self._on_consume:
                self._on_consume(signal, reason)

    def consume_all(self, trading_pair: Optional[str] = None, reason: str = "discarded"):
        for signal in self.pending(trading_pair):
            self.consume(signal, reason)

    def on_consume(self, callback: Callable[[TradeSignal, str], None]):
        self._on_consume = callback

    @property
    def last_timestamp(self) -> float:
        return self._last_ts

    @last_timestamp.setter
    def last_timestamp(self, value: float):
        self._last_ts = value

    def is_paused(self) -> bool:
        return self._manager.is_paused(self.subscriber_id)


class SignalManager:
    _instances: Dict[str, "SignalManager"] = {}

    @classmethod
    def instance(cls, url: str, user_id: str) -> "SignalManager":
        key = f"{url}:{user_id}"
        if key not in cls._instances:
            cls._instances[key] = cls(url, user_id)
        return cls._instances[key]

    @classmethod
    def clear_instances(cls):
        cls._instances.clear()

    def __init__(self, url: str, user_id: str):
        self.url = url
        self.user_id = user_id
        self.signals: List[TradeSignal] = []
        self.paused: List[str] = []
        self.states: Dict[str, Dict] = {}
        self.state_error: Optional[str] = None
        self.events_error: Optional[str] = None
        self._subscriptions: Dict[str, SignalSubscription] = {}

    def subscribe(self, subscriber_id: str) -> SignalSubscription:
        if subscriber_id not in self._subscriptions:
            self._subscriptions[subscriber_id] = SignalSubscription(self, subscriber_id)
        return self._subscriptions[subscriber_id]

    def signals_for(self, trading_pair: str) -> List[TradeSignal]:
        pair_upper = trading_pair.upper()
        return [s for s in self.signals if s.trading_pair.upper() == pair_upper]

    def is_paused(self, controller_name: str) -> bool:
        return controller_name in self.paused

    def market_regime(self, strategy: Optional[str] = None) -> Optional[str]:
        keys = [f"{t}:{strategy}" for t in ("BULLISH", "BEARISH", "NEUTRAL")] if strategy else ["BULLISH", "BEARISH", "NEUTRAL"]
        for key in keys:
            if key in self.states:
                return self.states[key].get("signalType")
        return None

    async def update(self, since: float = 0):
        await asyncio.gather(
            self._fetch_states(),
            self._fetch_events(since),
            return_exceptions=True
        )

    async def _fetch_states(self):
        if not self.user_id:
            self.state_error = "user_id not configured"
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.url}/signals/{self.user_id}/latest", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        self.states = data.get("states", {})
                        self.paused = data.get("pausedStrategies", [])
                        self.state_error = None
                    else:
                        self.state_error = f"HTTP {r.status}"
        except Exception as e:
            self.state_error = str(e)

    async def _fetch_events(self, since: float = 0):
        if not self.user_id:
            self.events_error = "user_id not configured"
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.url}/signals/{self.user_id}/events?since={int(since)}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        self.signals = [
                            TradeSignal(
                                signal_id=e.get("signalId", ""),
                                signal_type=e.get("signalType", "BUY"),
                                timestamp=e.get("timestamp", 0),
                                trading_pair=e.get("tradingPair", ""),
                                source_id=e.get("sourceId", ""),
                                price=e.get("price"),
                                amount=e.get("amount"),
                            )
                            for e in data.get("events", [])
                            if e.get("signalMode") == "event" and e.get("signalType") in ("BUY", "SELL")
                        ]
                        self.events_error = None
                    else:
                        self.events_error = f"HTTP {r.status}"
        except Exception as e:
            self.events_error = str(e)

    def status(self, subscriber_id: Optional[str] = None) -> List[str]:
        sub = self._subscriptions.get(subscriber_id) if subscriber_id else None
        pending = len(sub.pending()) if sub else len(self.signals)
        lines = [
            f"Paused: {self.is_paused(subscriber_id) if subscriber_id else False}",
            f"Signals: {pending} pending / {len(self.signals)} total",
        ]
        if sub:
            lines.append(f"Last Consumed: {sub.last_timestamp}")
        if self.state_error:
            lines.append(f"State Error: {self.state_error}")
        if self.events_error:
            lines.append(f"Events Error: {self.events_error}")
        return lines
 