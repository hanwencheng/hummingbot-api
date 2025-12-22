"""
Bayesian Optimization Service for Trading Strategy Parameters

Optimizes BollingerDynamicBBGridV6Config parameters using Optuna TPE sampler
with constrained parameter sampling and monthly statistical analysis.
"""

import optuna
import asyncio
import httpx
import json
import time
import base64
from optuna.storages import InMemoryStorage
from optuna.trial import TrialState

# Optional dependencies for optimization
try:
    import numpy as np
    import pandas as pd
    from scipy import stats
    import matplotlib.pyplot as plt
    import seaborn as sns
    HAS_SCIENTIFIC_LIBS = True
except ImportError:
    # Mock numpy for basic functionality
    class MockNumPy:
        def mean(self, values):
            return sum(values) / len(values) if values else 0
    np = MockNumPy()
    pd = None
    stats = None
    plt = None
    sns = None
    HAS_SCIENTIFIC_LIBS = False

from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Union, Any
from dataclasses import dataclass, asdict
from decimal import Decimal
import logging
from pathlib import Path
import warnings

# Suppress optuna warnings for cleaner output
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)

logger = logging.getLogger(__name__)

# Constants
DEFAULT_PNL_NORMALIZATION_FACTOR = 500.0
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_EARLY_STOPPING_PATIENCE_RATIO = 5
DEFAULT_SAVE_INTERVAL = 10
MIN_TRIALS_FOR_IMPORTANCE = 10
MIN_TRIALS_FOR_PARALLEL_PLOT = 5
MAX_PARAMS_FOR_PARALLEL_PLOT = 8
TOP_PARAMS_FOR_SLICE_PLOT = 3
OBJECTIVE_PNL_WEIGHT = 0.5
OBJECTIVE_WIN_RATE_WEIGHT = 0.5
MIN_TRADES_FOR_WIN_RATE = 1

def generate_auth_header(username: str, password: str) -> str:
    """Generate Basic Auth header for API authentication"""
    auth_str = f"{username}:{password}"
    auth_bytes = auth_str.encode('utf-8')
    base64_encoded = base64.b64encode(auth_bytes).decode('utf-8')
    return f"Basic {base64_encoded}"


@dataclass
class OptimizationResult:
    """Results from a single optimization trial"""
    trial_number: int
    params: Dict
    pnl: float
    win_rate: float
    total_trades: int
    objective_score: float
    execution_time: float
    trading_pair: str
    month: str
    start_time: int
    end_time: int
    error: Optional[str] = None

