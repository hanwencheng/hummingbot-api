"""
Base Signal Provider Interface

This module defines the abstract interface for all signal providers.
Signal providers can be:
- Technical indicators (Bollinger Bands, RSI, MACD)
- External sources (Telegram bot, Discord, webhooks)
- Copy trading (on-chain wallet monitoring)
- ML/AI models
- Custom algorithms
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Callable
import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Signal types that can be emitted by providers"""
    BUY = 1
    SELL = -1
    HOLD = 0
    CLOSE_LONG = 2      # Close existing long position
    CLOSE_SHORT = -2    # Close existing short position
    CLOSE_ALL = 3       # Close all positions


@dataclass
class Signal:
    """
    Represents a trading signal from any source.

    Attributes:
        signal_type: The type of signal (BUY, SELL, HOLD, etc.)
        strength: Signal strength/confidence from 0.0 to 1.0
        source: Name of the signal provider
        timestamp: Unix timestamp when signal was generated
        entry_price: Suggested entry price (optional)
        stop_loss: Suggested stop loss price (optional)
        take_profit: Suggested take profit price (optional)
        metadata: Additional provider-specific data
        ttl: Time-to-live in seconds (signal expires after this)
    """
    signal_type: SignalType
    strength: float = 1.0  # 0.0 to 1.0 confidence
    source: str = "unknown"
    timestamp: float = field(default_factory=time.time)
    entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    ttl: float = 300  # Default 5 minutes TTL

    @property
    def is_expired(self) -> bool:
        """Check if signal has expired based on TTL"""
        return time.time() > (self.timestamp + self.ttl)

    @property
    def is_buy(self) -> bool:
        return self.signal_type == SignalType.BUY

    @property
    def is_sell(self) -> bool:
        return self.signal_type == SignalType.SELL

    @property
    def is_hold(self) -> bool:
        return self.signal_type == SignalType.HOLD

    @property
    def direction(self) -> int:
        """Returns 1 for buy, -1 for sell, 0 for hold/close"""
        if self.signal_type == SignalType.BUY:
            return 1
        elif self.signal_type == SignalType.SELL:
            return -1
        return 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert signal to dictionary for logging/storage"""
        return {
            "signal_type": self.signal_type.name,
            "strength": self.strength,
            "source": self.source,
            "timestamp": self.timestamp,
            "entry_price": str(self.entry_price) if self.entry_price else None,
            "stop_loss": str(self.stop_loss) if self.stop_loss else None,
            "take_profit": str(self.take_profit) if self.take_profit else None,
            "metadata": self.metadata,
            "ttl": self.ttl,
            "is_expired": self.is_expired
        }


class SignalProvider(ABC):
    """
    Abstract base class for all signal providers.

    Implement this class to create custom signal sources:
    - TelegramSignalProvider
    - CopyTradingSignalProvider
    - WebhookSignalProvider
    - etc.
    """

    def __init__(self, name: str, weight: float = 1.0, enabled: bool = True):
        """
        Initialize signal provider.

        Args:
            name: Unique identifier for this provider
            weight: Weight for signal aggregation (0.0 to 1.0)
            enabled: Whether this provider is active
        """
        self.name = name
        self.weight = weight
        self.enabled = enabled
        self._last_signal: Optional[Signal] = None
        self._callbacks: List[Callable[[Signal], None]] = []

    @abstractmethod
    async def get_signal(self, market_data: Dict[str, Any]) -> Optional[Signal]:
        """
        Get the current signal from this provider.

        Args:
            market_data: Dictionary containing market data (prices, indicators, etc.)
                Expected keys may include:
                - 'current_price': Current market price
                - 'features': DataFrame with OHLCV and indicators
                - 'connector_name': Exchange connector name
                - 'trading_pair': Trading pair
                - Any other data the provider needs

        Returns:
            Signal object or None if no signal
        """
        pass

    @abstractmethod
    async def start(self):
        """Start the signal provider (connect to APIs, start listeners, etc.)"""
        pass

    @abstractmethod
    async def stop(self):
        """Stop the signal provider (disconnect, cleanup resources)"""
        pass

    def on_signal(self, callback: Callable[[Signal], None]):
        """
        Register a callback to be called when a new signal is received.
        Useful for push-based signal providers (websockets, message queues).
        """
        self._callbacks.append(callback)

    def emit_signal(self, signal: Signal):
        """Emit a signal to all registered callbacks"""
        self._last_signal = signal
        for callback in self._callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error(f"Error in signal callback for {self.name}: {e}")

    @property
    def last_signal(self) -> Optional[Signal]:
        """Get the most recent signal from this provider"""
        return self._last_signal

    def __repr__(self):
        return f"{self.__class__.__name__}(name={self.name}, weight={self.weight}, enabled={self.enabled})"


class SignalAggregator:
    """
    Aggregates signals from multiple providers and produces a combined signal.

    Supports multiple aggregation strategies:
    - WEIGHTED_AVERAGE: Combine signals based on provider weights
    - UNANIMOUS: All providers must agree
    - MAJORITY: Majority vote
    - PRIORITY: Use highest-priority signal
    - FIRST_VALID: Use first non-HOLD signal
    """

    class AggregationMode(Enum):
        WEIGHTED_AVERAGE = "weighted_average"
        UNANIMOUS = "unanimous"
        MAJORITY = "majority"
        PRIORITY = "priority"
        FIRST_VALID = "first_valid"

    def __init__(self, mode: AggregationMode = AggregationMode.PRIORITY):
        self.mode = mode
        self._providers: Dict[str, SignalProvider] = {}
        self._provider_priority: List[str] = []  # Ordered by priority
        self._last_aggregated_signal: Optional[Signal] = None
        self._signal_history: List[Signal] = []
        self._max_history = 100

    def add_provider(self, provider: SignalProvider, priority: int = 0):
        """
        Add a signal provider to the aggregator.

        Args:
            provider: The SignalProvider instance
            priority: Priority for PRIORITY mode (higher = more important)
        """
        self._providers[provider.name] = provider

        # Insert into priority list at correct position
        self._provider_priority.append(provider.name)
        # Sort by priority (we'll store priority separately if needed)

        logger.info(f"Added signal provider: {provider.name} (weight={provider.weight})")

    def remove_provider(self, name: str):
        """Remove a signal provider by name"""
        if name in self._providers:
            del self._providers[name]
            self._provider_priority.remove(name)
            logger.info(f"Removed signal provider: {name}")

    def get_provider(self, name: str) -> Optional[SignalProvider]:
        """Get a provider by name"""
        return self._providers.get(name)

    @property
    def providers(self) -> List[SignalProvider]:
        """Get all registered providers"""
        return list(self._providers.values())

    async def start_all(self):
        """Start all providers"""
        for provider in self._providers.values():
            if provider.enabled:
                try:
                    await provider.start()
                    logger.info(f"Started signal provider: {provider.name}")
                except Exception as e:
                    logger.error(f"Failed to start provider {provider.name}: {e}")

    async def stop_all(self):
        """Stop all providers"""
        for provider in self._providers.values():
            try:
                await provider.stop()
                logger.info(f"Stopped signal provider: {provider.name}")
            except Exception as e:
                logger.error(f"Failed to stop provider {provider.name}: {e}")

    async def get_aggregated_signal(self, market_data: Dict[str, Any]) -> Signal:
        """
        Get aggregated signal from all providers.

        Args:
            market_data: Market data to pass to providers

        Returns:
            Aggregated Signal object
        """
        # Collect signals from all enabled providers
        signals: List[Signal] = []

        for provider in self._providers.values():
            if not provider.enabled:
                continue

            try:
                signal = await provider.get_signal(market_data)
                if signal and not signal.is_expired:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error getting signal from {provider.name}: {e}")

        if not signals:
            return Signal(
                signal_type=SignalType.HOLD,
                strength=0.0,
                source="aggregator",
                metadata={"reason": "no_signals"}
            )

        # Aggregate based on mode
        aggregated = self._aggregate_signals(signals)

        # Store in history
        self._last_aggregated_signal = aggregated
        self._signal_history.append(aggregated)
        if len(self._signal_history) > self._max_history:
            self._signal_history.pop(0)

        return aggregated

    def _aggregate_signals(self, signals: List[Signal]) -> Signal:
        """Aggregate signals based on the configured mode"""

        if self.mode == self.AggregationMode.PRIORITY:
            return self._aggregate_priority(signals)
        elif self.mode == self.AggregationMode.WEIGHTED_AVERAGE:
            return self._aggregate_weighted(signals)
        elif self.mode == self.AggregationMode.UNANIMOUS:
            return self._aggregate_unanimous(signals)
        elif self.mode == self.AggregationMode.MAJORITY:
            return self._aggregate_majority(signals)
        elif self.mode == self.AggregationMode.FIRST_VALID:
            return self._aggregate_first_valid(signals)
        else:
            return signals[0] if signals else Signal(signal_type=SignalType.HOLD, source="aggregator")

    def _aggregate_priority(self, signals: List[Signal]) -> Signal:
        """Use the signal from the highest priority provider"""
        for provider_name in self._provider_priority:
            for signal in signals:
                if signal.source == provider_name and not signal.is_hold:
                    return Signal(
                        signal_type=signal.signal_type,
                        strength=signal.strength,
                        source=f"aggregator({signal.source})",
                        entry_price=signal.entry_price,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        metadata={
                            "primary_source": signal.source,
                            "all_signals": [s.to_dict() for s in signals]
                        }
                    )

        # If all signals are HOLD, return HOLD
        return Signal(
            signal_type=SignalType.HOLD,
            strength=0.0,
            source="aggregator",
            metadata={"all_signals": [s.to_dict() for s in signals]}
        )

    def _aggregate_weighted(self, signals: List[Signal]) -> Signal:
        """Combine signals using weighted average"""
        total_weight = 0.0
        weighted_direction = 0.0
        weighted_strength = 0.0

        for signal in signals:
            provider = self._providers.get(signal.source)
            weight = provider.weight if provider else 1.0

            total_weight += weight
            weighted_direction += signal.direction * signal.strength * weight
            weighted_strength += signal.strength * weight

        if total_weight == 0:
            return Signal(signal_type=SignalType.HOLD, source="aggregator")

        avg_direction = weighted_direction / total_weight
        avg_strength = weighted_strength / total_weight

        # Determine signal type from weighted direction
        if avg_direction > 0.3:
            signal_type = SignalType.BUY
        elif avg_direction < -0.3:
            signal_type = SignalType.SELL
        else:
            signal_type = SignalType.HOLD

        return Signal(
            signal_type=signal_type,
            strength=min(abs(avg_direction), 1.0),
            source="aggregator(weighted)",
            metadata={
                "weighted_direction": avg_direction,
                "all_signals": [s.to_dict() for s in signals]
            }
        )

    def _aggregate_unanimous(self, signals: List[Signal]) -> Signal:
        """All providers must agree for a non-HOLD signal"""
        directions = [s.direction for s in signals if not s.is_hold]

        if not directions:
            return Signal(signal_type=SignalType.HOLD, source="aggregator(unanimous)")

        if all(d > 0 for d in directions):
            avg_strength = sum(s.strength for s in signals) / len(signals)
            return Signal(
                signal_type=SignalType.BUY,
                strength=avg_strength,
                source="aggregator(unanimous)",
                metadata={"all_signals": [s.to_dict() for s in signals]}
            )
        elif all(d < 0 for d in directions):
            avg_strength = sum(s.strength for s in signals) / len(signals)
            return Signal(
                signal_type=SignalType.SELL,
                strength=avg_strength,
                source="aggregator(unanimous)",
                metadata={"all_signals": [s.to_dict() for s in signals]}
            )

        return Signal(
            signal_type=SignalType.HOLD,
            source="aggregator(unanimous)",
            metadata={"reason": "no_consensus", "all_signals": [s.to_dict() for s in signals]}
        )

    def _aggregate_majority(self, signals: List[Signal]) -> Signal:
        """Majority vote among providers"""
        buy_count = sum(1 for s in signals if s.is_buy)
        sell_count = sum(1 for s in signals if s.is_sell)

        total = len(signals)

        if buy_count > total / 2:
            buy_signals = [s for s in signals if s.is_buy]
            avg_strength = sum(s.strength for s in buy_signals) / len(buy_signals)
            return Signal(
                signal_type=SignalType.BUY,
                strength=avg_strength,
                source="aggregator(majority)",
                metadata={"votes": {"buy": buy_count, "sell": sell_count, "hold": total - buy_count - sell_count}}
            )
        elif sell_count > total / 2:
            sell_signals = [s for s in signals if s.is_sell]
            avg_strength = sum(s.strength for s in sell_signals) / len(sell_signals)
            return Signal(
                signal_type=SignalType.SELL,
                strength=avg_strength,
                source="aggregator(majority)",
                metadata={"votes": {"buy": buy_count, "sell": sell_count, "hold": total - buy_count - sell_count}}
            )

        return Signal(
            signal_type=SignalType.HOLD,
            source="aggregator(majority)",
            metadata={"votes": {"buy": buy_count, "sell": sell_count, "hold": total - buy_count - sell_count}}
        )

    def _aggregate_first_valid(self, signals: List[Signal]) -> Signal:
        """Return first non-HOLD signal based on priority order"""
        for provider_name in self._provider_priority:
            for signal in signals:
                if signal.source == provider_name and not signal.is_hold:
                    return signal

        return Signal(signal_type=SignalType.HOLD, source="aggregator(first_valid)")

    @property
    def last_signal(self) -> Optional[Signal]:
        """Get the last aggregated signal"""
        return self._last_aggregated_signal

    @property
    def signal_history(self) -> List[Signal]:
        """Get signal history"""
        return self._signal_history.copy()
