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
from typing import List, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
API_BASE_URL = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin"
TIMEOUT = 300.0  # 5 minutes timeout per request
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
        self.results_dir = Path("minimal_optimization_results")
        self.results_dir.mkdir(exist_ok=True)

        logger.info(f"Initialized with auth header for user: {USERNAME}")

        # Simple base config for testing - using bollinger_dynamic_bb_grid_v6 for the 3 BB stage parameters
        self.base_config = {
            'controller_name': 'bollinger_dynamic_bb_grid_v6',
            'controller_type': 'dynamic_bb_grid',
            'connector_name': 'binance_perpetual',
            'trading_pair': 'BTC-USDT',
            'candles_connector': 'binance_perpetual',
            'candles_trading_pair': 'BTC-USDT',
            'interval': '1m',
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
            'accumulate_pct': 0.0025,
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

    def get_test_period(self):
        """Get a consistent test period for all trials (1 week starting from Feb 1st, 2025)"""
        start_date = datetime(2025, 12, 1)  # Feb 1st, 2025
        end_date = start_date + timedelta(weeks=2)  # One week period

        start_timestamp = int(start_date.timestamp())
        end_timestamp = int(end_date.timestamp())

        return start_timestamp, end_timestamp

    def execute_backtest_request(self, params: dict, trial_number: int) -> dict:
        """Execute a single backtest request and wait for actual completion"""
        start_time, end_time = self.get_test_period()

        # Create config
        config = self.base_config.copy()
        config.update(params)

        backtest_config = {
            'start_time': start_time,
            'end_time': end_time,
            'backtesting_resolution': '1m',
            'trade_cost': 0.0006,
            'config': config
        }

        logger.info(f"Trial {trial_number}: Executing backtest with {len(params)} optimized parameters: {list(params.keys())}")

        for retry in range(MAX_RETRIES + 1):
            try:
                logger.info(f"Trial {trial_number}: Sending request to backend (attempt {retry + 1}/{MAX_RETRIES + 1})")
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
                    logger.info(f"Trial {trial_number}: ✅ Request completed in {request_duration:.2f}s - Backend fully processed and ready")
                    return result
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    logger.warning(f"Trial {trial_number}: ❌ Request failed - {error_msg}")
                    if retry < MAX_RETRIES:
                        logger.info(f"Trial {trial_number}: Retrying immediately...")
                        continue
                    else:
                        return {"error": error_msg}

            except requests.exceptions.Timeout:
                logger.error(f"Trial {trial_number}: ⏰ Request timeout after {TIMEOUT}s")
                if retry < MAX_RETRIES:
                    logger.info(f"Trial {trial_number}: Retrying immediately...")
                    continue
                else:
                    return {"error": "Request timeout"}

            except Exception as e:
                logger.error(f"Trial {trial_number}: 💥 Request failed: {str(e)}")
                if retry < MAX_RETRIES:
                    logger.info(f"Trial {trial_number}: Retrying immediately...")
                    continue
                else:
                    return {"error": str(e)}

        return {"error": "Max retries exceeded"}

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

    def process_trial_result(self, trial: optuna.Trial, params: dict, response_data: dict, execution_time: float) -> float:
        """Process trial result and return objective score - shared logic"""
        try:
            if "error" in response_data:
                logger.error(f"Trial {trial.number}: ❌ Failed - {response_data['error']}")
                return 0.0

            # Check if result has the expected structure
            if "results" not in response_data:
                logger.error(f"Trial {trial.number}: ❌ Unexpected response structure: {response_data}")
                return 0.0

            # Extract results
            results = response_data.get("results", {})
            logger.info(f"result is {results}")
            pnl = float(results.get("net_pnl", 0))
            accuracy = float(response_data.get("accuracy", 0))
            total_trades = int(results.get("total_orders", 0))

            # Simple objective: just use PnL
            score = pnl

            # Save result
            result_data = {
                'trial_number': trial.number,
                'params': params,
                'pnl': pnl,
                'accuracy': accuracy,
                'total_trades': total_trades,
                'score': score,
                'execution_time': execution_time,
                'timestamp': datetime.now().isoformat()
            }

            # Save to file
            filename = f"trial_{trial.number:03d}.json"
            with open(self.results_dir / filename, 'w') as f:
                json.dump(result_data, f, indent=2)

            logger.info(f"Trial {trial.number}: ✅ Score={score:.4f}, PnL={pnl:.4f}, Accuracy={accuracy:.4f}")
            return score

        except Exception as e:
            logger.error(f"Trial {trial.number}: 💥 Processing error: {str(e)}")
            return 0.0

    def execute_single_trial(self, trial: optuna.Trial, params: dict) -> float:
        """Execute a single trial and return objective score - shared logic"""
        trial_start_time = time.time()

        logger.info(f"Trial {trial.number}: Sampled parameters: {params}")

        # Execute backtest and wait for completion
        response_data = self.execute_backtest_request(params, trial.number)
        execution_time = time.time() - trial_start_time

        # Process result and return score
        return self.process_trial_result(trial, params, response_data, execution_time)

    def save_optimization_summary(self, study: optuna.Study, execution_mode: str = "sequential", batch_size: int = 1):
        """Save final optimization summary - shared logic"""
        if study.best_trial:
            logger.info(f"\n=== {execution_mode.title()} Optimization Completed ===")
            logger.info(f"🏆 Best Score: {study.best_value:.4f}")
            logger.info(f"🎯 Best Params: {study.best_params}")

            # Get completed trials
            completed_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]

            # Load trial data with accuracy from saved files
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
                            'accuracy': trial_data.get('accuracy', 0),
                            'pnl': trial_data.get('pnl', 0),
                            'total_trades': trial_data.get('total_trades', 0)
                        })
                    except Exception as e:
                        logger.warning(f"Could not load data for trial {trial.number}: {e}")

            # Top 5 by score (PnL)
            top_5_by_score = sorted(trials_with_data, key=lambda t: t['score'], reverse=True)[:5]
            top_5_score_results = []
            logger.info(f"\n🏆 Top 5 by Score (PnL):")
            for i, trial_data in enumerate(top_5_by_score, 1):
                top_5_score_results.append({
                    'rank': i,
                    'trial_number': trial_data['trial_number'],
                    'score': trial_data['score'],
                    'accuracy': trial_data['accuracy'],
                    'params': trial_data['params']
                })
                logger.info(f"🥇 Rank {i}: Trial {trial_data['trial_number']}, Score={trial_data['score']:.4f}, Accuracy={trial_data['accuracy']:.4f}")

            # Top 5 by accuracy
            top_5_by_accuracy = sorted(trials_with_data, key=lambda t: t['accuracy'], reverse=True)[:5]
            top_5_accuracy_results = []
            logger.info(f"\n🎯 Top 5 by Accuracy:")
            for i, trial_data in enumerate(top_5_by_accuracy, 1):
                top_5_accuracy_results.append({
                    'rank': i,
                    'trial_number': trial_data['trial_number'],
                    'accuracy': trial_data['accuracy'],
                    'score': trial_data['score'],
                    'params': trial_data['params']
                })
                logger.info(f"🎯 Rank {i}: Trial {trial_data['trial_number']}, Accuracy={trial_data['accuracy']:.4f}, Score={trial_data['score']:.4f}")

            summary = {
                'best_score': study.best_value,
                'best_params': study.best_params,
                'top_5_by_score': top_5_score_results,
                'top_5_by_accuracy': top_5_accuracy_results,
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
        """Run optimization with TRUE sequential execution - no artificial delays"""
        logger.info(f"🚀 Starting TRUE SEQUENTIAL optimization with {n_trials} trials")
        logger.info(f"🔄 Each trial waits for ACTUAL completion of previous trial")
        logger.info(f"⏱️  No artificial delays - pure request completion detection")

        study = optuna.create_study(direction="maximize")

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
                logger.info(f"Trial {trial.number}: 🔄 Starting execution (will block until complete)...")
                objective_value = self.execute_single_trial(trial, params)

                # Only reaches here when request is FULLY complete
                logger.info(f"Trial {trial.number}: ✅ FULLY COMPLETED - Backend ready for next trial")

                # Tell Optuna the result
                study.tell(trial, objective_value)

                # Show current best
                if study.best_trial:
                    logger.info(f"🏆 Current Best: Score={study.best_value:.4f}")
                    logger.info(f"🎯 Best Params: {study.best_params}")

                # Ready for next trial immediately - no delays needed
                if trial_number < n_trials:
                    logger.info(f"✅ Trial {trial.number} complete - Backend ready for next trial")

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