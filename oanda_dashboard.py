import os
from datetime import datetime

import json

import pandas as pd
from dash import Dash, Input, Output, dcc, html, dash_table


SYMBOL_PARAMS_PATH = "backtest_outputs/symbol_params_latest.json"
TRADE_HISTORY_PATH = "backtest_outputs/oanda_trade_history.csv"


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
        ])
    trades = pd.read_csv(TRADE_HISTORY_PATH)
    if not trades.empty:
        trades["timestamp"] = pd.to_datetime(trades["timestamp"], errors="coerce")
    return trades


def build_layout(symbols, trades):
    symbol_items = [html.Li(symbol) for symbol in symbols] if symbols else [html.Li("No symbols loaded")]

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
            html.H1("Oanda Trading Dashboard"),
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
                            html.H3("Trade History"),
                            trade_table,
                        ],
                    ),
                ],
            ),
        ],
    )


app = Dash(__name__)
server = app.server

app.layout = html.Div(
    children=[
        html.Div(id="dashboard-content"),
        dcc.Interval(id="refresh", interval=30_000, n_intervals=0),
    ]
)


@app.callback(Output("dashboard-content", "children"), Input("refresh", "n_intervals"))
def refresh_dashboard(_):
    return build_layout(load_symbols(), load_trades())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=False)
