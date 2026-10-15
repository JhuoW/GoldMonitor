"""Public gold/FX feeds for the desktop widget (Python standard library only).

Gold: Swissquote's public XAU/USD bid/ask feed, sampled once per second.
The displayed value is its AT/standard midpoint, not an executed trade price.
Optional Gold API source: https://gold-api.com/docs and /llms.txt; its public
XAU endpoint defaults to USD and requests a 30-second client cache.
FX: https://www.exchangerate-api.com/docs/free
The open FX endpoint updates once per day. Its required attribution is
``Rates By Exchange Rate API`` linking to https://www.exchangerate-api.com.

Prices are indicative provider quotes. CNY/gram is an international spot-price
conversion, not a domestic exchange quote or a jewellery-store sale price.
"""

from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GOLD_URL = "https://api.gold-api.com/price/XAU"
GOLD_SOURCE_NAME = "Gold API"
GOLD_SOURCE_URL = "https://gold-api.com"
SWISSQUOTE_GOLD_URL = (
    "https://forex-data-feed.swissquote.com/public-quotes/"
    "bboquotes/instrument/XAU/USD"
)
SWISSQUOTE_SOURCE_NAME = "Swissquote"
SWISSQUOTE_SOURCE_URL = "https://www.swissquote.com/en-ch/private/trade/products/gold"
FX_URL = "https://open.er-api.com/v6/latest/USD"
FX_SOURCE_NAME = "ExchangeRate-API"
FX_SOURCE_URL = "https://www.exchangerate-api.com"
TROY_OUNCE_GRAMS = Decimal("31.1034768")
GOLD_REFRESH_SECONDS = 1
GOLD_API_REFRESH_SECONDS = 30
GOLD_STALE_SECONDS = 180
SWISSQUOTE_STALE_SECONDS = 20
FX_STALE_SECONDS = 48 * 60 * 60
FX_REFRESH_MIN_SECONDS = 60 * 60
FX_RETRY_SECONDS = 60
RATE_LIMIT_RETRY_SECONDS = 20 * 60
MAX_FUTURE_SECONDS = 5 * 60
MAX_RESPONSE_BYTES = 128 * 1024


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PriceDataError(Exception):
    """A network or data error that can safely be displayed in the widget."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class GoldQuote:
    usd_per_oz: Decimal
    quoted_at: datetime
    fetched_at: datetime
    source_name: str = GOLD_SOURCE_NAME
    source_url: str = GOLD_SOURCE_URL
    bid: Decimal | None = None
    ask: Decimal | None = None

    def is_stale(self, now: datetime | None = None) -> bool:
        age = ((now or utc_now()) - self.quoted_at).total_seconds()
        limit = SWISSQUOTE_STALE_SECONDS if self.source_name == SWISSQUOTE_SOURCE_NAME else GOLD_STALE_SECONDS
        return age > limit or age < -MAX_FUTURE_SECONDS


@dataclass(frozen=True)
class FxQuote:
    cny_per_usd: Decimal
    updated_at: datetime
    next_update_at: datetime
    fetched_at: datetime

    def is_stale(self, now: datetime | None = None) -> bool:
        age = ((now or utc_now()) - self.updated_at).total_seconds()
        return age > FX_STALE_SECONDS or age < -MAX_FUTURE_SECONDS


@dataclass(frozen=True)
class PriceSnapshot:
    gold: GoldQuote | None
    fx: FxQuote | None
    fetched_at: datetime
    errors: tuple[str, ...] = ()

    @property
    def usd_per_oz(self) -> Decimal | None:
        return self.gold.usd_per_oz if self.gold else None

    @property
    def cny_per_gram(self) -> Decimal | None:
        if self.gold is None or self.fx is None:
            return None
        return self.gold.usd_per_oz * self.fx.cny_per_usd / TROY_OUNCE_GRAMS

    def is_gold_stale(self, now: datetime | None = None) -> bool:
        return self.gold is None or self.gold.is_stale(now)

    def is_fx_stale(self, now: datetime | None = None) -> bool:
        return self.fx is None or self.fx.is_stale(now)

    @property
    def gold_stale(self) -> bool:
        """Freshness at snapshot creation; use is_gold_stale for a live clock."""
        return self.is_gold_stale(self.fetched_at)

    @property
    def fx_stale(self) -> bool:
        return self.is_fx_stale(self.fetched_at)


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidOperation
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result > Decimal("1e12"):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PriceDataError(f"数据格式错误（{field}）") from exc


def _iso_datetime(value: Any, field: str, now: datetime) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        stamp = stamp.astimezone(timezone.utc)
        if stamp > now + timedelta(seconds=MAX_FUTURE_SECONDS):
            raise ValueError
        return stamp
    except (ValueError, OverflowError) as exc:
        raise PriceDataError(f"数据时间无效（{field}）") from exc


def _unix_datetime(value: Any, field: str) -> datetime:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise PriceDataError(f"数据时间无效（{field}）") from exc


def parse_gold(data: Mapping[str, Any], now: datetime) -> GoldQuote:
    if not isinstance(data, Mapping) or data.get("symbol") != "XAU":
        raise PriceDataError("金价接口返回了错误的品种")
    if data.get("currency", "USD") != "USD":
        raise PriceDataError("金价接口返回了错误的币种")
    return GoldQuote(
        usd_per_oz=_positive_decimal(data.get("price"), "price"),
        quoted_at=_iso_datetime(data.get("updatedAt"), "updatedAt", now),
        fetched_at=now,
    )


def parse_swissquote_gold(data: Any, now: datetime) -> GoldQuote:
    """Read the same AT/standard book each time, irrespective of array order.

    The endpoint itself fixes the instrument to XAU/USD. The upstream top-level
    timestamp is in Unix milliseconds; fetching does not make an old quote new.
    Never substitute another book, a futures contract, or a tokenized asset.
    """
    if not isinstance(data, list):
        raise PriceDataError("Swissquote 金价接口返回格式错误")
    for book in data:
        if not isinstance(book, Mapping):
            continue
        topo = book.get("topo")
        if not isinstance(topo, Mapping) or topo.get("platform") != "AT" or topo.get("server") != "AT":
            continue
        timestamp = book.get("ts")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, int)
                or timestamp <= 0 or timestamp > 253402300799999):
            raise PriceDataError("数据时间无效（ts）")
        quoted_at = _unix_datetime(timestamp / 1000, "ts")
        if quoted_at > now + timedelta(seconds=MAX_FUTURE_SECONDS):
            raise PriceDataError("金价更新时间在未来，请检查系统时间")
        spreads = book.get("spreadProfilePrices")
        if not isinstance(spreads, list):
            raise PriceDataError("Swissquote 买卖报价缺失")
        for spread in spreads:
            if not isinstance(spread, Mapping) or str(spread.get("spreadProfile", "")).lower() != "standard":
                continue
            bid = _positive_decimal(spread.get("bid"), "bid")
            ask = _positive_decimal(spread.get("ask"), "ask")
            if ask < bid:
                raise PriceDataError("Swissquote 买卖报价倒置")
            return GoldQuote(
                usd_per_oz=(bid + ask) / 2,
                quoted_at=quoted_at,
                fetched_at=now,
                source_name=SWISSQUOTE_SOURCE_NAME,
                source_url=SWISSQUOTE_SOURCE_URL,
                bid=bid,
                ask=ask,
            )
        raise PriceDataError("Swissquote standard 报价缺失")
    raise PriceDataError("Swissquote AT 报价缺失")


def parse_fx(data: Mapping[str, Any], now: datetime) -> FxQuote:
    if not isinstance(data, Mapping) or data.get("result") != "success":
        raise PriceDataError("汇率接口暂不可用")
    if data.get("base_code") != "USD" or not isinstance(data.get("rates"), Mapping):
        raise PriceDataError("汇率接口返回了错误的基准币种")
    rate = _positive_decimal(data["rates"].get("CNY"), "CNY")
    updated_at = _unix_datetime(data.get("time_last_update_unix"), "time_last_update_unix")
    next_update_at = _unix_datetime(data.get("time_next_update_unix"), "time_next_update_unix")
    if updated_at > now + timedelta(seconds=MAX_FUTURE_SECONDS):
        raise PriceDataError("汇率更新时间在未来，请检查系统时间")
    if next_update_at <= updated_at or next_update_at - updated_at > timedelta(days=7):
        raise PriceDataError("汇率下次更新时间无效")
    return FxQuote(rate, updated_at, next_update_at, now)


def _retry_after_seconds(value: str | None, now: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        if value.strip().isdigit():
            return max(1, int(value.strip()))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(1, math.ceil((stamp - now).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


def _request_json(url: str, timeout: float) -> Any:
    request = Request(url, headers={
        "User-Agent": "Gold-Monitor/2.0 (Windows desktop widget)",
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PriceDataError("接口响应过大")
        result = json.loads(raw.decode("utf-8"), parse_float=Decimal)
        if not isinstance(result, (dict, list)):
            raise PriceDataError("接口响应格式错误")
        return result
    except HTTPError as exc:
        if exc.code == 429:
            retry = _retry_after_seconds(exc.headers.get("Retry-After"), utc_now())
            if url == FX_URL:
                # The open FX service documents a 20-minute cooldown.
                retry = max(retry or 0, RATE_LIMIT_RETRY_SECONDS)
            elif retry is None:
                retry = 60 if url == SWISSQUOTE_GOLD_URL else RATE_LIMIT_RETRY_SECONDS
            raise PriceDataError(
                f"接口请求过于频繁，{retry} 秒后自动重试",
                retry_after_seconds=retry,
            ) from exc
        raise PriceDataError(
            f"接口返回 HTTP {exc.code}",
            retry_after_seconds=_retry_after_seconds(exc.headers.get("Retry-After"), utc_now()),
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise PriceDataError("网络连接超时") from exc
    except (URLError, OSError) as exc:
        raise PriceDataError("无法连接数据源，请检查网络") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise PriceDataError("接口返回的数据无法解析") from exc


class PriceClient:
    """Fetch on one background worker, never the Tk event thread.

    Calls are synchronous (up to two request timeouts on a cold refresh).
    Successful values are retained on errors, with their original timestamps.
    FX is cached independently until its next daily update, at least one hour.
    Gold failures back off from two seconds to one minute. FX failures retry
    after one minute. Server Retry-After is honored; FX throttles wait 20 min.
    No invented/demo quote or fixed FX fallback is used.
    """

    def __init__(
        self,
        timeout: float = 8,
        *,
        source: str = "swissquote",
        fetch_json: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if source not in ("swissquote", "goldapi"):
            raise ValueError("source must be swissquote or goldapi")
        self.source = source
        self.minimum_interval_seconds = GOLD_REFRESH_SECONDS if source == "swissquote" else GOLD_API_REFRESH_SECONDS
        self.source_name = SWISSQUOTE_SOURCE_NAME if source == "swissquote" else GOLD_SOURCE_NAME
        self.source_url = SWISSQUOTE_SOURCE_URL if source == "swissquote" else GOLD_SOURCE_URL
        self._gold_url = SWISSQUOTE_GOLD_URL if source == "swissquote" else GOLD_URL
        self._parse_gold = parse_swissquote_gold if source == "swissquote" else parse_gold
        self._fetch_json = fetch_json or (lambda url: _request_json(url, timeout))
        self._clock = clock
        self._gold: GoldQuote | None = None
        self._fx: FxQuote | None = None
        self._gold_error: str | None = None
        self._fx_error: str | None = None
        self._gold_retry_at: datetime | None = None
        self._fx_retry_at: datetime | None = None
        self._gold_failure_count = 0

    def refresh(self) -> PriceSnapshot:
        now = self._clock()
        if self._gold_retry_at is None or now >= self._gold_retry_at:
            self._gold_retry_at = now + timedelta(seconds=self.minimum_interval_seconds)
            try:
                quote = self._parse_gold(self._fetch_json(self._gold_url), self._clock())
                if self._gold is not None and quote.quoted_at < self._gold.quoted_at:
                    raise PriceDataError("数据源返回了较早的报价，保留上一条报价")
                self._gold = quote
                self._gold_error = None
                self._gold_failure_count = 0
            except (PriceDataError, OSError) as exc:
                self._gold_error = f"金价：{exc}"
                self._gold_failure_count = min(self._gold_failure_count + 1, 6)
                retry_seconds = max(self.minimum_interval_seconds, min(60, 2 ** self._gold_failure_count))
                if isinstance(exc, PriceDataError) and exc.retry_after_seconds is not None:
                    retry_seconds = max(retry_seconds, exc.retry_after_seconds)
                self._gold_retry_at = self._clock() + timedelta(seconds=retry_seconds)

        now = self._clock()
        if self._fx_retry_at is None or now >= self._fx_retry_at:
            self._fx_retry_at = now + timedelta(seconds=FX_RETRY_SECONDS)
            try:
                self._fx = parse_fx(self._fetch_json(FX_URL), self._clock())
                self._fx_retry_at = max(
                    self._fx.next_update_at,
                    now + timedelta(seconds=FX_REFRESH_MIN_SECONDS),
                )
                self._fx_error = None
            except (PriceDataError, OSError) as exc:
                self._fx_error = f"汇率：{exc}"
                retry_seconds = FX_RETRY_SECONDS
                if isinstance(exc, PriceDataError) and exc.retry_after_seconds is not None:
                    retry_seconds = max(retry_seconds, exc.retry_after_seconds)
                self._fx_retry_at = self._clock() + timedelta(seconds=retry_seconds)

        return PriceSnapshot(
            gold=self._gold,
            fx=self._fx,
            fetched_at=self._clock(),
            errors=tuple(error for error in (self._gold_error, self._fx_error) if error),
        )
