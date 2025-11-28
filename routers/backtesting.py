import json
import logging
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

        # Detailed controller config logging
        controller_dict = {}
        try:
            if hasattr(controller_config, '__dict__'):
                controller_dict = {k: str(v) for k, v in controller_config.__dict__.items()}
            elif hasattr(controller_config, 'dict'):
                controller_dict = controller_config.dict()
            else:
                controller_dict = str(controller_config)
        except Exception as e:
            controller_dict = {"error_serializing": str(e), "type": str(type(controller_config))}

        logger.warning(f"Controller config JSON: {json.dumps(controller_dict, indent=2, default=str)}")
        logger.warning(f"Controller config type: {type(controller_config)}")
        logger.warning(f"Controller config attributes: {dir(controller_config)}")
        logger.warning(f"Backtesting parameters - start: {backtesting_config.start_time}, end: {backtesting_config.end_time}, resolution: {backtesting_config.backtesting_resolution}")

        # Enable detailed logging from backtesting engine
        import logging
        backtesting_logger = logging.getLogger('hummingbot.strategy_v2.backtesting.backtesting_engine_base')
        original_level = backtesting_logger.level
        backtesting_logger.setLevel(logging.DEBUG)

        try:
            backtesting_results = await backtesting_engine.run_backtesting(
                controller_config=controller_config, trade_cost=backtesting_config.trade_cost,
                start=int(backtesting_config.start_time), end=int(backtesting_config.end_time),
                backtesting_resolution=backtesting_config.backtesting_resolution)

            logger.warning("Backtesting engine execution completed successfully")

            # Detailed backtesting results logging
            results_summary = {
                "keys": list(backtesting_results.keys()),
                "executors_count": len(backtesting_results['executors']),
                "processed_data_shape": str(backtesting_results['processed_data']['features'].shape),
                "results": backtesting_results['results']
            }
        except Exception as e:
            logger.warning(f"Backtesting engine error: {str(e)}")
            logger.warning(f"Error type: {type(e)}")
            import traceback
            logger.warning(f"Full traceback: {traceback.format_exc()}")
            raise  # Re-raise the exception so it's handled by the outer try-catch
        finally:
            # Restore original logging level
            backtesting_logger.setLevel(original_level)


        # Log detailed executors info
        if backtesting_results['executors']:
            executor_details = []
            for i, executor in enumerate(backtesting_results['executors'][:5]):  # Log first 5 executors
                try:
                    executor_dict = executor.to_dict() if hasattr(executor, 'to_dict') else str(executor)
                    executor_details.append({"index": i, "executor": executor_dict})
                except Exception as e:
                    executor_details.append({"index": i, "error": str(e), "type": str(type(executor))})
            logger.warning(f"Sample executors JSON: {json.dumps(executor_details, indent=2, default=str)}")

        # Log processed data sample
        if not backtesting_results['processed_data']['features'].empty:
            data_sample = {
                "columns": list(backtesting_results['processed_data']['features'].columns),
                "first_few_rows": backtesting_results['processed_data']['features'].head().to_dict()
            }
            logger.warning(f"Processed data sample JSON: {json.dumps(data_sample, indent=2, default=str)}")

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
