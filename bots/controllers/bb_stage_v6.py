from typing import TypedDict, List

NORMAL_STAGE='normal'
BREAKTHROUGH_STAGE='breakthrough'
FALLBACK_STAGE='fallback'

class BBStageThresholds(TypedDict):
    bb: float
    entry_normal: float
    entry_follow: float
    entry_anti: float

class BBStage:
    """
    Represents a Bollinger Band stage with thresholds, and provides transitions to the next and last stage.
    """

    def generate_stages(self, bb_stage_thresholds_dict: List[BBStageThresholds]):
        self.stages = {
            NORMAL_STAGE: bb_stage_thresholds_dict[0],
            BREAKTHROUGH_STAGE: bb_stage_thresholds_dict[1],
            FALLBACK_STAGE: bb_stage_thresholds_dict[2]
        }
        

    def __init__(self, macd_threshold: float, bb_stage_thresholds_dict: List[BBStageThresholds]):
        self.name = NORMAL_STAGE
        self.generate_stages(bb_stage_thresholds_dict)
        self.thresholds = self.stages[NORMAL_STAGE]
        self.macd_threshold = macd_threshold

    def to_next_stage(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        self.name = stage_name
        self.thresholds = self.stages[stage_name]

    def get_signal_in_current_stage(self, bbp: float, bb_width_multiplier: float, is_breakthrough: bool):
        signal = 0
        if bbp > self.high_threshold:
            signal = 1 if is_breakthrough else -1
        elif bbp < self.low_threshold:
            signal = -1 if is_breakthrough else 1
        return signal * bb_width_multiplier

    def get_high_entry_threshold(self, current_price: float, stage_name: str, bbu: float, bbl: float, macd: float):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        if macd/current_price > self.macd_threshold:
            return bbu + float(current_price) * self.stages[stage_name]["entry_follow"]
        elif macd/current_price < -self.macd_threshold:
            return bbu + float(current_price) * self.stages[stage_name]["entry_anti"]
        else:
            return bbu + float(current_price) * self.stages[stage_name]["entry_normal"]
            
    
    def get_low_entry_threshold(self, current_price: float, stage_name: str, bbu: float, bbl: float, macd: float):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        if macd/current_price > self.macd_threshold:
            return bbl - float(current_price) * self.stages[stage_name]["entry_anti"]
        elif macd/current_price < -self.macd_threshold:
            return bbl - float(current_price) * self.stages[stage_name]["entry_follow"]
        else:
            return bbl - float(current_price) * self.stages[stage_name]["entry_normal"]

    @property
    def high_threshold(self):
        return 1 + self.thresholds["bb"]

    @property
    def low_threshold(self):
        return 0 - self.thresholds["bb"]

    def to_normal_stage(self):
        self.name = NORMAL_STAGE
        self.thresholds = self.stages[NORMAL_STAGE]

    def get_signal(self, current_price: float, bbp: float, bbu: float, bbl: float, bb_width_multiplier: float, macd: float) -> float:
        if self.name == NORMAL_STAGE:
            if current_price > self.get_high_entry_threshold(current_price, BREAKTHROUGH_STAGE, bbu, bbl, macd) or current_price < self.get_low_entry_threshold(current_price, BREAKTHROUGH_STAGE, bbu, bbl, macd):
                self.to_next_stage(BREAKTHROUGH_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, True)
            else:
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
        elif self.name == BREAKTHROUGH_STAGE:
            if current_price > self.get_high_entry_threshold(current_price, FALLBACK_STAGE, bbu, bbl, macd) or current_price < self.get_low_entry_threshold(current_price, FALLBACK_STAGE, bbu, bbl, macd):
                self.to_next_stage(FALLBACK_STAGE)
                return self.get_signal_in_current_stage(bbp, 1, False)
            elif current_price < self.get_high_entry_threshold(current_price, NORMAL_STAGE, bbu, bbl, macd) and current_price > self.get_low_entry_threshold(current_price, NORMAL_STAGE, bbu, bbl, macd):
                self.to_next_stage(NORMAL_STAGE)
                return self.get_signal_in_current_stage(bbp, bb_width_multiplier, False)
            else:
                return self.get_signal_in_current_stage(bbp, 1, True)
        elif self.name == FALLBACK_STAGE:
            if current_price < self.get_high_entry_threshold(current_price, NORMAL_STAGE, bbu, bbl, macd) and current_price > self.get_low_entry_threshold(current_price, NORMAL_STAGE, bbu, bbl, macd):
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