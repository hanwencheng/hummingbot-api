#!/usr/bin/env python3
"""
Minimal POC for Bayesian Optimization with rate limiting
Tests the backtesting endpoint with controlled request frequency
"""

import requests
import optuna
import time
import logging
import base64
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
import json
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
import statistics
import math

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

Backtesting_Interval = '1m'

def get_interval_minutes(interval_str: str) -> int:
    """Parse interval string and return minutes"""
    if interval_str.endswith('m'):
        return int(interval_str[:-1])
    elif interval_str.endswith('h'):
        return int(interval_str[:-1]) * 60
    elif interval_str.endswith('s'):
        return max(1, int(interval_str[:-1]) // 60)  # Convert to minutes, minimum 1
    else:
        return 1  # Default to 1 minute

def get_requests_per_group(interval_str: str) -> int:
    """Calculate how many requests can be sent per minute based on interval"""
    interval_minutes = get_interval_minutes(interval_str)
    # If Backtesting_Interval is 1min, then we have 1 request per minute group
    # If Backtesting_Interval is 5min, then we have 5 requests per minute group
    return interval_minutes

@dataclass
class BacktestResults:
    """Typed structure for backtest results"""
    accuracy: float
    accuracy_long: float
    accuracy_short: float
    close_types: Dict[str, int]
    loss_signals: int
    max_drawdown_pct: float
    max_drawdown_usd: float
    net_pnl: float
    net_pnl_quote: float
    profit_factor: float
    sharpe_ratio: float
    total_executors: int
    total_executors_with_position: int
    total_long: int
    total_positions: int
    total_short: int
    total_volume: float
    win_signals: int

    @classmethod
    def from_dict(cls, data: Dict) -> 'BacktestResults':
        """Create BacktestResults from API response dictionary"""
        return cls(
            accuracy=float(data.get('accuracy', 0)),
            accuracy_long=float(data.get('accuracy_long', 0)),
            accuracy_short=float(data.get('accuracy_short', 0)),
            close_types=data.get('close_types', {}),
            loss_signals=int(data.get('loss_signals', 0)),
            max_drawdown_pct=float(data.get('max_drawdown_pct', 0)),
            max_drawdown_usd=float(data.get('max_drawdown_usd', 0)),
            net_pnl=float(data.get('net_pnl', 0)),
            net_pnl_quote=float(data.get('net_pnl_quote', 0)),
            profit_factor=float(data.get('profit_factor', 0)),
            sharpe_ratio=float(data.get('sharpe_ratio', 0)),
            total_executors=int(data.get('total_executors', 0)),
            total_executors_with_position=int(data.get('total_executors_with_position', 0)),
            total_long=int(data.get('total_long', 0)),
            total_positions=int(data.get('total_positions', 0)),
            total_short=int(data.get('total_short', 0)),
            total_volume=float(data.get('total_volume', 0)),
            win_signals=int(data.get('win_signals', 0))
        )

    def calculate_total_accuracy(self) -> float:
        """Calculate weighted total accuracy"""
        total_trades = self.total_long + self.total_short
        if total_trades == 0:
            return 0.0

        return (self.total_long * self.accuracy_long + self.total_short * self.accuracy_short) / total_trades

@dataclass
class AggregatedResults:
    """Aggregated results from multiple monthly backtests"""
    monthly_results: List[BacktestResults]
    total_pnl: float
    total_accuracy: float
    mean_pnl: float
    std_pnl: float
    variance_pnl: float
    cv_pnl: float
    execution_time: float

    @classmethod
    def from_monthly_results(cls, monthly_results: List[BacktestResults], execution_time: float) -> 'AggregatedResults':
        """Create aggregated results from monthly backtest results"""
        if not monthly_results:
            return cls([], 0.0, 0.0, 0.0, 0.0, 0.0, float('inf'), execution_time)

        # Calculate totals
        total_pnl = sum(result.net_pnl for result in monthly_results)

        # Calculate weighted total accuracy
        total_long = sum(result.total_long for result in monthly_results)
        total_short = sum(result.total_short for result in monthly_results)
        total_trades = total_long + total_short

        if total_trades > 0:
            weighted_long_accuracy = sum(result.total_long * result.accuracy_long for result in monthly_results)
            weighted_short_accuracy = sum(result.total_short * result.accuracy_short for result in monthly_results)
            total_accuracy = (weighted_long_accuracy + weighted_short_accuracy) / total_trades
        else:
            total_accuracy = 0.0

        # Calculate PnL statistics
        pnl_values = [result.net_pnl for result in monthly_results]
        n_months = len(pnl_values)

        mean_pnl = statistics.mean(pnl_values) if n_months > 0 else 0.0

        if n_months < 2:
            std_pnl = 0.0
            variance_pnl = 0.0
        else:
            std_pnl = statistics.stdev(pnl_values)  # Sample std (ddof=1)
            variance_pnl = statistics.variance(pnl_values)  # Sample variance (ddof=1)

        # Calculate CV (coefficient of variation)
        if mean_pnl <= 0:
            cv_pnl = float('inf')
        else:
            cv_pnl = std_pnl / mean_pnl

        return cls(
            monthly_results=monthly_results,
            total_pnl=total_pnl,
            total_accuracy=total_accuracy,
            mean_pnl=mean_pnl,
            std_pnl=std_pnl,
            variance_pnl=variance_pnl,
            cv_pnl=cv_pnl,
            execution_time=execution_time
        )

# Configuration
API_BASE_URL = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin"
TIMEOUT = 3000.0  # 50 minutes timeout per request
MAX_RETRIES = 2

def generate_auth_header(username: str, password: str) -> str:
    """Generate Basic Auth header like the TypeScript function"""
    auth_str = f"{username}:{password}"
    auth_bytes = auth_str.encode('utf-8')
    base64_encoded = base64.b64encode(auth_bytes).decode('utf-8')
    return f"Basic {base64_encoded}"

class MinimalOptimizer:
    def __init__(self):
        # Generate auth header
        self.auth_header = generate_auth_header(USERNAME, PASSWORD)
        self.headers = {
            'Authorization': self.auth_header,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # Create timestamped subdirectory for this test run
        timestamp = datetime.now().strftime("%m-%d-%H-%M")
        trading_pair = "SOL-USDT"  # From base_config
        test_dir_name = f"{trading_pair}_{timestamp}"

        self.results_dir = Path("minimal_optimization_results") / test_dir_name
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Global rate limiting - persists across all trials
        self.global_request_times = []  # Track all request start times globally
        self.requests_per_group = get_requests_per_group(Backtesting_Interval) * 2

        logger.info(f"Initialized with auth header for user: {USERNAME}")
        logger.info(f"Results will be saved to: {self.results_dir}")
        logger.info(f"Global rate limiting: {self.requests_per_group} requests per group (60s window)")

        # Simple base config for testing - using bollinger_dynamic_bb_grid_v6 for the 3 BB stage parameters
        self.base_config = {
            'controller_name': 'bollinger_dynamic_bb_grid_v6',
            'controller_type': 'dynamic_bb_grid',
            'connector_name': 'binance_perpetual',
            'trading_pair': 'SOL-USDT',
            'candles_connector': 'binance_perpetual',
            'candles_trading_pair': 'SOL-USDT',
            'interval': Backtesting_Interval,
            'position_mode': 'ONEWAY',
            'profit_skew': 1,
            'stop_loss_skew': 1,
            'time_limit_hours': 1.5,
            'bb_length': 1100,
            'bb_std': 2.0,
            'bb_weak_threshold': 0.05,
            'bb_strong_threshold': 0.1,
            'level_number': 5,
            'leverage': 10,
            'level_size': 100,
            'cooldown_time': 600,
            'accumulate_pct': 0.003,
            'profit_pct': 0.005,
            'stop_loss_pct': 0.003,
            'stop_loss_waiting_time_hours': 2,
            'reverse_skew': 0.1,
        }

    def sample_minimal_parameters(self, trial: optuna.Trial) -> dict:
        """Sample all BB stage and entry parameters plus MACD threshold"""
        return {
            'normal_stage_bb': trial.suggest_float('normal_stage_bb', -0.8, 1.0),
            'normal_entry_normal': trial.suggest_float('normal_entry_normal', -0.1, 0.1),
            'normal_entry_follow': trial.suggest_float('normal_entry_follow', -0.1, 0.1),
            'normal_entry_anti': trial.suggest_float('normal_entry_anti', -0.1, 0.1),
            'breakthrough_stage_bb': trial.suggest_float('breakthrough_stage_bb', -0.1, 0.1),
            'breakthrough_entry_normal': trial.suggest_float('breakthrough_entry_normal', -0.1, 0.1),
            'breakthrough_entry_follow': trial.suggest_float('breakthrough_entry_follow', -0.1, 0.1),
            'breakthrough_entry_anti': trial.suggest_float('breakthrough_entry_anti', -0.1, 0.1),
            'fallback_stage_bb': trial.suggest_float('fallback_stage_bb', -0.1, 0.1),
            'fallback_entry_normal': trial.suggest_float('fallback_entry_normal', -0.1, 0.1),
            'fallback_entry_follow': trial.suggest_float('fallback_entry_follow', -0.1, 0.1),
            'fallback_entry_anti': trial.suggest_float('fallback_entry_anti', -0.1, 0.1),
            'macd_threshold': trial.suggest_float('macd_threshold', 0.0, 0.1),
        }

    def get_test_periods(self) -> List[Tuple[int, int]]:
        """Get monthly test periods to avoid rate limiting"""
        start_date = datetime(2025, 7, 10)  # June 10th, 2025
        end_date = datetime(2025, 12, 10)   # December 10th, 2025

        periods = []
        current_date = start_date

        while current_date < end_date:
            # Get the end of current month or final end date, whichever is earlier
            if current_date.month == 12:
                next_month = datetime(current_date.year + 1, 1, current_date.day)
            else:
                next_month = datetime(current_date.year, current_date.month + 1, current_date.day)

            period_end = min(next_month, end_date)

            start_timestamp = int(current_date.timestamp())
            end_timestamp = int(period_end.timestamp())

            periods.append((start_timestamp, end_timestamp))
            logger.info(f"📅 Month {len(periods)}: {current_date.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}")

            current_date = period_end

        logger.info(f"📊 Total monthly periods: {len(periods)}")
        return periods

    def apply_global_rate_limit(self, trial_number: int, month_num: int = None) -> None:
        """Apply global rate limiting across all trials and requests"""
        current_time = time.time()

        # Check if we need to wait based on global requests per group
        if len(self.global_request_times) >= self.requests_per_group:
            # Check if we need to wait for the next minute window
            oldest_request_in_group = self.global_request_times[-(self.requests_per_group)]
            time_since_group_start = current_time - oldest_request_in_group

            if time_since_group_start < 60.0:
                wait_time = 60.0 - time_since_group_start
                context = f"Month {month_num}" if month_num else "Request"
                logger.info(f"Trial {trial_number}, {context}: Global rate limiting - waiting {wait_time:.1f} more seconds...")
                logger.info(f"Total requests so far: {len(self.global_request_times)}, Group limit: {self.requests_per_group}")
                time.sleep(wait_time)
                current_time = time.time()

        # Record this request time globally
        self.global_request_times.append(current_time)
        return current_time

    def execute_monthly_backtest(self, params: dict, start_time: int, end_time: int, month_num: int, trial_number: int) -> Optional[BacktestResults]:
        """Execute a single monthly backtest and return typed results"""
        # Create config
        config = self.base_config.copy()
        config.update(params)

        backtest_config = {
            'start_time': start_time,
            'end_time': end_time,
            'backtesting_resolution': Backtesting_Interval,
            'trade_cost': 0.0006,
            'config': config
        }

        start_date = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d')
        end_date = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d')
        logger.info(f"Trial {trial_number}, Month {month_num}: Testing period {start_date} to {end_date}")

        for retry in range(MAX_RETRIES + 1):
            try:
                logger.info(f"Trial {trial_number}, Month {month_num}: Sending request to backend (attempt {retry + 1}/{MAX_RETRIES + 1})")
                request_start_time = time.time()

                # This blocks until the request is COMPLETELY finished
                response = self.session.post(
                    f"{API_BASE_URL}/backtesting/run-backtesting",
                    json=backtest_config,
                    timeout=TIMEOUT
                )

                request_duration = time.time() - request_start_time

                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Trial {trial_number}, Month {month_num}: ✅ Request completed in {request_duration:.2f}s")

                    # Parse results into typed structure
                    if "results" in result:
                        results_data = result["results"]
                        logger.info(f"Trial {trial_number}, Month {month_num}: PnL={results_data.get('net_pnl', 0):.4f}, Accuracy={results_data.get('accuracy', 0):.4f}")
                        return BacktestResults.from_dict(results_data)
                    else:
                        logger.error(f"Trial {trial_number}, Month {month_num}: ❌ Unexpected response structure")
                        return None

                else:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    logger.warning(f"Trial {trial_number}, Month {month_num}: ❌ Request failed - {error_msg}")
                    if retry < MAX_RETRIES:
                        logger.info(f"Trial {trial_number}, Month {month_num}: Retrying immediately...")
                        continue
                    else:
                        return None

            except requests.exceptions.Timeout:
                logger.error(f"Trial {trial_number}, Month {month_num}: ⏰ Request timeout after {TIMEOUT}s")
                if retry < MAX_RETRIES:
                    logger.info(f"Trial {trial_number}, Month {month_num}: Retrying immediately...")
                    continue
                else:
                    return None

            except Exception as e:
                logger.error(f"Trial {trial_number}, Month {month_num}: 💥 Request failed: {str(e)}")
                if retry < MAX_RETRIES:
                    logger.info(f"Trial {trial_number}, Month {month_num}: Retrying immediately...")
                    continue
                else:
                    return None

        return None

    def execute_aggregated_backtest(self, params: dict, trial_number: int) -> AggregatedResults:
        """Execute monthly backtests and aggregate results with global rate limiting"""
        trial_start_time = time.time()
        periods = self.get_test_periods()

        logger.info(f"Trial {trial_number}: Executing {len(periods)} monthly backtests with {len(params)} optimized parameters: {list(params.keys())}")
        logger.info(f"Trial {trial_number}: Using global rate limiting - {self.requests_per_group} requests per 60s window")

        monthly_results = []

        for month_num, (start_time, end_time) in enumerate(periods, 1):
            # Apply global rate limiting before each request
            request_start_time = self.apply_global_rate_limit(trial_number, month_num)

            monthly_result = self.execute_monthly_backtest(params, start_time, end_time, month_num, trial_number)

            request_end_time = time.time()
            request_duration = request_end_time - request_start_time

            logger.info(f"Trial {trial_number}, Month {month_num}: Request took {request_duration:.2f}s")

            if monthly_result:
                monthly_results.append(monthly_result)
            else:
                logger.warning(f"Trial {trial_number}, Month {month_num}: Failed to get results")

        execution_time = time.time() - trial_start_time

        # Create aggregated results
        aggregated = AggregatedResults.from_monthly_results(monthly_results, execution_time)

        logger.info(f"Trial {trial_number}: ✅ Aggregated {len(monthly_results)}/{len(periods)} months")
        logger.info(f"Trial {trial_number}: Total execution time: {execution_time:.2f}s")
        logger.info(f"Trial {trial_number}: Global requests so far: {len(self.global_request_times)}")
        logger.info(f"Trial {trial_number}: Total PnL={aggregated.total_pnl:.4f}, Total Accuracy={aggregated.total_accuracy:.4f}, CV={aggregated.cv_pnl:.4f}")

        return aggregated

    def execute_concurrent_batch(self, trial_params_list: List[Tuple[optuna.Trial, dict]]) -> List[float]:
        """Execute a batch of trials concurrently and return objective scores"""
        batch_size = len(trial_params_list)
        logger.info(f"🔄 Starting CONCURRENT batch of {batch_size} requests")

        def execute_trial_wrapper(trial_params_tuple):
            trial, params = trial_params_tuple
            logger.info(f"Trial {trial.number}: 🚀 Starting concurrent execution")
            try:
                # Use the shared execute_single_trial method
                score = self.execute_single_trial(trial, params)
                logger.info(f"Trial {trial.number}: ✅ Concurrent request completed")
                return score
            except Exception as e:
                logger.error(f"Trial {trial.number}: ❌ Concurrent request failed: {str(e)}")
                return 0.0

        # Execute all requests concurrently using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
            logger.info(f"🚀 Submitting {batch_size} concurrent requests to backend")

            # Submit all tasks
            future_to_trial = {
                executor.submit(execute_trial_wrapper, trial_params): trial_params[0].number
                for trial_params in trial_params_list
            }

            # Wait for ALL tasks to complete
            scores = []
            completed_trials = []

            for future in concurrent.futures.as_completed(future_to_trial):
                trial_number = future_to_trial[future]
                try:
                    score = future.result()
                    scores.append(score)
                    completed_trials.append(trial_number)
                    logger.info(f"Trial {trial_number}: ✅ Batch request completed ({len(completed_trials)}/{batch_size})")
                except Exception as e:
                    logger.error(f"Trial {trial_number}: 💥 Batch request exception: {str(e)}")
                    scores.append(0.0)
                    completed_trials.append(trial_number)

            logger.info(f"🎯 BATCH COMPLETED: All {batch_size} requests finished - Backend ready for next batch")
            return scores

    def process_aggregated_result(self, trial: optuna.Trial, params: dict, aggregated_result: AggregatedResults) -> float:
        """Process aggregated trial result and return objective score"""
        try:
            # Use total PnL as the primary objective
            score = aggregated_result.total_pnl

            # Save detailed result
            result_data = {
                'trial_number': trial.number,
                'params': params,
                'total_pnl': aggregated_result.total_pnl,
                'total_accuracy': aggregated_result.total_accuracy,
                'mean_pnl': aggregated_result.mean_pnl,
                'std_pnl': aggregated_result.std_pnl,
                'variance_pnl': aggregated_result.variance_pnl,
                'cv_pnl': aggregated_result.cv_pnl,
                'monthly_count': len(aggregated_result.monthly_results),
                'monthly_pnls': [month.net_pnl for month in aggregated_result.monthly_results],
                'monthly_accuracies': [month.calculate_total_accuracy() for month in aggregated_result.monthly_results],
                'execution_time': aggregated_result.execution_time,
                'timestamp': datetime.now().isoformat()
            }

            # Save to file
            filename = f"trial_{trial.number:03d}.json"
            with open(self.results_dir / filename, 'w') as f:
                json.dump(result_data, f, indent=2)

            logger.info(f"Trial {trial.number}: ✅ Score={score:.4f}, Total PnL={aggregated_result.total_pnl:.4f}, Total Accuracy={aggregated_result.total_accuracy:.4f}, CV={aggregated_result.cv_pnl:.4f}")
            return score

        except Exception as e:
            logger.error(f"Trial {trial.number}: 💥 Processing error: {str(e)}")
            return 0.0

    def execute_single_trial(self, trial: optuna.Trial, params: dict) -> float:
        """Execute a single trial and return objective score - shared logic"""
        logger.info(f"Trial {trial.number}: Sampled parameters: {params}")

        # Execute aggregated backtest across multiple months
        aggregated_result = self.execute_aggregated_backtest(params, trial.number)

        # Process result and return score
        return self.process_aggregated_result(trial, params, aggregated_result)

    def save_optimization_summary(self, study: optuna.Study, execution_mode: str = "sequential", batch_size: int = 1):
        """Save final optimization summary with CV rankings - shared logic"""
        if study.best_trial:
            logger.info(f"\n=== {execution_mode.title()} Optimization Completed ===")
            logger.info(f"🏆 Best Score: {study.best_value:.4f}")
            logger.info(f"🎯 Best Params: {study.best_params}")

            # Get completed trials
            completed_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]

            # Load trial data with aggregated results from saved files
            trials_with_data = []
            for trial in completed_trials:
                trial_file = self.results_dir / f"trial_{trial.number:03d}.json"
                if trial_file.exists():
                    try:
                        with open(trial_file, 'r') as f:
                            trial_data = json.load(f)
                        trials_with_data.append({
                            'trial': trial,
                            'trial_number': trial.number,
                            'score': trial.value,
                            'params': trial.params,
                            'total_pnl': trial_data.get('total_pnl', 0),
                            'total_accuracy': trial_data.get('total_accuracy', 0),
                            'cv_pnl': trial_data.get('cv_pnl', float('inf')),
                            'mean_pnl': trial_data.get('mean_pnl', 0),
                            'std_pnl': trial_data.get('std_pnl', 0),
                            'variance_pnl': trial_data.get('variance_pnl', 0),
                            'monthly_count': trial_data.get('monthly_count', 0)
                        })
                    except Exception as e:
                        logger.warning(f"Could not load data for trial {trial.number}: {e}")

            # Top 5 by score (Total PnL)
            top_5_by_score = sorted(trials_with_data, key=lambda t: t['score'], reverse=True)[:5]
            top_5_score_results = []
            logger.info(f"\n🏆 Top 5 by Total PnL:")
            for i, trial_data in enumerate(top_5_by_score, 1):
                top_5_score_results.append({
                    'rank': i,
                    'trial_number': trial_data['trial_number'],
                    'total_pnl': trial_data['total_pnl'],
                    'total_accuracy': trial_data['total_accuracy'],
                    'cv_pnl': trial_data['cv_pnl'],
                    'params': trial_data['params']
                })
                logger.info(f"🥇 Rank {i}: Trial {trial_data['trial_number']}, PnL={trial_data['total_pnl']:.4f}, Accuracy={trial_data['total_accuracy']:.4f}, CV={trial_data['cv_pnl']:.4f}")

            # Top 5 by accuracy
            top_5_by_accuracy = sorted(trials_with_data, key=lambda t: t['total_accuracy'], reverse=True)[:5]
            top_5_accuracy_results = []
            logger.info(f"\n🎯 Top 5 by Total Accuracy:")
            for i, trial_data in enumerate(top_5_by_accuracy, 1):
                top_5_accuracy_results.append({
                    'rank': i,
                    'trial_number': trial_data['trial_number'],
                    'total_accuracy': trial_data['total_accuracy'],
                    'total_pnl': trial_data['total_pnl'],
                    'cv_pnl': trial_data['cv_pnl'],
                    'params': trial_data['params']
                })
                logger.info(f"🎯 Rank {i}: Trial {trial_data['trial_number']}, Accuracy={trial_data['total_accuracy']:.4f}, PnL={trial_data['total_pnl']:.4f}, CV={trial_data['cv_pnl']:.4f}")

            # Top 5 by CV (lower is better) - exclude infinite values
            finite_cv_trials = [t for t in trials_with_data if t['cv_pnl'] != float('inf')]
            top_5_by_cv = sorted(finite_cv_trials, key=lambda t: t['cv_pnl'])[:5]
            top_5_cv_results = []
            logger.info(f"\n📊 Top 5 by CV (Coefficient of Variation - Lower is Better):")
            for i, trial_data in enumerate(top_5_by_cv, 1):
                top_5_cv_results.append({
                    'rank': i,
                    'trial_number': trial_data['trial_number'],
                    'cv_pnl': trial_data['cv_pnl'],
                    'total_pnl': trial_data['total_pnl'],
                    'total_accuracy': trial_data['total_accuracy'],
                    'params': trial_data['params']
                })
                logger.info(f"📊 Rank {i}: Trial {trial_data['trial_number']}, CV={trial_data['cv_pnl']:.4f}, PnL={trial_data['total_pnl']:.4f}, Accuracy={trial_data['total_accuracy']:.4f}")

            summary = {
                'best_score': study.best_value,
                'best_params': study.best_params,
                'top_5_by_total_pnl': top_5_score_results,
                'top_5_by_total_accuracy': top_5_accuracy_results,
                'top_5_by_cv_pnl': top_5_cv_results,
                'total_trials': len(study.trials),
                'completed_trials': len(completed_trials),
                'completed_at': datetime.now().isoformat(),
                'execution_mode': execution_mode,
                'batch_size': batch_size
            }

            with open(self.results_dir / "optimization_summary.json", 'w') as f:
                json.dump(summary, f, indent=2)
        else:
            logger.warning("No trials completed successfully")

    def run_sequential_optimization(self, n_trials: int = 3):
        """Run optimization with TRUE sequential execution - interval-based rate limiting"""
        logger.info(f"🚀 Starting TRUE SEQUENTIAL optimization with {n_trials} trials")
        logger.info(f"🔄 Each trial waits for ACTUAL completion of previous trial")
        logger.info(f"⏱️  Rate limiting based on {Backtesting_Interval} interval")

        study = optuna.create_study(direction="maximize")

        # Enqueue initial starting values for Bayesian optimization
        initial_params = {
            'normal_stage_bb': 0.147,
            'normal_entry_normal': -0.037,
            'normal_entry_follow': -0.089,
            'normal_entry_anti': -0.016,
            'breakthrough_stage_bb': -0.032,
            'breakthrough_entry_normal': 0.087,
            'breakthrough_entry_follow': 0.020,
            'breakthrough_entry_anti': -0.095,
            'fallback_stage_bb': -0.096,
            'fallback_entry_normal': 0.013,
            'fallback_entry_follow': -0.031,
            'fallback_entry_anti': 0.028,
            'macd_threshold': 0.06,
        }
        study.enqueue_trial(initial_params)
        logger.info(f"📍 Enqueued initial trial with baseline parameters: {initial_params}")

        for trial_index in range(n_trials):
            trial_number = trial_index + 1
            logger.info(f"\n{'='*60}")
            logger.info(f"🎯 TRIAL {trial_number}/{n_trials}")
            logger.info(f"{'='*60}")

            try:
                # Get trial from Optuna and sample parameters
                trial = study.ask()
                params = self.sample_minimal_parameters(trial)
                logger.info(f"Trial {trial.number}: 🎲 Parameters sampled by Optuna")

                # Execute single trial - this blocks until request completes
                objective_value = self.execute_single_trial(trial, params)

                logger.info(f"Trial {trial.number}: ✅ FULLY COMPLETED")

                # Tell Optuna the result
                study.tell(trial, objective_value)

                # Show current best
                if study.best_trial:
                    logger.info(f"🏆 Current Best: Score={study.best_value:.4f}")
                    logger.info(f"🎯 Best Params: {study.best_params}")

            except Exception as e:
                logger.error(f"Trial {trial_number}: 💥 Failed completely: {str(e)}")
                continue

        # Save results using shared function
        self.save_optimization_summary(study, execution_mode="sequential")

    def run_concurrent_batch_optimization(self, n_trials: int = 4, batch_size: int = 2):
        """Run optimization with concurrent batching - process multiple requests simultaneously"""
        logger.info(f"🚀 Starting CONCURRENT BATCH optimization")
        logger.info(f"📊 Total trials: {n_trials}, Batch size: {batch_size}")
        logger.info(f"🔄 Each batch waits for ALL {batch_size} requests to complete before next batch")

        study = optuna.create_study(direction="maximize")

        # Process trials in batches
        for batch_start in range(0, n_trials, batch_size):
            batch_end = min(batch_start + batch_size, n_trials)
            current_batch_size = batch_end - batch_start
            batch_number = (batch_start // batch_size) + 1
            total_batches = (n_trials + batch_size - 1) // batch_size

            logger.info(f"\n{'='*60}")
            logger.info(f"📦 BATCH {batch_number}/{total_batches} - Processing {current_batch_size} trials concurrently")
            logger.info(f"{'='*60}")

            # Prepare batch of trials
            trial_params_list = []
            for i in range(current_batch_size):
                trial = study.ask()
                params = self.sample_minimal_parameters(trial)
                trial_params_list.append((trial, params))
                logger.info(f"Trial {trial.number}: 🎲 Sampled parameters: {params}")

            # Execute batch concurrently - now returns objective scores directly
            logger.info(f"📦 Executing batch {batch_number} with {current_batch_size} concurrent requests...")
            objective_scores = self.execute_concurrent_batch(trial_params_list)

            # Tell Optuna the results
            for i, (trial, params) in enumerate(trial_params_list):
                objective_value = objective_scores[i]
                study.tell(trial, objective_value)

            # Show batch summary
            if study.best_trial:
                logger.info(f"🏆 Batch {batch_number} Best: Score={study.best_value:.4f}")
                logger.info(f"🎯 Current Best Params: {study.best_params}")

            logger.info(f"✅ Batch {batch_number} completed - All {current_batch_size} requests finished")

        # Save final summary using shared function
        self.save_optimization_summary(study, execution_mode="concurrent_batch", batch_size=batch_size)

    def close(self):
        """Cleanup"""
        self.session.close()

def main():
    """Main function to run optimization - choose mode"""
    import sys

    optimizer = MinimalOptimizer()

    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--concurrent":
            # Test concurrent batch mode
            optimizer.run_concurrent_batch_optimization(n_trials=4, batch_size=2)
        else:
            # Default: sequential mode
            optimizer.run_sequential_optimization(n_trials=3)
    finally:
        optimizer.close()

if __name__ == "__main__":
    main()