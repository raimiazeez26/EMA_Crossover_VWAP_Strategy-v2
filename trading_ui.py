import dash
from dash import dcc, html, Input, Output, State, ALL  # Added ALL
import dash_bootstrap_components as dbc
import pandas as pd
import json
import os
import requests
import random
from datetime import datetime
from io import StringIO
from trading_engine import AlpacaTradingBot  # Import the trading engine

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG], title="EMA cross + VWAP strategy")
server = app.server


# Load NASDAQ symbols (updated daily)
def fetch_nasdaq_symbols():
    try:
        url = "https://pkgstore.datahub.io/core/nasdaq-listings/nasdaq-listed_csv/data/7665719fb51081ba0bd834fde71ce822/nasdaq-listed_csv.csv"
        response = requests.get(url)
        df = pd.read_csv(StringIO(response.text))
        return [{'label': f"{row['Symbol']} - {row['Company Name']}", 'value': row['Symbol']}
                for _, row in df.iterrows()]
    except:
        return [  # Fallback symbols
            {'label': 'AAPL - Apple Inc.', 'value': 'AAPL'},
            {'label': 'MSFT - Microsoft Corporation', 'value': 'MSFT'},
            {'label': 'AMZN - Amazon.com Inc.', 'value': 'AMZN'},
            {'label': 'GOOGL - Alphabet Inc.', 'value': 'GOOGL'},
            {'label': 'META - Meta Platforms Inc.', 'value': 'META'}
        ]


# Load initial settings
SETTINGS_FILE = 'trading_settings.json'

def load_settings():
    default = {
        'tickers': ['AAPL', 'MSFT'],
        'risk_percent': 1,
        'accounts': []
    }

    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                loaded = json.load(f)
                # Merge all settings
                default.update(loaded)
                # Validate accounts
                default['accounts'] = [
                    acc for acc in loaded.get('accounts', [])
                    if all(k in acc for k in ('name', 'key', 'secret', 'base_url'))
                ]
    except Exception as e:
        print(f"Error loading settings: {e}")

    return default


