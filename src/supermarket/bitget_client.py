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

NET_RETRY = 3          # 网络错误重试次数
NET_RETRY_BACKOFF = 1.0  # 退避基数(秒)

PRODUCT_TYPE = "USDT-FUTURES"
CATEGORY = "USDT-FUTURES"   # v3(统一账户)中同义字段名
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
        """带网络重试(2026-09: 实测 DNS/连接抖动 24h 内 14 次导致持仓/账户查询失败)。"""
        body_str = json.dumps(body) if body is not None else ""
        headers = self._headers(method, path, body_str) if self.api_key else {}
        data = None
        last_err: Exception | None = None
        for attempt in range(NET_RETRY):
            try:
                resp = self._client.request(method, self.base_url + path,
                                            content=body_str.encode() if body is not None else None,
                                            headers=headers)
                data = resp.json()
                break
            except httpx.HTTPStatusError as e:
                # HTTP 状态错误(4xx/5xx) → 业务错误, 不重试
                raise BitgetError(str(e.response.status_code), e.response.text[:200], path)
            except Exception as e:  # 网络层错误(DNS/连接/超时) → 退避重试
                last_err = e
                if attempt < NET_RETRY - 1:
                    time.sleep(NET_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise BitgetError("NET", str(e), path)
        if data is None:
            raise BitgetError("NET", str(last_err or "unknown"), path)
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


    def v3_account(self) -> dict[str, Any]:
        """统一账户资产。可用保证金 = assets[USDT].available。"""
        d = self._request("GET", "/api/v3/account/assets") or {}
        eq = float(d.get("accountEquity", 0) or 0)
        avail = 0.0
        for a in d.get("assets") or []:
            if str(a.get("coin", "")).upper() == MARGIN_COIN:
                avail = float(a.get("available", 0) or 0)
        return {"equity": eq, "available": avail,
                "unrealised": float(d.get("unrealisedPnl", 0) or 0), "raw": d}

    def v3_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account/settings") or {}

    def v3_instruments(self, symbol: str | None = None) -> dict[str, dict[str, Any]]:
        """合约表(缓存)。返回 {symbol: info}。"""
        if not getattr(self, "_v3_instr_cache", None):
            path = f"/api/v3/market/instruments?category={CATEGORY}"
            d = self._request("GET", path)
            lst = d.get("list") if isinstance(d, dict) else d
            self._v3_instr_cache = {x["symbol"]: x for x in (lst or [])}
        if symbol:
            return {symbol: self._v3_instr_cache.get(symbol, {})}
        return self._v3_instr_cache

    def v3_precision(self, symbol: str) -> tuple[int, int]:
        """(数量小数位, 价格小数位)。"""
        info = (self.v3_instruments(symbol) or {}).get(symbol) or {}
        return (int(info.get("quantityPrecision", 2) or 2),
                int(info.get("pricePrecision", 2) or 2))

    def v3_min_order_amount(self, symbol: str) -> float:
        info = (self.v3_instruments(symbol) or {}).get(symbol) or {}
        return float(info.get("minOrderAmount", 5) or 5)

    def v3_positions(self) -> list[dict[str, Any]]:
        """统一账户持仓(只返回有量的)。字段: symbol/posSide/total/available/avgPrice/
        leverage/marginMode/unrealisedPnl/liquidationPrice/createdTime。"""
        d = self._request("GET", f"/api/v3/position/current-position?category={CATEGORY}")
        lst = d.get("list") if isinstance(d, dict) else d
        out = []
        for r in lst or []:
            if float(r.get("total", 0) or 0) <= 0:
                continue
            if r.get("posSide") not in ("long", "short"):
                continue
            out.append(r)
        return out

    def v3_set_leverage(self, symbol: str, leverage: int, margin_mode: str = "crossed") -> dict:
        return self._request("POST", "/api/v3/account/set-leverage", {
            "category": CATEGORY, "symbol": symbol,
            "leverage": str(leverage), "marginMode": margin_mode,
        })

    def v3_place_order(self, symbol: str, side: str, qty: float, pos_side: str = "long",
                       order_type: str = "market", price: float | None = None,
                       stop_loss: float | None = None, take_profit: float | None = None,
                       client_oid: str | None = None) -> dict[str, Any]:
        """开仓/加仓下单(可带 preset TP/SL)。side: buy/sell; pos_side: long/short。"""
        qp, pp = self.v3_precision(symbol)
        body: dict[str, Any] = {
            "category": CATEGORY, "symbol": symbol, "orderType": order_type,
            "qty": f"{qty:.{qp}f}", "side": side, "posSide": pos_side,
            "timeInForce": "gtc",
        }
        if order_type == "limit" and price:
            body["price"] = f"{price:.{pp}f}"
        if stop_loss:
            body["stopLoss"] = f"{stop_loss:.{pp}f}"
            body["slTriggerBy"] = "mark"
        if take_profit:
            body["takeProfit"] = f"{take_profit:.{pp}f}"
            body["tpTriggerBy"] = "mark"
        if client_oid:
            body["clientOid"] = client_oid[:32]
        return self._request("POST", "/api/v3/trade/place-order", body) or {}

    def v3_close_order(self, symbol: str, qty: float, pos_side: str) -> dict[str, Any]:
        """平仓(hedge 模式: 只传 posSide, ⚠️不能带 reduceOnly → 25238)。"""
        side = "sell" if pos_side == "long" else "buy"
        qp, _ = self.v3_precision(symbol)
        return self._request("POST", "/api/v3/trade/place-order", {
            "category": CATEGORY, "symbol": symbol, "orderType": "market",
            "qty": f"{qty:.{qp}f}", "side": side, "posSide": pos_side,
            "timeInForce": "gtc",
            "clientOid": f"hclose{int(time.time()*1000)}"[:32],
        }) or {}

    def v3_cancel_order(self, symbol: str, order_id: str) -> dict:
        return self._request("POST", "/api/v3/trade/cancel-order", {
            "category": CATEGORY, "symbol": symbol, "orderId": order_id}) or {}

    def v3_order_info(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v3/trade/order-info?orderId={order_id}") or {}

    def v3_unfilled_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        path = f"/api/v3/trade/unfilled-orders?category={CATEGORY}"
        if symbol:
            path += f"&symbol={symbol}"
        d = self._request("GET", path)
        return list((d.get("list") if isinstance(d, dict) else d) or [])

    def v3_strategy_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """未触发策略单(= 交易所侧 TPSL)。字段: takeProfit/stopLoss/tpTriggerBy/slTriggerBy。

        注意: Bitget /api/v3/trade/unfilled-strategy-orders 实测忽略 symbol 参数,
        始终返回账户下全部持仓的策略单。此处在客户端侧做二次过滤以防止跨标的误操作
        (Bug 修复 2026-09-17: 平仓时误撤其他持仓 TPSL 导致裸仓)。
        """
        path = f"/api/v3/trade/unfilled-strategy-orders?category={CATEGORY}"
        if symbol:
            path += f"&symbol={symbol}"
        d = self._request("GET", path)
        result = list((d.get("list") if isinstance(d, dict) else d) or [])
        # 客户端侧按 symbol 过滤: 防止服务器不过滤时跨标的串单
        if symbol:
            result = [s for s in result if s.get("symbol") == symbol]
        return result

    def v3_cancel_strategy(self, symbol: str, order_id: str) -> dict:
        return self._request("POST", "/api/v3/trade/cancel-strategy-order", {
            "category": CATEGORY, "symbol": symbol, "orderId": order_id}) or {}

    def v3_modify_strategy(self, symbol: str, order_id: str,
                           stop_loss: float | None = None,
                           take_profit: float | None = None) -> dict:
        """修改持仓 TPSL 策略单(撤旧+modify)。"""
        _, pp = self.v3_precision(symbol)
        body: dict[str, Any] = {"category": CATEGORY, "symbol": symbol, "orderId": order_id}
        if stop_loss:
            body["stopLoss"] = f"{stop_loss:.{pp}f}"
            body["slTriggerBy"] = "mark"
        if take_profit:
            body["takeProfit"] = f"{take_profit:.{pp}f}"
            body["tpTriggerBy"] = "mark"
        return self._request("POST", "/api/v3/trade/modify-strategy-order", body) or {}

    def v3_fills(self, symbol: str, hours: float = 24) -> list[dict[str, Any]]:
        """成交明细。字段: execPnl/execPrice/execQty/tradeSide/feeDetail。"""
        end = int(time.time() * 1000)
        start = int((time.time() - hours * 3600) * 1000)
        d = self._request(
            "GET",
            f"/api/v3/trade/fills?category={CATEGORY}&symbol={symbol}"
            f"&startTime={start}&endTime={end}",
        )
        return list((d.get("list") if isinstance(d, dict) else d) or [])

    def v3_last_closed_pnl(self, symbol: str) -> float:
        """最近一笔平仓的真实已实现盈亏(execPnl)。"""
        try:
            rows = [f for f in self.v3_fills(symbol, hours=168)
                    if str(f.get("tradeSide", "")).startswith("close")]
            if not rows:
                return 0.0
            rows.sort(key=lambda f: int(f.get("createdTime", 0) or 0))
            return float(rows[-1].get("execPnl", 0) or 0)
        except Exception as e:
            log.warning("v3 fills 对账失败 %s: %s", symbol, str(e)[:100])
            return 0.0

    def close(self) -> None:
        self._client.close()