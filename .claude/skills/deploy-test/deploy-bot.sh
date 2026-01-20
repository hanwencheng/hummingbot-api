#!/bin/bash

BOT_NAME="${1:-test-bot}"
ACTION="${2:-deploy}"
CONFIG_NAME="${3:-}"
ACCOUNT_NAME="${4:-master_account}"
API_URL="http://localhost:8000"
API_AUTH="admin:admin"
LOG_FILE="/tmp/${BOT_NAME}-logs.txt"

get_container_name() {
    docker ps --filter "name=${BOT_NAME}" --format "{{.Names}}" | head -1
}

deploy() {
    if [ -z "$CONFIG_NAME" ]; then
        echo "Error: CONFIG_NAME required for deploy"
        echo "Usage: $0 <bot_name> deploy <config_name>"
        exit 1
    fi

    echo "Deploying bot: ${BOT_NAME} with config: ${CONFIG_NAME} using account: ${ACCOUNT_NAME}"

    curl -s -X POST -u "${API_AUTH}" \
        -H "Content-Type: application/json" \
        "${API_URL}/bot-orchestration/deploy-v2-controllers" \
        -d "{
            \"instance_name\": \"${BOT_NAME}\",
            \"controllers_config\": [\"${CONFIG_NAME}\"],
            \"credentials_profile\": \"${ACCOUNT_NAME}\"
        }" | python3 -m json.tool

    echo ""
    echo "Waiting for bot to start..."
    sleep 5
    status
}

status() {
    echo "=== Bot Status: ${BOT_NAME} ==="
    curl -s -u "${API_AUTH}" "${API_URL}/bot-orchestration/${BOT_NAME}/status" | python3 -m json.tool

    echo ""
    echo "=== Container Info ==="
    CONTAINER=$(get_container_name)
    if [ -n "$CONTAINER" ]; then
        docker ps --filter "name=${CONTAINER}" --format "Name: {{.Names}}\nStatus: {{.Status}}\nCreated: {{.CreatedAt}}"
    else
        echo "No running container found for ${BOT_NAME}"
    fi
}

logs() {
    CONTAINER=$(get_container_name)
    if [ -z "$CONTAINER" ]; then
        echo "No running container found for ${BOT_NAME}"
        exit 1
    fi

    TAIL_LINES="${3:-100}"
    echo "=== Logs for ${CONTAINER} (last ${TAIL_LINES} lines) ==="
    docker logs "${CONTAINER}" --tail "${TAIL_LINES}" 2>&1 | tee "${LOG_FILE}"
    echo ""
    echo "Logs saved to: ${LOG_FILE}"
}

errors() {
    CONTAINER=$(get_container_name)
    if [ -z "$CONTAINER" ]; then
        echo "No running container found for ${BOT_NAME}"
        exit 1
    fi

    echo "=== Errors for ${CONTAINER} ==="
    docker logs "${CONTAINER}" 2>&1 | grep -i "error\|exception\|failed\|traceback" | tail -50 | tee "${LOG_FILE}"
    echo ""
    echo "Errors saved to: ${LOG_FILE}"
}

follow() {
    CONTAINER=$(get_container_name)
    if [ -z "$CONTAINER" ]; then
        echo "No running container found for ${BOT_NAME}"
        exit 1
    fi

    echo "=== Following logs for ${CONTAINER} (Ctrl+C to stop) ==="
    docker logs "${CONTAINER}" -f --tail 50
}

stop() {
    echo "Stopping bot: ${BOT_NAME}"
    curl -s -X POST -u "${API_AUTH}" "${API_URL}/bot-orchestration/stop-bot/${BOT_NAME}" | python3 -m json.tool
}

list() {
    echo "=== All Running Bots ==="
    curl -s -u "${API_AUTH}" "${API_URL}/bot-orchestration/status" | python3 -m json.tool

    echo ""
    echo "=== Bot Containers ==="
    docker ps --filter "name=instance" --format "table {{.Names}}\t{{.Status}}\t{{.CreatedAt}}"
}

case "$ACTION" in
    deploy)
        deploy
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    errors)
        errors
        ;;
    follow)
        follow
        ;;
    stop)
        stop
        ;;
    list)
        list
        ;;
    *)
        echo "Usage: $0 <bot_name> <action> [config_name] [account_name]"
        echo ""
        echo "Actions:"
        echo "  deploy <config> [account]  - Deploy bot with config (default account: master_account)"
        echo "  status                     - Check bot status"
        echo "  logs [lines]               - Get recent logs (default: 100)"
        echo "  errors                     - Get error logs only"
        echo "  follow                     - Follow live logs"
        echo "  stop                       - Stop the bot"
        echo "  list                       - List all running bots"
        echo ""
        echo "Examples:"
        echo "  $0 my-bot deploy my_strategy_config"
        echo "  $0 my-bot deploy my_strategy_config gopher"
        echo "  $0 my-bot status"
        echo "  $0 my-bot logs 200"
        echo "  $0 my-bot stop"
        exit 1
        ;;
esac