def save_settings(settings):
    try:
        # Preserve all settings
        current_settings = load_settings()
        current_settings.update(settings)

        # Validate accounts
        current_settings['accounts'] = [
            acc for acc in current_settings.get('accounts', [])
            if all(k in acc for k in ('name', 'key', 'secret', 'base_url'))
        ]

        with open(SETTINGS_FILE, 'w') as f:
            json.dump(current_settings, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

accounts = load_settings().get('accounts', [])

app.layout = dbc.Container([
    # html.Div(id='app-load', style={'display': 'none'}, **{'data-trigger': True}),
    dcc.Store(id='app-load', data=True),
    dcc.Store(id='signal-store'),
    dcc.Store(id='account-store', data=[]),
    # Add to layout
    dcc.Store(id='last-signals', data={}),

    dbc.Row([

        dbc.Col(html.H1("EMA Cross + VWAP strategy",
                        className='text-center mb-4 text-primary'),
                width=12)
    ]),

    dbc.Toast(
        id="save-toast",
        header="Settings Saved",
        icon="success",
        dismissable=True,
        is_open=False,
        duration=3000,
        className="position-fixed top-0 end-0 m-3"
    ),

    dbc.Row([

        # Add to layout
        # dbc.Col([
        #     dbc.Card([
        #         dbc.CardHeader("Account Management", className='h5'),
        #         dbc.CardBody([
        #             html.Div(id='account-list'),
        #             dbc.Button("Add Account", id='add-account-btn', className='mb-3'),
        #             dbc.Modal([
        #                 dbc.ModalHeader("Add Trading Account"),
        #                 dbc.ModalBody([
        #                     dbc.Input(id='account-name', placeholder="Account Name"),
        #                     dbc.Input(id='api-key', placeholder="API Key"),
        #                     dbc.Input(id='api-secret', type='password', placeholder="API Secret"),
        #                     dbc.Input(id='base-url', placeholder="Base URL",
        #                              value="https://paper-api.alpaca.markets")
        #                 ]),
        #                 dbc.ModalFooter([
        #                     dbc.Button("Save", id="save-account", className="ms-auto")
        #                 ])
        #             ], id="account-modal", is_open=False)
        #         ])
        #     ], className='shadow-lg border-warning')
        # ], md=4),

        # Add to layout
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Account Management", className='h5'),
                dbc.CardBody([
                    html.Div([
                        dbc.ListGroup([
                                dbc.ListGroupItem([
                                    html.Div([
                                        html.Strong(acc['name']),
                                        html.Small(f"Key: {acc['key'][:4]}...", className='d-block text-muted')
                                    ]),
                                    dbc.Button("×", color="danger", size="sm", className='ms-auto',
                                               id={'type': 'delete-account', 'index': idx})
                                ], className='d-flex justify-content-between align-items-center')
                                for idx, acc in enumerate(accounts)
                            ])
],


                             id='account-list', className='mb-3'),
                    dbc.Button("Add Account", id='add-account-btn', className='w-100'),
                    dbc.Modal([
                        dbc.ModalHeader("Add Trading Account"),
                        dbc.ModalBody([
                            dbc.Input(id='account-name', placeholder="Account Name", className='mb-2'),
                            dbc.Input(id='api-key', placeholder="API Key", className='mb-2'),
                            dbc.Input(id='api-secret', type='password', placeholder="API Secret", className='mb-2'),
                            dbc.Input(id='base-url', placeholder="Base URL",
                                      value="https://paper-api.alpaca.markets", className='mb-2')
                        ]),
                        dbc.ModalFooter([
                            dbc.Button("Save", id="save-account", className="ms-auto")
                        ])
                    ], id="account-modal", is_open=False)
                ])
            ], className='shadow-lg border-warning')
        ], md=4),

        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Trading Configuration", className='h5'),
                dbc.CardBody([
                    html.Label("Select NASDAQ Stocks:", className='mb-2'),
                    dcc.Dropdown(
                        id='ticker-selector',
                        options=fetch_nasdaq_symbols(),
                        multi=True,
                        value=load_settings()['tickers'],
                        placeholder="Search 3,000+ NASDAQ stocks...",
                        className='mb-3'
                    ),

                    html.Label("Risk Per Trade (%):", className='mb-2'),
                    dcc.Slider(
                        id='risk-slider',
                        min=0.5,
                        max=5,
                        step=0.5,
                        marks={i: f'{i}%' for i in range(0, 6)},
                        value=1,
                        className='mb-4'
                    ),

                    dbc.Button("Save Configuration",
                               id='save-button',
                               color='success',
                               className='w-100',
                               n_clicks=0)
                ])
            ], className='shadow-lg border-primary')
        ], md=4),

        dbc.Col([
            dbc.Card([
                dbc.CardHeader("Live Trading Signals", className='h5'),
                dbc.CardBody([
                    html.Div(id='signal-display',
                             className='table-responsive',
                             style={'maxHeight': '600px', 'overflowY': 'auto'}),
                    dcc.Interval(
                        id='update-interval',
                        interval=300 * 1000,  # 1 minute
                        n_intervals=0
                    )
                ])
            ], className='shadow-lg border-success')
        ], md=8)
    ], className='mt-4')
], fluid=True, className='p-4')

@app.callback(
    Output('account-list', 'children', allow_duplicate=True),
    Input('app-load', 'data'),
    prevent_initial_call=True
)
def initialize_accounts(_):
    return create_account_list()

