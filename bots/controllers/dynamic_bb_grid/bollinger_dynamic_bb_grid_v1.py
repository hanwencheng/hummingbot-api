"""
Bollinger Bands Dynamic BB-Grid Strategy V1

Advanced Bollinger Bands strategy with dynamic grid management:
- Signal-based entry level adjustment (signal + 2*spread)
- Keep filled levels, update unfilled levels on new signals
- BUY signal when BBP < bb_long_threshold (oversold condition)
- Dynamic level management with no cooldown
- Final profit/stop loss with waiting period after stop loss
"""

from typing import List
import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.models.executors import CloseType
from bots.controllers.dynamic_bb_grid_controller_base import (
    DynamicBBGridControllerBase,
    DynamicBBGridControllerConfigBase
)


class BollingerDynamicBBGridV1Config(DynamicBBGridControllerConfigBase):
    controller_name: str = "bollinger_dynamic_bb_grid_v1"
    candles_config: List[CandlesConfig] = []

    # Candles configuration
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
            "prompt_on_new": True
        }
    )
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
            "prompt_on_new": True
        }
    )
    interval: str = Field(
        default="3m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True
        }
    )

    # Bollinger Bands parameters
    bb_length: int = Field(
        default=20,
        json_schema_extra={
            "prompt": "Enter the Bollinger Bands length: ",
            "prompt_on_new": True
        }
    )
    bb_std: float = Field(
        default=2.0,
        json_schema_extra={
            "prompt": "Enter the Bollinger Bands standard deviation: ",
            "prompt_on_new": True
        }
    )
    bb_long_threshold: float = Field(
        default=0.2,
        json_schema_extra={
            "prompt": "Enter the BBP threshold for BUY signals (e.g., 0.2 for oversold): ",
            "prompt_on_new": True
        }
    )
    bb_short_threshold: float = Field(
        default=0.8,
        json_schema_extra={
            "prompt": "Enter the BBP threshold for SELL signals (e.g., 0.8 for overbought): ",
            "prompt_on_new": True
        }
    )

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v


