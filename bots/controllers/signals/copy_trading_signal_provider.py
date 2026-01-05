"""
Copy Trading Signal Provider

Monitors on-chain wallet addresses and generates signals based on their trades.
Supports multiple chains: Ethereum, Solana, Hyperliquid, etc.
"""

import asyncio
import logging
import time
from abc import abstractmethod
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from .base import Signal, SignalProvider, SignalType

logger = logging.getLogger(__name__)


class CopyTradingSignalProvider(SignalProvider):
    """
    Base class for copy trading signal providers.

    Monitors specified wallet addresses and generates signals when they trade.

    Configuration:
    - watched_addresses: List of wallet addresses to monitor
    - min_trade_size_usd: Minimum trade size to generate signal
    - copy_ratio: Position size ratio relative to the copied trader
    - chains: Which chains to monitor
    """

    def __init__(
        self,
        name: str = "copy_trading",
        weight: float = 1.0,
        enabled: bool = True,
        # Wallets to copy
        watched_addresses: Optional[List[str]] = None,
        # Filters
        min_trade_size_usd: float = 1000.0,
        target_tokens: Optional[List[str]] = None,  # Filter for specific tokens
        # Copy parameters
        copy_ratio: float = 1.0,  # 1.0 = same size, 0.5 = half size
        signal_ttl: float = 60,  # Signals expire quickly for copy trading
        # Polling
        poll_interval: float = 2.0,
    ):
        super().__init__(name, weight, enabled)

        self.watched_addresses: Set[str] = set(watched_addresses or [])
        self.min_trade_size_usd = min_trade_size_usd
        self.target_tokens = set(t.upper() for t in target_tokens) if target_tokens else None
        self.copy_ratio = copy_ratio
        self.signal_ttl = signal_ttl
        self.poll_interval = poll_interval

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._pending_signals: List[Signal] = []
        self._processed_tx_hashes: Set[str] = set()
        self._max_processed_cache = 10000

    async def start(self):
        """Start monitoring wallet addresses"""
        if not self.watched_addresses:
            logger.warning(f"Copy trading provider {self.name}: No addresses configured")
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Copy trading provider started: {self.name}, watching {len(self.watched_addresses)} addresses")

    async def stop(self):
        """Stop monitoring"""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Copy trading provider stopped: {self.name}")

    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                await self._check_for_new_trades()
            except Exception as e:
                logger.error(f"Error in copy trading monitor: {e}")

            await asyncio.sleep(self.poll_interval)

    @abstractmethod
    async def _check_for_new_trades(self):
        """
        Check for new trades from watched addresses.
        Implement this for specific chain/protocol.
        """
        pass

    @abstractmethod
    async def _fetch_recent_transactions(self, address: str) -> List[Dict[str, Any]]:
        """
        Fetch recent transactions for an address.
        Returns list of transaction data.
        """
        pass

    def _process_transaction(self, tx: Dict[str, Any]) -> Optional[Signal]:
        """
        Process a transaction and generate a signal if applicable.

        Expected tx format:
        {
            "hash": "tx_hash",
            "from": "address",
            "action": "buy" | "sell" | "swap",
            "token_in": "USDC",
            "token_out": "ETH",
            "amount_in": 1000.0,
            "amount_out": 0.5,
            "usd_value": 1000.0,
            "price": 2000.0,
            "timestamp": 1234567890
        }
        """
        tx_hash = tx.get("hash", "")

        # Skip already processed transactions
        if tx_hash in self._processed_tx_hashes:
            return None

        self._processed_tx_hashes.add(tx_hash)

        # Limit cache size
        if len(self._processed_tx_hashes) > self._max_processed_cache:
            # Remove oldest entries (convert to list, slice, convert back)
            self._processed_tx_hashes = set(list(self._processed_tx_hashes)[-5000:])

        # Check minimum trade size
        usd_value = tx.get("usd_value", 0)
        if usd_value < self.min_trade_size_usd:
            return None

        # Determine action and relevant token
        action = tx.get("action", "").lower()
        token_in = tx.get("token_in", "").upper()
        token_out = tx.get("token_out", "").upper()

        # Determine signal type and target token
        if action in ["buy", "long"]:
            signal_type = SignalType.BUY
            relevant_token = token_out
        elif action in ["sell", "short"]:
            signal_type = SignalType.SELL
            relevant_token = token_in
        elif action == "swap":
            # For swaps, determine direction based on stable coin
            stables = {"USDC", "USDT", "DAI", "BUSD", "UST"}
            if token_in in stables:
                signal_type = SignalType.BUY
                relevant_token = token_out
            elif token_out in stables:
                signal_type = SignalType.SELL
                relevant_token = token_in
            else:
                # Non-stable to non-stable swap, skip
                return None
        else:
            return None

        # Filter by target tokens
        if self.target_tokens and relevant_token not in self.target_tokens:
            return None

        # Calculate copied position size
        copied_size = usd_value * self.copy_ratio

        # Create signal
        return Signal(
            signal_type=signal_type,
            strength=min(1.0, usd_value / 10000),  # Scale strength by trade size
            source=self.name,
            entry_price=Decimal(str(tx.get("price", 0))) if tx.get("price") else None,
            ttl=self.signal_ttl,
            metadata={
                "tx_hash": tx_hash,
                "copied_address": tx.get("from"),
                "original_size_usd": usd_value,
                "copied_size_usd": copied_size,
                "token": relevant_token,
                "token_in": token_in,
                "token_out": token_out,
                "action": action,
                "timestamp": tx.get("timestamp", time.time())
            }
        )

    async def get_signal(self, market_data: Dict[str, Any]) -> Optional[Signal]:
        """Get the latest copy trading signal"""
        # Clean up expired signals
        self._pending_signals = [s for s in self._pending_signals if not s.is_expired]

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

    def add_watched_address(self, address: str):
        """Add a wallet address to monitor"""
        self.watched_addresses.add(address)
        logger.info(f"Added watched address: {address}")

    def remove_watched_address(self, address: str):
        """Remove a wallet address from monitoring"""
        self.watched_addresses.discard(address)
        logger.info(f"Removed watched address: {address}")


