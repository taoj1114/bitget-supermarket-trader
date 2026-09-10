"""批量识别 Bitget 美股合约中的 ETF — 生成 ETF 黑名单。"""
import asyncio, json, sys
import yfinance as yf
import time

sys.path.insert(0, '.')
from src.datasources.bitget.symbols import BitgetSymbolSource


async def main():
    src = BitgetSymbolSource()
    stocks = await src.get_stock_symbols()
    symbols = [s.symbol for s in stocks]
    print(f'美股合约: {len(symbols)} 个')

    etfs, equities, unknown = [], [], []
    for i, s in enumerate(symbols):
        try:
            info = yf.Ticker(s).get_info()
            qt = info.get("quoteType", "?")
            if qt == "ETF":
                etfs.append(s)
            elif qt == "EQUITY":
                equities.append(s)
            else:
                unknown.append((s, qt))
        except Exception:
            unknown.append((s, "ERROR"))
        if (i + 1) % 25 == 0:
            print(f'  ...{i+1}/{len(symbols)}')
        time.sleep(0.1)

    print(f'\n=== ETF ({len(etfs)}) ===')
    print(" ".join(sorted(etfs)))
    print(f'\n=== 未知/异常 ({len(unknown)}) ===')
    print(unknown)
    with open('data/etf_blacklist.json', 'w') as f:
        json.dump(sorted(etfs), f, indent=1)
    print('\n已保存 data/etf_blacklist.json')
    await src.close()


asyncio.run(main())
