"""
Prueba completa con velas DIARIAS.

Estrategias sueltas (con muchas variantes de ajustes):
  cruce     - cruce de medias con filtro de tendencia opcional
  ruptura   - rompe el máximo/mínimo de N días (tipo "tortugas"), sale al romper el canal contrario
  rebote    - bandas de Bollinger + RSI extremo, apuesta a volver a la media
Combinadas:
  regimen   - un detector ADX elige tendencia (cruce + ruptura) o lateral (rebote)
  votos     - entra solo cuando al menos 2 de las 3 estrategias coinciden
Referencia:
  comprar y mantener

Cada variante se prueba en solo largos y en largos + cortos, en BTC, ETH y ambos.
Se elige con el periodo antiguo (A) y se comprueba en el reciente (B), que no se usa para elegir.
Incluye comisiones y un coste aproximado de financiación del perpetuo por cada día abierto.
"""

import math
import time

import requests

FEE = 0.0005             # por lado, orden a mercado
FUNDING_DAY = 0.0003     # coste aproximado de financiación por día con la posición abierta
DAY_MS = 86400 * 1000


# ---------------------------------------------------------------- datos
def daily_history(charts_url, sym):
    """Velas diarias del perpetuo PF_ y, para años anteriores, del perpetuo antiguo PI_."""
    now = int(time.time())
    out = {}
    for s in (sym.replace("PF_", "PI_"), sym):
        end = now
        for _ in range(4):
            frm = end - 1500 * 86400
            try:
                r = requests.get(f"{charts_url}/trade/{s}/1d", params={"from": frm, "to": end}, timeout=20)
                r.raise_for_status()
                for x in r.json().get("candles", []):
                    out[int(x["time"])] = {"t": int(x["time"]), "h": float(x["high"]), "l": float(x["low"]),
                                           "c": float(x["close"])}
            except Exception:
                break
            end = frm
            time.sleep(0.3)
    c = sorted(out.values(), key=lambda x: x["t"])
    return [x for x in c if x["t"] + DAY_MS <= now * 1000]


# ---------------------------------------------------------------- indicadores
def ema(v, n):
    k, out = 2 / (n + 1), []
    for i, x in enumerate(v):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def wilder(v, n, start=0):
    out = [None] * len(v)
    if len(v) < start + n:
        return out
    a = sum(v[start:start + n]) / n
    out[start + n - 1] = a
    for i in range(start + n, len(v)):
        a = (a * (n - 1) + v[i]) / n
        out[i] = a
    return out


def indicators(c):
    cl, h, lo = [x["c"] for x in c], [x["h"] for x in c], [x["l"] for x in c]
    n = len(c)
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, n)]
    atr = wilder(tr, 14, 1)
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if (h[i] - h[i - 1]) > (lo[i - 1] - lo[i]) else 0.0 for i in range(1, n)]
    mdm = [0.0] + [max(lo[i - 1] - lo[i], 0) if (lo[i - 1] - lo[i]) > (h[i] - h[i - 1]) else 0.0 for i in range(1, n)]
    sp, sm = wilder(pdm, 14, 1), wilder(mdm, 14, 1)
    dx, first = [0.0] * n, None
    for i in range(n):
        if atr[i] and sp[i] is not None:
            a, b = 100 * sp[i] / atr[i], 100 * sm[i] / atr[i]
            dx[i] = 100 * abs(a - b) / (a + b) if a + b else 0.0
            first = i if first is None else first
    adx = wilder(dx, 14, first if first is not None else n)
    up = [0.0] + [max(cl[i] - cl[i - 1], 0) for i in range(1, n)]
    dn = [0.0] + [max(cl[i - 1] - cl[i], 0) for i in range(1, n)]
    ag, al = wilder(up, 14, 1), wilder(dn, 14, 1)
    rsi = [None if ag[i] is None else (100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])) for i in range(n)]
    x = {"c": cl, "h": h, "l": lo, "atr": atr, "adx": adx, "rsi": rsi, "ema": {}, "bb": {}, "don": {}}
    for p in (10, 20, 30, 50, 100, 200):
        x["ema"][p] = ema(cl, p)
    mid, hi, low = [None] * n, [None] * n, [None] * n
    for i in range(19, n):
        w = cl[i - 19:i + 1]
        m = sum(w) / 20
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / 20)
        mid[i], hi[i], low[i] = m, m + 2 * sd, m - 2 * sd
    x["bb"] = {"mid": mid, "hi": hi, "lo": low}
    for p in (10, 20, 27, 50, 55, 100):
        dh, dl = [None] * n, [None] * n
        for i in range(p, n):
            dh[i], dl[i] = max(h[i - p:i]), min(lo[i - p:i])
        x["don"][p] = (dh, dl)
    return x


