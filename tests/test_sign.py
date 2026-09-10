"""签名/客户端测试。签名算法与旧项目一致(已通过实测验证)。"""

from supermarket.bitget_client import BitgetClient


def test_sign_known_vector():
    """手工构造的确定性向量: base64(HMAC-SHA256(secret, ts+method+path+body))。"""
    import base64
    import hashlib
    import hmac

    secret = "secRET123"
    ts, method, path, body = "1789047000000", "GET", "/api/v2/mix/account/account?productType=USDT-FUTURES", ""
    expect = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).digest()
    ).decode()
    bg = BitgetClient("https://api.bitget.com", "k", secret, "pp")
    assert bg._sign(ts, method, path, body) == expect


def test_headers_present():
    bg = BitgetClient("https://api.bitget.com", "api_key_x", "secret", "pp")
    h = bg._headers("GET", "/api/v2/mix/market/tickers", "")
    assert h["ACCESS-KEY"] == "api_key_x"
    assert h["ACCESS-PASSPHRASE"] == "pp"
    assert len(h["ACCESS-SIGN"]) > 10
    assert h["ACCESS-TIMESTAMP"]


def test_contracts_parse(tmp_path):
    """公开合约接口返回美股永续(实测字段)。"""
    from unittest.mock import patch
    sample = [{
        "symbol": "NVDAUSDT", "maxLever": "100", "minTradeUSDT": "5",
        "sizeMultiplier": "0.01", "takerFeeRate": "0.0006", "makerFeeRate": "0.0002",
        "fundInterval": "8", "symbolStatus": "normal", "posLimit": "0.2",
    }]
    bg = BitgetClient("https://api.bitget.com", "", "", "")
    with patch.object(bg, "_request", return_value=sample):
        rows = bg.contracts()
        assert rows[0]["symbol"] == "NVDAUSDT"
        assert int(rows[0]["maxLever"]) == 100