# @app.callback(
#     [Output('account-modal', 'is_open'),
#      Output('account-list', 'children'),
#      Output('account-store', 'data', allow_duplicate=True),
#      Output('save-toast', 'is_open', allow_duplicate=True),
#      Output('save-toast', 'header', allow_duplicate=True),
#      Output('save-toast', 'icon', allow_duplicate=True)],
#     [Input('add-account-btn', 'n_clicks'),
#      Input('save-account', 'n_clicks'),
#      Input({'type': 'delete-account', 'index': ALL}, 'n_clicks')],
#     [State('account-modal', 'is_open'),
#      State('account-name', 'value'),
#      State('api-key', 'value'),
#      State('api-secret', 'value'),
#      State('base-url', 'value'),
#      State('account-store', 'data')],
#     prevent_initial_call=True
# )
@app.callback(
    [Output('account-modal', 'is_open'),
     Output('account-list', 'children'),
     # Output('account-store', 'data', allow_duplicate=True),
     Output('save-toast', 'is_open', allow_duplicate=True),
     Output('save-toast', 'header', allow_duplicate=True),
     Output('save-toast', 'icon', allow_duplicate=True)],
    [Input('add-account-btn', 'n_clicks'),
     Input('save-account', 'n_clicks'),
     Input({'type': 'delete-account', 'index': ALL}, 'n_clicks')],
    [State('account-modal', 'is_open'),
     State('account-name', 'value'),
     State('api-key', 'value'),
     State('api-secret', 'value'),
     State('base-url', 'value')],
    # State('account-store', 'data'),
    prevent_initial_call=True

)
def manage_accounts(add_clicks, save_clicks, delete_clicks, is_open, name, key, secret, url):
    ctx = dash.callback_context
    settings = load_settings()
    current_accounts = settings.get('accounts', [])

    if not ctx.triggered:
        return dash.no_update

    # Add account button
    if ctx.triggered[0]['prop_id'] == 'add-account-btn.n_clicks':
        return not is_open, dash.no_update, False, "", ""

    # Save account button
    if ctx.triggered[0]['prop_id'] == 'save-account.n_clicks':
        if not all([name, key, secret]):
            return dash.no_update, dash.no_update, True, "Missing Required Fields", "danger"

        # Check for duplicates
        if any(acc['key'] == key and acc['secret'] == secret for acc in current_accounts):
            return dash.no_update, dash.no_update, True, "Account Already Exists", "danger"

        new_account = {
            'name': name,
            'key': key,
            'secret': secret,
            'base_url': url or "https://paper-api.alpaca.markets"
        }

        updated_accounts = current_accounts + [new_account]
        success = save_settings({'accounts': updated_accounts})

        return (
            False,
            create_account_list(),  # Refresh with updated accounts
            True,
            "Account Saved" if success else "Save Failed",
            "success" if success else "danger"
        )

    # Delete account
    if any('delete-account' in x['prop_id'] for x in ctx.triggered):
        current_accounts = settings.get('accounts', [])

        delete_index = json.loads(ctx.triggered[0]['prop_id'].split('.')[0])['index']
        updated_accounts = [acc for idx, acc in enumerate(current_accounts) if idx != delete_index]
        success = save_settings({'accounts': updated_accounts})

        return (
            dash.no_update,
            create_account_list(),  # Refresh with updated accounts
            True,
            "Account Removed" if success else "Removal Failed",
            "success" if success else "danger"
        )

    return dash.no_update

#
# def create_account_list():
#     """Load accounts directly from settings file"""
#     accounts = load_settings().get('accounts', [])
#
#     if not accounts:
#         return html.Div("No accounts configured. Add one to start trading.", className='text-muted')
#
#     return dbc.ListGroup([
#         dbc.ListGroupItem([
#             html.Div([
#                 html.Strong(acc['name']),
#                 html.Small(f"Key: {acc['key'][:4]}...", className='d-block text-muted')
#             ]),
#             dbc.Button("×", color="danger", size="sm", className='ms-auto',
#                        id={'type': 'delete-account', 'index': idx})
#         ], className='d-flex justify-content-between align-items-center')
#         for idx, acc in enumerate(accounts)  # Use only saved accounts
#     ])


# In trading_ui.py - Updated account display
def create_account_list():
    accounts = load_settings().get('accounts', [])
    bot = AlpacaTradingBot(accounts=accounts, last_signal={})

    if not accounts:
        return html.Div("No accounts configured. Add one to start trading.", className='text-muted')

    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.Strong(client['name']),
                html.Small([
                    html.Div(f"Acct: {bot.get_account_info(client)[1][-4:] if idx == 0 else ''}",
                             className='text-muted'),
                    html.Div(f"Balance: ${bot.get_account_info(client)[0]:,.2f}",
                             className='text-info'),
                    html.Div(f"Status: {'🟢' if bot.get_account_info(client)[2] else '🔴'}",
                             className='text-success' if bot.get_account_info(client)[2] else 'text-danger')
                ])
            ]),
            dbc.Button("×", color="danger", size="sm", className='ms-auto',
                       id={'type': 'delete-account', 'index': idx})
        ], className='d-flex justify-content-between align-items-center')
        for idx, client in enumerate(bot.clients)
    ])