# ---------------------------------------------------------------- señales
def sig_cross(x, i, p):
    f, s = x["ema"][p["fast"]], x["ema"][p["slow"]]
    up = f[i - 1] <= s[i - 1] and f[i] > s[i]
    dn = f[i - 1] >= s[i - 1] and f[i] < s[i]
    if p["filter"]:
        t = x["ema"][p["filter"]][i]
        up, dn = up and x["c"][i] > t, dn and x["c"][i] < t
    return "long" if up else "short" if dn else None


def exit_cross(x, i, side, p):
    f, s = x["ema"][p["fast"]], x["ema"][p["slow"]]
    return (side == "long" and f[i] < s[i]) or (side == "short" and f[i] > s[i])


def sig_breakout(x, i, p):
    dh, dl = x["don"][p["n"]]
    if dh[i] is None:
        return None
    t = x["ema"][p["filter"]][i] if p["filter"] else None
    if x["c"][i] > dh[i] and (t is None or x["c"][i] > t):
        return "long"
    if x["c"][i] < dl[i] and (t is None or x["c"][i] < t):
        return "short"
    return None


def exit_breakout(x, i, side, p):
    dh, dl = x["don"][p["exit"]]
    if dh[i] is None:
        return False
    return (side == "long" and x["c"][i] < dl[i]) or (side == "short" and x["c"][i] > dh[i])


def sig_reversion(x, i, p):
    r, b = x["rsi"][i], x["bb"]
    if r is None or b["lo"][i] is None:
        return None
    if x["c"][i] < b["lo"][i] and r < p["rsi_lo"]:
        return "long"
    if x["c"][i] > b["hi"][i] and r > 100 - p["rsi_lo"]:
        return "short"
    return None


def exit_reversion(x, i, side, p):
    m = x["bb"]["mid"][i]
    return m is not None and ((side == "long" and x["c"][i] >= m) or (side == "short" and x["c"][i] <= m))


TREND_DEF = {"fast": 20, "slow": 50, "filter": 200}
BREAK_DEF = {"n": 55, "exit": 20, "filter": 0}
REV_DEF = {"rsi_lo": 30}


def sig_regime(x, i, p):
    a = x["adx"][i]
    if a is None:
        return None
    if a >= p["hi"]:
        s = sig_cross(x, i, TREND_DEF) or sig_breakout(x, i, BREAK_DEF)
        return (s, "tendencia") if s else None
    if a < p["lo"]:
        s = sig_reversion(x, i, REV_DEF)
        return (s, "rebote") if s else None
    return None


def exit_regime(x, i, side, p, sub):
    return exit_reversion(x, i, side, REV_DEF) if sub == "rebote" else exit_cross(x, i, side, TREND_DEF)


def sig_votes(x, i, p):
    f, s = x["ema"][20][i], x["ema"][50][i]
    votes = [("long" if f > s else "short"),
             sig_breakout(x, i, {"n": 20, "filter": 0}),
             ("long" if x["c"][i] > x["ema"][200][i] else "short")]
    if p["with_rsi"] and x["rsi"][i] is not None:
        votes.append("long" if x["rsi"][i] > 55 else "short" if x["rsi"][i] < 45 else None)
    need = p["need"]
    if votes.count("long") >= need:
        return "long"
    if votes.count("short") >= need:
        return "short"
    return None


