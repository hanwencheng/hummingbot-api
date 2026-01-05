"""
Telegram Signal Provider

Receives trading signals from a Telegram bot.
Supports both:
1. Polling mode: Check for new messages periodically
2. Push mode: Webhook-based real-time signals (requires external server)
"""

import asyncio
import logging
import re
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from .base import Signal, SignalProvider, SignalType

logger = logging.getLogger(__name__)


class TelegramSignalProvider(SignalProvider):
    """
    Signal provider that receives signals from Telegram.

    Supports multiple signal formats:
    1. Simple: "BUY BTC" or "SELL ETH"
    2. With price: "BUY BTC @ 50000"
    3. With TP/SL: "BUY BTC @ 50000 TP: 52000 SL: 49000"
    4. JSON format: {"action": "BUY", "pair": "BTC-USDT", "price": 50000}

    Configuration:
    - bot_token: Telegram bot token
    - chat_ids: List of authorized chat IDs to receive signals from
    - signal_patterns: Custom regex patterns for parsing signals
    """

    # Default signal parsing patterns
    DEFAULT_PATTERNS = {
        "simple": r"(?P<action>BUY|SELL|LONG|SHORT|CLOSE)\s+(?P<pair>[A-Z0-9\-\/]+)",
        "with_price": r"(?P<action>BUY|SELL|LONG|SHORT)\s+(?P<pair>[A-Z0-9\-\/]+)\s*@\s*(?P<price>[\d.]+)",
        "with_tp_sl": r"(?P<action>BUY|SELL|LONG|SHORT)\s+(?P<pair>[A-Z0-9\-\/]+)\s*@\s*(?P<price>[\d.]+)\s*TP:\s*(?P<tp>[\d.]+)\s*SL:\s*(?P<sl>[\d.]+)",
    }

    def __init__(
        self,
        name: str = "telegram",
        weight: float = 1.0,
        enabled: bool = True,
        # Telegram config
        bot_token: Optional[str] = None,
        chat_ids: Optional[List[int]] = None,
        # Signal parsing
        target_pair: Optional[str] = None,  # Filter for specific trading pair
        custom_patterns: Optional[Dict[str, str]] = None,
        # Polling config
        poll_interval: float = 5.0,  # Seconds between polls
        signal_ttl: float = 300,  # Signal TTL in seconds
    ):
        super().__init__(name, weight, enabled)

        self.bot_token = bot_token
        self.chat_ids = set(chat_ids) if chat_ids else set()
        self.target_pair = target_pair
        self.patterns = {**self.DEFAULT_PATTERNS, **(custom_patterns or {})}
        self.poll_interval = poll_interval
        self.signal_ttl = signal_ttl

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._pending_signals: List[Signal] = []
        self._last_update_id = 0

        # Compile regex patterns
        self._compiled_patterns = {
            name: re.compile(pattern, re.IGNORECASE)
            for name, pattern in self.patterns.items()
        }

    async def start(self):
        """Start polling for Telegram messages"""
        if not self.bot_token:
            logger.warning(f"Telegram provider {self.name}: No bot token configured")
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"Telegram signal provider started: {self.name}")

    async def stop(self):
        """Stop polling"""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Telegram signal provider stopped: {self.name}")

    async def _poll_loop(self):
        """Main polling loop for Telegram updates"""
        while self._running:
            try:
                await self._fetch_updates()
            except Exception as e:
                logger.error(f"Error polling Telegram: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _fetch_updates(self):
        """Fetch new messages from Telegram API"""
        try:
            import aiohttp

            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            params = {
                "offset": self._last_update_id + 1,
                "timeout": 30,
                "allowed_updates": ["message"]
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=35) as response:
                    if response.status != 200:
                        logger.error(f"Telegram API error: {response.status}")
                        return

                    data = await response.json()

                    if not data.get("ok"):
                        logger.error(f"Telegram API returned error: {data}")
                        return

                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"]
                        await self._process_update(update)

        except ImportError:
            logger.error("aiohttp not installed. Install with: pip install aiohttp")
        except Exception as e:
            logger.error(f"Error fetching Telegram updates: {e}")

    async def _process_update(self, update: Dict[str, Any]):
        """Process a single Telegram update"""
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")

        # Check if from authorized chat
        if self.chat_ids and chat_id not in self.chat_ids:
            logger.debug(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        # Parse the message
        signal = self._parse_signal_message(text)
        if signal:
            self._pending_signals.append(signal)
            self.emit_signal(signal)
            logger.info(f"Received Telegram signal: {signal.signal_type.name} from chat {chat_id}")

    def _parse_signal_message(self, text: str) -> Optional[Signal]:
        """Parse a text message into a Signal object"""
        if not text:
            return None

        # Try JSON format first
        try:
            import json
            data = json.loads(text)
            return self._parse_json_signal(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex patterns (most specific first)
        for pattern_name in ["with_tp_sl", "with_price", "simple"]:
            pattern = self._compiled_patterns.get(pattern_name)
            if pattern:
                match = pattern.search(text)
                if match:
                    return self._parse_regex_match(match, pattern_name)

        return None

    def _parse_json_signal(self, data: Dict[str, Any]) -> Optional[Signal]:
        """Parse JSON-formatted signal"""
        action = data.get("action", "").upper()
        pair = data.get("pair", data.get("trading_pair", ""))

        # Filter by target pair if configured
        if self.target_pair and pair.upper() != self.target_pair.upper():
            return None

        signal_type = self._action_to_signal_type(action)
        if signal_type is None:
            return None

        return Signal(
            signal_type=signal_type,
            strength=float(data.get("strength", data.get("confidence", 1.0))),
            source=self.name,
            entry_price=Decimal(str(data["price"])) if data.get("price") else None,
            take_profit=Decimal(str(data["tp"])) if data.get("tp") else None,
            stop_loss=Decimal(str(data["sl"])) if data.get("sl") else None,
            ttl=self.signal_ttl,
            metadata={
                "format": "json",
                "pair": pair,
                "raw": data
            }
        )

    def _parse_regex_match(self, match: re.Match, pattern_name: str) -> Optional[Signal]:
        """Parse regex match into Signal"""
        groups = match.groupdict()

        action = groups.get("action", "").upper()
        pair = groups.get("pair", "")

        # Filter by target pair if configured
        if self.target_pair and pair.upper() != self.target_pair.upper():
            return None

        signal_type = self._action_to_signal_type(action)
        if signal_type is None:
            return None

        entry_price = Decimal(groups["price"]) if groups.get("price") else None
        take_profit = Decimal(groups["tp"]) if groups.get("tp") else None
        stop_loss = Decimal(groups["sl"]) if groups.get("sl") else None

        return Signal(
            signal_type=signal_type,
            strength=1.0,
            source=self.name,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            ttl=self.signal_ttl,
            metadata={
                "format": pattern_name,
                "pair": pair,
                "raw_match": groups
            }
        )

    def _action_to_signal_type(self, action: str) -> Optional[SignalType]:
        """Convert action string to SignalType"""
        action_map = {
            "BUY": SignalType.BUY,
            "LONG": SignalType.BUY,
            "SELL": SignalType.SELL,
            "SHORT": SignalType.SELL,
            "CLOSE": SignalType.CLOSE_ALL,
            "CLOSE_LONG": SignalType.CLOSE_LONG,
            "CLOSE_SHORT": SignalType.CLOSE_SHORT,
        }
        return action_map.get(action.upper())

    async def get_signal(self, market_data: Dict[str, Any]) -> Optional[Signal]:
        """
        Get the latest pending signal.

        Note: For Telegram, signals come asynchronously. This method
        returns the most recent non-expired signal from the queue.
        """
        # Clean up expired signals
        current_time = time.time()
        self._pending_signals = [
            s for s in self._pending_signals
            if not s.is_expired
        ]

        if not self._pending_signals:
            return Signal(
                signal_type=SignalType.HOLD,
                strength=0.0,
                source=self.name,
                metadata={"reason": "no_pending_signals"}
            )

        # Return and remove the oldest pending signal (FIFO)
        signal = self._pending_signals.pop(0)
        self._last_signal = signal
        return signal

    def add_signal_manually(self, signal: Signal):
        """
        Manually add a signal (useful for testing or webhook integration).

        Example:
            provider.add_signal_manually(Signal(
                signal_type=SignalType.BUY,
                strength=0.8,
                source="telegram",
                entry_price=Decimal("50000")
            ))
        """
        self._pending_signals.append(signal)
        self.emit_signal(signal)

    def add_authorized_chat(self, chat_id: int):
        """Add an authorized chat ID"""
        self.chat_ids.add(chat_id)

    def remove_authorized_chat(self, chat_id: int):
        """Remove an authorized chat ID"""
        self.chat_ids.discard(chat_id)