# Update the update_signals callback
@app.callback(
    [Output('signal-display', 'children'),
     Output('last-signals', 'data')],
    [Input('update-interval', 'n_intervals'),
     Input('account-store', 'modified_timestamp')],
    [State('ticker-selector', 'value'),
     State('last-signals', 'data')]
)
# def update_signals(n, ts, selected_tickers, last_signals):
#     accounts = load_settings().get('accounts', [])
#
#     try:
#         last_signals = last_signals or {}
#         bot = AlpacaTradingBot(accounts=accounts or [], last_signal=last_signals)
#         new_signals = last_signals.copy()
#         signals_to_display = []
#         # if bot.market_is_open():
#
#         for ticker in selected_tickers:
#             # Check for exit conditions
#             exit_signals = bot.check_exit_conditions(ticker)
#             for exit_signal in exit_signals:
#                 bot.send_telegram_alert(ticker, exit_signal)
#                 # Close position for each client
#                 for client in bot.clients:
#                     bot.close_position(client, ticker)
#                 # Update last signal to NEUTRAL
#                 new_signals[ticker] = 'NEUTRAL'
#                 signals_to_display.append({
#                     'symbol': ticker,
#                     'signal': 'NEUTRAL',
#                     'price': exit_signal['exit_price'],
#                     'time': datetime.now().strftime('%H:%M:%S')
#                 })
#
#             # Generate entry signal
#             entry_signal = bot.generate_signal(ticker, risk_percent=1)
#             entry_signal['direction'] = random.choice(['BUY', 'SELL', 'NEUTRAL']) # dummy random signal assignment
#             current_direction = entry_signal['direction']
#             print(f"Entry signal for {ticker}: {current_direction}")
#
#             previous_direction = last_signals.get(ticker, 'NEUTRAL')
#
#             # Handle signal change
#             if current_direction != previous_direction:
#                 # Send Telegram alert
#                 bot.send_telegram_alert(ticker, entry_signal)
#                 # Execute trade if market is open
#                 if current_direction != 'NEUTRAL':
#                     for client in bot.clients:
#                         balance = bot.get_account_info(client)[0]
#                         risk_amount = balance * (1 / 100)  # Using 1% risk for example
#                         qty = int(risk_amount / entry_signal['entry'])
#                         bot.execute_trade(client, ticker, qty, current_direction)
#                 # If changing to NEUTRAL, close position
#                 elif current_direction == 'NEUTRAL' and previous_direction != 'NEUTRAL':
#                     for client in bot.clients:
#                         bot.close_position(client, ticker)
#
#                 # Update last signal
#                 new_signals[ticker] = current_direction
#                 print(f"Last signal: {new_signals}")
#
#             # Add to display
#             signals_to_display.append({
#                 'symbol': ticker,
#                 'signal': current_direction,
#                 'price': entry_signal['entry'],
#                 'time': datetime.now().strftime('%H:%M:%S')
#             })
#
#         # Create table
#         table = dbc.Table([
#             html.Thead(html.Tr([
#                 html.Th("Symbol"), html.Th("Company Name"),
#                 html.Th("Signal"), html.Th("Price"), html.Th("Timestamp")
#             ])),
#             html.Tbody([
#                 html.Tr([
#                     html.Td(sig['symbol']),
#                     html.Td(next((item['label'].split(' - ')[1] for item in fetch_nasdaq_symbols() if item['value'] == sig['symbol']), "")),
#                     html.Td(html.Span(sig['signal'], className=f"badge bg-{'success' if sig['signal'] == 'BUY' else 'danger' if sig['signal'] == 'SELL' else 'secondary'}")),
#                     html.Td(f"${sig['price']:.2f}"),
#                     html.Td(sig['time'])
#                 ]) for sig in signals_to_display
#             ])
#         ], striped=True, hover=True, className='align-middle')
#
#         return table, new_signals
#         # else:
#         #     print("MARKET NOT OPEN!")
#
#     except Exception as e:
#         return dbc.Alert(f"System error: {str(e)}", color="danger"), last_signals

