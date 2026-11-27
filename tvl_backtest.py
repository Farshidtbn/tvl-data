# tvl_backtest.py
# Backtest of two TVL based signals over all coins on DefiLlama.
# Signal A: TVL change over N days >= threshold  -> long
# Signal B: price/TVL below k * its own recent geometric mean -> long
# Stdlib only. Works with pypy3. No f-strings.
#
# Two modes:
#   python tvl_backtest.py download   -> only downloads data into tvl_cache.jsonl
#                                        (this is what GitHub runs; resumes from the cache)
#   pypy3 tvl_backtest.py             -> backtest only, reads tvl_cache.jsonl, no internet
# On the phone put tvl_cache.jsonl in /storage/emulated/0/Download/

import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

# ---------------- CONFIG ----------------
OUT_DIR = "/storage/emulated/0/Download"
MAX_COINS = 0            # 0 = all coins; a number = only the biggest by TVL
MIN_COMMON_DAYS = 200    # coin needs at least this many days with both price and TVL
FEE = 0.001              # per side (0.1%), so 0.2% round trip
MIN_TRADES_PER_COIN = 10 # coin counts in "coins" and "prof" only with this many trades
SLEEP = 0.12             # pause between requests (seconds)
HOLDS = [7, 14, 30]
A_LOOKBACKS = [7, 14, 30]
A_THRESHOLDS = [0.10, 0.20, 0.30, 0.50]
B_WINDOWS = [60, 90, 180]
B_FACTORS = [0.50, 0.70, 0.85]
TOP_SHOW = 15
MAX_MINUTES = float(os.environ.get("TVL_MAX_MINUTES", "0"))  # 0 = no limit (used by GitHub)
# ----------------------------------------

if not os.path.isdir(OUT_DIR):
    OUT_DIR = "."

SKIP_KEYS = ("staking", "pool2", "borrowed", "treasury", "vesting", "offers")


