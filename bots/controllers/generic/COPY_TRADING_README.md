# Copy Trading Strategy Controller

A generic copy trading strategy that copies trades from a followed account via the Wildmeta Signal Server, with full pause handling that closes positions and resets state.

## Architecture Overview

```
Signal Server ──► CopyTradingManager ──► CopyTradingSubscription ──► Controller
     │                (singleton)          (per-controller)              │
     │                                                                   │
GET /signals/:userId/events?since=X   ──►   CopyTradeSignal list   ──► ExecutorAction
GET /signals/:userId/latest           ──►   pausedStrategies       ──► Pause check
```

### Components

1. **CopyTradingManager** (`copy_trading_manager.py`)
   - Singleton per `{url}:{user_id}:{following_address}` combination
   - Fetches events and states from the signal server
   - Filters events for `signalType == "COPY_TRADE"` and matching `copyAccount`
   - Maintains list of paused strategies

2. **CopyTradingSubscription**
   - Per-controller subscription to the manager
   - Tracks consumed signals to avoid duplicate processing
   - Provides `clear_consumed()` for state reset on resume

3. **CopyTradingController** (`copy_trading.py`)
   - Consumes signals and creates executor actions
   - Handles pause/resume lifecycle
   - Persists state across restarts

## Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `copy_trading_server_url` | str | `http://host.docker.internal:8006` | Signal server URL |
| `copy_trading_user_id` | str | `""` | User identifier for the signal server |
| `following_address` | str | `""` | Address to copy trades from |
| `connector_name` | str | `binance_perpetual` | Exchange connector |
| `trading_pair` | str | `BTC-USDT` | Trading pair |
| `amount_mode` | Literal | `fixed` | Order amount calculation mode |
| `fixed_order_amount` | Decimal | `100` | Fixed amount (when mode=fixed) |
| `proportional_ratio` | Decimal | `1.0` | Scale ratio (when mode=proportional) |
| `max_order_amount` | Decimal | `1000` | Maximum order limit |
| `signal_ttl` | int | `60` | Signal expiration in seconds |
| `leverage` | int | `1` | For perpetual trading |
| `state_file_name` | str | `copy_trading_state.json` | State persistence file |

### Amount Modes

- **fixed**: Uses `fixed_order_amount` regardless of signal amount
- **mirror**: Uses the signal's amount directly (falls back to `fixed_order_amount` if not provided)
- **proportional**: Multiplies signal amount by `proportional_ratio`

All modes respect `max_order_amount` as an upper limit.

## Usage Examples

### Standalone Usage

```python
from bots.controllers.generic.copy_trading import CopyTradingConfig, CopyTradingController

config = CopyTradingConfig(
    id="my_copy_trader",
    copy_trading_server_url="http://signal-server:8006",
    copy_trading_user_id="123456",
    following_address="0xABC...123",
    connector_name="binance_perpetual",
    trading_pair="ETH-USDT",
    amount_mode="proportional",
    proportional_ratio=Decimal("0.5"),  # Copy at 50% size
    max_order_amount=Decimal("500"),
    signal_ttl=120,
    leverage=5
)

controller = CopyTradingController(config, market_data_provider, actions_queue)
```

### In Custom Strategy

```python
from bots.controllers.generic.copy_trading import CopyTradingConfig, CopyTradingController

class MyCustomStrategy(StrategyV2Base):
    def __init__(self, connectors, config):
        super().__init__(connectors, config)

        copy_config = CopyTradingConfig(
            id="copy_trader_btc",
            copy_trading_user_id=config.signal_user_id,
            following_address=config.target_address,
            connector_name="binance_perpetual",
            trading_pair="BTC-USDT",
            amount_mode="fixed",
            fixed_order_amount=Decimal("100")
        )

        self.copy_controller = CopyTradingController(
            copy_config,
            self.market_data_provider,
            self.actions_queue
        )
```

### Bot Deployment Config

```json
{
  "controller_type": "generic",
  "controller_name": "copy_trading",
  "id": "copy_eth_trader",
  "copy_trading_server_url": "http://host.docker.internal:8006",
  "copy_trading_user_id": "614270688",
  "following_address": "0x1234567890abcdef1234567890abcdef12345678",
  "connector_name": "binance_perpetual",
  "trading_pair": "ETH-USDT",
  "amount_mode": "mirror",
  "max_order_amount": "1000",
  "signal_ttl": 60,
  "leverage": 10
}
```

## Pause/Resume Behavior

### On PAUSE Signal

When the controller detects it's paused (via `pausedStrategies` from the signal server):

