"""
Estrategia activa: ruptura de 20 días con filtro de la media de 200 (velas DIARIAS).
Fue de las más sólidas de la prueba de 972 variantes y de las que más opera (~15 veces al año).

  Compra: el cierre diario supera el máximo de los 20 días anteriores y el precio está sobre la media de 200.
  Venta:  el cierre diario cae por debajo del mínimo de los 20 días anteriores y el precio está bajo la media de 200.
  Stop a 2 ATR diarios, puesto en Kraken. Sin objetivo fijo: se deja correr la ganancia.
  Salida: la compra se cierra si el precio cae bajo el mínimo de 10 días; la venta, si supera el máximo de 10 días.
"""

EMA_TREND, ATR_LEN, ENTRY_N, EXIT_N = 200, 14, 20, 10
SL_ATR, TP_ATR = 2.0, 0.0
WARMUP = EMA_TREND + 5


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


def channel(h, lo, n):
    dh, dl = [None] * len(h), [None] * len(h)
    for i in range(n, len(h)):
        dh[i], dl[i] = max(h[i - n:i]), min(lo[i - n:i])
    return dh, dl


def compute(c):
    cl, h, lo = [x["c"] for x in c], [x["h"] for x in c], [x["l"] for x in c]
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(c))]
    eh, el = channel(h, lo, ENTRY_N)
    xh, xl = channel(h, lo, EXIT_N)
    return {"c": cl, "trend": ema(cl, EMA_TREND), "atr": wilder(tr, ATR_LEN, 1),
            "eh": eh, "el": el, "xh": xh, "xl": xl}


def decide(x, i):
    if i < WARMUP or x["atr"][i] is None or x["eh"][i] is None:
        return None
    p, t = x["c"][i], x["trend"][i]
    bull = p > t
    d = {"side": None, "mode": "ruptura", "atr": x["atr"][i], "price": p, "trend": "alcista" if bull else "bajista",
         "regime": "diario", "adx": None, "rsi": None, "sl": SL_ATR, "tp": TP_ATR}
    hi, lo = x["eh"][i], x["el"][i]
    if p > hi and bull:
        d.update(side="long", reason=f"Cierra por encima del máximo de 20 días ({hi:.0f}) en tendencia alcista")
    elif p < lo and not bull:
        d.update(side="short", reason=f"Cierra por debajo del mínimo de 20 días ({lo:.0f}) en tendencia bajista")
    elif p > hi or p < lo:
        d["reason"] = "Rompe el rango de 20 días, pero contra la tendencia de fondo (media de 200), así que no entra"
    else:
        d["reason"] = f"Sin señal: el precio sigue dentro del rango de 20 días ({lo:.0f} – {hi:.0f})"
    return d


def should_exit(x, i, side, mode=None):
    p = x["c"][i]
    if x["xl"][i] is None:
        return False
    return (side == "long" and p < x["xl"][i]) or (side == "short" and p > x["xh"][i])