def exit_votes(x, i, side, p):
    f, s = x["ema"][20][i], x["ema"][50][i]
    return (side == "long" and f < s) or (side == "short" and f > s)


# ---------------------------------------------------------------- motor de simulación
def run(c, x, kind, p, sides, warm=205):
    trades, i, n = [], warm, len(c)
    while i < n - 1:
        s = None
        sub = None
        if kind == "cruce":
            s = sig_cross(x, i, p)
        elif kind == "ruptura":
            s = sig_breakout(x, i, p)
        elif kind == "rebote":
            s = sig_reversion(x, i, p)
        elif kind == "regimen":
            r = sig_regime(x, i, p)
            s, sub = (r if r else (None, None))
        elif kind == "votos":
            s = sig_votes(x, i, p)
        if s is None or (sides == "long" and s == "short") or x["atr"][i] is None:
            i += 1
            continue
        e, a, sg = x["c"][i], x["atr"][i], 1 if s == "long" else -1
        sl = e - sg * p["sl"] * a
        tp = e + sg * p["tp"] * a if p.get("tp") else None
        risk = abs(e - sl)
        exit_px, j = None, i
        for j in range(i + 1, n):
            if (x["l"][j] <= sl) if sg > 0 else (x["h"][j] >= sl):
                exit_px = sl
                break
            if tp is not None and ((x["h"][j] >= tp) if sg > 0 else (x["l"][j] <= tp)):
                exit_px = tp
                break
            if kind == "cruce" and exit_cross(x, j, s, p):
                exit_px = x["c"][j]
                break
            if kind == "ruptura" and exit_breakout(x, j, s, p):
                exit_px = x["c"][j]
                break
            if kind == "rebote" and exit_reversion(x, j, s, p):
                exit_px = x["c"][j]
                break
            if kind == "regimen" and exit_regime(x, j, s, p, sub):
                exit_px = x["c"][j]
                break
            if kind == "votos" and exit_votes(x, j, s, p):
                exit_px = x["c"][j]
                break
        if exit_px is None:
            exit_px = x["c"][n - 1]
        days = j - i
        cost = FEE * (e + exit_px) + FUNDING_DAY * e * days
        trades.append({"t": c[i]["t"], "r": (sg * (exit_px - e) - cost) / risk, "days": days})
        i = j + 1
    return trades


