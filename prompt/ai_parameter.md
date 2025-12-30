You are a crypto trader and AI-RL engineer

I have the strategy written at /Users/hanwencheng/Projects/hummingbot-api/bots/controllers/dynamic_bb_grid/bollinger_dynamic_bb_grid_v6.py as a controller, I want to optimization the configs of BollingerDynamicBBGridV6Config, the backtesting configs sent to run-backtesting endpoint in /Users/hanwencheng/Projects/hummingbot-api/routers/backtesting.py, it generally retrieve the 1m candles to simulate the running result. 

the configs need to be optmized and their ranges are:
    normal_stage_bb: float(min -0.8 max 1)
    normal_entry_normal: float(min -0.1 max 0.1)
    normal_entry_follow: float(min -0.1 max 0.1)
    normal_entry_anti: float(min -0.1 max 0.1)
    breakthrough_stage_bb: float(min -0.1 max 0.1) 
    breakthrough_entry_normal: float(min -0.1 max 0.1) 
    breakthrough_entry_follow: float(min -0.1 max 0.1) 
    breakthrough_entry_anti: float(min -0.1 max 0.1) 
    fallback_stage_bb: float(min -0.1 max 0.1) 
    fallback_entry_normal: float(min -0.1 max 0.1) 
    fallback_entry_follow: float(min -0.1 max 0.1) 
    fallback_entry_anti: float(min -0.1 max 0.1) 
    macd_threshold: float(min 0 max 0.1) 

also they should follow the rules that:
normal_entry_normal < normal_entry_follow < normal_entry_anti
breakthrough_entry_normal < breakthrough_entry_follow < breakthrough_entry_anti
fallback_entry_normal < fallback_entry_follow < fallback_entry_anti
normal_entry_normal < breakthrough_entry_normal < fallback_entry_normal

other config data are used in /Users/hanwencheng/Projects/hummingbot-api/bots/controllers/dynamic_bb_grid_controller_base.py do not need to be optimized.

  connector_name: 'hyperliquid_perpetual',
  trading_pair: 'HYPE-USD',
  candles_connector: 'binance_perpetual',
  candles_trading_pair: 'HYPE-USDT',
  interval: '1m',
  position_mode: 'ONEWAY',
  profit_skew: 1,
  stop_loss_skew: 1,
  time_limit_hours: 1.5,
  bb_length: 1100,
  bb_std: 2.0,
  bb_weak_threshold: 0.05,
  bb_strong_threshold: 0.1,
  level_number: 5,
  leverage: 10,
  level_size: 100,
  cooldown_time: 600,
  accumulate_pct: 0.0025,
  profit_pct: 0.005,
  stop_loss_pct: 0.003,
  stop_loss_waiting_time_hours: 2,
  reverse_skew: 0.1,

We also have a dependency called hummingbot, it do the actual computation in the backtesting.

For 1 month result on my macbook pro with Apple M3 Max chip, it take about 3 minutes to simulate.

To backtest them all from jan 2025 to nov 2025 (11 months), each coin(like HYPE) will take around 30 minutes.

I want to use Bayesian Optimization / TPE (Tree-of-Parzen Estimators) with Optuna to do it.

For result I consider 50% importance of pnl and 50% importantce of win rate.

I can first try it on local, and they train the result on remote station with 8*h100 gpu.:

The other coins list are:
BTC
ETH
SOL
HYPE
XRP
ADA
DOT
AVAX
UNI
ZEC
BNB
DOGE
BCH
PEPE
SUI
ENA

I will need to train first on one coin, and on othercoins. I want to have a good statistics in the dimension of each month, they should be trained by each month.

To avoid making too many requests at a time, we need to split the testing time into months, and aggregate the pnl and accuracy data of each month

the type of response_data is a json like followings:

results: {
  accuracy: 0.3764705882352941
  accuracy_long: 0.43448275862068964
  accuracy_short: 0.3
  close_types: {EARLY_STOP: 3048, STOP_LOSS: 6, TIME_LIMIT: 183}
  loss_signals: 159
  max_drawdown_pct: -0.5154241224854229
  max_drawdown_usd: -51.50584748939382
  net_pnl: -0.44756139527335514
  net_pnl_quote: -44.75613952733552
  profit_factor: 0.3711347468605157
  sharpe_ratio: -0.9146778838950478
  total_executors: 3237
  total_executors_with_position: 255
  total_long: 145
  total_positions: 255
  total_short: 110
  total_volume: 27130.64477046326
  win_signals: 96`
}

Make the result into typed data structure, refactor the old untyped code

we need to calculate total_accuracy = (total_long * accuracy_long + total_short * accuracy_short)/(total_long + total_short)

we also calcualte:
   - mean_pnl
   - std_pnl (sample std, ddof=1; handle N<2)
   - variance_pnl (sample variance, ddof=1; handle N<2)
   - cv_pnl = std_pnl / mean_pnl (if mean_pnl<=0 then cv_pnl = +inf)

In the result we already have top 5 pnl list, and top 5 accuracy list, we also need to have top 5 cv_pnl list(the lower the better)
