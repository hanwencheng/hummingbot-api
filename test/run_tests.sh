#!/bin/bash

# Test runner script for Hummingbot API backtesting functionality
# Make sure the API is running before executing tests

set -e

echo "🧪 Running Hummingbot API Backtesting Tests"
echo "============================================="

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "❌ Virtual environment not found. Please run 'make install' first."
    exit 1
fi

# Activate virtual environment
echo "📦 Activating virtual environment..."
source venv/bin/activate

# Check if API is running
echo "🔍 Checking if API is running on localhost:8000..."
if ! curl -s -f -m 5 "http://localhost:8000/docs" > /dev/null 2>&1; then
    echo "❌ API is not running or not accessible at localhost:8000"
    echo "   Please start the API with: docker compose up -d"
    exit 1
fi

echo "✅ API is running"

# Install test dependencies if needed
echo "📥 Installing test dependencies..."
pip install -q pytest requests

# Run integration tests
echo "🚀 Running integration tests..."
python -m pytest test/test_backtesting_integration.py -v --tb=short

echo ""
echo "✅ Test execution completed!"
echo ""
echo "📝 Test Summary:"
echo "   - Tests your fixed logging functionality"
echo "   - Tests backtesting endpoint with dict and YAML configs"
echo "   - Tests error handling and validation"
echo "   - Tests concurrent request handling"
echo "   - Tests authentication requirements"
echo ""
echo "💡 To view detailed logs, run: docker logs hummingbot-api"