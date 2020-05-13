from typing import List, Dict, Union,Tuple
from vnpy.trader.constant import Interval
from vnpy.app.portfolio_strategy import StrategyTemplate, StrategyEngine
from vnpy.trader.utility import BarGenerator, ArrayManager
from vnpy.trader.object import TickData, BarData
from vnpy.app.cta_strategy import (
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
    SpreadArrayManager,
    CtaSignal,
    TargetPosTemplate
)
import numpy as np
import talib
from datetime import datetime, timedelta
import statsmodels.tsa.stattools as ts
import statsmodels.api as sm

class DynamicResidualModelStrategy(StrategyTemplate):
    """"""

    author = "yiran"

    x_symbol: str = None
    y_symbol: str = None


    # 策略内动态调整
    x_multiplier: float = 0
    y_multiplier: float = 1
    intercept: float = 0

    # 策略内动态调整周期
    renew_interval: int = 7
    hedge_ratio_window: int = 7*240

    #轨道宽度
    entry_multiplier: float = 2.5


    # 预期价差盈利
    difference_filter_num: float = 10
    difference_exit_num: float = 0
    # 预期止盈为预期价差盈利的1/2

    # 指标计算参数
    std_window = 320
    # mean_window =320


    #固定下单单位
    fixed_size = 1
    x_fixed_size: float = None
    y_fixed_size: float = None

    #价差值
    spread_value: float = 0

    # 用来过滤交易量低的情况
    spread_volume_threshold = 0
    spread_volume: float = 0
    spread_volume_filter: bool = False

    # 进场，出场点位的缓存
    spread_long_entry: float = 0
    spread_long_exit: float = 0
    spread_long_loss_exit: float = 0

    spread_short_entry: float = 0
    spread_short_exit: float = 0
    spread_short_loss_exit: float =0

    #用来判断操作价差的方向
    open_direction_dict: Dict[str, float] = {}
    close_direction_dict: Dict[str, float] = {}

    # 用来进行参数更新判断
    last_renew_date: datetime = None
    renew_date: datetime = None
    renew_status: bool = False

    # 用来进行预期价差收益过滤
    price_diff: bool = False

    parameters = [
        "renew_interval",
        "hedge_ratio_window",
        'entry_multiplier',
        "fixed_size",
        "std_window",
        "difference_filter_num"
    ]
    variables = ["x_multiplier","y_multiplier","spread_value","spread_long_entry","spread_long_exit","spread_long_loss_exit"
    "spread_short_entry", "spread_short_exit","spread_short_loss_exit","spread_volume_filter"]

    def __init__(
        self,
        strategy_engine: StrategyEngine,
        strategy_name: str,
        vt_symbols: List[str],
        setting: dict
    ):
        """"""
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)

        self.short_entry_multiplier = abs(self.entry_multiplier)
        self.short_exit_multiplier = 0

        self.long_entry_multiplier =  -abs(self.entry_multiplier)
        self.long_exit_multiplier = 0

        self.mean_window = self.std_window 

        # self.difference_exit_num = self.difference_filter_num / 2

        self.y_symbol = self.vt_symbols[0]
        self.x_symbol = self.vt_symbols[1]


        self.x_fixed_size = int(np.abs(self.fixed_size * self.x_multiplier))
        self.y_fixed_size = int(np.abs(self.fixed_size * self.y_multiplier))

        self.open_direction_dict['x_symbol'] = 0
        self.open_direction_dict['y_symbol'] = 0

        self.close_direction_dict['x_symbol'] = 0
        self.close_direction_dict['y_symbol'] = 0


        # 实例化缓存期货品种价格序列容器
        self.ams:Dict = {}
        self.ams[self.y_symbol] = ArrayManager(size=self.hedge_ratio_window + 50)
        self.ams[self.x_symbol] = ArrayManager(size=self.hedge_ratio_window + 50)
        # 实例化缓存价差价格序列容器
        self.sam = SpreadArrayManager(size=max(self.std_window,self.mean_window)+50)
        self.record_array = []
        self.record_boll_up = []
        self.record_boll_down = []
        self.record_mean = []


    def on_init(self):
        """
        Callback when strategy is inited.
        """
        self.write_log("策略初始化")

        self.load_bars(10)

    def on_start(self):
        """
        Callback when strategy is started.
        """
        self.write_log("策略启动")

    def on_stop(self):
        """
        Callback when strategy is stopped.
        """
        self.write_log("策略停止")


    def on_bars(self, bars: Dict[str, BarData]):
        """"""
        self.cancel_all()

        
        # OLS动态线性回归，需要缓存close_array
        self.ams[self.y_symbol].update_bar(bars[self.y_symbol])
        self.ams[self.x_symbol].update_bar(bars[self.x_symbol])
        # 动态线性回归函数
        self.renew_hedge_ratio(bars[self.y_symbol])


        # 计算价差
        # 将价差放入sam 进行后续技术指标计算
        self.spread_value = self.y_multiplier*(bars[self.y_symbol].close_price) - self.x_multiplier*(bars[self.x_symbol].close_price) - self.intercept
        self.spread_volume = min(bars[self.y_symbol].volume, bars[self.x_symbol].volume)
        self.price_diff = self.y_multiplier*(bars[self.y_symbol].close_price) - self.x_multiplier*(bars[self.x_symbol].close_price)
        self.record_array.append((bars[self.y_symbol].datetime, self.spread_value,self.price_diff))
        self.sam.update_spread(self.spread_value, self.spread_volume)

        # 成交量过滤，成交量低于指定阈值将不会进行操作
        if self.spread_volume < self.spread_volume_threshold:
            self.spread_volume_filter = False
        else:
            self.spread_volume_filter = True

        if not self.sam.inited:
            return

        # 计算技术指标
        sam = self.sam
        std = sam.std(self.std_window)
        mean = sam.sma(self.mean_window)


        # 计算是否满足做多价差要求
        spread_long_entry = mean + self.long_entry_multiplier * std
        self.record_boll_down.append((bars[self.y_symbol].datetime, spread_long_entry))

        spred_long_exit = mean + self.long_exit_multiplier * std

        # 预期收益筛选
        if np.abs(spread_long_entry - spred_long_exit) >= self.difference_filter_num:

            self.spread_long_entry = spread_long_entry
            # self.spread_long_exit = spred_long_exit

        else:
            self.spread_long_entry = None

        # 计算是否满足作空价差要求
        spread_short_entry = mean + self.short_entry_multiplier * std
        self.record_boll_up.append((bars[self.y_symbol].datetime, spread_short_entry))

        spread_short_exit = mean + self.short_exit_multiplier * std

        # 预期收益筛选
        if np.abs(spread_short_entry - spread_short_exit) >= self.difference_filter_num:

            self.spread_short_enrtry = spread_short_entry
            #  self.spread_short_exit = spred_short_exit


        else:
            self.spread_short_enrtry = None

       
        # 获取每个品种持仓
        x_pos = self.get_pos(self.x_symbol)
        y_pos = self.get_pos(self.y_symbol)


        # 平仓逻辑判断(平仓之后,在当前bar中不会再开仓),通过判断open_direction的方向来判断是做多还是做空价差
        if self.open_direction_dict['y_symbol'] == 1 and self.open_direction_dict['x_symbol'] == -1:

            # 更新hedge ratio之后对当前仓位进行清空，当前bar不进行操作
            if self.renew_status:

                self.sell(self.y_symbol, bars[self.y_symbol].close_price*0.95, np.abs(y_pos))
                self.cover(self.x_symbol,bars[self.x_symbol].close_price*1.05, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = -1
                self.close_direction_dict['x_symbol'] = 1
                print(f'时间{bars[self.y_symbol].datetime}','多平 rebalance',f'平多{self.y_symbol} {self.y_fixed_size} 手 平空{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return

            # 做多价差止盈平仓,不进行成交量过滤;
            if self.spread_value >= self.spread_long_exit:

                self.sell(self.y_symbol, bars[self.y_symbol].close_price*0.95, np.abs(y_pos))
                self.cover(self.x_symbol,bars[self.x_symbol].close_price*1.05, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = -1
                self.close_direction_dict['x_symbol'] = 1
                print(f'时间{bars[self.y_symbol].datetime}','多平 调参',f'平多{self.y_symbol} {self.y_fixed_size} 手 平空{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return

            # 做多价差止损平仓,不进行成交量过滤;
            if self.spread_value <= self.spread_long_loss_exit:

                self.sell(self.y_symbol, bars[self.y_symbol].close_price*0.95, np.abs(y_pos))
                self.cover(self.x_symbol,bars[self.x_symbol].close_price*1.05, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = -1
                self.close_direction_dict['x_symbol'] = 1
                print(f'时间{bars[self.y_symbol].datetime}','多平 止损',f'平多{self.y_symbol} {self.y_fixed_size} 手 平空{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return

        elif self.open_direction_dict['y_symbol'] == -1 and self.open_direction_dict['x_symbol'] == 1:

            # 更新hedge ratio之后对当前仓位进行清空，当前bar不进行操作
            if self.renew_status:

                self.cover(self.y_symbol, bars[self.y_symbol].close_price*1.05, np.abs(y_pos))
                self.sell(self.x_symbol,bars[self.x_symbol].close_price*0.95, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = 1
                self.close_direction_dict['x_symbol'] = -1

                print(f'时间{bars[self.y_symbol].datetime}','空平 调参',f'平空{self.y_symbol} {self.y_fixed_size} 手 平多{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return

            # 做空价差止盈平仓,不进行成交量过滤;
            if self.spread_value <= self.spread_short_exit:
                self.cover(self.y_symbol, bars[self.y_symbol].close_price*1.05, np.abs(y_pos))
                self.sell(self.x_symbol,bars[self.x_symbol].close_price*0.95, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = 1
                self.close_direction_dict['x_symbol'] = -1
                print(f'时间{bars[self.y_symbol].datetime}','空平 止盈',f'平空{self.y_symbol} {self.y_fixed_size} 手 平多{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return
            # 做空价差止损平仓,不进行成交量过滤;
            if self.spread_value >= self.spread_short_loss_exit:

                self.cover(self.y_symbol, bars[self.y_symbol].close_price*1.05, np.abs(y_pos))
                self.sell(self.x_symbol,bars[self.x_symbol].close_price*0.95, np.abs(x_pos))

                self.open_direction_dict['y_symbol'] = 0
                self.open_direction_dict['x_symbol'] = 0

                self.close_direction_dict['y_symbol'] = 1
                self.close_direction_dict['x_symbol'] = -1
                print(f'时间{bars[self.y_symbol].datetime}','空平 止损',f'平空{self.y_symbol} {self.y_fixed_size} 手 平多{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')
                return



        # 开仓逻辑判断，只有两个品种都是空仓的时候才会进行开仓
        if x_pos == 0 and y_pos == 0:
            # 做多价差开仓，加入对成交量过滤；
            if self.spread_long_entry and self.spread_value <= self.spread_long_entry and self.spread_volume_filter:
                self.buy(self.y_symbol, bars[self.y_symbol].close_price*1.01, self.y_fixed_size)
                self.short(self.x_symbol,bars[self.x_symbol].close_price*0.99, self.x_fixed_size)

                self.open_direction_dict['y_symbol'] = 1
                self.open_direction_dict['x_symbol'] = -1

                self.close_direction_dict['y_symbol'] = 0
                self.close_direction_dict['x_symbol'] = 0
                # 退出点位
                self.spread_long_exit = mean + self.long_exit_multiplier * std
                self.spread_long_loss_exit = self.spread_value - self.difference_exit_num
                print(f'时间{bars[self.y_symbol].datetime}','多开',f'多{self.y_symbol} {self.y_fixed_size} 手 空{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')

            # 做空价差开仓，加入对成交量过滤；
            elif self.spread_short_entry and self.spread_value >= self.spread_short_entry and self.spread_volume_filter:
                self.short(self.y_symbol, bars[self.y_symbol].close_price*0.99, self.y_fixed_size)
                self.buy(self.x_symbol,bars[self.x_symbol].close_price*1.01, self.x_fixed_size)

                self.open_direction_dict['y_symbol'] = -1
                self.open_direction_dict['x_symbol'] = 1

                self.close_direction_dict['y_symbol'] = 0
                self.close_direction_dict['x_symbol'] = 0
                #退出点位
                self.spread_short_exit = mean + self.short_exit_multiplier * std
                self.spread_short_loss_exit = self.spread_value + self.difference_exit_num
                print(f'时间{bars[self.y_symbol].datetime}','空开',f'空{self.y_symbol} {self.y_fixed_size} 手 多{self.x_symbol} {self.x_fixed_size} 手',f'价差{self.price_diff}')


    def renew_hedge_ratio(self,bar:BarData):
        """
        renew the hedge ratio
        based on the passed days including the not trading days.

        """
        # 计算动态调参时间
        if not self.last_renew_date:

            self.last_renew_date = bar.datetime
            self.renew_date = self.last_renew_date + timedelta(days=self.renew_interval)
            return


        # 动态调参时间
        if bar.datetime >=  self.renew_date:
            self.last_renew_date = bar.datetime
            self.renew_date = self.last_renew_date + timedelta(days=self.renew_interval)

            X = self.ams[self.x_symbol].close[-self.hedge_ratio_window:]
            
            y = self.ams[self.y_symbol].close[-self.hedge_ratio_window:]

            result = sm.OLS(y,X).fit()
            hedge_ratio = result.params
            intercept = 0
            for n in range(10):
                    mulitiplier_x = n*hedge_ratio
                    mulitipler_intercept = n*intercept
                    mulitipler = n
                    if int(mulitiplier_x) >=1:
                        if hedge_ratio*self.x_multiplier < 0:

                            print('warning ! the hedge ratio direction is changed  ')

                        self.x_multiplier = mulitiplier_x
                        self.y_multiplier = mulitipler
                        self.intercept = mulitipler_intercept
                        self.x_fixed_size = int(np.abs(self.fixed_size * self.x_multiplier))
                        self.y_fixed_size = int(np.abs(self.fixed_size * self.y_multiplier))

                        # print(f'the hedge ratio is updated at {bar.datetime}')
                        # print(f'hedge ratio is {self.x_multiplier}, intercept is {self.intercept}, multiplier is {self.y_multiplier}')
                        break
            self.renew_status = True
        else:
            self.renew_status = False
