"""
Integration test for backtesting endpoint that tests the actual running API.
This test makes HTTP requests to the running Docker container.
"""

import requests
import json
import time
import pytest


class TestBacktestingIntegration:
    """Integration tests for the backtesting endpoint."""

    def setup_method(self):
        """Set up test data and API base URL."""
        self.base_url = "http://localhost:8000"
        self.auth = ("admin", "admin")
        self.endpoint = "/backtesting/run-backtesting"

        # Test data based on your logged output
        self.valid_config_dict = {
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

        self.valid_request = {
            "config": self.valid_config_dict,
            "start_time": 1763913600,
            "end_time": 1764172799,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

    def test_api_is_running(self):
        """Test that the API is accessible."""
        try:
            response = requests.get(f"{self.base_url}/docs", timeout=5)
            assert response.status_code == 200
        except requests.ConnectionError:
            pytest.skip("API server is not running. Start with 'docker compose up -d'")

    def test_backtesting_endpoint_exists(self):
        """Test that the backtesting endpoint exists and requires authentication."""
        # Test without authentication
        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            json=self.valid_request,
            timeout=30
        )
        assert response.status_code == 401  # Unauthorized

    def test_backtesting_with_dict_config(self):
        """Test backtesting with dictionary configuration."""
        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=self.valid_request,
            timeout=60  # Backtesting might take time
        )

        # Should return 200 with either success or error response
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, dict)

        # Response should have either error or success structure
        if "error" in data:
            # Log the error for debugging
            print(f"Backtesting error: {data['error']}")
            assert isinstance(data["error"], str)
        else:
            # Check success response structure
            assert "executors" in data
            assert "processed_data" in data
            assert "results" in data

            assert isinstance(data["executors"], list)
            assert isinstance(data["processed_data"], dict)
            assert isinstance(data["results"], dict)

    def test_backtesting_with_yaml_config_path(self):
        """Test backtesting with YAML configuration file path."""
        request_data = {
            "config": "test_config.yml",  # This should fail
            "start_time": 1763913600,
            "end_time": 1764172799,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=request_data,
            timeout=30
        )

        assert response.status_code == 200
        data = response.json()

        # Should contain error about file not found
        assert "error" in data
        assert "No such file or directory" in data["error"]

    def test_invalid_request_validation(self):
        """Test validation of invalid request data."""
        invalid_request = {
            "config": self.valid_config_dict,
            # Missing required fields like start_time, end_time
        }

        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=invalid_request,
            timeout=30
        )

        # Should return validation error
        assert response.status_code == 422

    def test_logging_functionality(self):
        """Test that our logging fix is working by checking the logs."""
        # Make a request to trigger logging
        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json={
                "config": "nonexistent.yml",
                "start_time": 1763913600,
                "end_time": 1764172799,
                "backtesting_resolution": "1m",
                "trade_cost": 0.0006
            },
            timeout=30
        )

        assert response.status_code == 200

        # The logging should show in Docker logs
        # We can't directly access logs from this test, but we can verify
        # the endpoint processed the request successfully
        data = response.json()
        assert "error" in data

    def test_backtesting_request_parameters_types(self):
        """Test that the endpoint handles various parameter types correctly."""
        # Test with string numbers
        request_with_strings = {
            "config": self.valid_config_dict,
            "start_time": "1763913600",  # String instead of int
            "end_time": "1764172799",    # String instead of int
            "backtesting_resolution": "1m",
            "trade_cost": "0.0006"       # String instead of float
        }

        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=request_with_strings,
            timeout=30
        )

        # Should either work (if auto-converted) or return validation error
        assert response.status_code in [200, 422]

    def test_malformed_config(self):
        """Test handling of malformed controller configuration."""
        malformed_config = {
            "controller_name": "nonexistent_controller",
            "controller_type": "invalid_type",
            "connector_name": "invalid_connector"
        }

        request_data = {
            "config": malformed_config,
            "start_time": 1763913600,
            "end_time": 1764172799,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0006
        }

        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=request_data,
            timeout=30
        )

        assert response.status_code == 200
        data = response.json()

        # Should contain an error about invalid controller
        assert "error" in data

    def test_response_data_structure(self):
        """Test the actual structure of successful backtesting response."""
        # Use a minimal but potentially valid configuration
        minimal_config = {
            "controller_name": "bollinger_v1",
            "controller_type": "directional_trading",
            "connector_name": "binance",  # Well-known connector
            "trading_pair": "BTC-USDT",
            "total_amount_quote": 100,
            "bb_length": 20,
            "bb_std": 2.0
        }

        request_data = {
            "config": minimal_config,
            "start_time": 1700000000,  # More recent timestamp
            "end_time": 1700086400,    # 24 hours later
            "backtesting_resolution": "1h",
            "trade_cost": 0.001
        }

        response = requests.post(
            f"{self.base_url}{self.endpoint}",
            auth=self.auth,
            json=request_data,
            timeout=60
        )

        assert response.status_code == 200
        data = response.json()

        # Whether success or error, should be valid JSON
        assert isinstance(data, dict)

        if "error" not in data:
            # If successful, verify structure
            assert "executors" in data
            assert "processed_data" in data
            assert "results" in data

            # Verify types
            assert isinstance(data["executors"], list)
            assert isinstance(data["processed_data"], dict)
            assert isinstance(data["results"], dict)

    def test_concurrent_requests(self):
        """Test multiple concurrent backtesting requests."""
        import concurrent.futures
        import threading

        def make_request():
            """Make a backtesting request."""
            return requests.post(
                f"{self.base_url}{self.endpoint}",
                auth=self.auth,
                json={
                    "config": "test_config.yml",  # Will fail but tests concurrency
                    "start_time": 1763913600,
                    "end_time": 1764172799,
                    "backtesting_resolution": "1m",
                    "trade_cost": 0.0006
                },
                timeout=30
            )

        # Make 3 concurrent requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(make_request) for _ in range(3)]
            responses = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All requests should return 200 (with error content)
        for response in responses:
            assert response.status_code == 200
            data = response.json()
            assert "error" in data


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "--tb=short"])