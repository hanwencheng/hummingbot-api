# Signal providers for Dynamic BB Grid Controller
from .base import SignalProvider, Signal, SignalType, SignalAggregator
from .bb_signal_provider import BollingerBandSignalProvider
from .telegram_signal_provider import TelegramSignalProvider
from .copy_trading_signal_provider import (
    CopyTradingSignalProvider,
    HyperliquidCopyTradingProvider,
    SolanaCopyTradingProvider,
    WebhookCopyTradingProvider,
)

__all__ = [
    # Base classes
    "SignalProvider",
    "Signal",
    "SignalType",
    "SignalAggregator",
    # Providers
    "BollingerBandSignalProvider",
    "TelegramSignalProvider",
    "CopyTradingSignalProvider",
    "HyperliquidCopyTradingProvider",
    "SolanaCopyTradingProvider",
    "WebhookCopyTradingProvider",
]
