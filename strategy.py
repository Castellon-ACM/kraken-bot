"""
Estrategia combinada. Un detector (ADX) decide qué tipo de mercado hay y qué estrategia usar:

  Mercado en tendencia (ADX >= 25)
    - Tendencia: la media de 21 cruza la de 55 a favor de la media de 200.
    - Ruptura: el precio rompe el máximo/mínimo de las últimas 20 horas a favor de la tendencia.
    Stop 2 ATR, objetivo 3 ATR. Se cierra antes si las medias se cruzan en contra.

  Mercado lateral (ADX < 20)
    - Rebote: el precio se sale de las bandas de Bollinger con RSI extremo (<30 o >70)
      y se apuesta a que vuelve hacia la media.
    Stop 1,5 ATR, objetivo 1,5 ATR.

  Mercado indeciso (ADX entre 20 y 25): no opera.

El bot en vivo y la prueba histórica usan exactamente este mismo código.
"""

import math

EMA_FAST, EMA_SLOW, EMA_TREND = 21, 55, 200
ATR_LEN, ADX_LEN, RSI_LEN = 14, 14, 14
BB_LEN, BB_K, DON_LEN = 20, 2.0, 20
ADX_TREND, ADX_RANGE = 25, 20
EXITS = {"tendencia": (2.0, 3.0), "ruptura": (2.0, 3.0), "rebote": (1.5, 1.5)}  # (stop, objetivo) en ATR
WARMUP = EMA_TREND + 10


def ema(v, n):
    k, out = 2 / (n + 1), []
    for i, x in enumerate(v):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def wilder(v, n, start=0):
    """Media suavizada de Wilder; None hasta tener n valores desde 'start'."""
    out = [None] * len(v)
    if len(v) < start + n:
        return out
    a = sum(v[start:start + n]) / n
    out[start + n - 1] = a
    for i in range(start + n, len(v)):
        a = (a * (n - 1) + v[i]) / n
        out[i] = a
    return out


def compute(c):
    cl = [x["c"] for x in c]
    h = [x["h"] for x in c]
    lo = [x["l"] for x in c]
    n = len(c)
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, n)]
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if (h[i] - h[i - 1]) > (lo[i - 1] - lo[i]) else 0.0 for i in range(1, n)]
    mdm = [0.0] + [max(lo[i - 1] - lo[i], 0) if (lo[i - 1] - lo[i]) > (h[i] - h[i - 1]) else 0.0 for i in range(1, n)]
    atr = wilder(tr, ATR_LEN, 1)
    sp, sm = wilder(pdm, ADX_LEN, 1), wilder(mdm, ADX_LEN, 1)
    dx = [0.0] * n
    first = None
    for i in range(n):
        if atr[i] and sp[i] is not None:
            pdi, mdi = 100 * sp[i] / atr[i], 100 * sm[i] / atr[i]
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
            first = i if first is None else first
    adx = wilder(dx, ADX_LEN, first if first is not None else n)
    up = [0.0] + [max(cl[i] - cl[i - 1], 0) for i in range(1, n)]
    dn = [0.0] + [max(cl[i - 1] - cl[i], 0) for i in range(1, n)]
    ag, al = wilder(up, RSI_LEN, 1), wilder(dn, RSI_LEN, 1)
    rsi = [None if ag[i] is None else (100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])) for i in range(n)]
    mid, bhi, blo = [None] * n, [None] * n, [None] * n
    for i in range(BB_LEN - 1, n):
        w = cl[i - BB_LEN + 1:i + 1]
        m = sum(w) / BB_LEN
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / BB_LEN)
        mid[i], bhi[i], blo[i] = m, m + BB_K * sd, m - BB_K * sd
    dhi, dlo = [None] * n, [None] * n
    for i in range(DON_LEN, n):
        dhi[i], dlo[i] = max(h[i - DON_LEN:i]), min(lo[i - DON_LEN:i])
    return {"c": cl, "fast": ema(cl, EMA_FAST), "slow": ema(cl, EMA_SLOW), "trend": ema(cl, EMA_TREND),
            "atr": atr, "adx": adx, "rsi": rsi, "bhi": bhi, "blo": blo, "mid": mid, "dhi": dhi, "dlo": dlo}


def cross_at(x, i):
    f, s = x["fast"], x["slow"]
    if f[i - 1] <= s[i - 1] and f[i] > s[i]:
        return "up"
    if f[i - 1] >= s[i - 1] and f[i] < s[i]:
        return "down"
    return None


def decide(x, i):
    """Qué haría la estrategia al cierre de la vela i. Devuelve None si faltan datos."""
    if i < WARMUP or None in (x["atr"][i], x["adx"][i], x["rsi"][i], x["bhi"][i], x["dhi"][i]):
        return None
    p, adx, rsi = x["c"][i], x["adx"][i], x["rsi"][i]
    f, s, t = x["fast"][i], x["slow"][i], x["trend"][i]
    cross = cross_at(x, i)
    bull = p > t
    d = {"side": None, "mode": None, "cross": cross, "adx": round(adx, 1), "rsi": round(rsi, 1),
         "atr": x["atr"][i], "price": p, "trend": "alcista" if bull else "bajista"}

    def go(side, mode, why):
        d.update(side=side, mode=mode, sl=EXITS[mode][0], tp=EXITS[mode][1], reason=why)
        return d

    if adx >= ADX_TREND:
        d["regime"] = "tendencia"
        if cross == "up" and bull:
            return go("long", "tendencia", "Cruce alcista de medias a favor de la tendencia")
        if cross == "down" and not bull:
            return go("short", "tendencia", "Cruce bajista de medias a favor de la tendencia")
        if p > x["dhi"][i] and bull and f > s:
            return go("long", "ruptura", "Rompe el máximo de las últimas 20 horas en tendencia alcista")
        if p < x["dlo"][i] and not bull and f < s:
            return go("short", "ruptura", "Rompe el mínimo de las últimas 20 horas en tendencia bajista")
        d["reason"] = f"Mercado con tendencia (ADX {adx:.0f}), pero sin cruce ni ruptura en esta vela"
    elif adx < ADX_RANGE:
        d["regime"] = "lateral"
        if p < x["blo"][i] and rsi < 30:
            return go("long", "rebote", f"Precio por debajo de su banda y RSI {rsi:.0f}: busca rebote al alza")
        if p > x["bhi"][i] and rsi > 70:
            return go("short", "rebote", f"Precio por encima de su banda y RSI {rsi:.0f}: busca rebote a la baja")
        d["reason"] = f"Mercado lateral (ADX {adx:.0f}), pero el precio no está en un extremo (RSI {rsi:.0f})"
    else:
        d["regime"] = "indeciso"
        d["reason"] = f"Mercado indeciso (ADX {adx:.0f}, entre 20 y 25): no opera"
    return d


def should_exit(x, i, side, mode):
    """Las operaciones de tendencia y ruptura se cierran si las medias se cruzan en contra."""
    if mode == "rebote":
        return False
    c = cross_at(x, i)
    return (side == "long" and c == "down") or (side == "short" and c == "up")
