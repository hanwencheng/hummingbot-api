"""
Optimization Service for Direct Execution

Manages optimization tasks running directly in the API process.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from services.bayesian_optimizer import BayesianOptimizer

logger = logging.getLogger(__name__)


@dataclass
class OptimizationTask:
    """Represents an optimization task"""
    task_id: str
    status: str  # 'pending', 'starting', 'running', 'completed', 'failed', 'stopped'
    progress: float
    coin: str
    month: Optional[str] = None
    n_trials: int = 100
    start_time: float = 0
    end_time: float = 0
    results: Dict = None
    error: Optional[str] = None
    log_file: Optional[str] = None
    optimizer: Optional[BayesianOptimizer] = None
    optimization_task: Optional[asyncio.Task] = None


class OptimizationService:
    """
    Manages optimization tasks running directly in the API process
    """

    def __init__(self,
                 api_url: str = "http://localhost:8000",
                 results_dir: str = "optimization_results",
                 logs_dir: str = "optimization_logs"):
        self.api_url = api_url

        # Maintain local directories for results and logs
        self.results_dir = Path(results_dir)
        self.logs_dir = Path(logs_dir)
        self.results_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

        # Track active optimization tasks
        self.active_tasks: Dict[str, OptimizationTask] = {}

        # Task management settings
        self.max_concurrent_tasks = 8  # Limit concurrent optimization tasks
        self.task_prefix = "hummingbot-optimizer"

    async def start_optimization_container(self,
                                         coin: str,
                                         month: Optional[str] = None,
                                         start_time: Optional[int] = None,
                                         end_time: Optional[int] = None,
                                         n_trials: int = 100,
                                         optimization_type: str = "single_coin") -> str:
        """
        Start a new optimization task running directly in the API process

        Args:
            coin: Coin symbol to optimize
            month: Specific month for optimization (optional)
            start_time: Start timestamp for monthly optimization
            end_time: End timestamp for monthly optimization
            n_trials: Number of optimization trials
            optimization_type: Type of optimization ('single_coin', 'monthly', 'multi_coin')

        Returns:
            task_id: Unique identifier for the optimization task
        """
        # Check task limits
        if len(self.active_tasks) >= self.max_concurrent_tasks:
            raise RuntimeError(f"Maximum concurrent tasks ({self.max_concurrent_tasks}) reached")

        # Generate unique task ID
        task_id = f"{self.task_prefix}-{coin.lower()}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

        # Create log file
        log_filename = f"{task_id}.log"
        log_file_path = self.logs_dir / log_filename

        # Create optimization task
        task = OptimizationTask(
            task_id=task_id,
            status='starting',
            progress=0.0,
            coin=coin,
            month=month,
            n_trials=n_trials,
            start_time=time.time(),
            log_file=str(log_file_path)
        )

        self.active_tasks[task_id] = task

        try:
            # Initialize optimizer
            optimizer = BayesianOptimizer(
                api_base_url=self.api_url,
                results_dir=str(self.results_dir)
            )
            task.optimizer = optimizer

            logger.info(f"Starting optimization for {coin} with task_id: {task_id}")

            # Start optimization task in background
            if optimization_type == "monthly" and month and start_time and end_time:
                optimization_task = asyncio.create_task(
                    self._run_monthly_optimization(task_id, coin, month, start_time, end_time, n_trials)
                )
            elif optimization_type == "single_coin":
                optimization_task = asyncio.create_task(
                    self._run_single_coin_optimization(task_id, coin, n_trials)
                )
            else:
                raise ValueError(f"Unknown optimization type: {optimization_type}")

            task.optimization_task = optimization_task
            task.status = 'running'

            logger.info(f"Optimization task started: {task_id}")

            return task_id

        except Exception as e:
            # Update task with error
            task.status = 'failed'
            task.error = str(e)
            logger.error(f"Failed to start optimization for {coin}: {e}")
            raise

    async def _run_monthly_optimization(
        self,
        task_id: str,
        coin: str,
        month: str,
        start_time: int,
        end_time: int,
        n_trials: int
    ):
        """Run monthly optimization"""
        task = self.active_tasks.get(task_id)
        if not task or not task.optimizer:
            return

        try:
            result = await task.optimizer.optimize_coin_month(
                coin=coin,
                month=month,
                start_time=start_time,
                end_time=end_time,
                n_trials=n_trials
            )

            task.results = result
            task.status = 'completed'
            task.progress = 1.0
            task.end_time = time.time()

            logger.info(f"Optimization task {task_id} completed successfully")

        except Exception as e:
            task.status = 'failed'
            task.error = str(e)
            task.end_time = time.time()
            logger.error(f"Optimization task {task_id} failed: {e}")
        finally:
            if task.optimizer:
                await task.optimizer.close()

    async def _run_single_coin_optimization(
        self,
        task_id: str,
        coin: str,
        n_trials: int
    ):
        """Run single coin optimization across all months"""
        task = self.active_tasks.get(task_id)
        if not task or not task.optimizer:
            return

        try:
            result = await task.optimizer.optimize_single_coin_all_months(
                coin=coin,
                n_trials=n_trials
            )

            task.results = result
            task.status = 'completed'
            task.progress = 1.0
            task.end_time = time.time()

            logger.info(f"Optimization task {task_id} completed successfully")

        except Exception as e:
            task.status = 'failed'
            task.error = str(e)
            task.end_time = time.time()
            logger.error(f"Optimization task {task_id} failed: {e}")
        finally:
            if task.optimizer:
                await task.optimizer.close()


    async def _update_task_progress(self, task: OptimizationTask):
        """
        Update task progress by checking log files or result files
        """
        try:
            # Check if log file contains progress information
            if task.log_file and Path(task.log_file).exists():
                with open(task.log_file, 'r') as f:
                    lines = f.readlines()

                # Look for progress indicators in logs
                for line in reversed(lines[-50:]):  # Check last 50 lines
                    if "Trial" in line and "/" in line:
                        # Extract trial progress (e.g., "Trial 25/100 completed")
                        try:
                            parts = line.split("Trial")[1].split("/")
                            if len(parts) >= 2:
                                current = int(parts[0].strip())
                                total = int(parts[1].split()[0])
                                progress = current / total
                                task.progress = min(progress, 0.99)  # Cap at 99% until completion
                                break
                        except (ValueError, IndexError):
                            continue

        except Exception as e:
            logger.debug(f"Could not update progress for task {task.task_id}: {e}")

    async def _load_optimization_results(self, task: OptimizationTask):
        """
        Load optimization results from result files
        """
        try:
            # Look for result files matching the task
            result_pattern = f"{task.coin}_*_study_results.json"
            result_files = list(self.results_dir.glob(result_pattern))

            if result_files:
                # Load the most recent result file
                latest_file = max(result_files, key=lambda f: f.stat().st_mtime)
                with open(latest_file, 'r') as f:
                    results = json.load(f)
                    task.results = results

        except Exception as e:
            logger.warning(f"Could not load results for task {task.task_id}: {e}")

    async def _save_error_logs(self, task_id: str, logs: str):
        """
        Save container error logs to file
        """
        try:
            error_log_file = self.logs_dir / f"{task_id}_error.log"
            with open(error_log_file, 'w') as f:
                f.write(f"Container Error Logs for {task_id}\n")
                f.write(f"Timestamp: {datetime.now().isoformat()}\n")
                f.write("=" * 50 + "\n")
                f.write(logs)

        except Exception as e:
            logger.error(f"Failed to save error logs for {task_id}: {e}")

    async def stop_optimization(self, task_id: str) -> bool:
        """
        Stop a running optimization task
        """
        task = self.active_tasks.get(task_id)
        if not task:
            return False

        try:
            if task.optimization_task and not task.optimization_task.done():
                task.optimization_task.cancel()
                try:
                    await task.optimization_task
                except asyncio.CancelledError:
                    pass

            if task.optimizer:
                await task.optimizer.close()

            task.status = 'stopped'
            task.end_time = time.time()

            logger.info(f"Stopped optimization task: {task_id}")
            return True

        except Exception as e:
            logger.error(f"Error stopping task {task_id}: {e}")
            task.error = str(e)

        return False

    async def get_task_status(self, task_id: str) -> Optional[OptimizationTask]:
        """
        Get status of optimization task
        """
        return self.active_tasks.get(task_id)

    async def list_active_tasks(self) -> List[OptimizationTask]:
        """
        List all active optimization tasks
        """
        return list(self.active_tasks.values())

    async def get_task_logs(self, task_id: str, tail_lines: int = 100) -> str:
        """
        Get logs from optimization task
        """
        task = self.active_tasks.get(task_id)
        if not task:
            return "Task not found"

        logs = []

        # Get log file contents
        if task.log_file and Path(task.log_file).exists():
            try:
                with open(task.log_file, 'r') as f:
                    file_lines = f.readlines()
                    if len(file_lines) > tail_lines:
                        file_lines = file_lines[-tail_lines:]

                    logs.append("=== Optimization Log File ===")
                    logs.append("".join(file_lines))
            except Exception as e:
                logs.append(f"Error reading log file: {e}")

        # Add task status information
        logs.append(f"\n=== Task Status ===")
        logs.append(f"Status: {task.status}")
        logs.append(f"Progress: {task.progress:.2%}")
        if task.error:
            logs.append(f"Error: {task.error}")

        return "\n".join(logs) if logs else "No logs available"

    async def cleanup_completed_tasks(self, older_than_hours: int = 24):
        """
        Clean up completed tasks
        """
        cutoff_time = time.time() - (older_than_hours * 3600)
        tasks_to_remove = []

        for task_id, task in self.active_tasks.items():
            if task.status in ['completed', 'failed', 'stopped'] and task.end_time < cutoff_time:
                # Clean up optimizer if it exists
                if task.optimizer:
                    try:
                        await task.optimizer.close()
                    except Exception as e:
                        logger.warning(f"Error closing optimizer for task {task_id}: {e}")

                tasks_to_remove.append(task_id)

        # Remove tasks from active list
        for task_id in tasks_to_remove:
            del self.active_tasks[task_id]

        logger.info(f"Cleaned up {len(tasks_to_remove)} completed optimization tasks")

    def format_task_summary(self, task: OptimizationTask) -> Dict[str, Any]:
        """
        Format task information for API responses
        """
        elapsed_time = (task.end_time or time.time()) - task.start_time

        return {
            "task_id": task.task_id,
            "status": task.status,
            "progress": task.progress,
            "coin": task.coin,
            "month": task.month,
            "n_trials": task.n_trials,
            "elapsed_time": elapsed_time,
            "start_time": task.start_time,
            "end_time": task.end_time,
            "error": task.error,
            "has_results": task.results is not None,
            "log_file": task.log_file
        }

    async def close(self):
        """
        Cleanup service and stop all tasks
        """
        logger.info("Shutting down optimization service...")

        # Stop all active tasks
        for task_id in list(self.active_tasks.keys()):
            await self.stop_optimization(task_id)