"""Тестовый MCP-сервер без сети: ``python fake_tinvest_server.py --http PORT``."""

import sys

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("fake-tinvest")


@mcp.tool()
def get_last_price(ticker: str) -> dict:
    """Последняя цена."""
    if ticker.upper() == "NOPE":
        return {"error": f"{ticker} not found"}
    return {"ticker": ticker, "price": 91234.5}


@mcp.tool()
def get_candles(ticker: str, interval: str = "1h", bars: int = 100) -> dict:
    """Свечи."""
    candles = [{"t": f"t{i}", "o": 1, "h": 2, "l": 0.5, "c": 1 + i, "v": 1} for i in range(bars)]
    return {"ticker": ticker, "interval": interval, "bars": bars, "candles": candles}


@mcp.tool()
def get_portfolio() -> dict:
    """Портфель."""
    raise RuntimeError("TINKOFF_TOKEN not set")


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Заголовок Authorization запроса (только для HTTP)."""
    request = ctx.request_context.request
    return {"authorization": request.headers.get("authorization", "") if request else ""}


if __name__ == "__main__":
    mcp.settings.port = int(sys.argv[sys.argv.index("--http") + 1])
    mcp.settings.log_level = "WARNING"
    mcp.run(transport="streamable-http")
