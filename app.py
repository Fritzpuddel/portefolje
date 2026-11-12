#!/usr/bin/env python3
"""Guggas portefølje på Raspberry Pi.

Henter kurser fra Nordnets offentlige kursdata, gemmer historikken i SQLite,
viser et live dashboard på hjemmenetværket og lægger hver aften dagens kurser
i et GitHub-repository, som Claude-dashboardet læser fra.

Kun Pythons standardbibliotek bruges, så intet skal installeres.

Brug:
    python3 app.py            start indsamling og dashboard (det systemd kører)
    python3 app.py hent       hent kurser én gang og afslut
    python3 app.py publicer   skriv kursfilerne og push til GitHub nu
"""
import csv
import datetime as dt
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
EKSPORT = os.path.join(BASE, "eksport")
SEED = os.path.join(BASE, "seed")
DB_PATH = os.path.join(DATA, "portefolje.db")
TZ = ZoneInfo("Europe/Copenhagen")

DEFAULT_CONFIG = {
    "port": 8080,
    "hent_hvert_minut": 5,
    "hent_fra_kl": 7,
    "hent_til_kl": 23,
    "konti": {},
    "historik_dage": 400,
    "github": {"aktiv": False, "publicer_kl": "22:45"},
}

log = logging.getLogger("portefolje")
STATUS = {"sidst_hentet": None, "fejl": None, "sidst_publiceret": None, "publicer_fejl": None}
LOCK = threading.Lock()


