from tkinter import NORMAL

from sqlalchemy import False_


NORMAL_STAGE='normal'
BREAKTHROUGH_STAGE='breakthrough'
FALLBACK_STAGE='fallback'

class BBStage:
    """
    Represents a Bollinger Band stage with thresholds, and provides transitions to the next and last stage.
    """

    def generate_stages(self, weak_bandwidth: float, strong_bandwidth: float):
        self.stages = {
            NORMAL_STAGE: {
                "thresholds": {"high": 0.95, "low": 0.05, "high_entry": weak_bandwidth, "low_entry": weak_bandwidth},
            },
            BREAKTHROUGH_STAGE: {
                "thresholds": {"high": 1.15, "low": -0.15, "high_entry": strong_bandwidth, "low_entry": strong_bandwidth},
            },
            FALLBACK_STAGE: {
                "thresholds": {"high": 1.2, "low": -0.2, "high_entry": strong_bandwidth + 0.05, "low_entry": strong_bandwidth + 0.05},
            }
        }
        

    def __init__(self, weak_bandwidth: float, strong_bandwidth: float):
        self.name = NORMAL_STAGE
        self.generate_stages(weak_bandwidth, strong_bandwidth)
        self.thresholds = self.stages[NORMAL_STAGE]["thresholds"]
        self.weak_bandwidth = weak_bandwidth
        self.strong_bandwidth = strong_bandwidth

    def to_next_stage(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        self.name = stage_name
        self.thresholds = self.stages[stage_name]["thresholds"]

    def get_signal_in_current_stage(self, bbp: float, bb_width_multiplier: float, is_breakthrough: bool):
        signal = 0
        if bbp > self.high_threshold:
            signal = 1 if is_breakthrough else -1
        elif bbp < self.low_threshold:
            signal = -1 if is_breakthrough else 1
        return signal * bb_width_multiplier

    def get_high_entry_threshold(self, stage_name: str, bbu: float, bbl: float):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        bb_mid = (bbu + bbl) / 2
        return bbu + bbu * self.stages[stage_name]["thresholds"]["high_entry"] / 2
    
    def get_low_entry_threshold(self, stage_name: str, bbu: float, bbl: float):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        bb_mid = (bbu + bbl) / 2
        return bb_mid - bbu * self.stages[stage_name]["thresholds"]["low_entry"] / 2

    def get_high_threshold(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        return self.stages[stage_name]["thresholds"]["high"]
    
    def get_low_threshold(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        return self.stages[stage_name]["thresholds"]["low"]

    @property
    def high_threshold(self):
        return self.thresholds["high"]

    @property
    def low_threshold(self):
        return self.thresholds["low"]

    @property
    def high_entry_threshold(self):
        return self.thresholds["high_entry"]

    @property
    def low_entry_threshold(self):
        return self.thresholds["low_entry"]

    def to_normal_stage(self):
        self.name = NORMAL_STAGE
        self.thresholds = self.stages[NORMAL_STAGE]["thresholds"]

    def get_last_stage(self):
        last_stage = self.stages[self.name]["last_stage"]
        if last_stage is None:
            return None
        return BBStage(last_stage)

    def get_signal(self, current_price: float, bbp: float, bbu: float, bbl: float, bb_width_multiplier: float) -> float:
        if self.name == NORMAL_STAGE:
            if current_price > self.get_high_entry_threshold(BREAKTHROUGH_STAGE, bbu, bbl) or current_price < self.get_low_entry_threshold(BREAKTHROUGH_STAGE, bbu, bbl):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, True)
            else:
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
        elif self.name == BREAKTHROUGH_STAGE:
            if current_price > self.get_high_entry_threshold(FALLBACK_STAGE, bbu, bbl) or current_price < self.get_low_entry_threshold(FALLBACK_STAGE, bbu, bbl):
                self.to_next_stage(FALLBACK_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, False)
            elif current_price < self.get_high_entry_threshold(NORMAL_STAGE, bbu, bbl) and current_price > self.get_low_entry_threshold(NORMAL_STAGE, bbu, bbl):
                self.to_next_stage(NORMAL_STAGE)
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
            else:
                return self.get_signal_in_current_stage(bbp, 1, True)
        elif self.name == FALLBACK_STAGE:
            if current_price < self.get_high_entry_threshold(NORMAL_STAGE, bbu, bbl) and current_price > self.get_low_entry_threshold(NORMAL_STAGE, bbu, bbl):
                self.to_next_stage(NORMAL_STAGE)
            return self.get_signal_in_current_stage(bbp, 1, False)
        else:
            return self.get_signal_in_current_stage(bbp, 1, False)
        
    @classmethod
    def all_stages(cls):
        return [cls(name) for name in cls._stages.keys()]

    def __repr__(self):
        return f"BBStage(name='{self.name}')"

    def __eq__(self, other):
        if isinstance(other, BBStage):
            return self.name == other.name
        return False