"""
Bollinger Band Signal Provider

Generates trading signals based on Bollinger Bands position and width.
"""

from decimal import Decimal
from typing import Any, Dict, Optional

from .base import Signal, SignalProvider, SignalType


class BollingerBandSignalProvider(SignalProvider):
    """
    Signal provider based on Bollinger Bands indicators.

    Signal Logic:
    - BUY: Price below lower band (oversold)
    - SELL: Price above upper band (overbought)
    - Signal strength based on how far price is from bands
    """

    def __init__(
        self,
        name: str = "bollinger_bands",
        weight: float = 1.0,
        enabled: bool = True,
        # BB parameters
        bb_length: int = 20,
        bb_std: float = 2.0,
        # Signal thresholds
        bbp_oversold: float = 0.0,      # BBP below this = BUY signal
        bbp_overbought: float = 1.0,     # BBP above this = SELL signal
        # Trend filters
        bb_weak_threshold: float = 0.04,   # BB width threshold for weak trend
        bb_strong_threshold: float = 0.08, # BB width threshold for strong trend
        # Signal confirmation
        require_rsi_confirmation: bool = False,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
    ):
        super().__init__(name, weight, enabled)

        self.bb_length = bb_length
        self.bb_std = bb_std
        self.bbp_oversold = bbp_oversold
        self.bbp_overbought = bbp_overbought
        self.bb_weak_threshold = bb_weak_threshold
        self.bb_strong_threshold = bb_strong_threshold
        self.require_rsi_confirmation = require_rsi_confirmation
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought

    async def start(self):
        """No setup needed for BB indicator-based signals"""
        pass

    async def stop(self):
        """No cleanup needed"""
        pass

    async def get_signal(self, market_data: Dict[str, Any]) -> Optional[Signal]:
        """
        Generate signal based on Bollinger Bands position.

        Expected market_data keys:
        - 'bbp': Bollinger Band Percentage (0-1 scale)
        - 'bbu': Upper Bollinger Band
        - 'bbl': Lower Bollinger Band
        - 'close': Current close price
        - 'rsi': RSI value (optional, for confirmation)
        """
        try:
            bbp = market_data.get("bbp")
            bbu = market_data.get("bbu")
            bbl = market_data.get("bbl")
            close = market_data.get("close")
            rsi = market_data.get("rsi")

            if bbp is None or bbu is None or bbl is None:
                return Signal(
                    signal_type=SignalType.HOLD,
                    strength=0.0,
                    source=self.name,
                    metadata={"reason": "missing_bb_data"}
                )

            # Calculate BB width for trend strength assessment
            bb_width = (bbu - bbl) / bbu if bbu != 0 else 0

            # Determine trend strength
            if bb_width < self.bb_weak_threshold:
                trend_strength = "weak"
                strength_multiplier = 0.5
            elif bb_width > self.bb_strong_threshold:
                trend_strength = "strong"
                strength_multiplier = 1.0
            else:
                trend_strength = "normal"
                strength_multiplier = 0.75

            # Generate signal based on BBP
            signal_type = SignalType.HOLD
            strength = 0.0
            entry_price = None

            if bbp < self.bbp_oversold:
                # Potential BUY signal - price below lower band
                signal_type = SignalType.BUY
                # Strength based on how far below the lower band
                strength = min(1.0, (self.bbp_oversold - bbp) * 2) * strength_multiplier
                entry_price = Decimal(str(bbl)) if bbl else None

                # RSI confirmation if required
                if self.require_rsi_confirmation and rsi is not None:
                    if rsi > self.rsi_oversold:
                        # RSI not confirming oversold - reduce strength
                        strength *= 0.5

            elif bbp > self.bbp_overbought:
                # Potential SELL signal - price above upper band
                signal_type = SignalType.SELL
                # Strength based on how far above the upper band
                strength = min(1.0, (bbp - self.bbp_overbought) * 2) * strength_multiplier
                entry_price = Decimal(str(bbu)) if bbu else None

                # RSI confirmation if required
                if self.require_rsi_confirmation and rsi is not None:
                    if rsi < self.rsi_overbought:
                        # RSI not confirming overbought - reduce strength
                        strength *= 0.5

            signal = Signal(
                signal_type=signal_type,
                strength=strength,
                source=self.name,
                entry_price=entry_price,
                metadata={
                    "bbp": bbp,
                    "bbu": bbu,
                    "bbl": bbl,
                    "bb_width": bb_width,
                    "trend_strength": trend_strength,
                    "rsi": rsi,
                    "close": close
                }
            )

            self._last_signal = signal
            return signal

        except Exception as e:
            return Signal(
                signal_type=SignalType.HOLD,
                strength=0.0,
                source=self.name,
                metadata={"error": str(e)}
            )
