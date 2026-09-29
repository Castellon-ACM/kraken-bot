"""
Estrategia activa: la única que ganó en los dos años de la prueba histórica.

  Solo BTC, velas de 1 hora.
  Compra: la media de 21 cruza por encima de la de 55 y el precio está sobre la media de 100.
  Venta:  la media de 21 cruza por debajo de la de 55 y el precio está bajo la media de 100.
  Stop a 3 ATR y objetivo a 4,5 ATR. Se cierra antes si las medias se cruzan en contra.

El bot en vivo y la prueba histórica usan este mismo código.
"""

EMA_FAST, EMA_SLOW, EMA_TREND, ATR_LEN = 21, 55, 100, 14
SL_ATR, TP_ATR = 3.0, 4.5
WARMUP = max(EMA_SLOW, EMA_TREND) + 5


def ema(v, n):
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


def compute(c):
    cl = [x["c"] for x in c]
    return {"c": cl, "fast": ema(cl, EMA_FAST), "slow": ema(cl, EMA_SLOW), "trend": ema(cl, EMA_TREND),
            "atr": atr_series(c, ATR_LEN)}


def cross_at(x, i):
    f, s = x["fast"], x["slow"]
    if f[i - 1] <= s[i - 1] and f[i] > s[i]:
        return "up"
    if f[i - 1] >= s[i - 1] and f[i] < s[i]:
        return "down"
    return None


def decide(x, i):
    if i < WARMUP or x["atr"][i] is None:
        return None
    p, f, s, t = x["c"][i], x["fast"][i], x["slow"][i], x["trend"][i]
    cross, bull = cross_at(x, i), p > t
    d = {"side": None, "mode": "tendencia", "cross": cross, "atr": x["atr"][i], "price": p,
         "trend": "alcista" if bull else "bajista", "regime": "tendencia", "adx": None, "rsi": None,
         "sl": SL_ATR, "tp": TP_ATR}
    if cross == "up" and bull:
        d.update(side="long", reason="Cruce alcista de medias con el precio sobre la media de 100")
    elif cross == "down" and not bull:
        d.update(side="short", reason="Cruce bajista de medias con el precio bajo la media de 100")
    elif cross:
        d["reason"] = (f"Cruce {'alcista' if cross == 'up' else 'bajista'} de medias, pero va contra la "
                       f"tendencia de fondo ({d['trend']}), así que no entra")
    else:
        rel = "por encima" if f > s else "por debajo"
        d["reason"] = f"Sin señal: la media rápida sigue {rel} de la lenta, no ha habido cruce en esta vela"
    return d


def should_exit(x, i, side, mode=None):
    c = cross_at(x, i)
    return (side == "long" and c == "down") or (side == "short" and c == "up")