class HyperliquidCopyTradingProvider(CopyTradingSignalProvider):
    """
    Copy trading provider for Hyperliquid DEX.

    Monitors specified addresses' positions and trades on Hyperliquid.
    """

    def __init__(
        self,
        name: str = "hyperliquid_copy",
        api_url: str = "https://api.hyperliquid.xyz",
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self.api_url = api_url
        self._last_positions: Dict[str, Dict[str, Any]] = {}

    async def _check_for_new_trades(self):
        """Check for position changes on Hyperliquid"""
        for address in self.watched_addresses:
            try:
                current_positions = await self._fetch_positions(address)
                previous_positions = self._last_positions.get(address, {})

                # Detect position changes
                for symbol, position in current_positions.items():
                    prev_position = previous_positions.get(symbol, {})
                    signal = self._detect_position_change(address, symbol, prev_position, position)
                    if signal:
                        self._pending_signals.append(signal)
                        self.emit_signal(signal)

                self._last_positions[address] = current_positions

            except Exception as e:
                logger.error(f"Error checking Hyperliquid positions for {address}: {e}")

    async def _fetch_positions(self, address: str) -> Dict[str, Dict[str, Any]]:
        """Fetch current positions for an address"""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                payload = {
                    "type": "clearinghouseState",
                    "user": address
                }
                async with session.post(f"{self.api_url}/info", json=payload) as response:
                    if response.status != 200:
                        return {}

                    data = await response.json()
                    positions = {}

                    for pos in data.get("assetPositions", []):
                        position_data = pos.get("position", {})
                        coin = position_data.get("coin", "")
                        if coin:
                            positions[coin] = {
                                "size": float(position_data.get("szi", 0)),
                                "entry_price": float(position_data.get("entryPx", 0)),
                                "unrealized_pnl": float(position_data.get("unrealizedPnl", 0)),
                                "leverage": float(position_data.get("leverage", {}).get("value", 1))
                            }

                    return positions

        except ImportError:
            logger.error("aiohttp not installed")
            return {}
        except Exception as e:
            logger.error(f"Error fetching Hyperliquid positions: {e}")
            return {}

    async def _fetch_recent_transactions(self, address: str) -> List[Dict[str, Any]]:
        """Fetch recent fills for an address"""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                payload = {
                    "type": "userFills",
                    "user": address
                }
                async with session.post(f"{self.api_url}/info", json=payload) as response:
                    if response.status != 200:
                        return []
                    return await response.json()

        except Exception as e:
            logger.error(f"Error fetching Hyperliquid transactions: {e}")
            return []

    def _detect_position_change(
        self,
        address: str,
        symbol: str,
        prev: Dict[str, Any],
        current: Dict[str, Any]
    ) -> Optional[Signal]:
        """Detect if a position has changed and generate signal"""
        prev_size = prev.get("size", 0)
        current_size = current.get("size", 0)

        if prev_size == current_size:
            return None

        size_change = current_size - prev_size
        entry_price = current.get("entry_price", 0)

        # Determine signal type
        if size_change > 0:
            # Position increased (or opened long)
            if current_size > 0:
                signal_type = SignalType.BUY
            else:
                signal_type = SignalType.CLOSE_SHORT  # Closing short
        else:
            # Position decreased (or opened short)
            if current_size < 0:
                signal_type = SignalType.SELL
            else:
                signal_type = SignalType.CLOSE_LONG  # Closing long

        # Estimate USD value
        usd_value = abs(size_change) * entry_price

        # Check minimum size
        if usd_value < self.min_trade_size_usd:
            return None

        return Signal(
            signal_type=signal_type,
            strength=min(1.0, usd_value / 50000),
            source=self.name,
            entry_price=Decimal(str(entry_price)) if entry_price else None,
            ttl=self.signal_ttl,
            metadata={
                "copied_address": address,
                "symbol": symbol,
                "size_change": size_change,
                "new_size": current_size,
                "entry_price": entry_price,
                "usd_value": usd_value,
                "leverage": current.get("leverage", 1)
            }
        )


class SolanaCopyTradingProvider(CopyTradingSignalProvider):
    """
    Copy trading provider for Solana.

    Monitors wallet addresses for DEX swaps on Jupiter, Raydium, etc.
    """

    def __init__(
        self,
        name: str = "solana_copy",
        helius_api_key: Optional[str] = None,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self.helius_api_key = helius_api_key
        self.rpc_url = rpc_url
        self._last_signatures: Dict[str, str] = {}

    async def _check_for_new_trades(self):
        """Check for new transactions on Solana"""
        for address in self.watched_addresses:
            try:
                transactions = await self._fetch_recent_transactions(address)

                for tx in transactions:
                    signal = self._process_transaction(tx)
                    if signal:
                        self._pending_signals.append(signal)
                        self.emit_signal(signal)

            except Exception as e:
                logger.error(f"Error checking Solana transactions for {address}: {e}")

    async def _fetch_recent_transactions(self, address: str) -> List[Dict[str, Any]]:
        """
        Fetch recent transactions for a Solana address.

        Uses Helius enhanced API if available, otherwise falls back to RPC.
        """
        if self.helius_api_key:
            return await self._fetch_via_helius(address)
        else:
            return await self._fetch_via_rpc(address)

    async def _fetch_via_helius(self, address: str) -> List[Dict[str, Any]]:
        """Fetch parsed transactions via Helius API"""
        try:
            import aiohttp

            url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
            params = {
                "api-key": self.helius_api_key,
                "type": "SWAP",
                "limit": 10
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        return []

                    data = await response.json()
                    transactions = []

                    for tx in data:
                        # Parse Helius enhanced transaction format
                        if tx.get("type") == "SWAP":
                            swap_info = tx.get("tokenTransfers", [])
                            if len(swap_info) >= 2:
                                transactions.append({
                                    "hash": tx.get("signature"),
                                    "from": address,
                                    "action": "swap",
                                    "token_in": swap_info[0].get("tokenSymbol", ""),
                                    "token_out": swap_info[1].get("tokenSymbol", ""),
                                    "amount_in": swap_info[0].get("tokenAmount", 0),
                                    "amount_out": swap_info[1].get("tokenAmount", 0),
                                    "usd_value": abs(swap_info[0].get("tokenAmount", 0) *
                                                     swap_info[0].get("price", 0)),
                                    "timestamp": tx.get("timestamp", time.time())
                                })

                    return transactions

        except Exception as e:
            logger.error(f"Error fetching from Helius: {e}")
            return []

    async def _fetch_via_rpc(self, address: str) -> List[Dict[str, Any]]:
        """Fetch transactions via standard Solana RPC (limited parsing)"""
        # Basic RPC implementation - requires additional parsing
        logger.warning("Using basic RPC - consider adding Helius API key for better parsing")
        return []


class WebhookCopyTradingProvider(CopyTradingSignalProvider):
    """
    Copy trading provider that receives signals via webhook.

    Useful for integrating with external services or custom monitoring solutions.
    """

    def __init__(
        self,
        name: str = "webhook_copy",
        webhook_secret: Optional[str] = None,
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self.webhook_secret = webhook_secret

    async def _check_for_new_trades(self):
        """Webhook-based provider doesn't poll - signals come via push"""
        pass

    async def _fetch_recent_transactions(self, address: str) -> List[Dict[str, Any]]:
        """Not used for webhook provider"""
        return []

    def receive_webhook(self, data: Dict[str, Any], signature: Optional[str] = None) -> bool:
        """
        Receive a webhook notification.

        Expected data format:
        {
            "address": "wallet_address",
            "action": "buy" | "sell",
            "token": "BTC",
            "price": 50000,
            "amount": 0.1,
            "usd_value": 5000
        }

        Returns True if signal was processed successfully.
        """
        # Verify signature if secret is configured
        if self.webhook_secret and signature:
            if not self._verify_signature(data, signature):
                logger.warning("Invalid webhook signature")
                return False

        # Check if address is watched
        address = data.get("address", "")
        if self.watched_addresses and address not in self.watched_addresses:
            logger.debug(f"Ignoring webhook from unwatched address: {address}")
            return False

        # Create transaction format
        tx = {
            "hash": data.get("tx_hash", f"webhook_{time.time()}"),
            "from": address,
            "action": data.get("action", ""),
            "token_in": data.get("token_in", data.get("token", "")),
            "token_out": data.get("token_out", data.get("token", "")),
            "usd_value": data.get("usd_value", 0),
            "price": data.get("price", 0),
            "timestamp": data.get("timestamp", time.time())
        }

        signal = self._process_transaction(tx)
        if signal:
            self._pending_signals.append(signal)
            self.emit_signal(signal)
            return True

        return False

    def _verify_signature(self, data: Dict[str, Any], signature: str) -> bool:
        """Verify webhook signature"""
        import hmac
        import hashlib
        import json

        if not self.webhook_secret:
            return True

        payload = json.dumps(data, sort_keys=True)
        expected = hmac.new(
            self.webhook_secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)
