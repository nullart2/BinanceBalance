import Tkinter as tk
import ttk
import tkFileDialog
import pandas as pd
from binance.client import Client
from binance.websockets import BinanceSocketManager
from binance.enums import *
from binance.exceptions import *
import numpy as np
from datetime import datetime
import time
from tkinter import messagebox
import Queue
from twisted.internet import reactor
import os.path
import ConfigParser
from collections import deque
from scipy.signal import detrend
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2TkAgg
import matplotlib.pyplot as pl
from matplotlib.finance import candlestick_ohlc
import matplotlib.dates as mdates
import warnings
import matplotlib.cbook
warnings.filterwarnings("ignore",category=matplotlib.cbook.mplDeprecation)

def round_decimal(num, decimal):
    '''
    Round a given floating point down number 'num' to the nearest integer
    multiple of another floating point number 'decimal' smaller than
    'num' and return it as a string with up to 8 decimal places,
    dropping any trailing zeros.
    '''
    if decimal > 0:
        x = int(num/decimal)*decimal
    else:
        x = np.round(num, 8)
    return '{0:.8f}'.format(x).rstrip('0').rstrip('.')


class TechnicalAnalysis:
    def __init__(self, symbol, client):
        self.t = deque()
        self.ohlc = deque()
        self.ema26 = deque()
        self.ema12 = deque()
        self.macd = deque()
        self.macd9 = deque()
        self.signal = deque()
        
        data = np.array(client.get_historical_klines(symbol, KLINE_INTERVAL_1MINUTE, '26 hours ago UTC'),dtype=np.float64)
        ema26 = self.ema(26*60, data[:,4])
        ema12 = self.ema(12*60, data[:,4])
        macd = ema12 - ema26
        macd9 = self.ema(9*60, macd)
        signal = macd - macd9
        for row, e26, e12, m, m9, s in zip(data, ema26, ema12, macd, macd9, signal):
            self.t.append(mdates.date2num(datetime.fromtimestamp(int(row[0])/1000)))
            self.ohlc.append([mdates.date2num(datetime.fromtimestamp(int(row[0])/1000)),row[1], row[2], row[3], row[4]])
            self.ema26.append(e26)
            self.ema12.append(e12)
            self.macd.append(m)
            self.macd9.append(m9)
            self.signal.append(s)
        self.get_initial_trend()

    def get_initial_trend(self):
        trend = 0
        if self.macd[-1] > 0:
            trend = 1
        else:
            trend = -1
        s0 = self.signal[0]
        for s in self.signal:
            if s0 >= 0 and s < 0:
                trend = -1
            elif s0 <= 0 and s > 0:
                trend = 1
            s0 = s
        self.trend = trend

    def ema(self, period, data):
        N = len(data)
        ema = np.zeros(N)
        ema[0] = data[0]
        factor = 2.0/(1 + period)
        for i in range(1,N):
            ema[i] = factor * data[i] + (1 - factor) * ema[i-1]
        return ema

    def update_ema(self, period, oldema, newprice):
        factor = 2.0/(1 + period)
        return factor * newprice + (1.0 - factor) * oldema
            
    def append(self, msg):
        self.t.popleft()
        self.ohlc.popleft()
        self.ema26.popleft()
        self.ema12.popleft()
        self.macd.popleft()
        self.macd9.popleft()
        self.signal.popleft()

        self.t.append(mdates.date2num(datetime.fromtimestamp(int(float(msg['k']['T']))/1000)))
        self.ohlc.append([mdates.date2num(datetime.fromtimestamp(int(float(msg['k']['T']))/1000)),float(msg['k']['o']), float(msg['k']['h']), float(msg['k']['l']), float(msg['k']['c'])])
        self.ema26.append(self.update_ema(26*60, self.ema26[-1], float(msg['k']['c'])))
        self.ema12.append(self.update_ema(12*60, self.ema12[-1], float(msg['k']['c'])))
        self.macd.append(self.ema12[-1] - self.ema26[-1])
        self.macd9.append(self.update_ema(9*60, self.macd9[-1], self.macd[-1]))
        self.signal.append(self.macd[-1] - self.macd9[-1])
        if self.trend == -1 and self.signal[-1] > 0:
            self.trend = 1
        elif self.trend == 1 and self.signal[-1] < 0:
            self.trend = -1
    
