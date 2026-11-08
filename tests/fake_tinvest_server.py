"""Тестовый MCP-сервер с теми же инструментами, что у t-invest-tinkoff-mcp (без сети)."""

from mcp.server.fastmcp import FastMCP

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