class ConstrainedParameterSampler:
    """
    Handles constrained parameter sampling with Optuna
    Enforces ordering constraints for entry parameters
    """

    def __init__(self):
        self.parameter_ranges = {
            'normal_stage_bb': (-0.8, 1.0),
            'normal_entry_normal': (-0.1, 0.1),
            'normal_entry_follow': (-0.1, 0.1),
            'normal_entry_anti': (-0.1, 0.1),
            'breakthrough_stage_bb': (-0.1, 0.1),
            'breakthrough_entry_normal': (-0.1, 0.1),
            'breakthrough_entry_follow': (-0.1, 0.1),
            'breakthrough_entry_anti': (-0.1, 0.1),
            'fallback_stage_bb': (-0.1, 0.1),
            'fallback_entry_normal': (-0.1, 0.1),
            'fallback_entry_follow': (-0.1, 0.1),
            'fallback_entry_anti': (-0.1, 0.1),
            'macd_threshold': (0.0, 0.1),
        }

    def sample_parameters(self, trial: optuna.Trial) -> Dict:
        """
        Sample parameters with ordering constraints enforced.
        
        Args:
            trial: Optuna trial object
            
        Returns:
            Dictionary of sampled parameters
            
        Raises:
            optuna.TrialPruned: If cross-stage ordering constraint is violated
        """
        params = {}

        # Sample unconstrained parameters first
        params['normal_stage_bb'] = trial.suggest_float('normal_stage_bb', -0.8, 1.0)
        params['breakthrough_stage_bb'] = trial.suggest_float('breakthrough_stage_bb', -0.1, 0.1)
        params['fallback_stage_bb'] = trial.suggest_float('fallback_stage_bb', -0.1, 0.1)
        params['macd_threshold'] = trial.suggest_float('macd_threshold', 0.0, 0.1)

        # Sample normal stage entry parameters with ordering constraint
        # normal_entry_normal < normal_entry_follow < normal_entry_anti
        normal_entry_normal = trial.suggest_float('normal_entry_normal', -0.1, 0.1)
        normal_entry_follow = trial.suggest_float('normal_entry_follow', normal_entry_normal, 0.1)
        normal_entry_anti = trial.suggest_float('normal_entry_anti', normal_entry_follow, 0.1)

        params.update({
            'normal_entry_normal': normal_entry_normal,
            'normal_entry_follow': normal_entry_follow,
            'normal_entry_anti': normal_entry_anti,
        })

        # Sample breakthrough stage entry parameters with ordering constraint
        # Ensure breakthrough_entry_normal > normal_entry_normal for cross-stage constraint
        # Use a small epsilon to ensure strict inequality
        epsilon = 0.0001
        breakthrough_lower = max(normal_entry_normal + epsilon, -0.1)
        breakthrough_upper = 0.1
        
        # Check if there's valid range for sampling
        if breakthrough_lower >= breakthrough_upper:
            raise optuna.TrialPruned("Insufficient range for breakthrough_entry_normal")
        
        breakthrough_entry_normal = trial.suggest_float(
            'breakthrough_entry_normal',
            breakthrough_lower,
            breakthrough_upper
        )
        breakthrough_entry_follow = trial.suggest_float('breakthrough_entry_follow', breakthrough_entry_normal, 0.1)
        breakthrough_entry_anti = trial.suggest_float('breakthrough_entry_anti', breakthrough_entry_follow, 0.1)

        params.update({
            'breakthrough_entry_normal': breakthrough_entry_normal,
            'breakthrough_entry_follow': breakthrough_entry_follow,
            'breakthrough_entry_anti': breakthrough_entry_anti,
        })

        # Sample fallback stage entry parameters with ordering constraint
        # Ensure fallback_entry_normal > breakthrough_entry_normal for cross-stage constraint
        fallback_lower = max(breakthrough_entry_normal + epsilon, -0.1)
        fallback_upper = 0.1
        
        # Check if there's valid range for sampling
        if fallback_lower >= fallback_upper:
            raise optuna.TrialPruned("Insufficient range for fallback_entry_normal")
        
        fallback_entry_normal = trial.suggest_float(
            'fallback_entry_normal',
            fallback_lower,
            fallback_upper
        )
        fallback_entry_follow = trial.suggest_float('fallback_entry_follow', fallback_entry_normal, 0.1)
        fallback_entry_anti = trial.suggest_float('fallback_entry_anti', fallback_entry_follow, 0.1)

        params.update({
            'fallback_entry_normal': fallback_entry_normal,
            'fallback_entry_follow': fallback_entry_follow,
            'fallback_entry_anti': fallback_entry_anti,
        })

        # Verify cross-stage ordering constraint (should always be satisfied now)
        if not (normal_entry_normal < breakthrough_entry_normal < fallback_entry_normal):
            # This should rarely happen now, but keep as safety check
            raise optuna.TrialPruned("Cross-stage ordering constraint violated")

        return params

    def validate_constraints(self, params: Dict) -> bool:
        """
        Validate all parameter constraints.
        
        Args:
            params: Dictionary of parameters to validate
            
        Returns:
            True if all constraints are satisfied, False otherwise
        """
        try:
            # Check ordering within each stage
            if not (params['normal_entry_normal'] < params['normal_entry_follow'] < params['normal_entry_anti']):
                return False
            if not (params['breakthrough_entry_normal'] < params['breakthrough_entry_follow'] < params['breakthrough_entry_anti']):
                return False
            if not (params['fallback_entry_normal'] < params['fallback_entry_follow'] < params['fallback_entry_anti']):
                return False

            # Check cross-stage ordering
            if not (params['normal_entry_normal'] < params['breakthrough_entry_normal'] < params['fallback_entry_normal']):
                return False

            return True
        except KeyError:
            return False

