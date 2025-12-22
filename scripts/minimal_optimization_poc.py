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
from datetime import datetime, timedelta
from pathlib import Path
import json

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
            # Fixed entry parameters (not optimized in POC)
            'normal_entry_normal': -0.05,
            'normal_entry_follow': 0.0,
            'normal_entry_anti': 0.05,
            'breakthrough_entry_normal': -0.03,
            'breakthrough_entry_follow': 0.0,
            'breakthrough_entry_anti': 0.03,
            'fallback_entry_normal': -0.01,
            'fallback_entry_follow': 0.0,
            'fallback_entry_anti': 0.01,
            'macd_threshold': 0.05,
        }

    def sample_minimal_parameters(self, trial: optuna.Trial) -> dict:
        """Sample only the 3 BB stage parameters for POC"""
        return {
            'normal_stage_bb': trial.suggest_float('normal_stage_bb', -0.8, 1.0),
            'breakthrough_stage_bb': trial.suggest_float('breakthrough_stage_bb', -0.1, 0.1),
            'fallback_stage_bb': trial.suggest_float('fallback_stage_bb', -0.1, 0.1),
        }

    def get_test_period(self):
        """Get a short test period (1 day in January 2025)"""
        start_date = datetime(2025, 10, 15)  # Jan 15, 2025
        end_date = start_date + timedelta(hours=72)  # 24 hours

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

        logger.info(f"Trial {trial_number}: Executing backtest with {len(params)} optimized parameters")

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

    def objective_function(self, trial: optuna.Trial) -> float:
        """Objective function that waits for actual request completion"""
        trial_start_time = time.time()

        try:
            # Sample the 3 BB stage parameters
            params = self.sample_minimal_parameters(trial)
            logger.info(f"Trial {trial.number}: Sampled parameters: {params}")

            # Execute backtest and wait for actual completion
            result = self.execute_backtest_request(params, trial.number)

            if "error" in result:
                logger.error(f"Trial {trial.number} failed: {result['error']}")
                return 0.0

            # Check if result has the expected structure
            if "results" not in result:
                logger.error(f"Trial {trial.number} - Unexpected response structure: {result}")
                return 0.0

            # Extract results
            results = result.get("results", {})
            pnl = float(results.get("net_pnl", 0))
            accuracy = float(result.get("accuracy", 0))
            total_trades = int(results.get("total_orders", 0))

            # Simple objective: just use PnL normalized
            score = pnl  # already normalized

            execution_time = time.time() - trial_start_time

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

            logger.info(f"Trial {trial.number}: ✅ Completed in {execution_time:.1f}s - Score={score:.4f}, PnL={pnl:.2f}")
            return score

        except Exception as e:
            logger.error(f"Trial {trial.number}: 💥 Error: {str(e)}")
            return 0.0

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
                # Get trial from Optuna
                trial = study.ask()
                logger.info(f"Trial {trial.number}: 🎲 Parameters sampled by Optuna")

                # Execute objective function - this blocks until request completes
                logger.info(f"Trial {trial.number}: 🔄 Starting execution (will block until complete)...")
                objective_value = self.objective_function(trial)

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

        # Final results
        if study.best_trial:
            logger.info(f"\n=== Optimization Completed ===")
            logger.info(f"Best Score: {study.best_value:.4f}")
            logger.info(f"Best Params: {study.best_params}")

            # Save final summary
            summary = {
                'best_score': study.best_value,
                'best_params': study.best_params,
                'total_trials': len(study.trials),
                'completed_at': datetime.now().isoformat()
            }

            with open(self.results_dir / "optimization_summary.json", 'w') as f:
                json.dump(summary, f, indent=2)
        else:
            logger.warning("No trials completed successfully")

    def close(self):
        """Cleanup"""
        self.session.close()

def main():
    """Main function to run the TRUE sequential optimization"""
    optimizer = MinimalOptimizer()

    try:
        # Test with 3 trials - true sequential execution
        optimizer.run_sequential_optimization(n_trials=3)
    finally:
        optimizer.close()

if __name__ == "__main__":
    main()