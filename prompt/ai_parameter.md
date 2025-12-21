You are a crypto trader and AI-RL engineer

I have the strategy written at /Users/hanwencheng/Projects/hummingbot-api/bots/controllers/dynamic_bb_grid/bollinger_dynamic_bb_grid_v5.py as a controller, when I do backtesting, I send the backtesting configs with the controller name and type sends to run-backtesting endpoint in /Users/hanwencheng/Projects/hummingbot-api/routers/backtesting.py, it generally retrieve the 1m candles to simulate the running result. 

We also have a dependency called hummingbot, it do the actual computation in the backtesting.

For 1 month result on my macbook pro with Apple M3 Max chip, it take about 3 minutes to simulate.

However there are lots of parameters can be adjusted, which can significantly affect the result(which is the overall pnl and overall winrate), plus there are around 20 coins(trading pair) I can select.

To backtest them all from jan 2025 to nov 2025, each coin will take around 30 minutes, and for 20 coins it's about 600 minuts. Not to mention the time taken by adjusting 10 parameters.

For result I consider 50% importance of pnl and 50% importantce of win rate.

I have remote server which has 8*h100 gpu. I want to know if I can use reinforcement learning to opimitize the parameters for each of the trading pair.

If I can, let me know how to do that, do not implement, we do analysis first.