class BollingerDynamicBBGridV1Controller(DynamicBBGridControllerBase):
    """
    Bollinger Bands Dynamic BB-Grid Strategy Controller.

    Signal Logic:
    - BUY: BBP < bb_long_threshold (price near lower band = oversold)
    - SELL: BBP > bb_short_threshold (price near upper band = overbought)
    - Both signals trigger level creation/updates for perpetual trading

    Dynamic Grid Logic:
    - BUY signal sets new entry level = signal_price + spread_multiplier*spread
    - SELL signal sets new entry level = signal_price - spread_multiplier*spread
    - Unfilled levels update to new entry prices, filled levels remain unchanged
    - All levels filled = wait for profit/stop loss targets
    """

    def __init__(self, config: BollingerDynamicBBGridV1Config, *args, **kwargs):
        self.config = config
        self.max_records = self.config.bb_length + 10  # Extra buffer for BB calculation

        # Set up candles config if not provided
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=config.interval,
                max_records=self.max_records
            )]

        super().__init__(config, *args, **kwargs)

    async def update_processed_data(self):
        """
        Update processed data with Bollinger Bands signal.
        Only generates BUY signals for oversold conditions.
        """
        try:
            # Get candles data
            df = self.market_data_provider.get_candles_df(
                connector_name=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.interval,
                max_records=self.max_records
            )

            if df is None or len(df) < self.config.bb_length:
                # Not enough data for BB calculation
                self.processed_data = {
                    "signal": 0,
                    "features": df if df is not None else pd.DataFrame()
                }
                return

            # Calculate Bollinger Bands indicators
            df.ta.bbands(
                length=self.config.bb_length,
                lower_std=self.config.bb_std,
                upper_std=self.config.bb_std,
                append=True
            )

            # Get BBP (Bollinger Band Position) - shows where price is within bands
            bbp_col = f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"

            if bbp_col not in df.columns:
                # Fallback: look for any BBP column
                bbp_cols = [col for col in df.columns if 'BBP' in col]
                if not bbp_cols:
                    self.logger().error(f"No BBP column found. Available columns: {list(df.columns)}")
                    self.processed_data = {"signal": 0, "features": df}
                    return
                bbp_col = bbp_cols[0]
                self.logger().warning(f"Using fallback BBP column: {bbp_col}")

            bbp = df[bbp_col].iloc[-1]  # Get latest BBP value

            # Generate signal based on BBP thresholds
            # BUY signals (oversold condition) and SELL signals (overbought condition)
            if bbp < self.config.bb_long_threshold:
                signal = 1  # BUY (oversold)
            elif bbp > self.config.bb_short_threshold:
                signal = -1  # SELL (overbought)
            else:
                signal = 0  # HOLD (neutral zone)

            # Store processed data
            self.processed_data = {
                "signal": signal,
                "features": df,
                "bbp": bbp,
                "bb_long_threshold": self.config.bb_long_threshold,
                "bb_short_threshold": self.config.bb_short_threshold,
                "current_price": self._get_current_price()
            }

            # Log signal for debugging
            if signal != 0:
                current_price = self.processed_data["current_price"]
                spread = self._get_spread()

                if signal > 0:
                    signal_type = "BUY"
                    threshold = self.config.bb_long_threshold
                    entry_price = current_price + (spread * self.config.spread_multiplier)
                    condition = f"BBP: {bbp:.3f} < {threshold}"
                else:
                    signal_type = "SELL"
                    threshold = self.config.bb_short_threshold
                    entry_price = current_price - (spread * self.config.spread_multiplier)
                    condition = f"BBP: {bbp:.3f} > {threshold}"

                self.logger().info(
                    f"Dynamic BB-Grid {signal_type} Signal | {condition} | "
                    f"Price: {current_price} | Entry will be: {entry_price}"
                )

        except Exception as e:
            self.logger().error(f"Error updating processed data: {e}")
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}

    def _track_filled_levels(self):
        """
        Track which levels have been filled by monitoring executor states.
        """
        # Check for newly filled levels
        for executor in self.executors_info:
            if (executor.custom_info.get("level_id") and
                not executor.is_active and
                executor.close_type and
                executor.close_type != CloseType.EXPIRED):

                # Extract level number from level_id
                level_num = int(executor.custom_info["level_id"].split("_")[1])
                if level_num not in self.filled_levels:
                    self.filled_levels.add(level_num)
                    self.logger().info(f"Level {level_num} filled and added to filled levels")

    def determine_executor_actions(self):
        """
        Enhanced executor actions with filled level tracking.
        """
        # Track filled levels before processing
        self._track_filled_levels()

        # Call parent method
        return super().determine_executor_actions()

    def to_format_status(self) -> List[str]:
        """Get formatted status with BB-specific information"""
        status = super().to_format_status()

        # Add Bollinger Bands specific information
        if "bbp" in self.processed_data:
            status.append("")
            status.append("Bollinger Bands Info:")
            status.append(f"  BBP: {self.processed_data['bbp']:.3f}")
            status.append(f"  BUY Threshold: < {self.config.bb_long_threshold}")
            status.append(f"  SELL Threshold: > {self.config.bb_short_threshold}")
            status.append(f"  BB Length: {self.config.bb_length}")
            status.append(f"  BB Std Dev: {self.config.bb_std}")

        # Add dynamic grid specific info
        if self.current_entry_price:
            status.append("")
            status.append("Dynamic Grid Info:")
            status.append(f"  Current Entry: {self.current_entry_price}")
            status.append(f"  Spread Multiplier: {self.config.spread_multiplier}")

            if self.filled_levels:
                status.append(f"  Filled Levels: {sorted(list(self.filled_levels))}")

        return status