1. **Stop all active executors** with `keep_position=False` (closes their positions)
2. **Close remaining positions** not tracked by executors via market orders
3. **Discard pending signals** by marking them as consumed with reason "paused"
4. **Save state** with `was_paused=True`

### On RESUME

When the controller transitions from paused to active:

1. **Clear subscription consumed set** - allows re-fetching of signals
2. **Clear processed signal IDs** - fresh start
3. **Reset pause cleanup flag**
4. **Save state**

This ensures a clean restart without carrying over stale signal state.

## Signal Server API Requirements

The copy trading manager expects the signal server to provide these endpoints:

### GET `/signals/:userId/latest`

Returns current state and paused strategies:

```json
{
  "states": {
    "REGIME": {"signalType": "NEUTRAL"},
    "REGIME:instance_1": {"signalType": "BULLISH"}
  },
  "pausedStrategies": ["strategy_1", "strategy_2"]
}
```

### GET `/signals/:userId/events?since=:timestamp`

Returns events since the given timestamp:

```json
{
  "events": [
    {
      "signalId": "signal_123",
      "signalMode": "event",
      "signalType": "BUY",
      "timestamp": 1704067200,
      "tradingPair": "BTC-USDT",
      "copyAccount": "0xABC...123",
      "price": 50000.0,
      "amount": 0.1,
      "leverage": 10,
      "positionSide": "LONG"
    }
  ]
}
```

**Important**: Only events with `signalMode == "event"` and `copyAccount` matching the configured `following_address` are processed.

## State Persistence Format

The controller persists state to `{data_path}/{state_file_name}`:

```json
{
  "last_consumed_timestamp": 1704067200,
  "processed_signal_ids": ["signal_1", "signal_2", "signal_3"],
  "was_paused": false,
  "last_updated": 1704067200.123
}
```

### Fields

- `last_consumed_timestamp`: Unix timestamp of last consumed signal (used for `since` parameter)
- `processed_signal_ids`: Set of signal IDs already processed (prevents duplicate execution)
- `was_paused`: Whether the controller was paused (triggers state reset on resume)
- `last_updated`: When the state was last saved

## Troubleshooting

### Signals Not Being Processed

1. **Check signal server connection**:
   - Look for "State error" or "Events error" in logs
   - Verify `copy_trading_server_url` is correct and accessible

2. **Verify `following_address`**:
   - Must exactly match `copyAccount` in signals (case-insensitive)
   - Check signal server is sending events with correct `copyAccount`

3. **Check signal TTL**:
   - Signals older than `signal_ttl` seconds are discarded
   - Increase `signal_ttl` if signals arrive with delay

### Duplicate Executions

1. **Check state file**:
   - Ensure `state_file_name` is unique per controller instance
   - Verify state is being saved (check for write errors in logs)

2. **Check `processed_signal_ids`**:
   - Signals in this set are skipped
   - State reset on resume clears this set

### Positions Not Closing on Pause

1. **Check executor state**:
   - `executors_info` must contain active executors
   - `positions_held` must contain positions for the trading pair

2. **Verify connector**:
   - `connector_name` must match positions exactly
   - `trading_pair` must match exactly

### Manager Instance Issues

If signals aren't isolated between controllers:

1. **Verify unique manager keys**:
   - Each `{url}:{user_id}:{following_address}` combo creates a separate manager
   - Different `following_address` = different manager instance

2. **Check subscription isolation**:
   - Each controller has its own subscription
   - Consumed signals are tracked per-subscription

## Logs to Monitor

```
# Successful signal execution
INFO: Executing BUY signal signal_123 from 0xABC...123

# Signal expiration
WARNING: Expired signal signal_456

# Pause activation
INFO: PAUSED - executing cleanup: closing positions and discarding signals
INFO: Stopping executor exec_789 (keep_position=False)
INFO: Closing position: SELL 0.1 BTC-USDT

# Resume activation
INFO: RESUMED - resetting state for fresh start
INFO: State reset: cleared consumed signals and processed IDs

# State persistence
INFO: State loaded: last_timestamp=1704067200, processed=15, was_paused=False
INFO: Copy trading controller stopping, state saved
```

## Comparison with HEI Signal Controller

| Feature | HEI Signal | Copy Trading |
|---------|------------|--------------|
| Signal filtering | By `signalMode` and `tradingPair` | By `copyAccount` address |
| Amount modes | Fixed or signal amount | Fixed, mirror, proportional |
| Pause handling | Discard signals only | Close positions + discard |
| State reset on resume | No | Yes |
| Manager singleton key | `{url}:{user_id}` | `{url}:{user_id}:{following_address}` |
| Position close on pause | No | Yes |