def stats(trades, years, risk=0.01):
    n = len(trades)
    if not n:
        return {"n": 0, "win": 0, "r": 0, "r_year": 0, "pct_year": 0, "dd_r": 0, "streak": 0}
    eq = peak = dd = 0.0
    cur = best = 0
    for t in trades:
        eq += t["r"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        cur = cur + 1 if t["r"] <= 0 else 0
        best = max(best, cur)
    tot = sum(t["r"] for t in trades)
    return {"n": n, "win": round(100 * sum(t["r"] > 0 for t in trades) / n, 1), "r": round(tot, 1),
            "r_year": round(tot / years, 2), "pct_year": round(tot / years * risk * 100, 1),
            "dd_r": round(dd, 1), "streak": best}


def buy_hold(c, t0, t1):
    seg = [x for x in c if t0 <= x["t"] < t1]
    if len(seg) < 2:
        return None
    peak, dd = seg[0]["c"], 0.0
    for x in seg:
        peak = max(peak, x["c"])
        dd = min(dd, x["c"] / peak - 1)
    years = (seg[-1]["t"] - seg[0]["t"]) / (365 * DAY_MS)
    tot = seg[-1]["c"] / seg[0]["c"] - 1
    return {"total_pct": round(tot * 100, 1),
            "year_pct": round(((1 + tot) ** (1 / years) - 1) * 100, 1) if years > 0 else None,
            "max_drop_pct": round(dd * 100, 1)}


def grid():
    g = []
    for fast, slow in ((10, 30), (20, 50), (50, 100), (50, 200)):
        for flt in (0, 100, 200):
            if flt and flt <= slow:
                continue
            for sl in (2, 3, 4):
                for tp in (0, 3, 6):
                    g.append(("cruce", {"fast": fast, "slow": slow, "filter": flt, "sl": sl, "tp": tp}))
    for n, ex in ((20, 10), (55, 20), (100, 50)):
        for flt in (0, 200):
            for sl in (2, 3, 4):
                for tp in (0, 6):
                    g.append(("ruptura", {"n": n, "exit": ex, "filter": flt, "sl": sl, "tp": tp}))
    for rl in (25, 30, 35):
        for sl in (1.5, 2, 3):
            for tp in (0, 2, 3):
                g.append(("rebote", {"rsi_lo": rl, "sl": sl, "tp": tp}))
    for lo, hi in ((20, 25), (20, 30), (25, 30)):
        for sl in (2, 3):
            g.append(("regimen", {"lo": lo, "hi": hi, "sl": sl, "tp": 0}))
    for need, wr in ((2, False), (3, False), (3, True), (4, True)):
        for sl in (2, 3, 4):
            g.append(("votos", {"need": need, "with_rsi": wr, "sl": sl, "tp": 0}))
    return g


def evaluate_daily(charts_url, symbols, names):
    data = {names[s]: daily_history(charts_url, s) for s in symbols}
    ind = {k: indicators(v) for k, v in data.items()}
    t_start = max(v[205]["t"] for v in data.values() if len(v) > 300)
    t_end = min(v[-1]["t"] for v in data.values())
    split = t_start + int((t_end - t_start) * 0.6)
    ya, yb = (split - t_start) / (365 * DAY_MS), (t_end - split) / (365 * DAY_MS)
    rows = []
    for kind, p in grid():
        for sides in ("long", "both"):
            per = {k: run(data[k], ind[k], kind, p, sides) for k in data}
            for combo in (["BTC"], ["ETH"], ["BTC", "ETH"]):
                tr = [t for k in combo for t in per[k] if t["t"] >= t_start]
                tr.sort(key=lambda t: t["t"])
                A = stats([t for t in tr if t["t"] < split], ya)
                B = stats([t for t in tr if t["t"] >= split], yb)
                rows.append({"kind": kind, "p": p, "sides": sides, "coins": "+".join(combo), "A": A, "B": B})
    def label(r):
        return f"{r['kind']} {r['p']} {'solo largos' if r['sides'] == 'long' else 'largos+cortos'} {r['coins']}"
    for r in rows:
        r["label"] = label(r)
    ok = [r for r in rows if r["A"]["n"] >= 10 and r["B"]["n"] >= 6]
    best_by_kind = {}
    for kind in ("cruce", "ruptura", "rebote", "regimen", "votos"):
        cand = sorted([r for r in ok if r["kind"] == kind], key=lambda r: r["A"]["r_year"], reverse=True)
        best_by_kind[kind] = cand[:3]
    robust = sorted([r for r in ok if r["A"]["r_year"] > 0 and r["B"]["r_year"] > 0],
                    key=lambda r: min(r["A"]["r_year"], r["B"]["r_year"]), reverse=True)
    iso = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t / 1000))
    return {
        "generated": int(time.time() * 1000), "tested": len(rows),
        "periodo_A": [iso(t_start), iso(split), round(ya, 1)], "periodo_B": [iso(split), iso(t_end), round(yb, 1)],
        "velas": {k: len(v) for k, v in data.items()},
        "comprar_y_mantener": {k: {"A": buy_hold(v, t_start, split), "B": buy_hold(v, split, t_end + 1)}
                               for k, v in data.items()},
        "mejor_por_tipo_elegida_en_A": best_by_kind,
        "robustas": robust[:15], "robustas_total": len(robust), "validas_total": len(ok),
    }
