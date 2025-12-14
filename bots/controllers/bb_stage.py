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
            "thresholds": {"high": 0.85, "low": 0.15},
        },
        BREAKTHROUGH_STAGE: {
            "thresholds": {"high": 1.2, "low": -0.2},
        },
        FALLBACK_STAGE: {
            "thresholds": {"high": 1, "low": 0},
        },
    }

    def __init__(self):
        self.name = NORMAL_STAGE
        self.thresholds = BBStage._stages[NORMAL_STAGE]["thresholds"]

    def to_next_stage(self, stage_name: str):
        if stage_name not in BBStage._stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list(BBStage._stages.keys())}.")
        self.name = stage_name
        self.thresholds = BBStage._stages[stage_name]["thresholds"]

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
        return self.thresholds.get("high")

    @property
    def low_threshold(self):
        return self.thresholds.get("low")

    def to_normal_stage(self):
        self.name = NORMAL_STAGE
        self.threshold = BBStage._stages[NORMAL_STAGE]["thresholds"]

    def get_last_stage(self):
        last_stage = BBStage._stages[self.name]["last_stage"]
        if last_stage is None:
            return None
        return BBStage(last_stage)

    def get_signal(self, bbp: float, bb_width_multiplier: float) -> float:
        if self.name == NORMAL_STAGE:
            if bbp > self.high_threshold and bbp < self.get_high_threshold(BREAKTHROUGH_STAGE):
                return -1 * bb_width_multiplier
            elif bbp > self.get_high_threshold(BREAKTHROUGH_STAGE):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return 1
            elif bbp < self.low_threshold and bbp > self.get_low_threshold(BREAKTHROUGH_STAGE):
                return 1 * bb_width_multiplier
            elif bbp < self.get_low_threshold(BREAKTHROUGH_STAGE):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return -1
            return 0
        if self.name == BREAKTHROUGH_STAGE:
            if bbp > self.get_high_threshold(FALLBACK_STAGE):
                return 1
            elif bbp < self.get_high_threshold(FALLBACK_STAGE) and bbp > self.get_high_threshold(NORMAL):
                self.to_next_stage(FALLBACK_STAGE)
                return -1
            elif bbp < self.get_low_threshold(FALLBACK_STAGE):
                return -1
            elif bbp > self.get_low_threshold(NORMAL) and bbp < self.get_low_threshold(FALLBACK_STAGE):
                self.to_next_stage(FALLBACK_STAGE)
                return 1
            return 0 # this case won't happen, must into fallback case
        if self.name == FALLBACK_STAGE:
            if bbp > self.get_high_threshold(BREAKTHROUGH_STAGE):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return 1
            elif bbp < self.get_high_threshold(BREAKTHROUGH_STAGE) and bbp > self.high_threshold:
                return -1
            elif bbp < self.high_threshold and bbp > self.get_high_threshold(NORMAL_STAGE):
                self.to_next_stage(NORMAL)
                return -1 * bb_width_multiplier
            elif bbp < self.get_low_threshold(BREAKTHROUGH_STAGE):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return -1
            elif bbp > self.get_low_threshold(BREAKTHROUGH_STAGE) and bbp < self.low_threshold:
                return 1
            elif bbp > self.low_threshold and bbp < self.get_low_threshold(NORMAL_STAGE):
                self.to_next_stage(NORMAL)
                return 1 * bb_width_multiplier
            return 0 # this case won't happen, must into fallback case
        
    @classmethod
    def all_stages(cls):
        return [cls(name) for name in cls._stages.keys()]

    def __repr__(self):
        return f"BBStage(name='{self.name}')"

    def __eq__(self, other):
        if isinstance(other, BBStage):
            return self.name == other.name
        return False