# ---------------------------------------------------------------- konfiguration

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.path.join(BASE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------- database

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(EKSPORT, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS kurser (
                isin TEXT NOT NULL, ts INTEGER NOT NULL, kurs REAL NOT NULL,
                valuta TEXT, PRIMARY KEY (isin, ts));
            CREATE TABLE IF NOT EXISTS papirer (
                isin TEXT PRIMARY KEY, navn TEXT, ticker TEXT, valuta TEXT, slug TEXT);
            CREATE TABLE IF NOT EXISTS valuta (
                dato TEXT NOT NULL, valuta TEXT NOT NULL, dkk REAL NOT NULL,
                PRIMARY KEY (dato, valuta));
            """
        )
        n = con.execute("SELECT COUNT(*) FROM kurser").fetchone()[0]
        if n == 0:
            seed(con)


def seed(con):
    """Første start: indlæs kurshistorikken fra Claude-dashboardet, så kurverne ikke starter fra nul."""
    path = os.path.join(SEED, "kurshistorik.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        ts = int(dt.datetime.fromisoformat(r["dato"] + "T17:30:00").replace(tzinfo=TZ).timestamp() * 1000)
        con.execute("INSERT OR IGNORE INTO kurser VALUES (?,?,?,?)", (r["isin"], ts, r["kurs"], r["valuta"]))
        if r["valuta"] != "DKK":
            con.execute("INSERT OR IGNORE INTO valuta VALUES (?,?,?)", (r["dato"], r["valuta"], r["dkk_pr_enhed"]))
    kpath = os.path.join(SEED, "kurser.json")
    if os.path.exists(kpath):
        with open(kpath, encoding="utf-8") as f:
            for k in json.load(f):
                con.execute("INSERT OR IGNORE INTO papirer VALUES (?,?,?,?,?)",
                            (k["isin"], k["papir"], k.get("ticker"), k["valuta"], None))
    log.info("Indlæste %d historiske kurser fra seed/", len(rows))


# ---------------------------------------------------------------- Nordnet-eksporter

def _num(s):
    s = (s or "").strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    return float(s.replace(".", "").replace(",", ".")) if s.count(",") == 1 and s.count(".") > 0 else float(s.replace(",", "."))


def read_exports(cfg):
    """Læs alle CSV-eksporter fra Nordnet i eksport/. Dubletter (samme Id) tælles én gang."""
    rows = {}
    for path in sorted(glob.glob(os.path.join(EKSPORT, "*.csv"))):
        raw = open(path, "rb").read()
        for enc in ("utf-16", "utf-8-sig", "latin-1"):
            try:
                text = raw.decode(enc)
                if "\t" in text.split("\n", 1)[0]:
                    break
            except UnicodeError:
                continue
        lines = list(csv.reader(text.splitlines(), delimiter="\t"))
        if not lines or lines[0][0].strip() != "Id":
            log.warning("Springer %s over: ligner ikke en Nordnet-eksport", path)
            continue
        for r in lines[1:]:
            if not r or not r[0].strip():
                continue
            r = r + [""] * (30 - len(r))
            depot = r[4].strip()
            rows[r[0]] = {
                "id": r[0].strip(),
                "dato": r[2].strip(),
                "konto": cfg["konti"].get(depot, "depot " + depot),
                "type": " ".join(r[5].split()),
                "papir": r[6].strip() or None,
                "isin": r[7].strip() or None,
                "antal": _num(r[8]),
                "kurs": _num(r[9]),
                "afgifter_dkk": _num(r[11]),
                "beloeb_dkk": _num(r[13]),
                "indkoebsvaerdi": _num(r[15]),
                "saldo_dkk": _num(r[20]),
            }
    return sorted(rows.values(), key=lambda t: int(t["id"]))


def delta(t):
    a = t["antal"] or 0
    if t["type"] == "KØBT" or t["type"].startswith("BYTTE INDL"):
        return a
    if t["type"] == "SOLGT" or t["type"].startswith("BYTTE OVERF") or t["type"].startswith("RENSNING"):
        return -a
    return 0


def holdings(tx):
    """Beholdning, kostpris og realiseret gevinst pr. papir (gennemsnitlig kostpris)."""
    m = {}
    for t in tx:
        if not t["isin"]:
            continue
        p = m.setdefault(t["isin"], {"isin": t["isin"], "papir": t["papir"], "antal": 0.0, "kostpris": 0.0, "realiseret": 0.0})
        a = t["antal"] or 0
        avg = p["kostpris"] / p["antal"] if p["antal"] > 1e-9 else 0
        ty = t["type"]
        if ty == "KØBT":
            p["antal"] += a
            p["kostpris"] -= t["beloeb_dkk"] or 0
        elif ty == "SOLGT":
            p["realiseret"] += (t["beloeb_dkk"] or 0) - avg * a
            p["kostpris"] -= avg * a
            p["antal"] -= a
        elif ty.startswith("BYTTE INDL"):
            p["antal"] += a
            p["kostpris"] += t["indkoebsvaerdi"] or 0
        elif ty.startswith("BYTTE OVERF"):
            p["kostpris"] -= avg * a
            p["antal"] -= a
        elif ty.startswith("RENSNING"):
            p["realiseret"] -= avg * a
            p["kostpris"] -= avg * a
            p["antal"] -= a
    return m


def held_isins(tx):
    return sorted(i for i, p in holdings(tx).items() if p["antal"] > 1e-6)


# ---------------------------------------------------------------- kurser og valuta

UA = "Mozilla/5.0 (X11; Linux aarch64) portefolje-pi"
PREF = ["DKK", "SEK", "EUR", "USD"]


def nordnet_lookup(isin):
    url = "https://www.nordnet.dk/api/2/instrument_search/query/instrument?apply_filters=" + urllib.parse.quote("isin=" + isin)
    req = urllib.request.Request(url, headers={"client-id": "NEXT", "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    best = None
    for x in data.get("results") or []:
        info, price = x.get("instrument_info", {}), x.get("price_info", {})
        last = (price.get("last") or {}).get("price") or (price.get("close") or {}).get("price")
        if not last:
            continue
        cand = {
            "navn": info.get("name"), "ticker": info.get("symbol"), "valuta": info.get("currency"),
            "kurs": float(last), "ts": int(price.get("tick_timestamp") or time.time() * 1000),
            "slug": (x.get("nnx_info") or {}).get("display_slug"),
            "rang": PREF.index(info.get("currency")) if info.get("currency") in PREF else 99,
        }
        if best is None or cand["rang"] < best["rang"]:
            best = cand
    return best


def fetch_quotes(cfg):
    tx = read_exports(cfg)
    isins = held_isins(tx)
    ok, fejl = 0, []
    with db() as con:
        for isin in isins:
            try:
                q = nordnet_lookup(isin)
            except Exception as e:  # netværk, ændret API osv.
                fejl.append(f"{isin}: {e}")
                continue
            if not q:
                fejl.append(f"{isin}: ingen kurs")
                continue
            con.execute("INSERT OR IGNORE INTO kurser VALUES (?,?,?,?)", (isin, q["ts"], q["kurs"], q["valuta"]))
            con.execute("INSERT OR REPLACE INTO papirer VALUES (?,?,?,?,?)", (isin, q["navn"], q["ticker"], q["valuta"], q["slug"]))
            ok += 1
            time.sleep(0.5)  # skån Nordnet
    with LOCK:
        STATUS["sidst_hentet"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
        STATUS["fejl"] = "; ".join(fejl) or None
    log.info("Hentede %d af %d kurser%s", ok, len(isins), (" – fejl: " + "; ".join(fejl)) if fejl else "")


def fetch_fx():
    """Dagens valutakurser fra Den Europæiske Centralbank (opdateres ca. kl. 16)."""
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    rates, dato = {"EUR": 1.0}, None
    for el in root.iter():
        if el.get("time"):
            dato = el.get("time")
        if el.get("currency"):
            rates[el.get("currency")] = float(el.get("rate"))
    if "DKK" not in rates or not dato:
        raise ValueError("ECB-filen manglede DKK")
    with db() as con:
        for cur, per_eur in rates.items():
            if cur == "DKK":
                continue
            con.execute("INSERT OR REPLACE INTO valuta VALUES (?,?,?)", (dato, cur, rates["DKK"] / per_eur))
    log.info("Valutakurser for %s hentet", dato)


# ---------------------------------------------------------------- beregninger

def local_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


class Prices:
    """Hurtige opslag i kurser og valutakurser med 'seneste kendte værdi på eller før'."""

    def __init__(self, con):
        self.q = {}
        for r in con.execute("SELECT isin, ts, kurs, valuta FROM kurser ORDER BY ts"):
            self.q.setdefault(r["isin"], []).append((r["ts"], r["kurs"], r["valuta"]))
        self.fx = {}
        for r in con.execute("SELECT dato, valuta, dkk FROM valuta ORDER BY dato"):
            self.fx.setdefault(r["valuta"], []).append((r["dato"], r["dkk"]))
        self.meta = {r["isin"]: dict(r) for r in con.execute("SELECT * FROM papirer")}

    def at(self, isin, ms):
        best = None
        for ts, k, v in self.q.get(isin, []):
            if ts > ms:
                break
            best = (ts, k, v)
        return best

    def latest(self, isin):
        s = self.q.get(isin)
        return s[-1] if s else None

    def dkk(self, valuta, dato):
        if valuta in (None, "DKK"):
            return 1.0
        best = None
        for d, x in self.fx.get(valuta, []):
            if d > dato:
                break
            best = x
        if best is None and self.fx.get(valuta):
            best = self.fx[valuta][0][1]
        return best

    def daily_closes(self, isin):
        out = {}
        for ts, k, v in self.q.get(isin, []):
            out[local_date(ts)] = (ts, k, v)
        return out


def account_state(tx_k, dato):
    """Antal pr. papir og kontant ved udgangen af en dato (kontant lægges sammen på tværs af konti)."""
    held, cash = {}, {}
    for t in tx_k:
        if t["dato"] > dato:
            break
        if t["isin"]:
            held[t["isin"]] = held.get(t["isin"], 0) + delta(t)
        if t["saldo_dkk"] is not None:
            cash[t["konto"]] = t["saldo_dkk"]
    return {i: a for i, a in held.items() if a > 1e-6}, sum(cash.values())


def value_at(P, held, cash, ms):
    dato = local_date(ms)
    v, missing = cash, False
    for isin, a in held.items():
        q = P.at(isin, ms)
        if not q:
            missing = True
            continue
        fx = P.dkk(q[2], dato)
        if fx is None:
            missing = True
            continue
        v += a * q[1] * fx
    return v, missing


def build_data(cfg):
    tx = read_exports(cfg)
    with db() as con:
        P = Prices(con)
    now = dt.datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    now_ms = int(now.timestamp() * 1000)
    konti = sorted({t["konto"] for t in tx})
    groups = {"alle": tx, **{k: [t for t in tx if t["konto"] == k] for k in konti}}

    # alle kursdatoer (luk-tidspunkt pr. dato = sidste kurs den dag)
    day_ts = {}
    for isin, s in P.q.items():
        for ts, _, _ in s:
            d = local_date(ts)
            day_ts[d] = max(day_ts.get(d, 0), ts)
    first = (now - dt.timedelta(days=cfg["historik_dage"])).strftime("%Y-%m-%d")
    dates = sorted(d for d in day_ts if d >= first)
    ref_date = dates[-1] if dates else today  # seneste handelsdag med kurser ("i dag" i weekenden = fredag)
    prev_dates = [d for d in dates if d < ref_date]
    prev_close_ms = int(dt.datetime.fromisoformat(prev_dates[-1] + "T23:59:59").replace(tzinfo=TZ).timestamp() * 1000) if prev_dates else None
    per_konto_h = {k: holdings([t for t in tx if t["konto"] == k]) for k in konti}

    out = {"konti": ["alle"] + konti, "opdateret": now.isoformat(timespec="seconds"), "status": dict(STATUS), "konto": {}}
    with LOCK:
        out["status"] = dict(STATUS)

    for k, tx_k in groups.items():
        if k == "alle":  # kostpris og realiseret lægges sammen pr. konto, så kontiene ikke blandes
            h = {}
            for hk in per_konto_h.values():
                for isin, p in hk.items():
                    q = h.setdefault(isin, {"isin": isin, "papir": p["papir"], "antal": 0.0, "kostpris": 0.0, "realiseret": 0.0})
                    for f in ("antal", "kostpris", "realiseret"):
                        q[f] += p[f]
        else:
            h = per_konto_h[k]
        held, cash = account_state(tx_k, today)
        pos = []
        for isin, a in held.items():
            q = P.latest(isin)
            meta = P.meta.get(isin, {})
            hp = h.get(isin, {})
            row = {"isin": isin, "papir": meta.get("navn") or hp.get("papir"), "ticker": meta.get("ticker"),
                   "antal": round(a, 4), "kostpris_dkk": round(hp.get("kostpris", 0), 2)}
            if q:
                fx = P.dkk(q[2], local_date(q[0])) or 0
                row.update(kurs=q[1], valuta=q[2], kurs_tid=dt.datetime.fromtimestamp(q[0] / 1000, TZ).isoformat(timespec="minutes"),
                           vaerdi_dkk=round(a * q[1] * fx, 2))
                row["afkast_dkk"] = round(row["vaerdi_dkk"] - row["kostpris_dkk"], 2)
                row["afkast_pct"] = row["afkast_dkk"] / row["kostpris_dkk"] if row["kostpris_dkk"] else None
                if prev_close_ms:
                    pq = P.at(isin, prev_close_ms)
                    if pq and pq[0] < q[0]:
                        row["dag_pct"] = q[1] / pq[1] - 1
            pos.append(row)
        vaerdi = cash + sum(p.get("vaerdi_dkk", 0) for p in pos)
        for p in pos:
            p["andel"] = p.get("vaerdi_dkk", 0) / vaerdi if vaerdi else 0
        flows = [t for t in tx_k if t["type"] in ("INDBETALING", "HÆVNING")]
        if k == "alle":  # overførsler mellem egne konti tæller ikke
            used = set()
            for a in flows:
                for b in flows:
                    if a["konto"] != b["konto"] and a["dato"] == b["dato"] and a["beloeb_dkk"] == -b["beloeb_dkk"] and a["id"] not in used and b["id"] not in used:
                        used |= {a["id"], b["id"]}
            flows = [t for t in flows if t["id"] not in used]
        indbetalt = sum(t["beloeb_dkk"] for t in flows if t["beloeb_dkk"] > 0)
        haevet = -sum(t["beloeb_dkk"] for t in flows if t["beloeb_dkk"] < 0)

        hist = []
        for d in dates:
            hd, cd = account_state(tx_k, d)
            if not hd and cd == 0:
                continue
            ms = int(dt.datetime.fromisoformat(d + "T23:59:59").replace(tzinfo=TZ).timestamp() * 1000)
            v, miss = value_at(P, hd, cd, ms)
            if not miss:
                hist.append({"dato": d, "vaerdi_dkk": round(v, 2)})
        prev_val = None
        if prev_close_ms:
            hp_, cp_ = account_state(tx_k, prev_dates[-1])
            pv, miss = value_at(P, hp_, cp_, prev_close_ms)
            prev_val = None if miss else pv

        intraday = []
        stamps = sorted({ts for isin in held for ts, _, _ in P.q.get(isin, []) if local_date(ts) == ref_date})
        for ts in stamps:
            v, miss = value_at(P, held, cash, ts)
            if not miss:
                intraday.append({"ts": ts, "vaerdi_dkk": round(v, 2)})

        papir_hist = {}
        for isin in held:
            closes = P.daily_closes(isin)
            papir_hist[isin] = [{"dato": d, "kurs": closes[d][1]} for d in dates if d in closes]

        out["konto"][k] = {
            "vaerdi_dkk": round(vaerdi, 2), "kontant_dkk": round(cash, 2),
            "indbetalt_dkk": round(indbetalt, 2), "haevet_dkk": round(haevet, 2),
            "afkast_dkk": round(vaerdi - indbetalt + haevet, 2),
            "urealiseret_dkk": round(sum(p.get("afkast_dkk", 0) for p in pos), 2),
            "realiseret_dkk": round(sum(p["realiseret"] for p in h.values()), 2),
            "udbytte_dkk": round(sum(t["beloeb_dkk"] or 0 for t in tx_k if t["type"] in ("UDBYTTE", "UDBYTTESKAT")), 2),
            "i_dag_dkk": round(vaerdi - prev_val, 2) if prev_val is not None else None,
            "i_dag_pct": (vaerdi / prev_val - 1) if prev_val else None,
            "positioner": sorted(pos, key=lambda p: -p.get("vaerdi_dkk", 0)),
            "historik": hist, "intraday": intraday, "papir_historik": papir_hist,
        }
    out["sidste_transaktion"] = max((t["dato"] for t in tx), default=None)
    return out


# ---------------------------------------------------------------- GitHub-publicering

def publish(cfg):
    gh = cfg["github"]
    repo = os.path.expanduser(gh.get("repo_mappe") or BASE)
    if not gh.get("aktiv") or not os.path.isdir(os.path.join(repo, ".git")):
        raise RuntimeError("GitHub er ikke sat op (se config.json og README)")
    tx = read_exports(cfg)
    isins = set(held_isins(tx))
    with db() as con:
        P = Prices(con)
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kurser, historik = [], []
    for isin in sorted(set(P.q) | isins):
        closes = P.daily_closes(isin)
        for d, (ts, k, v) in sorted(closes.items()):
            historik.append({"dato": d, "isin": isin, "kurs": k, "valuta": v, "dkk_pr_enhed": P.dkk(v, d)})
        if isin in isins and closes:
            d = max(closes)
            ts, k, v = closes[d]
            m = P.meta.get(isin, {})
            kurser.append({"isin": isin, "papir": m.get("navn") or isin, "ticker": m.get("ticker"), "kurs": k, "valuta": v,
                           "dkk_pr_enhed": P.dkk(v, d), "kursdato": d, "kilde": "Nordnet (via Raspberry Pi)", "hentet": now_utc})
    historik.sort(key=lambda r: (r["dato"], r["isin"]))
    os.makedirs(os.path.join(repo, "kurser"), exist_ok=True)
    for name, obj in (("kurser.json", kurser), ("kurshistorik.json", historik)):
        with open(os.path.join(repo, "kurser", name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=120)
    git("pull", "--rebase", "--autostash")
    git("add", "kurser/kurser.json", "kurser/kurshistorik.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        log.info("Ingen ændringer at publicere")
    else:
        r = git("commit", "-m", "Kurser " + dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))
        if r.returncode:
            raise RuntimeError("git commit fejlede: " + r.stderr.strip())
        r = git("push")
        if r.returncode:
            raise RuntimeError("git push fejlede: " + r.stderr.strip())
        log.info("Publicerede %d kurser og %d historikrækker til GitHub", len(kurser), len(historik))
    with LOCK:
        STATUS["sidst_publiceret"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
        STATUS["publicer_fejl"] = None


# ---------------------------------------------------------------- baggrundsjob

def scheduler(cfg):
    last_fx_day = None
    last_pub_day = None
    while True:
        now = dt.datetime.now(TZ)
        try:
            if now.weekday() < 5 and cfg["hent_fra_kl"] <= now.hour < cfg["hent_til_kl"]:
                fetch_quotes(cfg)
            day = now.strftime("%Y-%m-%d")
            if last_fx_day != day and (now.hour >= 17 or last_fx_day is None):
                fetch_fx()
                last_fx_day = day
        except Exception as e:
            log.exception("Fejl under hentning")
            with LOCK:
                STATUS["fejl"] = str(e)
        hh, mm = (int(x) for x in cfg["github"]["publicer_kl"].split(":"))
        if cfg["github"].get("aktiv") and last_pub_day != now.strftime("%Y-%m-%d") and (now.hour, now.minute) >= (hh, mm):
            try:
                publish(cfg)
            except Exception as e:
                log.error("Publicering fejlede: %s", e)
                with LOCK:
                    STATUS["publicer_fejl"] = str(e)
            last_pub_day = now.strftime("%Y-%m-%d")
        time.sleep(max(60, cfg["hent_hvert_minut"] * 60))


# ---------------------------------------------------------------- webserver

class Handler(BaseHTTPRequestHandler):
    cfg = None

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE, "dashboard.html"), encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/data":
            try:
                return self._send(200, json.dumps(build_data(self.cfg), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                log.exception("Kunne ikke bygge data")
                return self._send(500, json.dumps({"fejl": str(e)}), "application/json")
        self._send(404, "Ikke fundet", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            n = int(self.headers.get("Content-Length") or 0)
            name = os.path.basename(urllib.parse.unquote(self.headers.get("X-Filename") or "eksport.csv"))
            name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
            if not name.lower().endswith(".csv") or n <= 0 or n > 5_000_000:
                return self._send(400, json.dumps({"fejl": "Vælg en CSV-fil fra Nordnet (højst 5 MB)."}), "application/json")
            body = self.rfile.read(n)
            stamp = dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
            with open(os.path.join(EKSPORT, f"{stamp}-{name}"), "wb") as f:
                f.write(body)
            n_tx = len(read_exports(self.cfg))
            threading.Thread(target=fetch_quotes, args=(self.cfg,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True, "transaktioner": n_tx}), "application/json")
        if path == "/api/hent":
            threading.Thread(target=fetch_quotes, args=(self.cfg,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}), "application/json")
        self._send(404, "Ikke fundet", "text/plain; charset=utf-8")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "hent":
        fetch_quotes(cfg)
        fetch_fx()
        return
    if cmd == "publicer":
        publish(cfg)
        return
    threading.Thread(target=scheduler, args=(cfg,), daemon=True).start()
    Handler.cfg = cfg
    srv = ThreadingHTTPServer(("0.0.0.0", int(cfg["port"])), Handler)
    log.info("Dashboard kører på http://<pi-adresse>:%s", cfg["port"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
