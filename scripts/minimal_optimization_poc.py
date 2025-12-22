#!/usr/bin/env python3
"""
Minimal POC for Bayesian Optimization with rate limiting
Tests the backtesting endpoint with controlled request frequency
"""

import asyncio
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
REQUEST_DELAY = 2.0  # 2 seconds between requests
TIMEOUT = 120.0  # 2 minutes timeout per request
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

    def run_single_backtest(self, params: dict, trial_number: int) -> dict:
        """Run a single backtest with rate limiting and retries"""
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

        logger.info(f"Trial {trial_number}: Starting backtest with params: {params}")

        for retry in range(MAX_RETRIES + 1):
            try:
                # Add delay before each request (except first trial)
                if trial_number > 0 or retry > 0:
                    logger.info(f"Trial {trial_number}: Waiting {REQUEST_DELAY}s before request...")
                    time.sleep(REQUEST_DELAY)

                logger.info(f"Trial {trial_number}: Sending POST request to /backtesting/run-backtesting")

                response = self.session.post(
                    f"{API_BASE_URL}/backtesting/run-backtesting",
                    json=backtest_config,
                    timeout=TIMEOUT
                )

                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Trial {trial_number}: Backtest completed successfully")
                    logger.debug(f"Trial {trial_number}: Response keys: {list(result.keys())}")
                    return result
                else:
                    logger.warning(f"Trial {trial_number}: HTTP {response.status_code}: {response.text}")
                    if retry < MAX_RETRIES:
                        wait_time = (retry + 1) * 5  # 5, 10, 15 seconds
                        logger.info(f"Trial {trial_number}: Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        return {"error": f"HTTP {response.status_code}: {response.text}"}

            except requests.exceptions.Timeout:
                logger.error(f"Trial {trial_number}: Request timeout (retry {retry}/{MAX_RETRIES})")
                if retry < MAX_RETRIES:
                    wait_time = (retry + 1) * 10  # 10, 20, 30 seconds
                    logger.info(f"Trial {trial_number}: Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    return {"error": "Request timeout"}

            except Exception as e:
                logger.error(f"Trial {trial_number}: Request failed: {str(e)}")
                if retry < MAX_RETRIES:
                    wait_time = (retry + 1) * 5
                    logger.info(f"Trial {trial_number}: Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    return {"error": str(e)}

        return {"error": "Max retries exceeded"}

    def objective_function(self, trial: optuna.Trial) -> float:
        """Objective function with proper error handling"""
        trial_start = time.time()

        try:
            # Sample minimal parameters
            params = self.sample_minimal_parameters(trial)

            # Run backtest
            result = self.run_single_backtest(params, trial.number)

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
            total_trades = int(results.get("total_orders", 0))

            # Simple objective: just use PnL normalized
            score = max(-1.0, min(1.0, pnl / 100.0))  # Normalize to [-1, 1]

            execution_time = time.time() - trial_start

            # Save result
            result_data = {
                'trial_number': trial.number,
                'params': params,
                'pnl': pnl,
                'total_trades': total_trades,
                'score': score,
                'execution_time': execution_time,
                'timestamp': datetime.now().isoformat()
            }

            # Save to file
            filename = f"trial_{trial.number:03d}.json"
            with open(self.results_dir / filename, 'w') as f:
                json.dump(result_data, f, indent=2)

            logger.info(f"Trial {trial.number} completed: Score={score:.4f}, PnL={pnl:.2f}, Time={execution_time:.1f}s")
            return score

        except Exception as e:
            logger.error(f"Trial {trial.number} error: {str(e)}")
            return 0.0

    def run_optimization(self, n_trials: int = 3):
        """Run minimal optimization with just a few trials"""
        logger.info(f"Starting minimal optimization with {n_trials} trials")
        logger.info(f"Request delay: {REQUEST_DELAY}s, Timeout: {TIMEOUT}s")

        # Create simple study
        study = optuna.create_study(direction="maximize")

        for trial_num in range(n_trials):
            logger.info(f"\n=== Starting Trial {trial_num + 1}/{n_trials} ===")

            try:
                trial = study.ask()
                objective_value = self.objective_function(trial)
                study.tell(trial, objective_value)

                if study.best_trial:
                    logger.info(f"Best so far: Score={study.best_value:.4f}, Params={study.best_params}")

            except Exception as e:
                logger.error(f"Trial {trial_num} failed completely: {str(e)}")
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
    """Main function to run the minimal optimization"""
    optimizer = MinimalOptimizer()

    try:
        # Test with just 3 trials first
        optimizer.run_optimization(n_trials=3)
    finally:
        optimizer.close()

if __name__ == "__main__":
    main()