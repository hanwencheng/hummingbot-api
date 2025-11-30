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
        self.max_records = self.config.bb_length # Extra buffer for BB calculation

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

            current_price = self._get_current_price()
            bbp_col = f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"
            print(f"📊 DEBUG: Looking for BBP column: {bbp_col}")
            print(f"📊 DEBUG: Available columns after bbands: {list(df.columns)}")

            if bbp_col in df.columns:
                bbp = df[bbp_col]
                print(f"📊 DEBUG: Successfully found BBP column: {bbp_col}")
            else:
                # Fallback: find any BBP column
                possible_bbp_cols = [col for col in df.columns if 'BBP' in col]
                if possible_bbp_cols:
                    bbp_col = possible_bbp_cols[0]
                    bbp = df[bbp_col]
                else:
                    raise KeyError(f"BBP column not found. Available columns: {list(df.columns)}")
        

            # Generate signal based on BBP thresholds
            # BUY signals (oversold condition) and SELL signals (overbought condition)
            long_condition = bbp < self.config.bb_long_threshold
            short_condition = bbp > self.config.bb_short_threshold
            df["signal"] = 0
            df.loc[long_condition, "signal"] = 1
            df.loc[short_condition, "signal"] = -1

            # Store processed data
            self.processed_data = {
                "signal": df["signal"].iloc[-1],
                "features": df,
                "current_price": current_price
            }

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