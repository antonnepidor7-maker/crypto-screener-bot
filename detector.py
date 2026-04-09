"""
Pattern detector: finds repeating market buys of the same volume.
"""
import time
import asyncio
from collections import defaultdict

from config import (
    MIN_REPEATS, MAX_WINDOW, TOLERANCE_REL, TOLERANCE_ABS,
    INTERVAL_DRIFT, TRADE_TTL, ALERT_COOLDOWN, MIN_PRINT_USD,
)

# Trade tuple layout: (qty, ts, price)
_QTY, _TS, _PRICE = 0, 1, 2


class Detector:
    def __init__(self, on_alert=None):
        self._trades: dict[str, list[tuple]] = defaultdict(list)
        self._last_alert: dict[str, float] = {}
        self._seen_clusters: dict[str, set] = defaultdict(set)
        self._on_alert = on_alert
        self._trade_count = 0
        self._alert_count = 0

    def on_trade(self, trade: dict):
        sym = trade["symbol"]
        self._trade_count += 1

        # Store as tuple — no dict overhead
        self._trades[sym].append((
            trade["qty"],
            trade["time"] / 1000.0,
            trade["price"],
        ))

        # Cooldown check — avoid time.time() if no prior alert
        last = self._last_alert.get(sym)
        if last is not None:
            if time.time() - last < ALERT_COOLDOWN:
                return

        self._detect(sym)

    def cleanup(self):
        now = time.time()
        cutoff = now - TRADE_TTL

        for sym in list(self._trades.keys()):
            trades = self._trades[sym]
            # Fast path: if all trades are fresh, skip rebuild
            if trades and trades[0][_TS] > cutoff:
                # Maybe only tail needs trimming — check last
                if trades[-1][_TS] > cutoff:
                    continue
            # Rebuild keeping only fresh trades
            fresh = [t for t in trades if t[_TS] > cutoff]
            if fresh:
                self._trades[sym] = fresh
            else:
                del self._trades[sym]

        for sym in list(self._seen_clusters.keys()):
            if sym not in self._last_alert or now - self._last_alert[sym] > 300:
                self._seen_clusters[sym].clear()

    def get_stats(self) -> str:
        return f"Trades: {self._trade_count} | Alerts: {self._alert_count}"

    def _detect(self, sym: str):
        trades = self._trades.get(sym)
        if not trades or len(trades) < MIN_REPEATS:
            return

        sorted_trades = sorted(trades, key=lambda t: t[_TS])
        clusters = self._cluster_by_qty(sorted_trades)

        for cluster in clusters:
            if len(cluster) < MIN_REPEATS:
                continue

            result = self._check_intervals(cluster)
            if result is None:
                continue

            count, avg_qty, avg_interval, first_price = result
            avg_usd = avg_qty * first_price

            if avg_usd < MIN_PRINT_USD:
                continue

            if avg_usd < 500:
                strength = "🟢 СЛАБЫЙ"
            elif avg_usd <= 1500:
                strength = "🟡 СРЕДНИЙ"
            else:
                strength = "🔴 СИЛЬНЫЙ"

            cluster_key = round(avg_qty, 4)

            if cluster_key in self._seen_clusters[sym]:
                continue

            self._seen_clusters[sym].add(cluster_key)
            self._last_alert[sym] = time.time()
            self._alert_count += 1
            self._fire_alert(sym, count, avg_qty, avg_interval, first_price, strength, avg_usd)
            return

    def _fire_alert(self, sym: str, count: int, avg_qty: float,
                    avg_interval: float, first_price: float,
                    strength: str, avg_usd: float):
        ts = time.strftime("%H:%M:%S")
        qty_str = _format_qty(avg_qty)
        interval_str = f"{avg_interval:.2f}s"
        usd_str = _format_usd(avg_usd)

        print(
            f"\n{'='*50}\n"
            f"🔴 ALGO DETECTED  [{ts}]  {strength}\n"
            f"   Symbol:    {sym}\n"
            f"   Price:     {first_price}\n"
            f"   Prints:    {count}\n"
            f"   Avg Size:  {qty_str}\n"
            f"   Avg $/pr:  {usd_str}\n"
            f"   Interval:  {interval_str}\n"
            f"{'='*50}",
            flush=True
        )

        if self._on_alert:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(
                        self._on_alert(sym, count, avg_qty, avg_interval,
                                       first_price, strength, avg_usd)
                    )
            except Exception as e:
                print(f"[TG] Alert error: {e}", flush=True)

    def _cluster_by_qty(self, trades: list[tuple]) -> list[list[tuple]]:
        """
        Cluster trades by similar quantity using sort + forward scan.
        O(n log n + n*k) where k = avg cluster size, vs old O(n²).

        Each unassigned trade seeds a cluster — forward scan pulls all
        subsequent trades within tolerance. Break on first miss (sorted).
        """
        indexed = sorted(enumerate(trades), key=lambda x: x[1][_QTY])
        n = len(indexed)
        assigned = set()
        clusters = []

        for i in range(n):
            idx_i, t_i = indexed[i]
            if idx_i in assigned:
                continue

            cluster = [t_i]
            assigned.add(idx_i)
            seed_qty = t_i[_QTY]

            for j in range(i + 1, n):
                idx_j, t_j = indexed[j]
                if idx_j in assigned:
                    continue
                if not _qty_match(seed_qty, t_j[_QTY]):
                    break  # Sorted — rest will be even further
                cluster.append(t_j)
                assigned.add(idx_j)

            if len(cluster) >= MIN_REPEATS:
                clusters.append(cluster)

        clusters.sort(key=len, reverse=True)
        return clusters

    def _check_intervals(self, cluster: list[tuple]):
        n = len(cluster)
        if n < MIN_REPEATS:
            return None

        # Compute span first — avoid full sort if span is too large
        min_ts = cluster[0][_TS]
        max_ts = cluster[0][_TS]
        for t in cluster[1:]:
            if t[_TS] < min_ts:
                min_ts = t[_TS]
            elif t[_TS] > max_ts:
                max_ts = t[_TS]

        span = max_ts - min_ts
        if span <= 0 or span > MAX_WINDOW:
            return None

        # Now sort for interval analysis
        sorted_c = sorted(cluster, key=lambda t: t[_TS])

        intervals = [
            sorted_c[i + 1][_TS] - sorted_c[i][_TS]
            for i in range(n - 1)
        ]

        # Median interval
        sorted_iv = sorted(intervals)
        median_iv = sorted_iv[len(sorted_iv) // 2]
        if median_iv <= 0:
            return None

        # Check drift — use precomputed threshold to avoid division per iv
        max_drift = median_iv * INTERVAL_DRIFT
        for iv in intervals:
            if abs(iv - median_iv) > max_drift:
                return None

        first_price = sorted_c[0][_PRICE]
        avg_qty = sum(t[_QTY] for t in sorted_c) / n
        avg_iv = sum(intervals) / (n - 1)

        return (n, avg_qty, avg_iv, first_price)


def _qty_match(q1: float, q2: float) -> bool:
    abs_diff = abs(q1 - q2)
    if TOLERANCE_ABS > 0 and abs_diff < TOLERANCE_ABS:
        return True
    max_q = q1 if q1 > q2 else q2
    if max_q == 0:
        return q1 == q2
    return (abs_diff / max_q) <= TOLERANCE_REL


def _format_qty(qty: float) -> str:
    if qty >= 1_000_000:
        return f"{qty / 1_000_000:.2f}M"
    elif qty >= 1_000:
        return f"{qty / 1_000:.2f}K"
    elif qty >= 1:
        return f"{qty:.2f}"
    elif qty >= 0.01:
        return f"{qty:.4f}"
    else:
        return f"{qty:.8f}"


def _format_usd(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.2f}M"
    elif usd >= 1_000:
        return f"${usd / 1_000:.1f}K"
    else:
        return f"${usd:.0f}"
