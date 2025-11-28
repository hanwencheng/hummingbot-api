"""
Logging configuration for development mode with detailed backtesting engine logs.
"""

import logging
import sys


def setup_development_logging():
    """
    Configure detailed logging for development mode.
    This will capture all logs from the Hummingbot backtesting engine.
    """

    # Create formatter for console output
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Remove any existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Set specific logger levels for different components
    loggers_config = {
        # Hummingbot components (keep detailed)
        'hummingbot': logging.INFO,
        'hummingbot.strategy_v2.backtesting': logging.INFO,  # Changed to INFO to reduce noise
        'hummingbot.strategy_v2.backtesting.backtesting_engine_base': logging.INFO,  # Main logs we want
        'hummingbot.strategy_v2.controllers': logging.INFO,
        'hummingbot.data_feed': logging.WARNING,  # Reduce noise
        'hummingbot.core': logging.WARNING,       # Reduce noise

        # API components (selective debugging)
        'routers': logging.INFO,
        'routers.backtesting': logging.INFO,      # Keep our custom logs
        'services': logging.INFO,
        'services.mqtt_manager': logging.WARNING,  # Reduce MQTT noise
        'services.bots_orchestrator': logging.INFO,

        # FastAPI and uvicorn (minimal)
        'uvicorn': logging.INFO,
        'uvicorn.access': logging.WARNING,        # Hide HTTP access logs
        'uvicorn.error': logging.INFO,
        'fastapi': logging.WARNING,               # Reduce FastAPI noise

        # External libraries (silence noisy ones)
        'urllib3': logging.WARNING,               # Hide HTTP connection details
        'urllib3.connectionpool': logging.WARNING,
        'requests': logging.WARNING,
        'docker': logging.WARNING,                # Hide Docker API calls
        'asyncio': logging.ERROR,
        'httpx': logging.WARNING,
        'aio_pika': logging.WARNING,
        'aiomqtt': logging.WARNING,
        'pandas': logging.WARNING,
        'numpy': logging.WARNING,
        'matplotlib': logging.WARNING,
        'websockets': logging.WARNING,
        'aiohttp': logging.WARNING,
    }

    # Apply logger configurations
    for logger_name, level in loggers_config.items():
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)

    # Completely suppress extremely noisy loggers
    noisy_loggers = [
        'urllib3.connectionpool',
        'docker.api',
        'docker.utils.config',
        'requests.packages.urllib3.connectionpool',
        'asyncio.selector_events',
        'aiodns',
        'charset_normalizer',
    ]

    for logger_name in noisy_loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.CRITICAL)  # Only critical errors
        logger.propagate = False  # Don't propagate to parent loggers

    print("🔍 Development logging configured with clean output:")
    print("   ✅ Enabled: hummingbot.strategy_v2.backtesting.backtesting_engine_base")
    print("   ✅ Enabled: routers.backtesting")
    print("   ✅ Enabled: services.*")
    print("   🔇 Silenced: urllib3, docker, requests, asyncio DEBUG logs")
    print("")


def setup_production_logging():
    """
    Configure logging for production mode with reduced verbosity.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Suppress noisy loggers in production
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.ERROR)


if __name__ == "__main__":
    # Test the logging configuration
    setup_development_logging()

    # Test different logger levels
    test_loggers = [
        'hummingbot.strategy_v2.backtesting.backtesting_engine_base',
        'routers.backtesting',
        'services.mqtt_manager'
    ]

    for logger_name in test_loggers:
        logger = logging.getLogger(logger_name)
        logger.debug(f"DEBUG test message from {logger_name}")
        logger.info(f"INFO test message from {logger_name}")
        logger.warning(f"WARNING test message from {logger_name}")