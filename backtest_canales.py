#!/usr/bin/env python3
"""
Compara periodos de canal para la estrategia que usa el bot ahora (strategy.py):
  entrada: cierre diario por encima del máximo (o por debajo del mínimo) de N días,
           a favor de la media de F días (F=0: sin filtro)
  stop:    2 ATR, comprobado con el máximo/mínimo de cada vela
  salida:  cierre por debajo del mínimo (o encima del máximo) de M días
Incluye comisiones y un coste aproximado de financiación del perpetuo.
Periodo A (60 % más antiguo) y B (40 % reciente) por separado, para ver si aguanta.
No usa claves de Kraken: solo velas públicas. Escribe backtest_canales.json y .md.
"""

import json
import time

from backtest_daily import daily_history, ema, wilder

CHARTS_URL = "https://futures.kraken.com/api/charts/v1"
SYMBOLS = {"PF_XBTUSD": "BTC", "PF_ETHUSD": "ETH"}
FEE, FUNDING_DAY, DAY_MS = 0.0005, 0.0003, 86400 * 1000
SL_ATR, RISK, WARM = 2.0, 0.03, 205

ENTRIES = (10, 15, 20, 25, 30, 40, 55)
EXITS = (5, 7, 10, 15, 20)
FILTERS = (200, 100, 50, 0)


def channel(h, lo, n):
    dh, dl = [None] * len(h), [None] * len(h)
    for i in range(n, len(h)):
        dh[i], dl[i] = max(h[i - n:i]), min(lo[i - n:i])
    return dh, dl


def prep(c):
    cl, h, lo = [x["c"] for x in c], [x["h"] for x in c], [x["l"] for x in c]
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(c))]
    return {"t": [x["t"] for x in c], "c": cl, "h": h, "l": lo, "atr": wilder(tr, 14, 1),
            "ema": {f: ema(cl, f) for f in FILTERS if f},
            "ch": {n: channel(h, lo, n) for n in set(ENTRIES) | set(EXITS)}}


def simulate(x, n_in, n_out, flt):
    eh, el = x["ch"][n_in]
    xh, xl = x["ch"][n_out]
    c, h, lo, atr = x["c"], x["h"], x["l"], x["atr"]
    trades, i, last = [], WARM, len(c) - 1
    while i < last:
        if eh[i] is None or atr[i] is None:
            i += 1
            continue
        t = x["ema"][flt][i] if flt else None
        p = c[i]
        side = 1 if p > eh[i] and (t is None or p > t) else -1 if p < el[i] and (t is None or p < t) else 0
        if not side:
            i += 1
            continue
        risk = SL_ATR * atr[i]
        sl = p - side * risk
        exit_px, j = None, i
        for j in range(i + 1, len(c)):
            if (lo[j] <= sl) if side > 0 else (h[j] >= sl):
                exit_px = sl
                break
            if (side > 0 and c[j] < xl[j]) or (side < 0 and c[j] > xh[j]):
                exit_px = c[j]
                break
        open_now = exit_px is None
        if open_now:
            exit_px = c[last]
        days = j - i
        cost = FEE * (p + exit_px) + FUNDING_DAY * p * days
        trades.append({"t": x["t"][i], "r": (side * (exit_px - p) - cost) / risk, "days": days,
                       "side": "compra" if side > 0 else "venta", "open": open_now})
        i = j + 1
    return trades


