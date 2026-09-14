"""Bitget v2 REST 客户端(公开+私有)。签名 = base64(HMAC-SHA256(ts+method+path+body))。

端点与字段已通过备份旧密钥实测验证(2026-09-10):
- account 端点必须带 symbol 参数, 否则 400172 参数校验失败
- 合约列表 productType=USDT-FUTURES, 美股品种 symbolType=perpetual
- 私有请求头: ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP / ACCESS-PASSPHRASE
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"


class BitgetError(RuntimeError):
    def __init__(self, code: str, msg: str, path: str = ""):
        super().__init__(f"[{code}] {msg} ({path})")
        self.code = code
        self.msg = msg


class BitgetClient:
    def __init__(self, base_url: str, api_key: str, secret: str, passphrase: str,
                 timeout_s: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self._client = httpx.Client(timeout=timeout_s, headers={"locale": "zh-CN"})

    # ---------- 签名 ----------
    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method.upper()}{path}{body}".encode()
        mac = hmac.new(self.secret.encode(), msg, hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        body_str = json.dumps(body) if body is not None else ""
        headers = self._headers(method, path, body_str) if self.api_key else {}
        try:
            resp = self._client.request(method, self.base_url + path,
                                        content=body_str.encode() if body is not None else None,
                                        headers=headers)
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise BitgetError(str(e.response.status_code), e.response.text[:200], path)
        except Exception as e:  # 网络错误
            raise BitgetError("NET", str(e), path)
        if data.get("code") not in ("00000", 0):
            raise BitgetError(str(data.get("code")), str(data.get("msg", "")), path)
        return data.get("data")

    # ---------- 公开 ----------
    def contracts(self) -> list[dict[str, Any]]:
        """全部 USDT-FUTURES 合约(含美股永续)。"""
        return self._request("GET", f"/api/v2/mix/market/contracts?productType={PRODUCT_TYPE}")

    def stock_contracts(self) -> list[dict[str, Any]]:
        """美股永续池: isRwa=YES(代币化股票) - ETF黑名单 - 黄金代币。"""
        import json as _json
        from pathlib import Path
        etf = set()
        bp = Path(__file__).resolve().parent.parent.parent
        try:
            etf = set(_json.loads((bp / "assets" / "etf_blacklist.json").read_text()))
        except Exception:
            pass
        gold = {"PAXG", "XAUT", "XAU", "XAG", "XPD", "XPT"}
        non_us: set[str] = set()
        try:
            non_us = set(_json.loads((bp / "assets" / "non_us_blacklist.json").read_text()))
        except Exception:
            pass
        out = []
        for r in self.contracts():
            if r.get("isRwa") != "YES":
                continue
            sym = r.get("symbol", "")
            base = sym[:-4] if sym.endswith("USDT") else sym  # "USDT" 是4字符
            if base in etf or base in gold or base in non_us:
                continue
            if r.get("symbolStatus") not in ("normal", "online"):
                continue
            out.append(r)
        return out

    def klines(self, symbol: str, granularity: str = "5m", limit: int | None = None) -> list[dict[str, Any]]:
        """K线。返回 [{open,high,low,close,volume,baseVolume,usdtVolume,ts,..}] 旧→新。
        limit=None → 按周期需求取量(kline_limits: 指标需求驱动)。"""
        path = (f"/api/v2/mix/market/candles?symbol={symbol}&productType={PRODUCT_TYPE}"
                f"&granularity={granularity}&limit={max(1, min(1000, limit or 200))}")
        data = self._request("GET", path)
        # 服务端返回横排数组: [ts, open, high, low, close, baseVolume, usdtVolume, ...]
        keys = ["ts", "open", "high", "low", "close", "baseVolume", "usdtVolume"]
        rows = []
        for arr in data or []:
            rows.append({k: arr[i] if i < len(arr) else "" for i, k in enumerate(keys)})
        return rows

    def tickers(self) -> list[dict[str, Any]]:
        """全市场 ticker: symbol/lastPr/suspend/baseVolume/usdtVolume/high24h/low24h/chgUtc24h。"""
        return self._request("GET", f"/api/v2/mix/market/tickers?productType={PRODUCT_TYPE}")

    def quote(self, symbol: str) -> dict[str, Any]:
        ticks = [t for t in self.tickers() if t.get("symbol") == symbol]
        return ticks[0] if ticks else {}

    def orderbook(self, symbol: str) -> dict[str, Any]:
        return self._request("GET",
                             f"/api/v2/mix/market/orderbook?symbol={symbol}&type=step0&productType={PRODUCT_TYPE}")

    def funding_history(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """资金费率历史(近 limit 条, 含当前下一期). 每次结算一条: fundingRate/settleTime。"""
        try:
            return self._request(
                "GET",
                f"/api/v2/mix/market/funding-time?symbol={symbol}&limit={limit}",
            )
        except BitgetError:
            return []

    # ---------- 私有 ----------
    def account(self, symbol: str = "NVDAUSDT") -> dict[str, Any]:
        """合约账户权益。实测: 必须带 symbol; 返回 USDT 全账户口径。"""
        path = (f"/api/v2/mix/account/account?productType={PRODUCT_TYPE}"
                f"&marginCoin={MARGIN_COIN}&symbol={symbol}")
        return self._request("GET", path)

    def positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/v2/mix/position/all-position?productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}")
        return data or []

    def set_leverage(self, symbol: str, leverage: int, hold_side: str = "long") -> None:
        """双向模式(hedge)下 holdSide=long/short; 必须带 productType(实测缺则400172)。"""
        self._request("POST", "/api/v2/mix/account/set-leverage", {
            "symbol": symbol, "marginCoin": MARGIN_COIN, "productType": PRODUCT_TYPE,
            "leverage": str(leverage), "holdSide": hold_side,
        })

    def place_order(self, symbol: str, side: str, size: str, order_type: str = "market",
                    leverage: int = 20, price: str | None = None,
                    margin_mode: str = "crossed", reduce_only: bool = False,
                    pos_side: str = "long") -> dict[str, Any]:
        """pos_side: 双向持仓模式的仓位方向(long/short), 开仓和平仓都必须传。"""
        body: dict[str, Any] = {
            "symbol": symbol, "marginCoin": MARGIN_COIN, "productType": PRODUCT_TYPE,
            "marginMode": margin_mode, "posSide": pos_side, "side": side,
            "orderType": order_type, "size": str(size), "leverage": str(leverage),
        }
        if price:
            body["price"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "true"
        return self._request("POST", "/api/v2/mix/order/place-order", body)

    def place_tpsl(self, symbol: str, plan_type: str, trigger_price: str,
                   execute_price: str | None = None, hold_side: str = "long") -> dict[str, Any]:
        """plan_type: pos_loss(止损) / pos_profit(止盈)。execute_price 为空=市价触发。"""
        body: dict[str, Any] = {
            "symbol": symbol, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
            "planType": plan_type, "triggerPrice": str(trigger_price),
            "holdSide": hold_side,
        }
        if execute_price:
            body["executePrice"] = str(execute_price)
        else:
            body["triggerPriceType"] = "last_price"
        return self._request("POST", "/api/v2/mix/order/place-tpsl-order", body)

    def cancel_plan(self, symbol: str, order_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/v2/mix/order/cancel-plan-order",
                             {"symbol": symbol, "marginCoin": MARGIN_COIN, "orderId": order_id})

    def pending_plans(self, symbol: str | None = None,
                      plan_type: str = "profit_loss") -> list[dict[str, Any]]:
        """未触发 TPSL 计划单(返回 list)。实测: planType 必填, TPSL 类型名为 profit_loss。"""
        path = (f"/api/v2/mix/order/orders-plan-pending?productType={PRODUCT_TYPE}"
                f"&planType={plan_type}")
        if symbol:
            path += f"&symbol={symbol}"
        try:
            d = self._request("GET", path)
        except Exception as e:
            log.warning("pending_plans 查询失败: %s", str(e)[:80])
            return []
        if isinstance(d, dict):
            for k in ("entrustedList", "planList", "list"):
                if d.get(k):
                    return list(d[k])
            return []
        return list(d or [])

    def close(self) -> None:
        self._client.close()