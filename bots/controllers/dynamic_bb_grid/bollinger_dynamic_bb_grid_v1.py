"""
Bollinger Bands Dynamic BB-Grid Strategy V1

Advanced Bollinger Bands strategy with dynamic grid management:
- Signal-based entry level adjustment (signal + 2*profit_pct)
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
        default="15m",
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
        Generates both BUY and SELL signals based on Bollinger Bands position.
        """
        try:
            # Get candles data
            df = self.market_data_provider.get_candles_df(
                connector_name=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.interval,
                max_records=self.max_records
            )

            # Debug logging for data availability
            self.logger().info(f"Backtesting Debug: Got candles data - rows: {len(df) if df is not None else 0}, required: {self.config.bb_length}")

            if df is None or len(df) < self.config.bb_length:
                # Not enough data for BB calculation
                self.logger().warning(f"Backtesting Debug: Insufficient data for BB calculation. Need {self.config.bb_length}, got {len(df) if df is not None else 0}")
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
                    self.logger().error(f"Backtesting Debug: No BBP column found. Available columns: {list(df.columns)}")
                    self.processed_data = {"signal": 0, "features": df}
                    return
                bbp_col = bbp_cols[0]
                self.logger().warning(f"Backtesting Debug: Using fallback BBP column: {bbp_col}")

            bbp = df[bbp_col].iloc[-1]  # Get latest BBP value
            current_price = self._get_current_price()

            # Debug logging for BBP and thresholds
            self.logger().info(f"Backtesting Debug: BBP={bbp:.4f}, Long threshold={self.config.bb_long_threshold}, Short threshold={self.config.bb_short_threshold}, Price={current_price}")

            # Generate signal based on BBP thresholds
            # BUY signals (oversold condition) and SELL signals (overbought condition)
            if bbp < self.config.bb_long_threshold:
                signal = 1  # BUY (oversold)
                self.logger().info(f"Backtesting Debug: BUY signal generated - BBP {bbp:.4f} < {self.config.bb_long_threshold}")
            elif bbp > self.config.bb_short_threshold:
                signal = -1  # SELL (overbought)
                self.logger().info(f"Backtesting Debug: SELL signal generated - BBP {bbp:.4f} > {self.config.bb_short_threshold}")
            else:
                signal = 0  # HOLD (neutral zone)
                self.logger().info(f"Backtesting Debug: HOLD signal - BBP {bbp:.4f} in neutral zone ({self.config.bb_long_threshold} to {self.config.bb_short_threshold})")

            # Store processed data
            self.processed_data = {
                "signal": signal,
                "features": df,
                "bbp": bbp,
                "bb_long_threshold": self.config.bb_long_threshold,
                "bb_short_threshold": self.config.bb_short_threshold,
                "current_price": current_price
            }

            # Log signal for debugging
            if signal != 0:
                if signal > 0:
                    signal_type = "BUY"
                    threshold = self.config.bb_long_threshold
                    entry_price = current_price * (1 - self.config.accumulate_pct)
                    condition = f"BBP: {bbp:.3f} < {threshold}"
                else:
                    signal_type = "SELL"
                    threshold = self.config.bb_short_threshold
                    entry_price = current_price * (1 + self.config.accumulate_pct)
                    condition = f"BBP: {bbp:.3f} > {threshold}"

                self.logger().info(
                    f"Dynamic BB-Grid {signal_type} Signal | {condition} | "
                    f"Price: {current_price} | Entry will be: {entry_price}"
                )
            else:
                # Log even when signal is 0 during backtesting for debugging
                self.logger().info(f"Backtesting Debug: No signal - BBP {bbp:.4f} between thresholds {self.config.bb_long_threshold} and {self.config.bb_short_threshold}")

        except Exception as e:
            self.logger().error(f"Error updating processed data: {e}")
            import traceback
            self.logger().error(f"Backtesting Debug: Full traceback: {traceback.format_exc()}")
            self.processed_data = {"signal": 0, "features": pd.DataFrame()}


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
        status.append("")
        status.append("Dynamic Grid Info:")
        status.append(f"  Direction: {'BUY' if self.direction_buy else 'SELL'}")
        status.append(f"  Accumulate %: {self.config.accumulate_pct:.2%}")
        status.append(f"  Profit %: {self.config.profit_pct:.2%}")

        if hasattr(self, 'filled_executor_ids'):
            status.append(f"  Filled Executors: {len(self.filled_executor_ids)}")
            status.append(f"  Total Executors: {len([e for e in self.executors_info if e.is_active])}")

        if self.final_stop_loss_price:
            status.append(f"  Final Stop Loss: {self.final_stop_loss_price}")

        # Add stop loss waiting info
        if self.stop_loss_waiting_until != 9999999999:
            current_time = self.market_data_provider.time()
            if current_time < self.stop_loss_waiting_until:
                remaining_hours = (self.stop_loss_waiting_until - current_time) / 3600
                status.append(f"  Stop Loss Wait: {remaining_hours:.1f}h remaining")

        return status