from typing import TypedDict, Dict

NORMAL_STAGE='normal'
BREAKTHROUGH_STAGE='breakthrough'
FALLBACK_STAGE='fallback'

class BBStageThresholds(TypedDict):
    high: float
    low: float
    entry_normal: float
    entry_follow: float
    entry_anti: float

class BBStage:
    """
    Represents a Bollinger Band stage with thresholds, and provides transitions to the next and last stage.
    """

    def generate_stages(self, bb_stage_thresholds_dict: Dict[str, BBStageThresholds]):
        self.stages: Dict[str, BBStageThresholds] = {
            NORMAL_STAGE: BBStageThresholds(
                high=0.95,
                low=0.05,
                entry_normal=-0.005,
                entry_follow=-0.01,
                entry_anti=0,
            ),
            BREAKTHROUGH_STAGE: BBStageThresholds(
                high=1.2,
                low=-0.2,
                entry_normal=0.015,
                entry_follow=0.005,
                entry_anti=0.025,
            ),
            FALLBACK_STAGE: BBStageThresholds(
                high=1.3,
                low=-0.3,
                entry_normal=0.025,
                entry_follow=0.025,
                entry_anti=0.025,
            ),
        }
        

    def __init__(self, weak_bandwidth: float, strong_bandwidth: float):
        self.name = NORMAL_STAGE
        self.generate_stages(weak_bandwidth, strong_bandwidth)
        self.thresholds = self.stages[NORMAL_STAGE]
        self.weak_bandwidth = weak_bandwidth
        self.strong_bandwidth = strong_bandwidth

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
        if macd/current_price > 0.005:
            return bbu + float(current_price) * self.stages[stage_name]["entry_follow"]
        elif macd/current_price < -0.005:
            return bbu + float(current_price) * self.stages[stage_name]["entry_anti"]
        else:
            return bbu + float(current_price) * self.stages[stage_name]["entry_normal"]
            
    
    def get_low_entry_threshold(self, current_price: float, stage_name: str, bbu: float, bbl: float, macd: float):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        if macd/current_price > 0.005:
            return bbl - float(current_price) * self.stages[stage_name]["entry_anti"]
        elif macd/current_price < -0.005:
            return bbl - float(current_price) * self.stages[stage_name]["entry_follow"]
        else:
            return bbl - float(current_price) * self.stages[stage_name]["entry_normal"]

    def get_high_threshold(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        return self.stages[stage_name]["high"]
    
    def get_low_threshold(self, stage_name: str):
        if stage_name not in self.stages:
            raise ValueError(f"Invalid stage_name: {stage_name}. Must be one of {list[str](self.stages.keys())}.")
        return self.stages[stage_name]["low"]

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