class BalanceGUI(tk.Frame):
    def __init__(self, parent, coins):
        ''' Initialize the GUI and read the config file '''
        tk.Frame.__init__(self, parent)
        parent.protocol('WM_DELETE_WINDOW', self.on_closing)
        self.parent = parent
        parent.deiconify()
        self.coins = coins
        self.coins_base = coins
        self.queue = Queue.Queue()
        self.trades_placed = 0
        self.trades_completed = 0
        self.trades = []
        self.headers = self.column_headers()
        self.read_config()
        
        #portfolio display
        self.portfolio_view = tk.LabelFrame(parent, text='Portfolio')
        self.portfolio_view.grid(row=0, column=0, columnspan=2, sticky=tk.E + tk.W + tk.N + tk.S)
        self.portfolio = ttk.Treeview(self.portfolio_view, height = len(self.coins), selectmode = 'extended')
        self.portfolio['columns']=('Stored',
                                   'Exchange',
                                   'Locked',
                                   'Target',
                                   'Actual',
                                   'Bid',
                                   'Ask',
                                   'Action',
                                   'Status',
                                   'Event'
                                   )
        for label in self.portfolio['columns']:
            if label == 'Status' or label == 'Event':
                self.portfolio.column(label, width=200)
            elif label == 'Action':
                self.portfolio.column(label, width=120)
            else:
                self.portfolio.column(label, width=100)
            self.portfolio.heading(label, text=label)
        self.portfolio.grid(row=0,column=0)

        for i in range(2):
            self.parent.columnconfigure(i,weight=1, uniform='parent')
            
        #options display
        self.controls_view = tk.LabelFrame(parent, text='Controls')
        for i in range(4):
            self.controls_view.columnconfigure(i,weight=1, uniform='controls')
        self.controls_view.grid(row=1, column=0, sticky=tk.E + tk.W + tk.N + tk.S)
        
        self.key_label = tk.Label(self.controls_view, text='API Key', relief='ridge')
        self.key_label.grid(row=0, column=0,sticky=tk.E + tk.W)
        
        self.secret_label = tk.Label(self.controls_view, text='API Secret', relief='ridge')
        self.secret_label.grid(row=1, column=0,sticky=tk.E + tk.W)
        
        self.key_entry = tk.Entry(self.controls_view, show='*')
        self.key_entry.grid(row=0, column=1, columnspan=2,sticky=tk.E + tk.W)
        
        self.secret_entry = tk.Entry(self.controls_view, show='*')
        self.secret_entry.grid(row=1, column=1, columnspan=2, sticky=tk.E + tk.W)
        
        self.login = tk.Button(self.controls_view,
                               text='Login',
                               command = self.api_enter)
        self.login.grid(row=0, column=3, rowspan=2, sticky=tk.E + tk.W + tk.N+tk.S)

        #Statistics display
        self.stats_view = tk.LabelFrame(parent, text='Statistics')
        self.stats_view.grid(row=1, column=1, sticky=tk.E + tk.W + tk.N + tk.S)
        for i in range(4):
            self.stats_view.columnconfigure(i,weight=1, uniform='stats')

        
        self.trade_currency_value_label = tk.Label(self.stats_view, text='Portfolio Value:', relief='ridge')
        self.trade_currency_value_label.grid(row=0, column=0, sticky=tk.E + tk.W)
        self.trade_currency_value_string = tk.StringVar()
        self.trade_currency_value_string.set('0')
        self.trade_currency_value = tk.Label(self.stats_view, textvariable=self.trade_currency_value_string)
        self.trade_currency_value.grid(row=0, column=1, sticky=tk.E + tk.W)

        self.imbalance_label = tk.Label(self.stats_view, text='Imbalance:', relief='ridge')
        self.imbalance_label.grid(row=1, column=0, sticky=tk.E + tk.W)
        self.imbalance_string = tk.StringVar()
        self.imbalance_string.set('0%')
        self.imbalance_value = tk.Label(self.stats_view, textvariable=self.imbalance_string)
        self.imbalance_value.grid(row=1, column=1, sticky=tk.E + tk.W)


        self.messages_queued_label = tk.Label(self.stats_view, text='Status', relief='ridge')
        self.messages_queued_label.grid(row=0, column=2, sticky=tk.E + tk.W)
        
        self.messages_string = tk.StringVar()
        self.messages_string.set('Up to Date')
        self.messages_queued = tk.Label(self.stats_view, textvariable=self.messages_string)
        self.messages_queued.grid(row=0, column=3, sticky=tk.E + tk.W)

        
        self.trades_label = tk.Label(self.stats_view, text='Trades Placed:', relief='ridge')
        self.trades_label.grid(row=1, column=2, sticky=tk.E + tk.W)
        self.trades_count = tk.IntVar()
        self.trades_count.set(0)
        self.trades_count_display = tk.Label(self.stats_view, textvariable=self.trades_count)
        self.trades_count_display.grid(row=1, column=3, sticky=tk.E + tk.W)


        self.analysis_frame = tk.LabelFrame(parent,text='Technical Analysis')
        self.analysis_frame.columnconfigure(0, weight=4)
        self.analysis_frame.columnconfigure(1, weight=1)
        self.priceplot = Figure(figsize=(8,2), dpi=100)
        self.pricecanvas = FigureCanvasTkAgg(self.priceplot, master=self.analysis_frame)
        self.pricecanvas.get_tk_widget().grid(row=0,column=0,sticky=tk.E+tk.W)
        self.pricecanvas._tkcanvas.config(highlightthickness=1,highlightcolor='black',highlightbackground='black')
        

        self.indicatorplot = Figure(figsize=(8,2), dpi=100)
        self.indicatorcanvas = FigureCanvasTkAgg(self.indicatorplot, master=self.analysis_frame)
        self.indicatorcanvas.get_tk_widget().grid(row=1,column=0,sticky=tk.E+tk.W)
        self.indicatorcanvas._tkcanvas.config(highlightthickness=1,highlightcolor='black',highlightbackground='black')

        self.plotcoin = tk.StringVar()
        self.plotcoin.set('ETH')
        self.coinopts = tk.OptionMenu(self.analysis_frame, self.plotcoin, *[coin for coin in self.coins['coin'] if coin != self.trade_currency], command=self.update_plots)
        self.coinopts.grid(row=0, column=1, stick=tk.E+tk.W+tk.N+tk.S)

        self.plotind = tk.StringVar()
        self.plotind.set('MACD')
        options = ['MACD']
        self.indopts = tk.OptionMenu(self.analysis_frame, self.plotind, *[option for option in options], command=self.update_plots)
        self.indopts.grid(row=1, column=1, stick=tk.E+tk.W+tk.N+tk.S)

        self.analysis_frame.grid(row=2,column=0,columnspan=2,sticky=tk.E+tk.W)

    def read_config(self):
        s_to_ms = 1000
        config = ConfigParser.RawConfigParser(allow_no_value=False)
        config.read('config.ini')
        self.trade_currency = config.get('trades', 'trade_currency')
        if self.trade_currency != 'BTC':
            self.display_error('Config Error',
                               '{0} trading pairs are not supported yet, only BTC'.format(self.trade_currency),
                               quit_on_exit=True)
        self.rebalance_time = int(config.get('trades', 'rebalance_period')) * s_to_ms
        if self.rebalance_time <= 0:
            self.display_error('Config Error',
                               'Rebalance period must be a positive integer (seconds)',
                               quit_on_exit=True)
        self.min_trade_value = float(config.get('trades', 'min_trade_value'))
        if self.min_trade_value <= 0:
            self.min_trade_value = None
        self.trade_type = config.get('trades', 'trade_type')
        if self. trade_type != 'MARKET' and self.trade_type != 'LIMIT':
            self.display_error('Config Error',
                               '{0} is not a supported trade type. Use MARKET or LIMIT'.format(trade_type),
                               quit_on_exit=True)
        self.ignore_backlog = int(config.get('websockets', 'ignore_backlog'))
        
    def on_closing(self):
        ''' Check that all trades have executed
        before starting the save and exit process
        '''
        if self.trades_placed > 0 and self.trades_completed < self.trades_placed:
            if messagebox.askokcancel('Quit', 'Not all trades have completed. Quit anyway?'):
                self.save_and_quit()
        else:
            self.save_and_quit()

    def save_and_quit(self):
        '''
        If trades have been executed in the current session,
        save them to file. Stop all websockets and exit the GUI.
        '''
        if self.trades:
            df = pd.DataFrame(self.trades)
            if os.path.isfile('trade_history.csv'):
                with open('trade_history.csv','a') as f:
                    df.to_csv(f, sep=',', header=False, index=False)
            else:
                with open('trade_history.csv','w') as f:
                    df.to_csv(f, sep=',', header=True, index=False)
        try:
            self.bm.close()
            reactor.stop()
        except AttributeError:
            self.parent.destroy()
        else:
            self.parent.destroy()

    def exit_error(self):
        if self.quit_on_exit:
            self.top.destroy()
            self.save_and_quit()
        else:
            self.top.destroy()

    def display_error(self, title, error, quit_on_exit=False):
        self.quit_on_exit = quit_on_exit
        self.top = tk.Toplevel()
        self.top.title('Login Error')
        msg = tk.Message(self.top, text=error)
        msg.grid(row=0, column=0)
        button = tk.Button(self.top, text="Dismiss", command=self.exit_error)
        button.grid(row=1, column=0)
        self.top.attributes('-topmost', 'true')
            
    def api_enter(self):
        '''
        Log in to Binance with the provided credentials,
        update user portfolio and start listening to price and
        account update websockets.
        '''
        api_key = self.key_entry.get()
        self.key_entry.delete(0,'end')
        api_secret = self.secret_entry.get()
        self.secret_entry.delete(0,'end')
        
        try:
            self.client = Client(api_key, api_secret)
            status = self.client.get_system_status()
        except (BinanceRequestException,
                BinanceAPIException) as e:
            self.display_error('Login Error', e.message)
        else:
            try:
                self.populate_portfolio()
            except BinanceAPIException as e:
                self.display_error('API Error', e.message, quit_on_exit=True)
            else:
                self.start_websockets()
                self.populate_price_history()
                self.process_queue(flush=True)
                self.update_context()
                self.parent.after_idle(self.process_queue)

    def update_context(self):
        self.progressbar.destroy()
        self.progresslabel.destroy()

        self.automate=tk.BooleanVar()
        self.automate.set(False)
        self.automate_text = tk.StringVar()
        self.automate_text.set('Start Automation')
        self.toggle_automate = tk.Button(self.controls_view,
                                         textvariable=self.automate_text,
                                         command=lambda: self.automation(toggle=True))
        self.toggle_automate.grid(row=0, column=0, rowspan=2, columnspan=2, sticky=tk.E + tk.W + tk.N + tk.S)
        self.sell_button = tk.Button(self.controls_view,
                                     text='Execute Sells',
                                     command=self.execute_sells)
        self.sell_button.grid(row=0, column=2, columnspan=2, sticky=tk.E + tk.W)
        self.buy_button = tk.Button(self.controls_view,
                                    text='Execute Buys',
                                    command=self.execute_buys)
        self.buy_button.grid(row=1, column=2, columnspan=2, sticky=tk.E + tk.W)

        
    def populate_price_history(self):
        progress = 0
        self.progress_var.set(progress)
        for coin in self.coins['coin']:
            self.progressbar.update()
            progress += 1
            self.progress_var.set(progress)
            self.updatetext.set('Fetching {0} price history'.format(coin))
            self.progresslabel.update()
            if coin != self.trade_currency:
                self.trendlines[coin] = TechnicalAnalysis(coin+self.trade_currency, self.client)
            
    def start_websockets(self):
        '''
        Start websockets to get price updates for all coins in the portfolio,
        trade execution reports, and user account balance updates.
        Start the message queue processor.
        '''
        self.bm = BinanceSocketManager(self.client)
        self.bm.start()
        trade_currency = self.trade_currency
        symbols = self.coins['symbol'].tolist()
        symbols.remove(trade_currency+trade_currency)
        self.sockets = {}
        for symbol in symbols:
            self.sockets[symbol] = self.bm.start_symbol_ticker_socket(symbol, self.queue_msg)
            self.sockets[symbol+'kline'] = self.bm.start_kline_socket(symbol, self.queue_msg)
        self.sockets['user'] = self.bm.start_user_socket(self.queue_msg)
        self.sockets['exchange_rate'] = self.bm.start_symbol_ticker_socket('BTCUSDT', self.queue_msg)
            
    def populate_portfolio(self):
        '''
        Get all symbol info from Binance needed to
        populate user portfolio data and execute trades
        '''
        self.coins = self.coins_base
        self.portfolio.delete(*self.portfolio.get_children())
        exchange_coins = []
        trade_currency = self.trade_currency
        self.trade_coin = trade_currency
        self.trendlines = {}


        #update the GUI context
        self.key_label.destroy()
        self.key_entry.destroy()
        self.secret_label.destroy()
        self.secret_entry.destroy()
        self.login.destroy()
        
        self.updatetext = tk.StringVar()
        self.updatetext.set('Initializing')
        self.progresslabel = tk.Label(self.controls_view, textvariable=self.updatetext)
        self.progresslabel.grid(row=1, column=0, columnspan=4, sticky=tk.E + tk.W)
        self.progress_var = tk.DoubleVar()
        progress = 0
        self.progress_var.set(progress)
        self.progressbar = ttk.Progressbar(self.controls_view, variable=self.progress_var, maximum=len(self.coins))
        self.progressbar.grid(row=0, column=0, columnspan=4, sticky=tk.E + tk.W)
        self.exchange_rate = float(self.client.get_symbol_ticker(symbol='BTCUSDT')['price'])
        for coin in self.coins['coin']:
            self.progressbar.update()
            progress += 1
            self.progress_var.set(progress)
            self.updatetext.set('Fetching {0} account information'.format(coin))
            self.progresslabel.update()
            pair = coin+trade_currency
            balance = self.client.get_asset_balance(asset=coin)
            if coin != trade_currency:
                price = float(self.client.get_symbol_ticker(symbol=pair)['price'])
                symbolinfo = self.client.get_symbol_info(symbol=pair)['filters']
                minvalue = float(symbolinfo[2]['minNotional'])
                if self.min_trade_value is not None:
                    minvalue = self.min_trade_value
                row = {'coin':              coin,
                       'exchange_balance':  float(balance['free']),
                       'locked_balance':    float(balance['locked']),
                       'minprice':          float(symbolinfo[0]['minPrice']),
                       'maxprice':          float(symbolinfo[0]['maxPrice']),
                       'ticksize':          float(symbolinfo[0]['tickSize']),
                       'minqty':            float(symbolinfo[1]['minQty']),
                       'maxqty':            float(symbolinfo[1]['maxQty']),
                       'stepsize':          float(symbolinfo[1]['stepSize']),                   
                       'minnotional':       minvalue,
                       'symbol':            pair,
                       'askprice' :         price,
                       'bidprice':          price,
                       'price':             price,
                       'last_placement':    None,
                       'last_execution':    None
                       }
            else:
                fixed_balance = self.coins.loc[self.coins['coin'] == coin]['fixed_balance']
                row = {'coin':              coin,
                       'exchange_balance':  float(balance['free']),
                       'locked_balance':    float(balance['locked']),
                       'minprice':          0,
                       'maxprice':          0,
                       'ticksize':          0,
                       'minqty':            0,
                       'maxqty':            0,
                       'stepsize':          0,                   
                       'minnotional':       0,
                       'symbol':            coin+coin,
                       'askprice' :         1.0,
                       'bidprice':          1.0,
                       'price':             1.0,
                       'last_placement':    None,
                       'last_execution':    None
                       }
            exchange_coins.append(row)
        exchange_coins = pd.DataFrame(exchange_coins)
        self.coins = pd.merge(self.coins, exchange_coins, on='coin', how='outer')
        self.coins['value'] = self.coins.apply(lambda row: row.price * (row.exchange_balance +
                                                                        row.fixed_balance), axis=1)
        self.total = np.sum(self.coins['value'])
        self.coins['actual'] = self.coins.apply(lambda row: 100.0 * row.value/self.total, axis=1)
        self.update_status()
        i = 0
        for row in self.coins.itertuples():
            self.portfolio.insert('' ,
                                  i,
                                  iid=row.coin,
                                  text=row.coin,
                                  values=(row.fixed_balance,
                                          row.exchange_balance,
                                          row.locked_balance,
                                          '{0} %'.format(row.allocation),
                                          '{0:.2f} %'.format(row.actual),
                                          round_decimal(row.price, row.ticksize),
                                          round_decimal(row.price, row.ticksize),
                                          '',
                                          ''
                                          )
                                  )
            i += 1
        self.updatetext.set('Testing connection'.format(coin))
        self.dryrun()

        
    def update_status(self):
        '''Update the statistics frame whenever a change occurs in balance or price'''
        value = '{0:.2f}'.format(self.total*self.exchange_rate)
        diff = np.diff(self.coins['actual'].values - self.coins['allocation'].values)
        imbalance = '{0:.2f}%'.format(np.sum(np.absolute(diff)))
        self.trade_currency_value_string.set('$'+value)
        self.imbalance_string.set(imbalance)
        
    def queue_msg(self, msg):
        '''
        Whenever a weboscket receives a message, check for errors.
        If an error occurs, restart websockets. If no error, add it to
        the message queue.
        '''
        if msg['e'] == 'error':
            self.bm.close()
            reactor.stop()
            self.start_websockets()
        else:
            self.queue.put(msg)

    def get_msg(self):
        '''Reroute new websocket messages to the appropriate handler'''
        try:
            msg = self.queue.get(block=False)
        except Queue.Empty:
            pass
        else:
            if msg['e'] == '24hrTicker':
                self.update_price(msg)
            elif msg['e'] == 'outboundAccountInfo':
                self.update_balance(msg)
            elif msg['e'] == 'executionReport':
                self.update_trades(msg)
            elif msg['e'] == 'kline':
                self.update_trends(msg)
                
    def process_queue(self, flush=False):
        '''
        Check for new messages in the queue periodically.
        Recursively calls itself to perpetuate the process.
        '''
        if flush:
            while not self.queue.empty():
                self.get_msg()
        else:
            self.get_msg()
            self.master.after_idle(self.process_queue)
        n = self.queue.qsize()
        if n > self.ignore_backlog:
            self.messages_string.set('{0} Updates Queued'.format(n))
        else:
            self.messages_string.set('Up to Date')

    def update_trends(self, msg):
        if msg['k']['x']:
            print 'appending'
            coin = msg['s'][:-len(self.trade_coin)]
            self.trendlines[coin].append(msg)
            plotcoin = self.plotcoin.get()
            if plotcoin == coin:
                self.update_plots()
            print 'done'

    def update_plots(self):
        print 'updating plot'
        coin = self.plotcoin.get()
        self.priceplot.clf()
        ohlc = self.trendlines[coin].ohlc
        ind = self.trendlines[coin].signal
        self.priceax = self.priceplot.add_subplot(111)
        self.priceax.xaxis_date()
        self.priceax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%H:%M:%S'))
        candlestick_ohlc(self.priceax, ohlc, width=0.005,colorup='g',colordown='r')
        self.priceax.plot(self.trendlines[coin].t, self.trendlines[coin].ema26,self.trendlines[coin].t, self.trendlines[coin].ema12)
        self.pricecanvas.show()

        self.indicatorplot.clf()
        self.indax = self.indicatorplot.add_subplot(111)
        self.indax.xaxis_date()
        self.indax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%H:%M:%S'))
        self.indax.plot(self.trendlines[coin].t, self.trendlines[coin].signal)
        self.indax.axhline(y=0)
        self.indicatorcanvas.show()
        print 'done'
        

    def update_trades(self, msg):
        ''' Update balances whenever a partial execution occurs '''
        coin = msg['s'][:-len(self.trade_coin)]
        savemsg = {self.headers[key] : value for key, value in msg.items()}
        filled = float(savemsg['cumulative_filled_quantity'])
        orderqty = float(savemsg['order_quantity'])
        side = savemsg['side']
        if filled >= orderqty:
            self.coins.loc[self.coins['coin'] == coin, 'last_execution'] = time.mktime(datetime.now().timetuple())
            self.trades_completed += 1
            self.trades_count.set(self.trades_completed)
        self.portfolio.set(coin, column='Event', value = '{0} {1}/{2} {3}'.format(side, filled, orderqty,datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        self.trades.append(savemsg)    

    def update_balance(self, msg):
        '''
        Update user balances internally and on the
        display whenever an account update message is received.
        '''
        balances = msg['B']
        coins = self.coins['coin'].values
        for balance in balances:
            coin = balance['a']
            if coin in coins:
                exchange_balance = float(balance['f']) + float(balance['l'])
                locked_balance = float(balance['l'])
                self.portfolio.set(coin, column='Exchange', value=round_decimal(exchange_balance,self.coins.loc[self.coins['coin'] == coin]['stepsize'].values[0]))
                self.portfolio.set(coin, column='Locked', value=round_decimal(locked_balance,self.coins.loc[self.coins['coin'] == coin]['stepsize'].values[0]))
                self.coins.loc[self.coins['coin'] == coin, 'exchange_balance'] = exchange_balance
                self.coins.loc[self.coins['coin'] == coin, 'locked_balance'] = locked_balance
                ask = self.coins.loc[self.coins['coin'] == coin, 'askprice'].values[0]
                value = (self.coins.loc[self.coins['coin'] == coin, 'exchange_balance'].values[0] +
                         self.coins.loc[self.coins['coin'] == coin, 'fixed_balance'].values[0]) * ask
                self.coins.loc[self.coins['coin'] == coin, 'value'] = value

        self.total = np.sum(self.coins['value']) 
        self.coins['actual'] = self.coins.apply(lambda row: 100.0 * row.value / self.total, axis=1)
        for row in self.coins.itertuples():
            coin = row.coin
            actual = '{0:.2f}%'.format(self.coins.loc[self.coins['coin'] == coin, 'actual'].values[0])
            self.portfolio.set(coin, column='Actual', value=actual)
        self.update_actions()
        self.update_status()
        
    def update_price(self, msg):
        '''
        Update symbol prices and user allocations internally
        and on the display whenever a price update is received.
        '''
        coin = msg['s'][:-len(self.trade_currency)]
        ask = float(msg['a'])
        bid = float(msg['b'])
        if msg['s'] == 'BTCUSDT':
            self.exchange_rate = (ask + bid)/2.0
        else:
            askprice = round_decimal(ask,self.coins.loc[self.coins['coin'] == coin, 'ticksize'].values[0])
            bidprice = round_decimal(bid,self.coins.loc[self.coins['coin'] == coin, 'ticksize'].values[0])
            self.portfolio.set(coin, column='Ask', value=askprice)
            self.coins.loc[self.coins['coin'] == coin, 'askprice'] = ask
            self.portfolio.set(coin, column='Bid', value=bidprice)
            self.coins.loc[self.coins['coin'] == coin, 'bidprice'] = bid
            value = (self.coins.loc[self.coins['coin'] == coin, 'exchange_balance'].values[0] +
                     self.coins.loc[self.coins['coin'] == coin, 'fixed_balance'].values[0]) * ask
            self.coins.loc[self.coins['coin'] == coin, 'value'] = value
            self.total = np.sum(self.coins['value'])
            self.coins['actual'] = self.coins.apply(lambda row: 100.0 * row.value / self.total, axis=1)
            for row in self.coins.itertuples():
                coin = row.coin
                actual = '{0:.2f}%'.format(self.coins.loc[self.coins['coin'] == coin, 'actual'].values[0])
                self.portfolio.set(coin, column='Actual', value=actual)
            self.update_actions()
            self.update_status()

    def update_actions(self):
        '''
        Calcuate required trades and update the main GUI
        '''
        for row in self.coins.itertuples():
            tradecoin_balance = np.squeeze(self.coins[self.coins['coin'] == self.trade_coin]['exchange_balance'].values)
            tradecoin_locked = np.squeeze(self.coins[self.coins['coin'] == self.trade_coin]['locked_balance'].values)
            tradecoin_free = tradecoin_balance - tradecoin_locked
            dif = (row.allocation - row.actual) / 100.0 * self.total / row.price

            if dif < 0:
                side = SIDE_SELL
            if dif > 0:
                side = SIDE_BUY
            
            status = ''
            coin = row.coin
            pair = coin + self.trade_coin
            balance = float(row.exchange_balance) - float(row.locked_balance)
            actual = row.actual
            qty = np.absolute(dif)

            action = '{0} {1}'.format(side, round_decimal(qty, row.stepsize))
            if side == SIDE_SELL:
                price = row.bidprice
            if side == SIDE_BUY:
                price = row.askprice
            if side == SIDE_SELL and qty > balance and coin != self.trade_coin:
                status = 'Insufficient ' + coin + ' for sale'
            if coin == self.trade_coin:
                status = 'Ready'
            elif qty < row.minqty or qty * price < row.minnotional:
                status = status = 'Trade value too small ({0:.0f}%)'.format(100.0 * qty * price / row.minnotional)
            elif qty > row.maxqty:
                status = 'Trade quantity too large'
            elif side == SIDE_BUY and qty * price > tradecoin_free:
                status = 'Insufficient ' + self.trade_coin + ' for purchase'
            else:
                status = 'Trade Ready'
            self.portfolio.set(coin, column='Status', value=status)
            self.portfolio.set(coin, column='Action', value=action)
            
    def execute_transactions(self, side, dryrun):
        '''
        Calculate the required trade for each coin and execute
        them if they belong to the appropriate side
        '''
        for row in self.coins.itertuples():
            self.process_queue(flush=True)
            tradecoin_balance = np.squeeze(self.coins[self.coins['coin'] == self.trade_coin]['exchange_balance'].values)
            tradecoin_locked = np.squeeze(self.coins[self.coins['coin'] == self.trade_coin]['locked_balance'].values)
            tradecoin_free = tradecoin_balance - tradecoin_locked
            dif = (row.allocation - row.actual) / 100.0 * self.total / row.price
            if dif < 0 and side == SIDE_BUY:
                continue
            if dif > 0 and side == SIDE_SELL:
                continue
            status = ''
            coin = row.coin
            pair = coin + self.trade_coin
            balance = float(row.exchange_balance) - float(row.locked_balance)
            actual = row.actual
            qty = np.absolute(dif)
            action = '{0} {1}'.format(side, round_decimal(qty, row.stepsize))
            last_placement = np.squeeze(self.coins[self.coins['coin'] == coin]['last_placement'].values)
            last_execution = np.squeeze(self.coins[self.coins['coin'] == coin]['last_execution'].values)            
            if side == SIDE_SELL:
                price = row.bidprice
            if side == SIDE_BUY:
                price = row.askprice
            if side == SIDE_SELL and qty > balance and coin != self.trade_coin:
                status = 'Insufficient ' + coin + ' for sale'
            if coin == self.trade_coin:
                status = 'Ready'
            elif qty < row.minqty or qty * price < row.minnotional:
                status = 'Trade value too small ({0:.0f}%)'.format(100.0 * qty * price / row.minnotional)
            elif qty > row.maxqty:
                status = 'Trade quantity too large'
            elif side == SIDE_BUY and qty * price > tradecoin_free:
                status = 'Insufficient ' + self.trade_coin + ' for purchase'
            elif last_placement == None or last_execution >= last_placement:
                trade_currency = self.trade_coin
                try:
                    self.place_order(coin, pair, self.trade_type, qty, price, side, dryrun, row.stepsize, row.ticksize)
                except (BinanceRequestException,
                        BinanceAPIException,
                        BinanceOrderException,
                        BinanceOrderMinAmountException,
                        BinanceOrderMinPriceException,
                        BinanceOrderMinTotalException,
                        BinanceOrderUnknownSymbolException,
                        BinanceOrderInactiveSymbolException) as e:
                    self.portfolio.set(coin, column='Event', value=e.message)
                else:
                    status = 'Trade Ready'
                    if not dryrun:
                        self.trades_placed += 1
                        status = 'Trade Placed'
                        self.portfolio.set(coin, column='Event', value='Trade Placed')
            self.portfolio.set(coin, column='Status', value=status)
            self.portfolio.set(coin, column='Action', value=action)
            
            
    def automation(self, toggle=False):
        if toggle:
            if not self.automate.get():
                self.automate_text.set('Stop Automation')
            else:
                self.automate_text.set('Start Automation')
            self.automate.set(not self.automate.get())
        if self.automate.get():
            self.execute_sells()
            self.execute_buys()
            self.rebalance_callback = self.parent.after(self.rebalance_time, self.automation)
        else:
            self.parent.after_cancel(self.rebalance_callback)
    
    def execute_sells(self):
        '''
        Perform any sells required by overachieving coins
        '''
        self.execute_transactions(side=SIDE_SELL, dryrun=False)

    def execute_buys(self):
        '''
        Perform any buys required by underachieving coins
        '''
        self.execute_transactions(side=SIDE_BUY, dryrun=False)

    def dryrun(self):
        '''
        perform a dry run to list what trades are required
        '''
        self.execute_transactions(side=SIDE_SELL, dryrun=True)
        self.execute_transactions(side=SIDE_BUY, dryrun=True)        
        
    def place_order(self, coin, pair, trade_type,
                    quantity, price, side, dryrun,
                    stepsize, ticksize):
        '''
        Format and place an order using the Binance API
        '''
        if trade_type == 'LIMIT':
            if dryrun:
                order = self.client.create_test_order(symbol=pair,
                                                      side=side,
                                                      type=ORDER_TYPE_LIMIT,
                                                      timeInForce=TIME_IN_FORCE_GTC,
                                                      quantity=round_decimal(quantity, stepsize),
                                                      price=round_decimal(price, ticksize))
            else:
                order = self.client.create_order(symbol=pair,
                                                 side=side,
                                                 type=ORDER_TYPE_LIMIT,
                                                 timeInForce=TIME_IN_FORCE_GTC,
                                                 quantity=round_decimal(quantity, stepsize),
                                                 price=round_decimal(price, ticksize))
        elif trade_type == 'MARKET':
            if dryrun:
                order = self.client.create_test_order(symbol=pair,
                                                      side=side,
                                                      type=ORDER_TYPE_MARKET,
                                                      quantity=round_decimal(quantity, stepsize))
            else:
                order = self.client.create_order(symbol=pair,
                                                 side=side,
                                                 type=ORDER_TYPE_MARKET,
                                                 quantity=round_decimal(quantity, stepsize))
        if not dryrun:
            self.coins.loc[self.coins['coin'] == coin, 'last_placement'] = time.mktime(datetime.now().timetuple())
            
    def column_headers(self):
        ''' define human readable aliases for the headers in trade execution reports. '''
        return {'e': 'event_type',
                'E': 'event_time',
                's': 'symbol',
                'c': 'client_order_id',
                'S': 'side',
                'o': 'type',
                'O': 'unknown_1',
                'f': 'time_in_force',
                'q': 'order_quantity',
                'p': 'order_price',
                'P': 'stop_price',
                'F': 'iceberg_quantity',
                'g': 'ignore_1',
                'C': 'original_client_order_id',
                'x': 'current_execution_type',
                'X': 'current_order_status',
                'r': 'order_reject_reason',
                'i': 'order_id',
                'l': 'last_executed_quantity',
                'z': 'cumulative_filled_quantity',
                'Z': 'unknown_2',
                'L': 'last_executed_price',
                'n': 'commission_amount',
                'N': 'commission_asset',
                'T': 'transaction_time',
                't': 'trade_id',
                'I': 'ignore_2',
                'w': 'order_working',
                'm': 'maker_side',
                'M': 'ignore_3'}
 
def main():
    portfolio = 'allocation.csv'
    coins = pd.read_csv(portfolio)
    if not np.sum(coins['allocation']) == 100:
        messagebox.showinfo('Bad Configuration','Your coin allocations to not sum to 100%')
    else:
        root = tk.Tk()
        root.withdraw()
        BalanceGUI(root, coins).grid(row=0, column=0)
        root.wm_title('BinanceBalance')
        root.mainloop()

if __name__=='__main__':
    main()