def get_json(url):
    last = None
    for k in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            time.sleep(2 * (k + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (k + 1))
    raise RuntimeError("request failed: " + url + " " + str(last))


def load_groups():
    prots = get_json("https://api.llama.fi/protocols")
    groups = {}
    for p in prots:
        gid = p.get("gecko_id")
        if not gid:
            continue
        if p.get("category") == "CEX":
            continue
        g = groups.get(gid)
        if g is None:
            g = {"slugs": [], "tvl": 0.0}
            groups[gid] = g
        g["slugs"].append(p["slug"])
        g["tvl"] += float(p.get("tvl") or 0)
    ids = sorted(groups, key=lambda x: -groups[x]["tvl"])
    if MAX_COINS:
        ids = ids[:MAX_COINS]
    return ids, groups


def protocol_tvl_days(slug):
    d = get_json("https://api.llama.fi/protocol/" + slug)
    out = {}
    if not d:
        return out
    arr = d.get("tvl")
    if arr:
        for row in arr:
            out[int(row["date"]) // 86400] = float(row["totalLiquidityUSD"])
        return out
    for k, v in (d.get("chainTvls") or {}).items():
        if "-" in k or k in SKIP_KEYS:
            continue
        for row in v.get("tvl", []):
            day = int(row["date"]) // 86400
            out[day] = out.get(day, 0.0) + float(row["totalLiquidityUSD"])
    return out


def coin_price_days(gid, first_day):
    out = {}
    today = int(time.time()) // 86400
    start = first_day
    while start <= today:
        span = min(365, today - start + 2)
        url = ("https://coins.llama.fi/chart/coingecko:%s?start=%d&span=%d&period=1d"
               % (gid, start * 86400, span))
        d = get_json(url)
        if d:
            c = (d.get("coins") or {}).get("coingecko:" + gid)
            if c:
                for row in c.get("prices", []):
                    out[int(row["timestamp"]) // 86400] = float(row["price"])
        start += 365
        time.sleep(SLEEP)
    return out


def build(tv, pr):
    days = [d for d in tv if d in pr and tv[d] > 0 and pr[d] > 0]
    if len(days) < MIN_COMMON_DAYS:
        return None
    d0 = min(days)
    d1 = max(days)
    n = d1 - d0 + 1
    P = [None] * n
    T = [None] * n
    for d in days:
        P[d - d0] = float("%.8g" % pr[d])
        T[d - d0] = float(int(tv[d]))
    return d0, P, T


def load_cache(path):
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                done[r["id"]] = r
    return done


def download_all(ids, groups, cache_path):
    done = load_cache(cache_path)
    f = open(cache_path, "a")
    failed = 0
    total = len(ids)
    t0 = time.time()
    for k, gid in enumerate(ids):
        if gid in done:
            continue
        if MAX_MINUTES and (time.time() - t0) > MAX_MINUTES * 60:
            print("Time budget reached. Run again to continue.")
            break
        try:
            tv = {}
            for slug in groups[gid]["slugs"]:
                for day, v in protocol_tvl_days(slug).items():
                    tv[day] = tv.get(day, 0.0) + v
                time.sleep(SLEEP)
            rec = {"id": gid, "skip": 1}
            if tv:
                pr = coin_price_days(gid, min(tv))
                b = build(tv, pr)
                if b is not None:
                    rec = {"id": gid, "d0": b[0], "P": b[1], "T": b[2]}
        except RuntimeError as e:
            failed += 1
            print("[%d/%d] %s FAILED (will retry next run)" % (k + 1, total, gid))
            continue
        f.write(json.dumps(rec) + "\n")
        f.flush()
        done[gid] = rec
        if rec.get("skip"):
            print("[%d/%d] %s skipped (not enough data)" % (k + 1, total, gid))
        else:
            print("[%d/%d] %s ok, %d days" % (k + 1, total, gid, len(rec["P"])))
    f.close()
    if failed:
        print("Failed coins: %d. Run again to retry them." % failed)
    return done


def run(sig, P, H):
    n = len(P)
    i = 0
    cnt = 0
    s = 0.0
    w = 0
    lim = n - 1 - H
    while i < lim:
        if sig[i]:
            pe = P[i + 1]
            px = P[i + 1 + H]
            if pe is not None and px is not None:
                r = px * (1 - FEE) / (pe * (1 + FEE)) - 1
                cnt += 1
                s += r
                if r > 0:
                    w += 1
                i += 1 + H
                continue
        i += 1
    return cnt, s, w


def roll_mean(R, W):
    n = len(R)
    S = [0.0] * (n + 1)
    C = [0] * (n + 1)
    for i in range(n):
        if R[i] is not None:
            S[i + 1] = S[i] + R[i]
            C[i + 1] = C[i] + 1
        else:
            S[i + 1] = S[i]
            C[i + 1] = C[i]
    M = [None] * n
    need = int(W * 0.8)
    for i in range(W, n):
        c = C[i] - C[i - W]
        if c >= need:
            M[i] = (S[i] - S[i - W]) / c
    return M


def add(res, key, r):
    cnt, s, w = r
    e = res.get(key)
    if e is None:
        e = [0, 0.0, 0, 0, 0]
        res[key] = e
    e[0] += cnt
    e[1] += s
    e[2] += w
    if cnt >= MIN_TRADES_PER_COIN:
        e[3] += 1
        if s > 0:
            e[4] += 1


def backtest(recs):
    res = {}
    base = {}
    for H in HOLDS:
        base[H] = [0, 0.0]
    ncoins = 0
    for rec in recs:
        if rec.get("skip"):
            continue
        P = rec["P"]
        T = rec["T"]
        n = len(P)
        ncoins += 1
        for H in HOLDS:
            for j in range(n - H):
                a = P[j]
                b = P[j + H]
                if a is not None and b is not None:
                    base[H][0] += 1
                    base[H][1] += b * (1 - FEE) / (a * (1 + FEE)) - 1
        # signal A
        for N in A_LOOKBACKS:
            chg = [None] * n
            for i in range(N, n):
                a = T[i]
                b = T[i - N]
                if a is not None and b is not None:
                    chg[i] = a / b - 1
            for thr in A_THRESHOLDS:
                sig = [(c is not None and c >= thr) for c in chg]
                for H in HOLDS:
                    add(res, ("A", N, thr, H), run(sig, P, H))
        # signal B
        R = [None] * n
        for i in range(n):
            if P[i] is not None and T[i] is not None:
                R[i] = math.log(P[i] / T[i])
        for W in B_WINDOWS:
            M = roll_mean(R, W)
            for kf in B_FACTORS:
                lk = math.log(kf)
                sig = [(R[i] is not None and M[i] is not None and R[i] <= M[i] + lk)
                       for i in range(n)]
                for H in HOLDS:
                    add(res, ("B", W, kf, H), run(sig, P, H))
    return res, base, ncoins


def label(key):
    if key[0] == "A":
        return "TVLchg N=%d thr=%d%% hold=%d" % (key[1], int(round(key[2] * 100)), key[3])
    return "P/TVL W=%d k=%.2f hold=%d" % (key[1], key[2], key[3])


def report(res, base, ncoins):
    bavg = {}
    for H in HOLDS:
        c, s = base[H]
        bavg[H] = (s / c) if c else 0.0
    rows = []
    for key, e in res.items():
        if e[0] == 0:
            continue
        avg = e[1] / e[0]
        edge = avg - bavg[key[3]]
        rows.append((key, e[0], e[3], e[4], 100.0 * e[2] / e[0],
                     100 * avg, 100 * bavg[key[3]], 100 * edge))
    print("")
    print("Coins used: %d | fee per side: %.2f%% | min trades per coin: %d"
          % (ncoins, FEE * 100, MIN_TRADES_PER_COIN))
    print("avg% = mean return per trade; base% = mean return of ANY entry day with same hold;")
    print("edge% = avg% - base%; coins = coins with enough trades; prof = of them, coins with positive mean")
    for sg, title in (("A", "SIGNAL A: TVL change"), ("B", "SIGNAL B: price/TVL (market cap to TVL proxy)")):
        sub = [r for r in rows if r[0][0] == sg]
        sub.sort(key=lambda r: -r[7])
        print("")
        print("=== " + title + " (sorted by edge) ===")
        print("%-32s %7s %5s %5s %6s %8s %8s %8s" %
              ("setting", "n", "coins", "prof", "win%", "avg%", "base%", "edge%"))
        for r in sub[:TOP_SHOW]:
            print("%-32s %7d %5d %5d %6.1f %8.2f %8.2f %8.2f" %
                  (label(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
    path = os.path.join(OUT_DIR, "tvl_results.csv")
    with open(path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["setting", "n", "coins", "prof", "win_pct", "avg_pct", "base_pct", "edge_pct"])
        for r in sorted(rows, key=lambda r: -r[7]):
            wr.writerow([label(r[0]), r[1], r[2], r[3], "%.2f" % r[4],
                         "%.3f" % r[5], "%.3f" % r[6], "%.3f" % r[7]])
    print("")
    print("All settings saved to: " + path)


def main():
    cache_path = os.path.join(OUT_DIR, "tvl_cache.jsonl")
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if mode == "download":
        ids, groups = load_groups()
        print("Coins to process: %d" % len(ids))
        download_all(ids, groups, cache_path)
        print("Download step finished. File: " + cache_path)
        return
    if not os.path.exists(cache_path):
        print("Data file not found: " + cache_path)
        print("Download it first (GitHub) and put it in that folder.")
        return
    done = load_cache(cache_path)
    recs = list(done.values())
    res, base, ncoins = backtest(recs)
    report(res, base, ncoins)


if __name__ == "__main__":
    main()
