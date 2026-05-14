"""
Market data tools — realtime/intraday quotes outside the OLAP snapshot.

Three tools, each a separate AgentTool entry so the LLM picks them by
intent rather than juggling a `type` parameter:

    • get_vn_quote(symbol)         — vnstock 3.x, latest VN ticker quote
    • get_vn_history(symbol, days) — vnstock 3.x, recent OHLCV history
    • get_world_quote(symbol)      — yfinance, FX / commodities / indices

Every handler is hard-wrapped in try/except + asyncio.to_thread so a
flaky upstream (vnstock scrapes VPS/SSI/TCBS, yfinance scrapes Yahoo)
never crashes the ReAct loop. On failure we return a JSON-friendly
error dict; the agent reads it and either retries with a different
symbol or moves on.

vnstock client is a singleton — re-instantiating per call hits a
connection setup cost on cold cache.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

log = logging.getLogger("finhouse.tools.market_data")

# ── vnstock singleton ───────────────────────────────────────

_vn_quote_client = None
_vn_load_attempted = False


def _get_vn_quote(symbol: str):
    """Return a vnstock Quote handle for the given symbol, or None on failure."""
    global _vn_quote_client, _vn_load_attempted

    try:
        from vnstock import Vnstock
    except Exception as e:
        if not _vn_load_attempted:
            log.warning(f"vnstock import failed: {e}")
            _vn_load_attempted = True
        return None

    try:
        # vnstock 3.x: data sources VCI / TCBS / MSN.
        # VCI is most reliable for VN tickers; TCBS as fallback.
        stock = Vnstock().stock(symbol=symbol.upper(), source="VCI")
        return stock.quote
    except Exception as e:
        log.warning(f"vnstock init for {symbol} failed: {e}")
        return None


def _vn_quote_sync(symbol: str) -> dict:
    """Latest quote for a VN ticker. Synchronous — wrap with to_thread."""
    quote = _get_vn_quote(symbol)
    if quote is None:
        return {"symbol": symbol, "error": "vnstock unavailable"}

    try:
        # Pull the last 2 trading days; latest row = 'today' (or last close).
        today = date.today()
        df = quote.history(
            start=(today - timedelta(days=10)).isoformat(),
            end=today.isoformat(),
            interval="1D",
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"vnstock history failed: {e}"}

    if df is None or len(df) == 0:
        return {"symbol": symbol, "error": "no data returned"}

    try:
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        out = {
            "symbol": symbol.upper(),
            "date": str(last.get("time", "")),
            "open": float(last.get("open", 0)),
            "high": float(last.get("high", 0)),
            "low": float(last.get("low", 0)),
            "close": float(last.get("close", 0)),
            "volume": int(last.get("volume", 0)),
        }
        if prev is not None:
            prev_close = float(prev.get("close", 0))
            if prev_close > 0:
                out["prev_close"] = prev_close
                out["change"] = out["close"] - prev_close
                out["change_pct"] = round(100.0 * out["change"] / prev_close, 2)
        return out
    except Exception as e:
        return {"symbol": symbol, "error": f"parse failed: {e}"}


def _vn_history_sync(symbol: str, days: int) -> dict:
    quote = _get_vn_quote(symbol)
    if quote is None:
        return {"symbol": symbol, "error": "vnstock unavailable"}

    days = max(1, min(int(days or 30), 365))
    try:
        today = date.today()
        df = quote.history(
            start=(today - timedelta(days=days * 2)).isoformat(),
            end=today.isoformat(),
            interval="1D",
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"vnstock history failed: {e}"}

    if df is None or len(df) == 0:
        return {"symbol": symbol, "error": "no data returned"}

    try:
        df = df.tail(days)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r.get("time", "")),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
            })
        return {"symbol": symbol.upper(), "rows": rows, "count": len(rows)}
    except Exception as e:
        return {"symbol": symbol, "error": f"parse failed: {e}"}


# ── yfinance ────────────────────────────────────────────────

_yf_load_attempted = False


def _world_quote_sync(symbol: str) -> dict:
    """
    yfinance quote for international symbols.

    Common symbols for VN macro context:
        VND=X    USD/VND
        GC=F     gold futures
        CL=F     crude oil futures
        ^GSPC    S&P 500
        ^IXIC    NASDAQ Composite
        BTC-USD  Bitcoin
    """
    global _yf_load_attempted
    try:
        import yfinance as yf
    except Exception as e:
        if not _yf_load_attempted:
            log.warning(f"yfinance import failed: {e}")
            _yf_load_attempted = True
        return {"symbol": symbol, "error": "yfinance unavailable"}

    try:
        ticker = yf.Ticker(symbol)
        # `.history(period="5d")` is the most reliable cross-version call;
        # `.fast_info` works on newer versions but yfinance churns API a lot.
        hist = ticker.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            return {"symbol": symbol, "error": "no data returned"}

        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else None
        out = {
            "symbol": symbol,
            "date": str(hist.index[-1].date()),
            "open": float(last.get("Open", 0)),
            "high": float(last.get("High", 0)),
            "low": float(last.get("Low", 0)),
            "close": float(last.get("Close", 0)),
            "volume": int(last.get("Volume", 0) or 0),
        }
        if prev is not None:
            prev_close = float(prev.get("Close", 0))
            if prev_close > 0:
                out["prev_close"] = prev_close
                out["change"] = out["close"] - prev_close
                out["change_pct"] = round(100.0 * out["change"] / prev_close, 2)
        return out
    except Exception as e:
        return {"symbol": symbol, "error": f"yfinance failed: {e}"}


# ── Async wrappers for AgentTool handlers ───────────────────


async def get_vn_quote(symbol: str) -> dict:
    return await asyncio.to_thread(_vn_quote_sync, symbol)


async def get_vn_history(symbol: str, days: int = 30) -> dict:
    return await asyncio.to_thread(_vn_history_sync, symbol, days)


async def get_world_quote(symbol: str) -> dict:
    return await asyncio.to_thread(_world_quote_sync, symbol)


# ── AgentTool schemas ───────────────────────────────────────


GET_VN_QUOTE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_vn_quote",
        "description": (
            "Lấy giá đóng cửa GẦN NHẤT của 1 mã cổ phiếu Việt Nam (HOSE/HNX/UPCOM) "
            "kèm % thay đổi so với phiên trước. Dùng khi user hỏi giá hôm nay / "
            "phiên gần nhất / mới nhất. KHÔNG dùng cho dữ liệu lịch sử dài (dùng "
            "get_vn_history) hay BCTC (dùng database)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Mã CK Việt Nam viết hoa, ví dụ VNM, FPT, HPG, VIC.",
                },
            },
            "required": ["symbol"],
        },
    },
}


GET_VN_HISTORY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_vn_history",
        "description": (
            "Lấy lịch sử OHLCV ngày của 1 mã CK Việt Nam trong N ngày gần nhất. "
            "Dùng khi cần xu hướng giá hoặc volume gần đây. Cap tối đa 365 ngày."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Mã CK VN viết hoa."},
                "days": {
                    "type": "integer",
                    "description": "Số ngày gần nhất, mặc định 30, tối đa 365.",
                },
            },
            "required": ["symbol"],
        },
    },
}


GET_WORLD_QUOTE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_world_quote",
        "description": (
            "Lấy giá đóng cửa gần nhất của một symbol QUỐC TẾ qua Yahoo Finance: "
            "FX (vd VND=X cho USD/VND), commodities (GC=F vàng, CL=F dầu), "
            "indices (^GSPC S&P500, ^IXIC NASDAQ), crypto (BTC-USD). KHÔNG dùng "
            "cho mã CK Việt Nam — dùng get_vn_quote."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol Yahoo Finance, vd VND=X, GC=F, ^GSPC, BTC-USD.",
                },
            },
            "required": ["symbol"],
        },
    },
}