def update_signals(n, ts, selected_tickers, last_signals):
    accounts = load_settings().get('accounts', [])

    try:
        last_signals = last_signals or {}
        bot = AlpacaTradingBot(accounts=accounts or [], last_signal=last_signals)
        new_signals = last_signals.copy()
        signals_to_display = []

        for ticker in selected_tickers:
            # Process exit conditions first
            exit_signals = bot.check_exit_conditions(ticker)
            for exit_signal in exit_signals:
                if bot.market_is_open():
                    print('MARKET IS OPEN')
                    bot.send_telegram_alert(ticker, exit_signal)
                    for client in bot.clients:
                        bot.close_position(client, ticker)
                else:
                    print('MARKET IS CLOSED')

                new_signals[ticker] = 'NEUTRAL'
                signals_to_display.append({
                    'symbol': ticker,
                    'signal': 'NEUTRAL',
                    'price': exit_signal['exit_price'],
                    'time': datetime.now().strftime('%H:%M:%S')
                })

            # Generate new signal
            entry_signal = bot.generate_signal(ticker, risk_percent=1)
            entry_signal['direction'] = random.choice(['BUY', 'SELL', 'NEUTRAL'])  # Temp test value
            current_direction = entry_signal['direction']
            previous_direction = last_signals.get(ticker, 'NEUTRAL')

            # Only process if signal changed
            if current_direction != previous_direction:
                # Always send telegram notification for signal changes
                bot.send_telegram_alert(ticker, entry_signal)

                if bot.market_is_open():
                    print('MARKET IS OPEN')

                    # Handle entry signals
                    if current_direction in ['BUY', 'SELL']:
                        for client in bot.clients:
                            try:
                                # Check existing position
                                position = client['rest'].get_position(ticker)
                                if position:
                                    print(f"Skipping {current_direction} order for {ticker} - existing position found")
                                    continue
                            except Exception as e:
                                # No position exists, proceed with trade
                                pass

                            # Calculate position size
                            balance = bot.get_account_info(client)[0]
                            risk_amount = balance * (1 / 100)
                            qty = int(risk_amount / entry_signal['entry'])

                            # Execute trade
                            bot.execute_trade(client, ticker, qty, current_direction)

                    # Handle exit signals (transition to NEUTRAL)
                    elif current_direction == 'NEUTRAL' and previous_direction in ['BUY', 'SELL']:
                        for client in bot.clients:
                            bot.close_position(client, ticker)

                # Update signal tracking
                new_signals[ticker] = current_direction

            else:
                print('MARKET IS CLOSED')

            # Add to display (always show current state)
            signals_to_display.append({
                'symbol': ticker,
                'signal': current_direction,
                'price': entry_signal['entry'],
                'time': datetime.now().strftime('%H:%M:%S')
            })

        # Create table with latest signals
        table = dbc.Table([
            html.Thead(html.Tr([
                html.Th("Symbol"), html.Th("Company Name"),
                html.Th("Signal"), html.Th("Price"), html.Th("Timestamp")
            ])),
            html.Tbody([
                html.Tr([
                    html.Td(sig['symbol']),
                    html.Td(next((item['label'].split(' - ')[1] for item in fetch_nasdaq_symbols()
                                  if item['value'] == sig['symbol']), "")),
                    html.Td(html.Span(sig['signal'],
                                      className=f"badge bg-{'success' if sig['signal'] == 'BUY' else 'danger' if sig['signal'] == 'SELL' else 'secondary'}")),
                    html.Td(f"${sig['price']:.2f}"),
                    html.Td(sig['time'])
                ]) for sig in signals_to_display
            ])
        ], striped=True, hover=True, className='align-middle')

        return table, new_signals

    except Exception as e:
        return dbc.Alert(f"System error: {str(e)}", color="danger"), last_signals

@app.callback(
    [Output('ticker-selector', 'value', allow_duplicate=True),
     Output('risk-slider', 'value', allow_duplicate=True),
     Output('save-toast', 'is_open', allow_duplicate=True),
     Output('save-toast', 'header', allow_duplicate=True),
     Output('save-toast', 'icon', allow_duplicate=True)],
    [Input('save-button', 'n_clicks')],
    [State('ticker-selector', 'value'),
     State('risk-slider', 'value')],
    prevent_initial_call=True
)
def save_configuration(n_clicks, selected_tickers, risk_percent):
    if n_clicks > 0:
        settings = {
            'tickers': selected_tickers,
            'risk_percent': risk_percent
        }
        success = save_settings(settings)
        return (
            selected_tickers,
            risk_percent,
            True,
            "Settings Saved Successfully" if success else "Save Failed",
            "success" if success else "danger"
        )
    return dash.no_update


if __name__ == '__main__':
    app.run(debug=True, port=2020)
