# BB-Grid Strategy

I want to build a bollinger and grid strategy on perpetual trading, we need to create a new strategy base combine  /Users/hanwencheng/Projects/hummingbot-api/bots/controllers/strategy_controller_base.py and /Users/hanwencheng/Projects/hummingbot-api/venv/lib/python3.12/site-packages/hummingbot/strategy_v2/controllers/directional_trading_controller_base.py

we should be able to deploy /Users/hanwencheng/Projects/hummingbot-api/bots/controllers/directional_trading/bollinger_v1.py to the new strategy base

## Strategy Explained.

It basically use the singal to call executor to do actions just like we did in directional_trading_controller_base, there is no entry_price in config.

However, we set level_numbers levels like strategy_controller_base, we accumulate the position once with accumulate pct when the singal comes. And we only sell them if they reach the profit_pct.

There are also some key info since we are doing perpetual trading.

1. each time the signal comes, we will set the signal + 2 spread as entry_price, and set accumulation levels.
2. if the accumulation levels are not fully filled, but we receive a new signal, we will set the new entry level, keep the filled levels, update the unfilled levels with the current entry_level.
3. if the accumulation levels are fully filled, but we receive a new signal, we don't do anything.
4. if the accumulation levels does not complete(not fully closed) for a hourly base time limit, we will close it, this logic is same with the time limit in strategy_controller_base
5. if acculated position is not zero, if the final take profit or the final stop loss is hit, we will stop all the accumulation levels, close all positions and wait for the next signal, if stop loss is hit, we will wait for a longer time of stop loss waiting time.
6. there is no coolining down time.

Please write the strategy base, and write a script implementation with a config.