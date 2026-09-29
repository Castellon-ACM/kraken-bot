"""
Prueba histórica: busca las últimas N señales que habría dado la estrategia en BTC y ETH
y simula qué habría pasado con cada una (stop 2 ATR, objetivo 3 ATR, cierre por cruce contrario).
"""

import time

import requests

FEE = 0.0005  # comisión aproximada por lado (orden a mercado) sobre el nominal


def candles_history(charts_url, sym, hours):
    now = int(time.time())
    out, end, chunk = {}, now, 2000 * 3600
    start_all = now - hours * 3600
    while end > start_all:
        frm = max(start_all, end - chunk)
        r = requests.get(f"{charts_url}/trade/{sym}/1h", params={"from": frm, "to": end}, timeout=20)
        r.raise_for_status()
        got = r.json().get("candles", [])
        for x in got:
            out[int(x["time"])] = {"t": int(x["time"]), "h": float(x["high"]), "l": float(x["low"]),
                                   "c": float(x["close"])}
        end = frm
        time.sleep(0.3)
    c = sorted(out.values(), key=lambda x: x["t"])
    return [x for x in c if x["t"] + 3600 * 1000 <= now * 1000]


def ema_series(v, n):
    k, out = 2 / (n + 1), []
    for i, x in enumerate(v):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def atr_series(c, n):
    out, a = [None] * len(c), None
    trs = [max(x["h"] - x["l"], abs(x["h"] - (c[i - 1]["c"] if i else x["c"])),
               abs(x["l"] - (c[i - 1]["c"] if i else x["c"]))) for i, x in enumerate(c)]
    for i in range(len(c)):
        if i == n - 1:
            a = sum(trs[:n]) / n
        elif i >= n:
            a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def simulate(sym, c, p):
    cl = [x["c"] for x in c]
    f, s, t = ema_series(cl, p["fast"]), ema_series(cl, p["slow"]), ema_series(cl, p["trend"])
    a = atr_series(c, p["atr"])
    trades, busy_until = [], -1
    for i in range(p["trend"] + 5, len(c) - 1):
        up = f[i - 1] <= s[i - 1] and f[i] > s[i]
        down = f[i - 1] >= s[i - 1] and f[i] < s[i]
        side = "long" if up and cl[i] > t[i] else "short" if down and cl[i] < t[i] else None
        if not side:
            continue
        sig = {"symbol": sym, "t": c[i]["t"], "side": side, "entry": cl[i]}
        if i <= busy_until:  # ya había una operación abierta en esta moneda
            sig["result"] = "omitida"
            trades.append(sig)
            continue
        sg = 1 if side == "long" else -1
        sl, tp = cl[i] - sg * p["sl"] * a[i], cl[i] + sg * p["tp"] * a[i]
        risk = abs(cl[i] - sl)
        exit_px, result, j = None, None, i
        for j in range(i + 1, len(c)):
            x = c[j]
            hit_sl = x["l"] <= sl if side == "long" else x["h"] >= sl
            hit_tp = x["h"] >= tp if side == "long" else x["l"] <= tp
            if hit_sl:  # si toca los dos en la misma vela, suponemos lo peor
                exit_px, result = sl, "stop"
                break
            if hit_tp:
                exit_px, result = tp, "objetivo"
                break
            rev = (f[j - 1] >= s[j - 1] and f[j] < s[j]) if side == "long" else (f[j - 1] <= s[j - 1] and f[j] > s[j])
            if rev:
                exit_px, result = x["c"], "cruce"
                break
        if result is None:
            sig["result"] = "abierta"
            trades.append(sig)
            break
        gross = sg * (exit_px - cl[i])
        fees = FEE * (cl[i] + exit_px)
        sig.update(result=result, exit=round(exit_px, 2), hours=j - i,
                   r=round((gross - fees) / risk, 2))
        trades.append(sig)
        busy_until = j
    return trades


def run_backtest(charts_url, symbols, names, p, n=100, hours=17520):
    allsig = []
    for sym in symbols:
        allsig += simulate(names[sym], candles_history(charts_url, sym, hours), p)
    allsig.sort(key=lambda x: x["t"])
    last = allsig[-n:]
    taken = [x for x in last if x["result"] in ("stop", "objetivo", "cruce")]
    wins = [x for x in taken if x["r"] > 0]
    net_r = round(sum(x["r"] for x in taken), 2)
    by = {}
    for x in taken:
        b = by.setdefault(x["symbol"], {"trades": 0, "wins": 0, "net_r": 0.0})
        b["trades"] += 1
        b["wins"] += x["r"] > 0
        b["net_r"] = round(b["net_r"] + x["r"], 2)
    return {
        "generated": int(time.time() * 1000), "signals": len(last),
        "from": last[0]["t"] if last else None, "to": last[-1]["t"] if last else None,
        "taken": len(taken), "skipped": sum(x["result"] == "omitida" for x in last),
        "still_open": sum(x["result"] == "abierta" for x in last),
        "wins": len(wins), "losses": len(taken) - len(wins),
        "by_exit": {k: sum(x["result"] == k for x in taken) for k in ("objetivo", "stop", "cruce")},
        "net_r": net_r, "risk_per_trade": p["risk"],
        "approx_return_pct": round(net_r * p["risk"] * 100, 1),
        "avg_hours": round(sum(x["hours"] for x in taken) / len(taken), 1) if taken else 0,
        "by_symbol": by,
        "best_r": max((x["r"] for x in taken), default=0), "worst_r": min((x["r"] for x in taken), default=0),
        "max_losing_streak": _streak(taken),
        "trades": last,
    }


def _streak(taken):
    best = cur = 0
    for x in taken:
        cur = cur + 1 if x["r"] <= 0 else 0
        best = max(best, cur)
    return best
