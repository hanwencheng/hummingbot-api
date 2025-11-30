from typing import List

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)


class BollingerV1ControllerConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "bollinger_v1"
    candles_config: List[CandlesConfig] = []
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
            "prompt_on_new": True})
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
            "prompt_on_new": True})
    interval: str = Field(
        default="3m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True})
    bb_length: int = Field(
        default=100,
        json_schema_extra={"prompt": "Enter the Bollinger Bands length: ", "prompt_on_new": True})
    bb_std: float = Field(default=2.0)
    bb_long_threshold: float = Field(default=0.0)
    bb_short_threshold: float = Field(default=1.0)

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


class BollingerV1Controller(DirectionalTradingControllerBase):
    def __init__(self, config: BollingerV1ControllerConfig, *args, **kwargs):
        self.config = config
        print(f"📊 ====================DEBUG: passed config.bb_std is: {config.bb_std}")
        self.max_records = self.config.bb_length
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=config.interval,
                max_records=self.max_records
            )]
        super().__init__(config, *args, **kwargs)

    async def update_processed_data(self):
        df = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                      trading_pair=self.config.candles_trading_pair,
                                                      interval=self.config.interval,
                                                      max_records=self.max_records)
        # Add indicators
        self.logger().info(f"Backtesting Debug: Got candles data - rows: {len(df) if df is not None else 0}, required: {self.config.bb_length}")
        df.ta.bbands(length=self.config.bb_length, lower_std=self.config.bb_std, upper_std=self.config.bb_std, append=True)
        # bbp = df[f"BBP_{self.config.bb_length}_{self.config.bb_std}_{self.config.bb_std}"]

        # The correct BBP column format is BBP_{length}_{lower_std}_{upper_std}
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
                print(f"📊 DEBUG: Using fallback BBP column: {bbp_col}")
            else:
                print(f"📊 ERROR: No BBP column found! Available columns: {list(df.columns)}")
                raise KeyError(f"BBP column not found. Available columns: {list(df.columns)}")

        # Generate signal
        long_condition = bbp < self.config.bb_long_threshold
        short_condition = bbp > self.config.bb_short_threshold

        # Generate signal
        df["signal"] = 0
        df.loc[long_condition, "signal"] = 1
        df.loc[short_condition, "signal"] = -1

        # Update processed data
        self.processed_data["signal"] = df["signal"].iloc[-1]
        self.processed_data["features"] = df
