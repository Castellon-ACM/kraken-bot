#!/usr/bin/env python3
"""
Bot de perpetuos para Kraken Futures pensado para GitHub Actions.
Se ejecuta una vez cada ~30 minutos, hace su trabajo y termina.

Archivos del repositorio:
  control.json  -> lo escribe el panel del iPhone (activar/desactivar, cerrar todo)
  state.json    -> memoria interna del bot
  status.json   -> lo que muestra el panel (saldo, ganancias, posiciones, eventos)

Estrategia (velas de 1 h):
  Largo: EMA21 cruza sobre EMA55 con precio sobre EMA200. Corto: al revés.
  Stop a 2 ATR y objetivo a 3 ATR como órdenes reduce-only en Kraken.
  Cruce contrario cierra la posición.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

ENV = os.getenv("KRAKEN_ENV", "demo").strip().lower() or "demo"
API_KEY = os.getenv("KRAKEN_API_KEY", "").strip()
API_SECRET = os.getenv("KRAKEN_API_SECRET", "").strip()
CONFIRM_LIVE = os.getenv("CONFIRMO_DINERO_REAL", "no").strip().lower()
SYMBOLS = ["PF_XBTUSD", "PF_ETHUSD"]
RISK_PER_TRADE = 0.02
MAX_LEVERAGE = 3
MAX_DAILY_LOSS = 0.06

TIMEFRAME, CANDLE_MS = "1h", 3600 * 1000
EMA_FAST, EMA_SLOW, EMA_TREND, ATR_LEN = 21, 55, 200, 14
SL_ATR, TP_ATR = 2.0, 3.0

BASES = {"demo": "https://demo-futures.kraken.com", "live": "https://futures.kraken.com"}
CHARTS_URL = "https://futures.kraken.com/api/charts/v1"
FALLBACK_SPECS = {"PF_XBTUSD": (4, 1.0), "PF_ETHUSD": (3, 0.1)}
NAMES = {"PF_XBTUSD": "BTC", "PF_ETHUSD": "ETH"}

HERE = os.path.dirname(os.path.abspath(__file__))
P_CONTROL, P_STATE, P_STATUS = (os.path.join(HERE, f) for f in ("control.json", "state.json", "status.json"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- Kraken Futures
class Kraken:
    def __init__(self, base, key, secret):
        self.base, self.key, self.secret = base, key, secret
        self.s = requests.Session()
        self._n = 0

    def _nonce(self):
        n = max(int(time.time() * 1000), self._n + 1)
        self._n = n
        return str(n)

    def _sign(self, endpoint, data, nonce):
        digest = hashlib.sha256((data + nonce + endpoint).encode()).digest()
        return base64.b64encode(hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512).digest()).decode()

    def req(self, method, endpoint, params=None, private=False):
        data = urlencode(params or {})
        url = self.base + "/derivatives" + endpoint
        h = {}
        if private:
            nonce = self._nonce()
            h = {"APIKey": self.key, "Nonce": nonce, "Authent": self._sign(endpoint, data, nonce)}
        if method == "POST":
            h["Content-Type"] = "application/x-www-form-urlencoded"
            r = self.s.post(url, data=data, headers=h, timeout=15)
        else:
            r = self.s.request(method, url + ("?" + data if data else ""), headers=h, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("result") != "success":
            raise RuntimeError(f"Kraken {endpoint}: {j.get('error', j)}")
        return j

    def instruments(self):
        return self.req("GET", "/api/v3/instruments")["instruments"]

    def tickers(self):
        return {t["symbol"].upper(): t for t in self.req("GET", "/api/v3/tickers")["tickers"]}

    def equity(self):
        return float(self.req("GET", "/api/v3/accounts", private=True)["accounts"]["flex"]["portfolioValue"])

    def positions(self):
        res = self.req("GET", "/api/v3/openpositions", private=True)["openPositions"]
        return {p["symbol"].upper(): p for p in res if float(p.get("size", 0)) > 0}

    def order(self, **p):
        j = self.req("POST", "/api/v3/sendorder", p, private=True)
        st = j.get("sendStatus", {}).get("status")
        if st != "placed":
            raise RuntimeError(f"orden rechazada ({st})")
        return j

    def cancel_all(self, sym):
        return self.req("POST", "/api/v3/cancelallorders", {"symbol": sym.lower()}, private=True)

    def set_leverage(self, sym, lev):
        return self.req("PUT", "/api/v3/leveragepreferences", {"symbol": sym, "maxLeverage": lev}, private=True)


def get_candles(sym, hours=450):
    now = int(time.time())
    r = requests.get(f"{CHARTS_URL}/trade/{sym}/{TIMEFRAME}", params={"from": now - hours * 3600, "to": now}, timeout=15)
    r.raise_for_status()
    c = [{"t": int(x["time"]), "h": float(x["high"]), "l": float(x["low"]), "c": float(x["close"])}
         for x in r.json()["candles"]]
    return [x for x in c if x["t"] + CANDLE_MS <= now * 1000]


def ema(v, n):
    k, out = 2 / (n + 1), []
    for i, x in enumerate(v):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def atr(c, n):
    trs = [max(x["h"] - x["l"], abs(x["h"] - (c[i - 1]["c"] if i else x["c"])),
               abs(x["l"] - (c[i - 1]["c"] if i else x["c"]))) for i, x in enumerate(c)]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def signal(c):
    if len(c) < EMA_TREND + 5:
        return None, None, None, None
    cl = [x["c"] for x in c]
    f, s, t = ema(cl, EMA_FAST), ema(cl, EMA_SLOW), ema(cl, EMA_TREND)
    cross = "up" if f[-2] <= s[-2] and f[-1] > s[-1] else "down" if f[-2] >= s[-2] and f[-1] < s[-1] else None
    entry = "long" if cross == "up" and cl[-1] > t[-1] else "short" if cross == "down" and cl[-1] < t[-1] else None
    return entry, cross, atr(c, ATR_LEN), cl[-1]


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.k = Kraken(BASES[ENV], API_KEY, API_SECRET)
        self.control = read_json(P_CONTROL, {"active": False, "close_all": 0})
        self.state = read_json(P_STATE, {})
        for key, val in {"day": "", "day_start_equity": 0, "initial_equity": 0, "halted_today": False,
                         "last_candle": {}, "open": {}, "handled_close_all": 0, "events": [],
                         "env": ENV, "leverage_set": False}.items():
            self.state.setdefault(key, val)
        if self.state["env"] != ENV:  # cambio demo <-> real: empezamos las cuentas de cero
            self.state.update(env=ENV, initial_equity=0, day="", open={}, events=[], leverage_set=False)
        self.specs = dict(FALLBACK_SPECS)
        try:
            for ins in self.k.instruments():
                sym = ins.get("symbol", "").upper()
                if sym in SYMBOLS:
                    self.specs[sym] = (int(ins.get("contractValuePrecision", self.specs[sym][0])),
                                       float(ins.get("tickSize", self.specs[sym][1])))
        except Exception:
            pass

    def event(self, text):
        print(text)
        self.state["events"] = ([{"t": now_iso(), "msg": text}] + self.state["events"])[:15]

    def rsize(self, sym, x):
        d = self.specs[sym][0]
        return math.floor(x * 10 ** d) / 10 ** d

    def rprice(self, sym, p):
        tick = self.specs[sym][1]
        dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
        return round(round(p / tick) * tick, dec)

    def close(self, sym, pos, why):
        self.k.order(orderType="mkt", symbol=sym, side="sell" if pos["side"] == "long" else "buy",
                     size=pos["size"], reduceOnly="true")
        try:
            self.k.cancel_all(sym)
        except Exception:
            pass
        self.state["open"].pop(sym, None)
        self.event(f"Cerrada {pos['side']} {NAMES[sym]} ({why})")

    def close_all(self, why):
        for sym, pos in self.k.positions().items():
            if sym in SYMBOLS:
                self.close(sym, pos, why)

    def open(self, sym, side, price, atr_val, equity):
        dist = SL_ATR * atr_val
        size = min(equity * RISK_PER_TRADE / dist, equity * MAX_LEVERAGE / len(SYMBOLS) / price)
        size = self.rsize(sym, size)
        if size <= 0:
            self.event(f"{NAMES[sym]}: saldo insuficiente para abrir con el riesgo fijado")
            return
        self.k.order(orderType="mkt", symbol=sym, side="buy" if side == "long" else "sell", size=size)
        time.sleep(3)
        pos = self.k.positions().get(sym)
        if not pos:
            self.event(f"{NAMES[sym]}: orden enviada pero no aparece la posición, revisa Kraken")
            return
        entry, size, sg = float(pos["price"]), float(pos["size"]), 1 if side == "long" else -1
        sl, tp = self.rprice(sym, entry - sg * dist), self.rprice(sym, entry + sg * TP_ATR * atr_val)
        ex = "sell" if side == "long" else "buy"
        try:
            self.k.order(orderType="stp", symbol=sym, side=ex, size=size, stopPrice=sl,
                         triggerSignal="mark", reduceOnly="true")
            self.k.order(orderType="take_profit", symbol=sym, side=ex, size=size, stopPrice=tp,
                         triggerSignal="mark", reduceOnly="true")
        except Exception as e:
            self.close(sym, pos, f"no se pudo poner el stop: {e}")
            return
        self.state["open"][sym] = {"side": side, "sl": sl, "tp": tp}
        self.event(f"Abierta {'compra' if side == 'long' else 'venta'} {NAMES[sym]} a {entry} (stop {sl}, objetivo {tp})")

    def run(self):
        if not self.state["leverage_set"]:
            for sym in SYMBOLS:
                try:
                    self.k.set_leverage(sym, MAX_LEVERAGE)
                except Exception as e:
                    print("apalancamiento", sym, e)
            self.state["leverage_set"] = True

        equity = self.k.equity()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # si el bot no tiene operaciones y el saldo cambia de golpe, es un ingreso o una retirada tuya
        last = self.state.get("last_equity")
        if last is not None and not self.state["open"]:
            delta = equity - last
            if abs(delta) > max(0.5, 0.02 * last):
                self.state["day_start_equity"] = max(0, self.state["day_start_equity"] + delta)
                self.state["initial_equity"] = max(0, self.state["initial_equity"] + delta)
                self.state["halted_today"] = False
                self.event(f"{'Ingreso' if delta > 0 else 'Retirada'} de {abs(delta):.2f} $ en la cartera de futuros. "
                           "No cuenta como ganancia ni pérdida.")
        if not self.state["initial_equity"]:
            self.state["initial_equity"] = equity
        if self.state["day"] != today:
            self.state.update(day=today, day_start_equity=equity, halted_today=False)

        req = int(self.control.get("close_all", 0) or 0)
        if req > self.state["handled_close_all"]:
            self.state["handled_close_all"] = req
            self.close_all("cierre manual desde el panel")

        start = self.state["day_start_equity"] or equity
        if start > 0 and (start - equity) / start >= MAX_DAILY_LOSS and not self.state["halted_today"]:
            self.close_all("pérdida máxima diaria")
            self.state["halted_today"] = True
            self.event("Pérdida diaria del 6 % alcanzada. Sin operar hasta mañana.")

        active = bool(self.control.get("active")) and not self.state["halted_today"] and equity >= 1
        positions = self.k.positions()
        for sym in SYMBOLS:
            pos = positions.get(sym)
            if not pos and sym in self.state["open"]:
                try:
                    self.k.cancel_all(sym)
                except Exception:
                    pass
                self.state["open"].pop(sym)
                self.event(f"{NAMES[sym]}: posición cerrada por stop u objetivo")
            candles = get_candles(sym)
            if not candles or self.state["last_candle"].get(sym) == candles[-1]["t"]:
                continue
            self.state["last_candle"][sym] = candles[-1]["t"]
            entry, cross, a, price = signal(candles)
            if a is None:
                continue
            if pos and ((pos["side"] == "long" and cross == "down") or (pos["side"] == "short" and cross == "up")):
                self.close(sym, pos, "cambio de tendencia")
                pos = None
            if entry and not pos and active:
                self.open(sym, entry, price, a, equity)

        self.write_status(active, error=None if equity >= 1 else
                          "No hay saldo en la cartera de futuros de Kraken. Transfiere fondos para que el bot pueda operar.")

    def write_status(self, active, error=None):
        try:
            equity = self.k.equity()
            tick = self.k.tickers()
            positions = self.k.positions()
        except Exception:
            equity, tick, positions = self.state.get("last_equity", 0), {}, {}
        self.state["last_equity"] = equity
        pos_list = []
        for sym, p in positions.items():
            entry, size = float(p["price"]), float(p["size"])
            mark = float(tick.get(sym, {}).get("markPrice", entry))
            pnl = (mark - entry) * size * (1 if p["side"] == "long" else -1)
            extra = self.state["open"].get(sym, {})
            pos_list.append({"symbol": NAMES.get(sym, sym), "side": p["side"], "size": size, "entry": entry,
                             "mark": mark, "pnl": round(pnl, 2), "sl": extra.get("sl"), "tp": extra.get("tp")})
        write_json(P_STATUS, {
            "updated": now_iso(), "env": ENV, "active": bool(self.control.get("active")),
            "trading": active, "halted_today": self.state["halted_today"],
            "equity": round(equity, 2),
            "day_pnl": round(equity - (self.state["day_start_equity"] or equity), 2),
            "total_pnl": round(equity - (self.state["initial_equity"] or equity), 2),
            "initial_equity": round(self.state["initial_equity"] or equity, 2),
            "positions": pos_list, "events": self.state["events"], "error": error,
        })
        write_json(P_STATE, self.state)


def main():
    if ENV not in BASES:
        raise SystemExit("KRAKEN_ENV debe ser demo o live")
    if not API_KEY or not API_SECRET:
        write_json(P_STATUS, {"updated": now_iso(), "env": ENV, "error": "Faltan las claves de Kraken en los Secrets de GitHub"})
        raise SystemExit("Faltan claves")
    if ENV == "live" and CONFIRM_LIVE != "si":
        write_json(P_STATUS, {"updated": now_iso(), "env": ENV, "error": "Para operar en real añade la variable CONFIRMO_DINERO_REAL = si"})
        raise SystemExit("Falta confirmación de dinero real")
    bot = Bot()
    try:
        bot.run()
    except Exception as e:
        bot.event(f"Error: {e}")
        bot.write_status(bool(bot.control.get("active")), error=str(e))
        raise


if __name__ == "__main__":
    main()
