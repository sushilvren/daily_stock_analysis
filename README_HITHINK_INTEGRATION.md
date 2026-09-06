# HiThink Financial API integration

The Railway market API includes a lightweight adapter for the official HiThink Financial API.

## Activation

Set the following secret on the Railway service:

- `HITHINK_FINANCE_API_KEY` (required)
- `HITHINK_FINANCE_BASE_URL` (optional, defaults to `https://fuyao.aicubes.cn`)
- `HITHINK_FINANCE_TIMEOUT_SECONDS` (optional, defaults to `10`)

Do not commit the API key to GitHub. If the key is absent, the provider remains installed but disabled and the existing AkShare/TongdaXin paths keep working.

## Provider strategy

- HiThink: official A-share auction, valuations, limit-up pool, ladder, anomalies, hot stocks, dragon-tiger data.
- AkShare/Eastmoney: broad-market intraday table and fields such as turnover ratio / volume ratio where available.
- TongdaXin: minute bars, transaction/tick-like feed, quote depth.

## API endpoints

- `GET /providers/hithink/status`
- `GET /scan/breakout-radar?mode=auction&auction_stage=final`
- `GET /hithink/auction?codes=920176,600519&stage=final`
- `GET /hithink/valuations?codes=600519,000001`
- `GET /hithink/limit-up-pool`
- `GET /hithink/limit-up-ladder`
- `GET /hithink/anomalies`
- `GET /hithink/hot-stocks?period=hour`
- `GET /hithink/dragon-tiger?board_type=all`

The adapter supports `.SH`, `.SZ`, and `.BJ`, including 92-prefix Beijing Stock Exchange tickers.
