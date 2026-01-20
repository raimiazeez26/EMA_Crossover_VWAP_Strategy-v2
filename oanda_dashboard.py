import os
from datetime import datetime

import json
import requests

import pandas as pd
from dash import Dash, Input, Output, State, dcc, html, dash_table, no_update


SYMBOL_PARAMS_PATH = "backtest_outputs/symbol_params_latest.json"
TRADE_HISTORY_PATH = "backtest_outputs/oanda_trade_history.csv"
STRATEGY_NAME = "EMA_VWAP"


def load_symbols():
    if not os.path.exists(SYMBOL_PARAMS_PATH):
        return []
    try:
        with open(SYMBOL_PARAMS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (ValueError, json.JSONDecodeError):
        return []
    assets = payload.get("assets", []) if isinstance(payload, dict) else []
    return [asset.get("symbol") for asset in assets if asset.get("symbol")]


def load_trades():
    if not os.path.exists(TRADE_HISTORY_PATH):
        return pd.DataFrame(columns=[
            "timestamp",
            "symbol",
            "signal",
            "units",
            "entry_price",
            "stop_loss",
            "take_profit",
            "strategy",
        ])
    trades = pd.read_csv(TRADE_HISTORY_PATH)
    if not trades.empty:
        trades["timestamp"] = pd.to_datetime(trades["timestamp"], errors="coerce")
    if "strategy" in trades.columns:
        trades = trades[trades["strategy"] == STRATEGY_NAME]
    return trades


def _matches_strategy(extensions):
    if not extensions:
        return True
    if extensions.get("comment") == STRATEGY_NAME:
        return True
    return extensions.get("tag") == STRATEGY_NAME


def fetch_open_trades():
    token = os.getenv("OANDA_TOKEN")
    account_id = os.getenv("OANDA_ACCOUNT_ID")
    base_url = os.getenv("OANDA_API_BASE_URL", "https://api-fxpractice.oanda.com")
    if not token or not account_id:
        return pd.DataFrame(
            columns=[
                "instrument",
                "currentUnits",
                "price",
                "unrealizedPL",
                "openTime",
            ]
        )
    url = f"{base_url}/v3/accounts/{account_id}/openTrades"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return pd.DataFrame(
            columns=[
                "instrument",
                "currentUnits",
                "price",
                "unrealizedPL",
                "openTime",
            ]
        )
    trades = []
    for trade in payload.get("trades", []):
        extensions = trade.get("clientExtensions") or trade.get("tradeClientExtensions") or {}
        if not _matches_strategy(extensions):
            continue
        trades.append(
            {
                "id": trade.get("id"),
                "instrument": trade.get("instrument"),
                "currentUnits": trade.get("currentUnits"),
                "price": trade.get("price"),
                "unrealizedPL": trade.get("unrealizedPL"),
                "openTime": trade.get("openTime"),
            }
        )
    return pd.DataFrame(trades)

def close_trade(trade_id):
    token = os.getenv("OANDA_TOKEN")
    account_id = os.getenv("OANDA_ACCOUNT_ID")
    base_url = os.getenv("OANDA_API_BASE_URL", "https://api-fxpractice.oanda.com")
    if not token or not account_id:
        return False, "Missing OANDA credentials."
    url = f"{base_url}/v3/accounts/{account_id}/trades/{trade_id}/close"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.put(url, headers=headers, timeout=20)
        response.raise_for_status()
        return True, f"Closed trade {trade_id}."
    except requests.RequestException as exc:
        return False, f"Failed to close trade {trade_id}: {exc}"


def build_layout(symbols, trades, open_trades):
    symbol_items = [html.Li(symbol) for symbol in symbols] if symbols else [html.Li("No symbols loaded")]

    open_trade_table = dash_table.DataTable(
        columns=[{"name": col.replace("_", " ").title(), "id": col} for col in open_trades.columns],
        data=open_trades.to_dict("records"),
        row_selectable="single",
        selected_rows=[],
        id="open-trades-table",
        page_size=10,
        style_table={"overflowX": "auto"},
        style_header={"backgroundColor": "#111827", "color": "white", "fontWeight": "bold"},
        style_cell={"padding": "8px", "fontFamily": "Arial", "fontSize": "14px"},
    )

    trade_table = dash_table.DataTable(
        columns=[{"name": col.replace("_", " ").title(), "id": col} for col in trades.columns],
        data=trades.sort_values("timestamp", ascending=False).to_dict("records"),
        page_size=15,
        style_table={"overflowX": "auto"},
        style_header={"backgroundColor": "#1f2937", "color": "white", "fontWeight": "bold"},
        style_cell={"padding": "8px", "fontFamily": "Arial", "fontSize": "14px"},
    )

    return html.Div(
        style={"maxWidth": "1000px", "margin": "40px auto", "fontFamily": "Arial"},
        children=[
            html.H1("EMA_VWAP Trading Dashboard"),
            html.P(f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"),
            html.Div(
                style={"display": "flex", "gap": "32px"},
                children=[
                    html.Div(
                        style={"flex": "1", "background": "#f9fafb", "padding": "16px", "borderRadius": "8px"},
                        children=[
                            html.H3("Tracked Symbols"),
                            html.Ul(symbol_items),
                        ],
                    ),
                    html.Div(
                        style={"flex": "2", "background": "#f9fafb", "padding": "16px", "borderRadius": "8px"},
                        children=[
                            html.H3("Open Trades"),
                            open_trade_table,
                            html.Button("Close Selected Trade", id="close-trade-button", n_clicks=0),
                            html.Div(id="close-status", style={"marginTop": "8px", "color": "#b91c1c"}),
                            html.H3("Trade History"),
                            trade_table,
                        ],
                    ),
                ],
            ),
        ],
    )


app = Dash(__name__)
app.title = "EMA_VWAP"
server = app.server

app.layout = html.Div(
    children=[
        html.Div(id="dashboard-content"),
        dcc.Interval(id="refresh", interval=30_000, n_intervals=0),
    ]
)


@app.callback(Output("dashboard-content", "children"), Input("refresh", "n_intervals"))
def refresh_dashboard(_):
    return build_layout(load_symbols(), load_trades(), fetch_open_trades())


@app.callback(
    Output("close-status", "children"),
    Input("close-trade-button", "n_clicks"),
    State("open-trades-table", "selected_rows"),
    State("open-trades-table", "data"),
)
def close_selected_trade(n_clicks, selected_rows, open_trades_data):
    if not n_clicks:
        return no_update
    if not selected_rows:
        return "Select a trade to close."
    selected_index = selected_rows[0]
    if not open_trades_data or selected_index >= len(open_trades_data):
        return "Selected trade not found."
    trade_id = open_trades_data[selected_index].get("id")
    if not trade_id:
        return "Selected trade is missing an ID."
    success, message = close_trade(trade_id)
    if success:
        return f"{message} Refreshing shortly."
    return message


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=False)
