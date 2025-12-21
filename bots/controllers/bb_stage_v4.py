from tkinter import NORMAL

from sqlalchemy import False_


NORMAL_STAGE='normal'
BREAKTHROUGH_STAGE='breakthrough'
FALLBACK_STAGE='fallback'

class BBStage:
    """
    Represents a Bollinger Band stage with thresholds, and provides transitions to the next and last stage.
    """
    _stages = {
        NORMAL_STAGE: {
            "thresholds": {"high": 0.95, "low": 0.05, "high_entry": 0.9, "low_entry": 0.1},
        },
        BREAKTHROUGH_STAGE: {
            "thresholds": {"high": 1.2, "low": -0.2, "high_entry": 1.25, "low_entry": -0.25},
        },
        FALLBACK_STAGE: {
            "thresholds": {"high": 1.35, "low": -0.35, "high_entry": 1.4, "low_entry": -0.4},
        }
    }

    def __init__(self):
        self.name = NORMAL_STAGE
        self.thresholds = BBStage._stages[NORMAL_STAGE]["thresholds"]

    def to_next_stage(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        self.name = stage_name
        self.thresholds = BBStage._stages[stage_name]["thresholds"]

    def get_signal_in_current_stage(self, bbp: float, bb_width_multiplier: float, is_breakthrough: bool):
        signal = 0
        if bbp > self.high_threshold:
            signal = 1 if is_breakthrough else -1
        elif bbp < self.low_threshold:
            signal = -1 if is_breakthrough else 1
        return signal * bb_width_multiplier

    def get_high_entry_threshold(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        return BBStage._stages[stage_name]["thresholds"]["high_entry"]
    
    def get_low_entry_threshold(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        return BBStage._stages[stage_name]["thresholds"]["low_entry"]

    def get_high_threshold(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        return BBStage._stages[stage_name]["thresholds"]["high"]
    
    def get_low_threshold(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        return BBStage._stages[stage_name]["thresholds"]["low"]

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
        self.thresholds = BBStage._stages[NORMAL_STAGE]["thresholds"]

    def get_last_stage(self):
        last_stage = BBStage._stages[self.name]["last_stage"]
        if last_stage is None:
            return None
        return BBStage(last_stage)

    def get_signal(self, bbp: float, bb_width_multiplier: float) -> float:
        if self.name == NORMAL_STAGE:
            if bbp > self.get_high_entry_threshold(BREAKTHROUGH_STAGE) or bbp < self.get_low_entry_threshold(BREAKTHROUGH_STAGE):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, True)
            else:
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
        elif self.name == BREAKTHROUGH_STAGE:
            if bbp > self.get_high_entry_threshold(FALLBACK_STAGE) or bbp < self.get_low_entry_threshold(FALLBACK_STAGE):
                self.to_next_stage(FALLBACK_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, False)
            elif bbp < self.get_high_entry_threshold(NORMAL_STAGE) and bbp > self.get_low_entry_threshold(NORMAL_STAGE):
                self.to_next_stage(NORMAL_STAGE)
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
            else:
                return self.get_signal_in_current_stage(bbp, 1, True)
        elif self.name == FALLBACK_STAGE:
            if bbp < self.get_high_entry_threshold(NORMAL_STAGE) and bbp > self.get_low_entry_threshold(NORMAL_STAGE):
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