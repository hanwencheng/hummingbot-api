"""
Comprehensive test suite for the backtesting endpoint.
Tests both successful backtesting scenarios and error handling.
"""

import pytest
import json
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
import pandas as pd
from decimal import Decimal

from main import app
from models.backtesting import BacktestingConfig


class TestBacktesting:
    """Test suite for backtesting endpoint functionality."""

    def setup_method(self):
        """Set up test client and common test data."""
        self.client = TestClient(app)
        self.auth = ("admin", "admin")  # Basic auth credentials

        # Test data based on the logged controller config from your output
        self.valid_controller_config_dict = {
            "controller_name": "bollinger_v1",
            "controller_type": "directional_trading",
            "total_amount_quote": 1000,
            "manual_kill_switch": False,
            "candles_config": [],
            "initial_positions": [],
            "connector_name": "kucoin",
            "trading_pair": "WLD-USDT",
            "max_executors_per_side": 5,
            "cooldown_time": 3600,
            "leverage": 20,
            "position_mode": "HEDGE",
            "stop_loss": 0.05,
            "take_profit": 0.02,
            "time_limit": 43200,
            "take_profit_order_type": "LIMIT",
            "trailing_stop": {
                "activation_price": 0.018,
                "trailing_delta": 0.002
            },
            "candles_connector": "kucoin",
            "candles_trading_pair": "WLD-USDT",
            "interval": "3m",
            "bb_length": 100,
            "bb_std": 2.0,
            "bb_long_threshold": 0.0,
            "bb_short_threshold": 1.0
        }

        self.valid_backtesting_request = {
            "config": self.valid_controller_config_dict,
            "start_time": 1763913600,  # From your logged output
            "end_time": 1764172799,    # From your logged output
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

        # Mock successful backtesting results
        self.mock_backtesting_results = {
            "executors": [self._create_mock_executor()],
            "processed_data": {
                "features": pd.DataFrame({
                    "timestamp": [1763913600, 1763913660, 1763913720],
                    "open": [1.5, 1.52, 1.51],
                    "high": [1.53, 1.54, 1.53],
                    "low": [1.49, 1.51, 1.50],
                    "close": [1.52, 1.51, 1.52],
                    "volume": [1000, 1100, 950]
                })
            },
            "results": {
                "total_pnl": 150.75,
                "total_return": 0.15075,
                "max_drawdown": -25.30,
                "sharpe_ratio": 1.25,
                "win_rate": 0.65,
                "total_trades": 23,
                "winning_trades": 15,
                "losing_trades": 8
            }
        }

    def _create_mock_executor(self):
        """Create a mock executor with to_dict method."""
        mock_executor = MagicMock()
        mock_executor.to_dict.return_value = {
            "id": "test_executor_1",
            "side": "BUY",
            "amount": 100.0,
            "price": 1.52,
            "timestamp": 1763913600,
            "status": "FILLED"
        }
        return mock_executor

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_dict")
    def test_successful_backtesting_with_dict_config(self, mock_get_config, mock_run_backtesting):
        """Test successful backtesting with dictionary configuration."""
        # Setup mocks
        mock_controller_config = MagicMock()
        mock_controller_config.__dict__ = self.valid_controller_config_dict
        mock_get_config.return_value = mock_controller_config
        mock_run_backtesting.return_value = self.mock_backtesting_results

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=self.valid_backtesting_request
        )

        # Assertions
        assert response.status_code == 200
        data = response.json()

        # Verify response structure
        assert "executors" in data
        assert "processed_data" in data
        assert "results" in data

        # Verify executors data
        assert len(data["executors"]) == 1
        assert data["executors"][0]["id"] == "test_executor_1"
        assert data["executors"][0]["side"] == "BUY"

        # Verify processed_data structure
        assert isinstance(data["processed_data"], dict)

        # Verify results
        results = data["results"]
        assert results["total_pnl"] == 150.75
        assert results["total_return"] == 0.15075
        assert results["max_drawdown"] == -25.30
        assert results["sharpe_ratio"] == 1.25
        assert results["win_rate"] == 0.65
        assert results["total_trades"] == 23

        # Verify mocks were called correctly
        mock_get_config.assert_called_once_with(
            config_data=self.valid_controller_config_dict,
            controllers_module=app.state.settings.app.controllers_module
        )
        mock_run_backtesting.assert_called_once()

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_yml")
    def test_successful_backtesting_with_string_config(self, mock_get_config_yml, mock_run_backtesting):
        """Test successful backtesting with YAML file path configuration."""
        # Setup test data
        config_path = "test_bollinger_config.yml"
        request_data = {
            "config": config_path,
            "start_time": 1763913600,
            "end_time": 1764172799,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

        # Setup mocks
        mock_controller_config = MagicMock()
        mock_controller_config.__dict__ = self.valid_controller_config_dict
        mock_get_config_yml.return_value = mock_controller_config
        mock_run_backtesting.return_value = self.mock_backtesting_results

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=request_data
        )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert "executors" in data
        assert "processed_data" in data
        assert "results" in data

        # Verify YAML config loading was called
        mock_get_config_yml.assert_called_once_with(
            config_path=config_path,
            controllers_conf_dir_path=app.state.settings.app.controllers_path,
            controllers_module=app.state.settings.app.controllers_module
        )

    def test_missing_config_file_error(self):
        """Test error handling when config file doesn't exist."""
        request_data = {
            "config": "nonexistent_config.yml",
            "start_time": 1763913600,
            "end_time": 1764172799,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=request_data
        )

        # Should return 200 with error in response body (as per current implementation)
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "No such file or directory" in data["error"]

    def test_invalid_request_data(self):
        """Test error handling with invalid request data."""
        invalid_request = {
            "config": self.valid_controller_config_dict,
            # Missing required fields
        }

        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=invalid_request
        )

        # Should return validation error
        assert response.status_code == 422

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_dict")
    def test_backtesting_engine_exception(self, mock_get_config, mock_run_backtesting):
        """Test error handling when backtesting engine raises an exception."""
        # Setup mocks
        mock_controller_config = MagicMock()
        mock_get_config.return_value = mock_controller_config
        mock_run_backtesting.side_effect = Exception("Backtesting engine failed")

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=self.valid_backtesting_request
        )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert "Backtesting engine failed" in data["error"]

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_dict")
    def test_sharpe_ratio_none_handling(self, mock_get_config, mock_run_backtesting):
        """Test handling of None sharpe_ratio in results."""
        # Setup mocks with None sharpe_ratio
        results_with_none_sharpe = self.mock_backtesting_results.copy()
        results_with_none_sharpe["results"]["sharpe_ratio"] = None

        mock_controller_config = MagicMock()
        mock_get_config.return_value = mock_controller_config
        mock_run_backtesting.return_value = results_with_none_sharpe

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=self.valid_backtesting_request
        )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["results"]["sharpe_ratio"] == 0  # Should be converted to 0

    def test_authentication_required(self):
        """Test that authentication is required for backtesting endpoint."""
        response = self.client.post(
            "/backtesting/run-backtesting",
            json=self.valid_backtesting_request
        )

        # Should return 401 Unauthorized
        assert response.status_code == 401

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_dict")
    def test_empty_processed_data_handling(self, mock_get_config, mock_run_backtesting):
        """Test handling when processed_data features is empty."""
        # Setup mocks with empty DataFrame
        results_with_empty_data = self.mock_backtesting_results.copy()
        results_with_empty_data["processed_data"]["features"] = pd.DataFrame()

        mock_controller_config = MagicMock()
        mock_get_config.return_value = mock_controller_config
        mock_run_backtesting.return_value = results_with_empty_data

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=self.valid_backtesting_request
        )

        # Should still return 200 with processed_data as empty dict
        assert response.status_code == 200
        data = response.json()
        assert data["processed_data"] == {}

    @patch("routers.backtesting.backtesting_engine.run_backtesting")
    @patch("routers.backtesting.backtesting_engine.get_controller_config_instance_from_dict")
    def test_no_executors_handling(self, mock_get_config, mock_run_backtesting):
        """Test handling when no executors are returned."""
        # Setup mocks with empty executors
        results_with_no_executors = self.mock_backtesting_results.copy()
        results_with_no_executors["executors"] = []

        mock_controller_config = MagicMock()
        mock_get_config.return_value = mock_controller_config
        mock_run_backtesting.return_value = results_with_no_executors

        # Make request
        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=self.valid_backtesting_request
        )

        # Should still return 200 with empty executors list
        assert response.status_code == 200
        data = response.json()
        assert data["executors"] == []

    def test_logging_functionality(self):
        """Test that logging statements are triggered during backtesting."""
        # This test verifies that the logging we fixed is working
        with patch("routers.backtesting.logger") as mock_logger:
            response = self.client.post(
                "/backtesting/run-backtesting",
                auth=self.auth,
                json={
                    "config": "test_config.yml",
                    "start_time": 1763913600,
                    "end_time": 1764172799,
                    "backtesting_resolution": "1m",
                    "trade_cost": 0.0006
                }
            )

            # Verify that warning log was called (our fixed logging)
            mock_logger.warning.assert_called()

            # Verify the first log message
            first_call_args = mock_logger.warning.call_args_list[0][0]
            assert "router receive! start process" in first_call_args[0]


# Integration test to verify actual endpoint behavior
class TestBacktestingIntegration:
    """Integration tests for backtesting endpoint without mocking internal components."""

    def setup_method(self):
        """Set up test client."""
        self.client = TestClient(app)
        self.auth = ("admin", "admin")

    def test_actual_endpoint_response_structure(self):
        """Test the actual endpoint returns proper response structure."""
        # Use a minimal valid request that should fail gracefully
        request_data = {
            "config": {
                "controller_name": "bollinger_v1",
                "controller_type": "directional_trading",
                "connector_name": "binance",
                "trading_pair": "BTC-USDT"
            },
            "start_time": 1700000000,
            "end_time": 1700086400,
            "backtesting_resolution": "1h",
            "trade_cost": 0.001
        }

        response = self.client.post(
            "/backtesting/run-backtesting",
            auth=self.auth,
            json=request_data
        )

        # Should get 200 response (may contain error, but structure should be valid JSON)
        assert response.status_code == 200
        data = response.json()

        # Response should be a dictionary
        assert isinstance(data, dict)

        # Should either have error or successful result structure
        if "error" in data:
            assert isinstance(data["error"], str)
        else:
            assert "executors" in data
            assert "processed_data" in data
            assert "results" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])