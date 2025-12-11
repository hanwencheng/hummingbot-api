#!/bin/bash

# Run script for Backend API
# Usage: ./run.sh [--dev]
# --dev: Run API from source using uvicorn
# Without --dev: Run using docker compose

if [[ "$1" == "--dev" ]]; then
    echo "Running API from source with detailed logging..."
    # Activate conda environment and run with uvicorn
    docker compose up emqx postgres -d
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate hummingbot-api

    # Set environment variables for verbose logging
    export PYTHONPATH="${PWD}:${PYTHONPATH}"
    export HUMMINGBOT_LOGGING_LEVEL="INFO"

    echo "🔍 Enhanced logging enabled for:"
    echo "   - Backtesting engine (DEBUG level)"
    echo "   - MQTT manager (DEBUG level)"
    echo "   - API routers (WARNING level)"
    echo ""
    echo "📝 To view logs in real-time, the output will appear in this terminal"
    echo ""

    uvicorn main:app --reload --log-level debug
else
    echo "Running with Docker Compose..."
    docker compose up -d
fi