"""Daily job: fetch the latest close price (and, best-effort, shares
outstanding) for every company in companies.json, append it to that
company's price history, and recompute market cap.

Run from the repo root:
    python scripts/fetch_prices.py

Designed to be idempotent: re-running on the same trading day just
overwrites that day's record instead of duplicating it, so a manual
re-run or a workflow retry is always safe.
"""
import datetime as dt
import sys
import time

import yfinance as yf

from common import (
    STATUS_PATH,
    load_companies,
    load_price_history,
    save_companies,
    save_json,
    save_price_history,
    append_ir_event,
)


def fetch_latest_close(symbol):
    """Return (date_str, close_price) for the most recent trading day, or None.

    yfinance's period-based history() can include a same-day placeholder row
    with a NaN Close for the current (not-yet-traded) session near the JST/UTC
    day boundary. Drop unclosed/NaN rows so we always return a real close.
    """
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
    hist = hist[hist["Close"].notna()]
    if hist.empty:
        return None
    last = hist.iloc[-1]
    date_str = hist.index[-1].strftime("%Y-%m-%d")
    return date_str, round(float(last["Close"]), 2)


def fetch_shares_outstanding(symbol):
    """Best-effort auto lookup. Returns an int, or None if unavailable."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.get_info()
        shares = info.get("sharesOutstanding")
        if shares:
            return int(shares)
    except Exception:
        pass
    return None


def main():
    companies = load_companies()
    today = dt.date.today().isoformat()
    errors = []
    updated = []

    for company in companies:
        code = company["code"]
        symbol = company["yahoo_symbol"]
        name = company["name"]
        try:
            result = fetch_latest_close(symbol)
            if result is None:
                errors.append({"code": code, "name": name, "error": "no price data returned"})
                continue
            price_date, close = result

            # Best-effort shares-outstanding auto-refresh.
            shares = fetch_shares_outstanding(symbol)
            if shares:
                previous = company.get("shares_outstanding")
                if previous != shares:
                    if previous is not None:
                        append_ir_event(code, {
                            "date": today,
                            "category": "株式数変更",
                            "title": f"発行済株式数が自動更新で変化を検知: {previous:,} → {shares:,} 株",
                            "url": "",
                            "note": "自動取得値の変化を検知して自動記録。自社株買い・処分・分割・IR訂正等の可能性があるため、根拠となる適時開示を確認して note を更新してください。",
                            "source": "auto",
                        })
                    company["shares_outstanding"] = shares
                    company["shares_outstanding_source"] = "auto (yfinance)"
                    company["shares_outstanding_updated_at"] = today

            shares_for_calc = company.get("shares_outstanding")
            market_cap = round(close * shares_for_calc) if shares_for_calc else None

            history = load_price_history(code)
            history = [r for r in history if r["date"] != price_date]
            history.append({
                "date": price_date,
                "close": close,
                "shares_outstanding": shares_for_calc,
                "market_cap": market_cap,
                "source": "daily_fetch",
            })
            save_price_history(code, history)
            updated.append(code)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the rest
            errors.append({"code": code, "name": name, "error": str(exc)})
        # Be polite to the upstream API between requests.
        time.sleep(0.5)

    save_companies(companies)
    save_json(STATUS_PATH, {
        "last_run_utc": dt.datetime.utcnow().isoformat() + "Z",
        "updated_codes": updated,
        "errors": errors,
    })

    if errors:
        print(f"Completed with {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e['code']} {e['name']}: {e['error']}", file=sys.stderr)
    print(f"Updated {len(updated)}/{len(companies)} companies.")


if __name__ == "__main__":
    main()