def stats(tr, years):
    n = len(tr)
    if not n or years <= 0:
        return {"n": 0, "por_año": 0, "acierto": 0, "r_año": 0, "pct_año": 0, "dd_pct": 0, "racha_mala": 0}
    eq = peak = dd = 0.0
    cur = worst = 0
    for t in tr:
        eq += t["r"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        cur = cur + 1 if t["r"] <= 0 else 0
        worst = max(worst, cur)
    tot = sum(t["r"] for t in tr)
    return {"n": n, "por_año": round(n / years, 1), "acierto": round(100 * sum(t["r"] > 0 for t in tr) / n),
            "r_año": round(tot / years, 2), "pct_año": round(tot / years * RISK * 100, 1),
            "dd_pct": round(dd * RISK * 100, 1), "racha_mala": worst}


def main():
    data = {name: daily_history(CHARTS_URL, sym) for sym, name in SYMBOLS.items()}
    x = {k: prep(v) for k, v in data.items()}
    t0 = max(v[WARM]["t"] for v in data.values())
    t1 = min(v[-1]["t"] for v in data.values())
    split = t0 + int((t1 - t0) * 0.6)
    y_all, ya, yb = [(b - a) / (365 * DAY_MS) for a, b in ((t0, t1), (t0, split), (split, t1))]
    t_year = t1 - 365 * DAY_MS
    iso = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t / 1000))

    rows = []
    for n_in in ENTRIES:
        for n_out in EXITS:
            if n_out >= n_in:
                continue
            for flt in FILTERS:
                tr = sorted((t for k in x for t in simulate(x[k], n_in, n_out, flt) if t["t"] >= t0),
                            key=lambda t: t["t"])
                ent = [iso(t["t"]) for t in tr[-3:]]
                rows.append({
                    "entrada": n_in, "salida": n_out, "media": flt,
                    "actual": (n_in, n_out, flt) == (20, 10, 200),
                    "total": stats(tr, y_all),
                    "A": stats([t for t in tr if t["t"] < split], ya),
                    "B": stats([t for t in tr if t["t"] >= split], yb),
                    "ultimo_año": stats([t for t in tr if t["t"] >= t_year], 1.0),
                    "ultimas_entradas": ent,
                })
    for r in rows:
        r["robusta"] = r["A"]["r_año"] > 0 and r["B"]["r_año"] > 0
        r["nota"] = min(r["A"]["r_año"], r["B"]["r_año"])

    out = {"generado": iso(time.time() * 1000), "velas": {k: len(v) for k, v in data.items()},
           "periodo_total": [iso(t0), iso(t1), round(y_all, 1)],
           "periodo_A": [iso(t0), iso(split)], "periodo_B": [iso(split), iso(t1)],
           "riesgo_por_operacion": RISK, "variantes": rows}
    with open("backtest_canales.json", "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    def line(r):
        tag = " ← ACTUAL" if r["actual"] else ""
        m = f"media {r['media']}" if r["media"] else "sin filtro"
        a, b, tt, u = r["A"], r["B"], r["total"], r["ultimo_año"]
        return (f"| {r['entrada']}/{r['salida']} {m}{tag} | {tt['por_año']} | {tt['acierto']} % | "
                f"{a['pct_año']} % | {b['pct_año']} % | {u['n']} ({u['pct_año']} %) | {tt['dd_pct']} % | "
                f"{tt['racha_mala']} | {', '.join(r['ultimas_entradas'][-2:])} |")

    head = ("| Canal entrada/salida | Ops/año | Acierto | %/año A | %/año B | Último año: ops (%) | "
            "Peor caída | Racha mala | Últimas entradas |\n|---|---|---|---|---|---|---|---|---|")
    robust = sorted([r for r in rows if r["robusta"] and r["total"]["n"] >= 15], key=lambda r: r["nota"], reverse=True)
    freq = sorted([r for r in rows if r["robusta"] and r["total"]["por_año"] >= 20],
                  key=lambda r: r["nota"], reverse=True)
    cur = [r for r in rows if r["actual"]]
    md = [f"# Prueba de canales ({out['generado']})",
          f"BTC + ETH, velas diarias {out['periodo_total'][0]} a {out['periodo_total'][1]} "
          f"({out['periodo_total'][2]} años). A = {out['periodo_A'][0]}→{out['periodo_A'][1]}, "
          f"B = {out['periodo_B'][0]}→{out['periodo_B'][1]}. % con riesgo del 3 % por operación, "
          "suma simple sin interés compuesto, con comisiones y financiación.",
          "", "## Estrategia actual", head, *map(line, cur),
          "", "## Las 15 más sólidas (ganan en A y en B)", head, *map(line, robust[:15]),
          "", "## Sólidas que operan 20+ veces al año", head, *map(line, freq[:10]),
          "", f"Variantes probadas: {len(rows)}. Sólidas: {sum(r['robusta'] for r in rows)}."]
    with open("backtest_canales.md", "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
