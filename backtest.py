"""
Prueba histórica: busca las últimas N señales que habría dado la estrategia en BTC y ETH
y simula qué habría pasado con cada una (stop 2 ATR, objetivo 3 ATR, cierre por cruce contrario).
Con mode=optimize en backtest_request.json prueba muchas combinaciones de ajustes.
Con mode=ensemble evalúa la estrategia combinada de strategy.py frente a la anterior.
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
    f, s = ema_series(cl, p["fast"]), ema_series(cl, p["slow"])
    t = ema_series(cl, p["trend"]) if p["trend"] else None
    a = atr_series(c, p["atr"])
    trades, busy_until = [], -1
    for i in range(max(p["trend"], p["slow"]) + 5, len(c) - 1):
        up = f[i - 1] <= s[i - 1] and f[i] > s[i]
        down = f[i - 1] >= s[i - 1] and f[i] < s[i]
        if p["trend"]:
            side = "long" if up and cl[i] > t[i] else "short" if down and cl[i] < t[i] else None
        else:
            side = "long" if up else "short" if down else None
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
    import json, os
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_request.json")) as fh:
            mode = json.load(fh).get("mode")
            if mode == "optimize":
                return optimize(charts_url, symbols, names, p, hours)
            if mode == "ensemble":
                return evaluate_ensemble(charts_url, symbols, names, p, hours)
    except FileNotFoundError:
        pass
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


def _stats(trades, risk):
    tk = [x for x in trades if x.get("r") is not None]
    n = len(tk)
    net = round(sum(x["r"] for x in tk), 2)
    return {"n": n, "win": round(100 * sum(x["r"] > 0 for x in tk) / n, 1) if n else 0,
            "net_r": net, "pct": round(net * risk * 100, 1), "streak": _streak(tk)}


def optimize(charts_url, symbols, names, base, hours=17520):
    """Busca la mejor combinación en el primer año y la comprueba en el segundo (datos que no ha visto)."""
    data = {names[s]: candles_history(charts_url, s, hours) for s in symbols}
    split = (int(time.time()) - 365 * 86400) * 1000
    grid = []
    for fast, slow in ((9, 21), (12, 26), (21, 55), (34, 89)):
        for trend in (0, 100, 200):
            for sl in (1.5, 2.0, 3.0):
                for tp in (2.0, 3.0, 4.5):
                    grid.append({"fast": fast, "slow": slow, "trend": trend, "atr": base["atr"], "sl": sl, "tp": tp})
    rows = []
    for p in grid:
        per = {name: simulate(name, c, p) for name, c in data.items()}
        for combo in (["BTC"], ["ETH"], ["BTC", "ETH"]):
            tr = [x for name in combo for x in per[name]]
            ins = _stats([x for x in tr if x["t"] < split], base["risk"])
            oos = _stats([x for x in tr if x["t"] >= split], base["risk"])
            rows.append({"coins": "+".join(combo), **{k: p[k] for k in ("fast", "slow", "trend", "sl", "tp")},
                         "year1": ins, "year2": oos})
    current = [r for r in rows if r["coins"] == "BTC+ETH" and r["fast"] == base["fast"] and r["slow"] == base["slow"]
               and r["trend"] == base["trend"] and r["sl"] == base["sl"] and r["tp"] == base["tp"]]
    ranked = sorted([r for r in rows if r["year1"]["n"] >= 30], key=lambda r: r["year1"]["net_r"], reverse=True)
    both_good = sorted([r for r in rows if r["year1"]["n"] >= 30 and r["year2"]["n"] >= 30
                        and r["year1"]["net_r"] > 0 and r["year2"]["net_r"] > 0],
                       key=lambda r: min(r["year1"]["net_r"], r["year2"]["net_r"]), reverse=True)
    return {"generated": int(time.time() * 1000), "tested": len(rows), "current": current[0] if current else None,
            "best_year1": ranked[:10], "robust": both_good[:10],
            "robust_count": len(both_good)}


def simulate_ensemble(sym, c):
    import strategy as st
    x = st.compute(c)
    trades, busy_until = [], -1
    for i in range(st.WARMUP, len(c) - 1):
        d = st.decide(x, i)
        if not d or not d["side"]:
            continue
        sig = {"symbol": sym, "t": c[i]["t"], "side": d["side"], "mode": d["mode"], "entry": d["price"]}
        if i <= busy_until:
            sig["result"] = "omitida"
            trades.append(sig)
            continue
        sg = 1 if d["side"] == "long" else -1
        e = d["price"]
        sl, tp = e - sg * d["sl"] * d["atr"], e + sg * d["tp"] * d["atr"]
        risk = abs(e - sl)
        result, exit_px, j = None, None, i
        for j in range(i + 1, len(c)):
            k = c[j]
            if (k["l"] <= sl) if sg > 0 else (k["h"] >= sl):
                result, exit_px = "stop", sl
                break
            if (k["h"] >= tp) if sg > 0 else (k["l"] <= tp):
                result, exit_px = "objetivo", tp
                break
            if st.should_exit(x, j, d["side"], d["mode"]):
                result, exit_px = "cruce", k["c"]
                break
        if result is None:
            sig["result"] = "abierta"
            trades.append(sig)
            break
        sig.update(result=result, hours=j - i, r=round((sg * (exit_px - e) - FEE * (e + exit_px)) / risk, 2))
        trades.append(sig)
        busy_until = j
    return trades


def evaluate_ensemble(charts_url, symbols, names, base, hours=17520):
    data = {names[s]: candles_history(charts_url, s, hours) for s in symbols}
    split = (int(time.time()) - 365 * 86400) * 1000
    risk = base["risk"]
    new = {name: simulate_ensemble(name, c) for name, c in data.items()}
    old = {name: simulate(name, c, base) for name, c in data.items()}
    out = {"generated": int(time.time() * 1000), "combos": {}}
    for combo in (["BTC"], ["ETH"], ["BTC", "ETH"]):
        key = "+".join(combo)
        tn = [x for n in combo for x in new[n]]
        to = [x for n in combo for x in old[n]]
        out["combos"][key] = {
            "nueva": {"year1": _stats([x for x in tn if x["t"] < split], risk),
                      "year2": _stats([x for x in tn if x["t"] >= split], risk)},
            "anterior": {"year1": _stats([x for x in to if x["t"] < split], risk),
                         "year2": _stats([x for x in to if x["t"] >= split], risk)},
            "por_modo": {m: {"year1": _stats([x for x in tn if x.get("mode") == m and x["t"] < split], risk),
                             "year2": _stats([x for x in tn if x.get("mode") == m and x["t"] >= split], risk)}
                         for m in ("tendencia", "ruptura", "rebote")},
        }
    return out