class BayesianOptimizer:
    """
    Main Bayesian optimization engine for trading strategy parameters
    """

    def __init__(self, api_base_url: str = "http://localhost:8000", results_dir: str = "optimization_results",
                 username: str = "admin", password: str = "admin"):
        self.api_base_url = api_base_url
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(exist_ok=True)

        # Setup authentication
        self.auth_header = generate_auth_header(username, password)
        self.headers = {
            'Authorization': self.auth_header,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        self.client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self.parameter_sampler = ConstrainedParameterSampler()
        self.results_cache = []

        # Base configuration template
        self.base_config = {
            'controller_name': 'bollinger_dynamic_bb_grid_v6',
            'controller_type': 'dynamic_bb_grid',
            'connector_name': 'hyperliquid_perpetual',
            'trading_pair': 'HYPE-USD',  # Will be updated per coin
            'candles_connector': 'binance_perpetual',
            'candles_trading_pair': 'HYPE-USDT',  # Will be updated per coin
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

    def get_coin_mapping(self, coin: str) -> Tuple[str, str]:
        """
        Get trading pair mappings for different coins.
        
        Args:
            coin: Coin symbol
            
        Returns:
            Tuple of (connector_trading_pair, candles_trading_pair)
        """
        coin_mappings = {
            'BTC': ('BTC-USD', 'BTC-USDT'),
            'ETH': ('ETH-USD', 'ETH-USDT'),
            'SOL': ('SOL-USD', 'SOL-USDT'),
            'HYPE': ('HYPE-USD', 'HYPE-USDT'),
            'XRP': ('XRP-USD', 'XRP-USDT'),
            'ADA': ('ADA-USD', 'ADA-USDT'),
            'DOT': ('DOT-USD', 'DOT-USDT'),
            'AVAX': ('AVAX-USD', 'AVAX-USDT'),
            'UNI': ('UNI-USD', 'UNI-USDT'),
            'ZEC': ('ZEC-USD', 'ZEC-USDT'),
            'BNB': ('BNB-USD', 'BNB-USDT'),
            'DOGE': ('DOGE-USD', 'DOGE-USDT'),
            'BCH': ('BCH-USD', 'BCH-USDT'),
            'PEPE': ('PEPE-USD', 'PEPE-USDT'),
            'SUI': ('SUI-USD', 'SUI-USDT'),
            'ENA': ('ENA-USD', 'ENA-USDT'),
        }

        return coin_mappings.get(coin, (f'{coin}-USD', f'{coin}-USDT'))

    def get_monthly_periods(self, year: int = 2025) -> List[Tuple[str, int, int]]:
        """
        Generate monthly time periods for backtesting.
        
        Args:
            year: Year for which to generate periods
            
        Returns:
            List of tuples (month_name, start_timestamp, end_timestamp)
        """
        months = []
        for month in range(1, 12):  # Jan to Nov
            start_date = datetime(year, month, 1)
            if month == 11:
                end_date = datetime(year, month + 1, 30)  # End of November
            else:
                end_date = datetime(year, month + 1, 1) - timedelta(days=1)

            month_name = start_date.strftime('%Y-%m')
            start_timestamp = int(start_date.timestamp())
            end_timestamp = int(end_date.timestamp())

            months.append((month_name, start_timestamp, end_timestamp))

        return months

    async def objective_function(
        self,
        trial: optuna.Trial,
        coin: str,
        month: str,
        start_time: int,
        end_time: int
    ) -> float:
        """
        Objective function for a single trial.
        
        Args:
            trial: Optuna trial object
            coin: Coin symbol
            month: Month identifier
            start_time: Start timestamp for backtest
            end_time: End timestamp for backtest
            
        Returns:
            Combined objective score (weighted PnL + win rate)
        """
        start_exec = time.time()

        try:
            # Sample parameters with constraints
            optimizable_params = self.parameter_sampler.sample_parameters(trial)

            # Create full configuration
            config = self.create_backtest_config(optimizable_params, coin, start_time, end_time)

            # Run backtest
            result = await self.run_backtest(config)
            execution_time = time.time() - start_exec

            if "error" in result:
                logger.error(f"Backtest error for trial {trial.number}: {result['error']}")
                return 0.0

            # Extract metrics
            results = result.get("results", {})
            pnl = float(results.get("net_pnl", 0))
            total_trades = max(int(results.get("total_orders", 0)), MIN_TRADES_FOR_WIN_RATE)
            winning_trades = int(results.get("total_winning_trades", 0))
            win_rate = winning_trades / total_trades

            # Normalize PnL to [-1, 1] range
            normalized_pnl = max(-1.0, min(1.0, pnl / DEFAULT_PNL_NORMALIZATION_FACTOR))

            # Combined objective (weighted PnL + win rate)
            objective_score = (
                OBJECTIVE_PNL_WEIGHT * normalized_pnl +
                OBJECTIVE_WIN_RATE_WEIGHT * win_rate
            )

            # Store result
            opt_result = OptimizationResult(
                trial_number=trial.number,
                params=optimizable_params,
                pnl=pnl,
                win_rate=win_rate,
                total_trades=total_trades,
                objective_score=objective_score,
                execution_time=execution_time,
                trading_pair=coin,
                month=month,
                start_time=start_time,
                end_time=end_time
            )

            self.results_cache.append(opt_result)
            await self.save_result(opt_result)

            logger.info(f"Trial {trial.number} completed: Score={objective_score:.4f}, PnL={pnl:.2f}, WinRate={win_rate:.3f}")

            return objective_score

        except optuna.TrialPruned:
            # Pruned trials are expected and should be logged at debug level
            logger.debug(f"Trial {trial.number} pruned: constraint violated")
            raise  # Re-raise pruned trials
        except Exception as e:
            logger.error(f"Trial {trial.number} failed: {str(e)}")
            return 0.0

    def create_backtest_config(
        self,
        optimizable_params: Dict,
        coin: str,
        start_time: int,
        end_time: int
    ) -> Dict:
        """
        Create complete backtest configuration.
        
        Args:
            optimizable_params: Dictionary of parameters to optimize
            coin: Coin symbol
            start_time: Start timestamp for backtest
            end_time: End timestamp for backtest
            
        Returns:
            Complete backtest configuration dictionary
        """
        connector_pair, candles_pair = self.get_coin_mapping(coin)

        config = self.base_config.copy()
        config.update({
            'trading_pair': connector_pair,
            'candles_trading_pair': candles_pair,
        })
        config.update(optimizable_params)

        return {
            'start_time': start_time,
            'end_time': end_time,
            'backtesting_resolution': '1m',
            'trade_cost': 0.0006,
            'config': config
        }

    async def run_backtest(self, config: Dict) -> Dict:
        """
        Run single backtest via API.
        
        Args:
            config: Backtest configuration dictionary
            
        Returns:
            Dictionary containing backtest results or error
        """
        try:
            response = await self.client.post(
                f"{self.api_base_url}/backtesting/run-backtesting",
                json=config,
                headers=self.headers,
                timeout=DEFAULT_TIMEOUT_SECONDS
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}

    async def save_result(self, result: OptimizationResult):
        """
        Save optimization result to file.
        
        Args:
            result: OptimizationResult object to save
        """
        filename = f"{result.trading_pair}_{result.month}_trial_{result.trial_number}.json"
        filepath = self.results_dir / filename

        with open(filepath, 'w') as f:
            json.dump(asdict(result), f, indent=2)

    async def optimize_coin_month(
        self,
        coin: str,
        month: str,
        start_time: int,
        end_time: int,
        n_trials: int = 100
    ) -> Dict:
        """
        Optimize parameters for a specific coin and month.
        
        Args:
            coin: Coin symbol
            month: Month identifier
            start_time: Start timestamp for backtest
            end_time: End timestamp for backtest
            n_trials: Number of optimization trials to run
            
        Returns:
            Dictionary containing optimization results
        """
        logger.info(f"Starting optimization for {coin} - {month}")

        # Create Optuna study with latest 4.6.0 configuration
        study_name = f"{coin}_{month}_optimization_{int(time.time())}"

        # Use in-memory storage with thread safety for better performance
        storage = InMemoryStorage()

        # Configure advanced TPE sampler with Optuna 4.6.0 features
        sampler = optuna.samplers.TPESampler(
            n_startup_trials=max(10, min(20, n_trials // 5)),
            n_ei_candidates=24,
            gamma=self._calculate_gamma(n_trials),
            seed=42,
            multivariate=True,
            group=True,
            warn_independent_sampling=False,
            constant_liar=True,
        )

        # Configure advanced pruner with percentile-based pruning
        pruner = optuna.pruners.PercentilePruner(
            percentile=25.0,
            n_startup_trials=max(5, n_trials // 10),
            n_warmup_steps=3,
            interval_steps=1,
        )

        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            pruner=pruner,
            study_name=study_name,
            storage=storage,
        )

        # Define objective function for this coin/month
        async def objective(trial):
            return await self.objective_function(trial, coin, month, start_time, end_time)

        # Run optimization with enhanced progress tracking
        best_score = float('-inf')
        no_improvement_count = 0
        early_stopping_patience = max(20, n_trials // DEFAULT_EARLY_STOPPING_PATIENCE_RATIO)

        for trial_num in range(n_trials):
            try:
                trial = study.ask()
                objective_value = await objective(trial)

                # Handle failed trials properly
                if objective_value is None:
                    study.tell(trial, state=TrialState.FAIL)
                elif HAS_SCIENTIFIC_LIBS:
                    try:
                        if np.isnan(objective_value):
                            study.tell(trial, state=TrialState.FAIL)
                        else:
                            study.tell(trial, objective_value)
                    except (TypeError, ValueError):
                        # objective_value might not be a number
                        study.tell(trial, state=TrialState.FAIL)
                else:
                    # Fallback: check if it's a valid number
                    try:
                        float(objective_value)
                        study.tell(trial, objective_value)
                    except (TypeError, ValueError):
                        study.tell(trial, state=TrialState.FAIL)

                    # Check for improvement (early stopping logic)
                    if objective_value > best_score:
                        best_score = objective_value
                        no_improvement_count = 0
                        logger.info(f"{coin} {month}: New best score {best_score:.4f} at trial {trial_num+1}")
                    else:
                        no_improvement_count += 1

                # Progress logging with more details
                progress = (trial_num + 1) / n_trials * 100
                current_best = study.best_value if study.best_trial else 0.0
                logger.info(f"{coin} {month}: Trial {trial_num+1}/{n_trials} ({progress:.1f}%) - Current best: {current_best:.4f}")

                # Save intermediate results periodically or on improvement
                if (trial_num + 1) % DEFAULT_SAVE_INTERVAL == 0 or no_improvement_count == 0:
                    try:
                        await self.save_study_results(study, coin, month)
                    except Exception as save_error:
                        logger.warning(f"Failed to save study results: {save_error}")
                        # Don't fail the trial if saving fails

                # Early stopping if no improvement for too long
                if no_improvement_count >= early_stopping_patience:
                    logger.info(f"{coin} {month}: Early stopping after {trial_num+1} trials (no improvement for {no_improvement_count} trials)")
                    break

            except KeyboardInterrupt:
                logger.info(f"Optimization interrupted at trial {trial_num+1}")
                break
            except Exception as e:
                import traceback
                logger.error(f"Error in trial {trial_num+1}: {str(e)}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                # Mark trial as failed
                try:
                    study.tell(trial, state=TrialState.FAIL)
                except Exception as tell_error:
                    logger.warning(f"Failed to mark trial as failed: {tell_error}")
                continue

        # Save final results
        await self.save_study_results(study, coin, month)

        # Generate comprehensive results with Optuna 4.6.0 features
        results = {
            'coin': coin,
            'month': month,
            'best_params': study.best_params if study.best_trial else {},
            'best_value': study.best_value if study.best_trial else 0.0,
            'n_trials': len(study.trials),
            'completed_trials': len([t for t in study.trials if t.state == TrialState.COMPLETE]),
            'failed_trials': len([t for t in study.trials if t.state == TrialState.FAIL]),
            'pruned_trials': len([t for t in study.trials if t.state == TrialState.PRUNED]),
            'study_statistics': self._get_study_statistics(study),
            'parameter_importance': self._get_parameter_importance(study) if len(study.trials) >= MIN_TRIALS_FOR_IMPORTANCE else {},
            'optimization_history': self._get_optimization_history(study),
        }

        logger.info(f"Optimization completed for {coin} - {month}: Best score = {study.best_value:.4f}")
        return results

    async def save_study_results(self, study: optuna.Study, coin: str, month: str):
        """
        Save comprehensive Optuna study results with 4.6.0 features.
        
        Args:
            study: Optuna study object
            coin: Coin symbol
            month: Month identifier
        """
        all_trials_data = []
        for trial in study.trials:
            duration = None
            if trial.datetime_complete and trial.datetime_start:
                duration = (trial.datetime_complete - trial.datetime_start).total_seconds()
            
            all_trials_data.append({
                'number': trial.number,
                'value': trial.value,
                'params': trial.params,
                'state': trial.state.name,
                'duration': duration,
                'user_attrs': trial.user_attrs,
                'system_attrs': trial.system_attrs
            })

        # Safely get statistics with error handling
        try:
            study_statistics = self._get_study_statistics(study)
        except Exception as e:
            logger.warning(f"Error getting study statistics: {e}", exc_info=True)
            study_statistics = {}
        
        try:
            parameter_importance = self._get_parameter_importance(study) if len(study.trials) >= MIN_TRIALS_FOR_IMPORTANCE else {}
        except Exception as e:
            logger.warning(f"Error getting parameter importance: {e}")
            parameter_importance = {}
        
        try:
            optimization_history = self._get_optimization_history(study)
        except Exception as e:
            logger.warning(f"Error getting optimization history: {e}")
            optimization_history = {}
        
        study_data = {
            'coin': coin,
            'month': month,
            'best_params': study.best_params if study.best_trial else {},
            'best_value': study.best_value if study.best_trial else None,
            'best_trial_number': study.best_trial.number if study.best_trial else None,
            'n_trials': len(study.trials),
            'completed_trials': len([t for t in study.trials if t.state == TrialState.COMPLETE]),
            'failed_trials': len([t for t in study.trials if t.state == TrialState.FAIL]),
            'pruned_trials': len([t for t in study.trials if t.state == TrialState.PRUNED]),
            'study_statistics': study_statistics,
            'parameter_importance': parameter_importance,
            'optimization_history': optimization_history,
            'all_trials': all_trials_data,
            'sampler_state': study.sampler.__class__.__name__,
            'pruner_state': study.pruner.__class__.__name__,
            'creation_time': datetime.now().isoformat(),
        }

        filename = f"{coin}_{month}_study_results.json"
        filepath = self.results_dir / filename

        with open(filepath, 'w') as f:
            json.dump(study_data, f, indent=2, default=str)

        # Also save visualization if scientific libraries available
        if HAS_SCIENTIFIC_LIBS and study.best_trial:
            await self._save_optimization_plots(study, coin, month)

    async def optimize_single_coin_all_months(
        self,
        coin: str,
        n_trials: int = 100
    ) -> Dict:
        """
        Optimize a single coin across all months.
        
        Args:
            coin: Coin symbol
            n_trials: Number of optimization trials per month
            
        Returns:
            Dictionary containing results for all months
        """
        monthly_periods = self.get_monthly_periods(2025)
        all_results = {}

        logger.info(f"Starting optimization for {coin} across {len(monthly_periods)} months")

        for month, start_time, end_time in monthly_periods:
            try:
                result = await self.optimize_coin_month(
                    coin=coin,
                    month=month,
                    start_time=start_time,
                    end_time=end_time,
                    n_trials=n_trials
                )
                all_results[month] = result

                # Save progress
                await self.save_coin_summary(coin, all_results)

            except Exception as e:
                logger.error(f"Failed to optimize {coin} for {month}: {str(e)}")
                continue

        return all_results

    async def save_coin_summary(self, coin: str, results: Dict):
        """
        Save summary results for a coin.
        
        Args:
            coin: Coin symbol
            results: Dictionary of monthly optimization results
        """
        best_values = [data['best_value'] for data in results.values() if data.get('best_value') is not None]
        if not best_values:
            average_score = 0.0
        elif HAS_SCIENTIFIC_LIBS:
            try:
                import numpy as np_funcs
                average_score = float(np_funcs.mean(best_values))
            except Exception as e:
                logger.warning(f"Error calculating average with numpy: {e}, using fallback")
                average_score = sum(best_values) / len(best_values)
        else:
            average_score = sum(best_values) / len(best_values)

        summary = {
            'coin': coin,
            'total_months': len(results),
            'monthly_results': {
                month: {
                    'best_score': data['best_value'],
                    'best_params': data['best_params'],
                    'n_trials': data['n_trials']
                }
                for month, data in results.items()
            },
            'average_score': average_score,
            'timestamp': datetime.now().isoformat()
        }

        filename = f"{coin}_optimization_summary.json"
        filepath = self.results_dir / filename

        with open(filepath, 'w') as f:
            json.dump(summary, f, indent=2)

    def _calculate_gamma(self, n_trials: int) -> float:
        """
        Calculate dynamic gamma based on number of trials.
        
        Args:
            n_trials: Number of trials in the study
            
        Returns:
            Gamma value for TPE sampler (higher = more exploitation)
        """
        if n_trials <= 50:
            return 0.10
        elif n_trials <= 100:
            return 0.15
        elif n_trials <= 200:
            return 0.25
        else:
            return 0.35

    def _get_study_statistics(self, study: optuna.Study) -> Dict[str, Any]:
        """
        Extract comprehensive study statistics.
        
        Args:
            study: Optuna study object
            
        Returns:
            Dictionary containing statistical metrics
        """
        if not study.trials:
            return {}

        completed_trials = [t for t in study.trials if t.state == TrialState.COMPLETE]
        if not completed_trials:
            return {'status': 'no_completed_trials'}

        values = [t.value for t in completed_trials if t.value is not None]
        
        if not values:
            return {'status': 'no_valid_values'}

        # Calculate basic statistics
        if HAS_SCIENTIFIC_LIBS:
            try:
                # Ensure we're using numpy functions correctly
                import numpy as np_funcs
                stats = {
                    'mean_objective': float(np_funcs.mean(values)),
                    'std_objective': float(np_funcs.std(values)),
                    'min_objective': float(min(values)),
                    'max_objective': float(max(values)),
                    'median_objective': float(np_funcs.median(values)),
                    'q25_objective': float(np_funcs.percentile(values, 25)),
                    'q75_objective': float(np_funcs.percentile(values, 75)),
                    'iqr_objective': float(np_funcs.percentile(values, 75) - np_funcs.percentile(values, 25)),
                }
            except Exception as e:
                logger.warning(f"Error calculating statistics with numpy: {e}, using fallback")
                # Fallback to basic statistics
                sorted_values = sorted(values)
                stats = {
                    'mean_objective': sum(values) / len(values),
                    'std_objective': 0.0,
                    'min_objective': min(values),
                    'max_objective': max(values),
                    'median_objective': sorted_values[len(sorted_values) // 2],
                }
        else:
            sorted_values = sorted(values)
            stats = {
                'mean_objective': sum(values) / len(values),
                'std_objective': 0.0,
                'min_objective': min(values),
                'max_objective': max(values),
                'median_objective': sorted_values[len(sorted_values) // 2],
            }

        # Calculate improvement rate
        if len(values) > 1:
            improvements = sum(1 for i in range(1, len(values)) if values[i] > max(values[:i]))
            stats['improvement_rate'] = improvements / (len(values) - 1)

        return stats

    def _get_parameter_importance(self, study: optuna.Study) -> Dict[str, float]:
        """Calculate parameter importance using Optuna 4.6.0 features"""
        try:
            if len(study.trials) < MIN_TRIALS_FOR_IMPORTANCE:
                return {}

            # Use Optuna's built-in importance evaluator
            importance_evaluator = optuna.importance.FanovaImportanceEvaluator()
            importance = optuna.importance.get_param_importances(
                study, evaluator=importance_evaluator
            )
            return {k: float(v) for k, v in importance.items()}
        except Exception as e:
            logger.warning(f"Could not calculate parameter importance: {e}")
            return {}

    def _get_optimization_history(self, study: optuna.Study) -> Dict[str, List]:
        """
        Extract optimization history for plotting.
        
        Args:
            study: Optuna study object
            
        Returns:
            Dictionary containing trial history data
        """
        completed_trials = [t for t in study.trials if t.state == TrialState.COMPLETE]
        if not completed_trials:
            return {}

        trial_numbers = [t.number for t in completed_trials]
        values = [t.value for t in completed_trials]

        # Calculate best value so far at each trial
        best_values = []
        current_best = float('-inf')
        for value in values:
            current_best = max(current_best, value)
            best_values.append(current_best)

        timestamps = [
            t.datetime_complete.isoformat() if t.datetime_complete else None
            for t in completed_trials
        ]

        return {
            'trial_numbers': trial_numbers,
            'objective_values': values,
            'best_values_so_far': best_values,
            'timestamps': timestamps,
        }

    async def _save_optimization_plots(self, study: optuna.Study, coin: str, month: str):
        """
        Save optimization visualization plots (requires matplotlib).

        Args:
            study: Optuna study object
            coin: Coin symbol
            month: Month identifier
        """
        if not HAS_SCIENTIFIC_LIBS or not plt:
            return

        try:
            import optuna.visualization.matplotlib as vis

            # Create plots directory
            plots_dir = self.results_dir / "plots"
            plots_dir.mkdir(exist_ok=True)

            # 1. Optimization history plot
            ax1 = vis.plot_optimization_history(study)
            fig1 = ax1.get_figure()
            fig1.savefig(
                plots_dir / f"{coin}_{month}_optimization_history.png",
                dpi=300,
                bbox_inches='tight'
            )
            plt.close(fig1)

            # 2. Parameter importance plot
            if len(study.trials) >= MIN_TRIALS_FOR_IMPORTANCE:
                ax2 = vis.plot_param_importances(study)
                fig2 = ax2.get_figure()
                fig2.savefig(
                    plots_dir / f"{coin}_{month}_param_importance.png",
                    dpi=300,
                    bbox_inches='tight'
                )
                plt.close(fig2)

            # 3. Parallel coordinate plot
            if len(study.trials) >= MIN_TRIALS_FOR_PARALLEL_PLOT:
                param_keys = list(study.best_params.keys())[:MAX_PARAMS_FOR_PARALLEL_PLOT]
                ax3 = vis.plot_parallel_coordinate(study, params=param_keys)
                fig3 = ax3.get_figure()
                fig3.savefig(
                    plots_dir / f"{coin}_{month}_parallel_coordinate.png",
                    dpi=300,
                    bbox_inches='tight'
                )
                plt.close(fig3)

            # 4. Slice plot for top important parameters
            importance = self._get_parameter_importance(study)
            if importance:
                top_params = sorted(
                    importance.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:TOP_PARAMS_FOR_SLICE_PLOT]
                for param_name, _ in top_params:
                    ax4 = vis.plot_slice(study, params=[param_name])
                    fig4 = ax4.get_figure()
                    fig4.savefig(
                        plots_dir / f"{coin}_{month}_slice_{param_name}.png",
                        dpi=300,
                        bbox_inches='tight'
                    )
                    plt.close(fig4)

            logger.info(f"Saved optimization plots for {coin} {month}")

        except Exception as e:
            logger.warning(f"Could not save optimization plots: {e}")

    async def close(self):
        """
        Cleanup resources.
        
        Closes the HTTP client connection.
        """
        await self.client.aclose()