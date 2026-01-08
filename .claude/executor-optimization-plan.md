# Optimization Plan: Executor Lookup Performance in HEI/BTC MM Strategy

## Problem Statement
The `_update_executor_states()` and `_is_order_filled()` methods loop through ALL executors in `self.executors_info`. As the strategy runs, this list grows indefinitely, causing performance degradation.

## Current Issue

```python
# Current inefficient pattern - loops through ALL executors
for executor in self.executors_info:  # Could be thousands!
    if executor.id == order_id:
        ...
```

## Available Optimization Tools

From `controller_base.py`:
1. **`filter_executors()`** - Static method for filtering with lambda predicates
2. **`is_active`** property - Filter out terminated executors
3. **Generator expressions** - Stop at first match using `next()`

## Solution: Optimized Executor Lookup

### 1. Use Generator Expression for ID Lookup (O(n) → O(1) average)
```python
def _get_executor_by_id(self, executor_id: str) -> Optional[ExecutorInfo]:
    """Efficient lookup using generator - stops at first match"""
    return next(
        (e for e in self.executors_info if e.id == executor_id),
        None
    )
```

### 2. Filter Active Executors First
```python
def _update_executor_states(self):
    """Only process active sell executors"""
    active_sell_executors = self.filter_executors(
        self.executors_info,
        lambda x: x.is_active and x.custom_info.get("level_id", "").startswith("active_sell_")
    )
    for executor in active_sell_executors:
        # Process only relevant executors
```

### 3. Cache Active Executor IDs (Optional - if needed)
```python
# In __init__:
self._known_order_ids: Set[str] = set()  # Track our order IDs

# When placing order:
self._known_order_ids.add(executor_config.id)

# When checking fill - quick set lookup first:
if order_id not in self._known_order_ids:
    return False
```

## Files Modified

| File | Change |
|------|--------|
| `bots/controllers/market_making/hei_btc_mm.py` | Optimized `_is_order_filled()`, `_update_executor_states()`, `_was_sell_filled_or_partial()`, `_cancel_order()` |

## Implementation Summary

1. **Added `_get_executor_by_id()` helper method**:
   - Uses generator expression with `next()` for single lookup
   - Stops iteration at first match

2. **Optimized `_is_order_filled()`**:
   - Uses `_get_executor_by_id()` instead of manual loop

3. **Optimized `_update_executor_states()`**:
   - Uses `filter_executors()` with `is_active` predicate
   - Only processes executors with `level_id.startswith("active_sell_")`

4. **Optimized `_was_sell_filled_or_partial()`**:
   - Uses `_get_executor_by_id()` instead of manual loop

5. **Optimized `_cancel_order()`**:
   - Uses `_get_executor_by_id()` instead of manual loop

## Key Reference
- `controller_base.py` line 185: `filter_executors()` static method
- Pattern from `market_making_controller_base.py`: Filter by `is_active` first
