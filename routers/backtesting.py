import logging
import traceback
from fastapi import APIRouter
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

from config import settings
from models.backtesting import BacktestingConfig

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Backtesting"], prefix="/backtesting")
candles_factory = CandlesFactory()
backtesting_engine = BacktestingEngineBase()


@router.post("/run-backtesting")
async def run_backtesting(backtesting_config: BacktestingConfig):
    """
    Run a backtesting simulation with the provided configuration.
    
    Args:
        backtesting_config: Configuration for the backtesting including start/end time,
                          resolution, trade cost, and controller config
                          
    Returns:
        Dictionary containing executors, processed data, and results from the backtest
        
    Raises:
        Returns error dictionary if backtesting fails
    """

    logger.warning(f"router receive! start process")
    try:
        if isinstance(backtesting_config.config, str):
            controller_config = backtesting_engine.get_controller_config_instance_from_yml(
                config_path=backtesting_config.config,
                controllers_conf_dir_path=settings.app.controllers_path,
                controllers_module=settings.app.controllers_module
            )
        else:
            controller_config = backtesting_engine.get_controller_config_instance_from_dict(
                config_data=backtesting_config.config,
                controllers_module=settings.app.controllers_module
            )

        logger.warning(f"Backtesting parameters - start: {backtesting_config.start_time}, end: {backtesting_config.end_time}, resolution: {backtesting_config.backtesting_resolution}")

        try:
            backtesting_results = await backtesting_engine.run_backtesting(
                controller_config=controller_config, trade_cost=backtesting_config.trade_cost,
                start=int(backtesting_config.start_time), end=int(backtesting_config.end_time),
                backtesting_resolution=backtesting_config.backtesting_resolution)

        except Exception as e:
            logger.warning(f"Backtesting engine error: {str(e)}")
            logger.warning(f"Error type: {type(e)}")
            logger.warning(f"Full traceback: {traceback.format_exc()}")
            raise  # Re-raise the exception so it's handled by the outer try-catch


        # Log detailed executors info
        if backtesting_results['executors']:
            executor_details = []
            for i, executor in enumerate(backtesting_results['executors'][:5]):  # Log first 5 executors
                try:
                    executor_dict = executor.to_dict() if hasattr(executor, 'to_dict') else str(executor)
                    executor_details.append({"index": i, "executor": executor_dict})
                except Exception as e:
                    executor_details.append({"index": i, "error": str(e), "type": str(type(executor))})

        processed_data = backtesting_results["processed_data"]["features"].fillna(0)
        executors_info = [e.to_dict() for e in backtesting_results["executors"]]
        backtesting_results["processed_data"] = processed_data.to_dict()
        results = backtesting_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0

        return {
            "executors": executors_info,
            "processed_data": backtesting_results["processed_data"],
            "results": backtesting_results["results"],
        }
    except Exception as e:
        return {"error": str(e)}
