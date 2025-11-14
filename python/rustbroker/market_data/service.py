from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

try:  # Optional dependency, lazily imported in XtDataProvider
    from xtquant import xtdata  # type: ignore
except Exception:  # pragma: no cover - only used for type hints
    xtdata = None  # type: ignore[assignment]

try:
    from engine_rust import get_market_data as get_market_data_rust  # type: ignore
    from engine_rust import save_klines as save_klines_rust  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "engine_rust extension is not built. Run 'maturin develop' under rust/engine_rust'."
    ) from exc


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


def _normalize_time_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_time_input(value: Optional[str]) -> Optional[str]:
    """
    Normalize incoming time strings to ISO format for DuckDB queries.
    Accepts '', None, 'YYYYMMDD', 'YYYYMMDDHHMMSS', or already formatted strings.
    """
    value = _normalize_time_str(value)
    if not value:
        return None

    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 14:
        return (
            f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} "
            f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        )
    return value


def _to_xt_time_str(value: Optional[str]) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits


@dataclass(frozen=True)
class DataRequest:
    """
    Encapsulates a market data request.
    """

    symbols: Sequence[str]
    period: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    count: int = -1
    fields: Optional[Sequence[str]] = None
    dividend_type: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(
            self, "start_time", _normalize_time_input(self.start_time)
        )
        object.__setattr__(self, "end_time", _normalize_time_input(self.end_time))
        if self.count == 0:
            raise ValueError("count must be -1 (all) or > 0; got 0")

    def slice(
        self,
        *,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        count: Optional[int] = None,
    ) -> "DataRequest":
        """
        Return a copy of the request with overridden range parameters.
        """
        return replace(
            self,
            start_time=_normalize_time_input(start_time) or self.start_time,
            end_time=_normalize_time_input(end_time) or self.end_time,
            count=self.count if count is None else count,
        )


@dataclass(frozen=True)
class MissingRange:
    """
    Represents a missing range for a particular request.
    """

    start_time: Optional[str]
    end_time: Optional[str]
    count: int = -1

    def as_request_kwargs(self) -> Dict[str, Optional[str | int]]:
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "count": self.count,
        }


@dataclass
class MarketDataConfig:
    """
    Runtime configuration for the market data service.
    """

    db_path: str
    xtdata_enabled: bool = True
    download_batch_size: int = 50
    download_retry: int = 3
    download_retry_backoff: float = 1.0
    xtdata_data_dir: Optional[str] = None
    allow_incremental: bool = True
    price_precision: int = 6
    volume_precision: int = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


PERIOD_OFFSETS = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "1d": pd.Timedelta(days=1),
}


def _period_offset(period: str) -> Optional[pd.Timedelta]:
    return PERIOD_OFFSETS.get(period.lower())


def _format_for_xtdata(ts: Optional[pd.Timestamp]) -> Optional[str]:
    if ts is None:
        return None
    return ts.strftime("%Y%m%d%H%M%S")


def _format_for_db(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _bars_to_dataframe(bars: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    if "datetime" not in df.columns:
        raise ValueError("Bars must include 'datetime' field")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df = df.set_index("datetime").sort_index()
    return df


# ---------------------------------------------------------------------------
# Providers and services
# ---------------------------------------------------------------------------


class DBDataProvider:
    """
    Read market data from DuckDB via Rust extension.
    """

    def __init__(
        self,
        db_path: str,
        *,
        fetch_fn: Callable[..., List[Mapping[str, object]]] = get_market_data_rust,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._db_path = db_path
        self._fetch_fn = fetch_fn
        self._logger = logger or logging.getLogger(__name__)

    def fetch(
        self, request: DataRequest
    ) -> Tuple[Dict[str, pd.DataFrame], List[MissingRange]]:
        data: Dict[str, pd.DataFrame] = {}
        missing: List[MissingRange] = []

        for symbol in request.symbols:
            bars: List[Mapping[str, object]] = self._fetch_fn(
                self._db_path,
                symbol,
                request.period,
                request.start_time,
                request.end_time,
                request.count,
            )
            df = _bars_to_dataframe(bars)
            data[symbol] = df
            missing.extend(
                self._detect_missing_ranges(request, df, symbol=symbol),
            )
        return data, missing

    def _detect_missing_ranges(
        self,
        request: DataRequest,
        df: pd.DataFrame,
        *,
        symbol: str,
    ) -> List[MissingRange]:
        start_ts = pd.to_datetime(request.start_time) if request.start_time else None
        end_ts = pd.to_datetime(request.end_time) if request.end_time else None
        freq = _period_offset(request.period)

        if df.empty:
            if request.count < 0 and not request.start_time and not request.end_time:
                self._logger.debug(
                    "No data for %s/%s; treating as missing (count=%s)",
                    symbol,
                    request.period,
                    request.count,
                )
            return [
                MissingRange(
                    start_time=_format_for_xtdata(start_ts),
                    end_time=_format_for_xtdata(end_ts),
                    count=request.count,
                )
            ]

        first_ts = df.index.min()
        last_ts = df.index.max()
        missing: List[MissingRange] = []

        if start_ts is not None and first_ts > start_ts:
            gap_end = first_ts - (freq or pd.Timedelta(0))
            if gap_end >= start_ts:
                missing.append(
                    MissingRange(
                        start_time=_format_for_xtdata(start_ts),
                        end_time=_format_for_xtdata(gap_end),
                    )
                )

        if end_ts is not None and last_ts < end_ts:
            gap_start = last_ts + (freq or pd.Timedelta(0))
            if gap_start <= end_ts:
                missing.append(
                    MissingRange(
                        start_time=_format_for_xtdata(gap_start),
                        end_time=_format_for_xtdata(end_ts),
                    )
                )

        if request.count > 0 and len(df) < request.count:
            missing.append(MissingRange(start_time=None, end_time=None, count=request.count))

        return missing


class XtDataProvider:
    """
    Wrapper around xtquant.xtdata.
    """

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        retry: int = 3,
        retry_backoff: float = 1.0,
        download_batch_size: int = 50,
        data_dir: Optional[str] = None,
    ) -> None:
        if xtdata is None:
            raise RuntimeError(
                "xtquant.xtdata is not available. Install and configure QMT/MiniQmt environment first."
            )
        self._xtdata = xtdata
        self._logger = logger or logging.getLogger(__name__)
        self._retry = retry
        self._retry_backoff = retry_backoff
        self._download_batch_size = download_batch_size
        self._data_dir = data_dir
        self._lock = threading.Lock()

        if self._data_dir:
            try:
                self._xtdata.data_dir = self._data_dir
            except AttributeError:
                # Older xtdata versions may expose setter as function
                set_dir = getattr(self._xtdata, "set_data_dir", None)
                if callable(set_dir):
                    set_dir(self._data_dir)
                else:  # pragma: no cover - unexpected versions
                    raise RuntimeError(
                        "xtdata does not expose data_dir configuration; update xtquant version."
                    )

    # -- download -----------------------------------------------------------------
    def download(self, request: DataRequest, missing: MissingRange) -> None:
        sub_req = request.slice(**missing.as_request_kwargs())
        stock_list = list(sub_req.symbols)
        start_time = _to_xt_time_str(sub_req.start_time)
        end_time = _to_xt_time_str(sub_req.end_time)
        incrementally = None
        if not start_time and not end_time:
            incrementally = True
        chunks = self._build_time_chunks(sub_req.period, sub_req.start_time, sub_req.end_time)
        if not chunks:
            chunks = [(sub_req.start_time, sub_req.end_time)]

        total_chunks = len(chunks)
        self._logger.info(
            "Downloading history from xtdata: stocks=%s period=%s start=%s end=%s count=%s chunks=%s",
            stock_list,
            sub_req.period,
            start_time,
            end_time,
            sub_req.count,
            total_chunks,
        )

        for attempt in range(1, self._retry + 1):
            try:
                with self._lock:
                    for idx, (chunk_start_iso, chunk_end_iso) in enumerate(chunks, start=1):
                        chunk_start = _to_xt_time_str(chunk_start_iso)
                        chunk_end = _to_xt_time_str(chunk_end_iso)
                        self._logger.info(
                            "Chunk %s/%s: %s → %s",
                            idx,
                            total_chunks,
                            chunk_start or "<auto>",
                            chunk_end or "<auto>",
                        )
                        if len(stock_list) == 1:
                            self._xtdata.download_history_data(
                                stock_list[0],
                                sub_req.period,
                                start_time=chunk_start,
                                end_time=chunk_end,
                                incrementally=None if chunk_start or chunk_end else incrementally,
                            )
                        else:
                            batched = [
                                stock_list[i : i + self._download_batch_size]
                                for i in range(0, len(stock_list), self._download_batch_size)
                            ]
                            for batch in batched:
                                self._xtdata.download_history_data2(
                                    batch,
                                    sub_req.period,
                                    start_time=chunk_start,
                                    end_time=chunk_end,
                                    callback=None,
                                    incrementally=None if chunk_start or chunk_end else incrementally,
                                )
                return
            except Exception as exc:  # pragma: no cover - depends on runtime
                self._logger.warning(
                    "xtdata download failed (attempt %s/%s): %s",
                    attempt,
                    self._retry,
                    exc,
                )
                if attempt == self._retry:
                    raise
                time.sleep(self._retry_backoff)

    # -- fetch --------------------------------------------------------------------
    def get_market_data(self, request: DataRequest) -> Dict[str, pd.DataFrame]:
        stock_list = list(request.symbols)
        field_list = list(request.fields or [])
        start_time = _to_xt_time_str(request.start_time)
        end_time = _to_xt_time_str(request.end_time)
        count = request.count

        self._logger.debug(
            "Fetching xtdata.get_market_data: stocks=%s period=%s start=%s end=%s count=%s",
            stock_list,
            request.period,
            start_time,
            end_time,
            count,
        )

        raw = self._xtdata.get_market_data(
            field_list=field_list,
            stock_list=stock_list,
            period=request.period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=request.dividend_type,
            fill_data=False,
        )

        return self._to_symbol_frames(raw, stock_list)

    def _to_symbol_frames(
        self,
        raw: Mapping[str, object],
        stock_list: Sequence[str],
    ) -> Dict[str, pd.DataFrame]:
        frames: Dict[str, pd.DataFrame] = {symbol: pd.DataFrame() for symbol in stock_list}

        for field, value in raw.items():
            if not isinstance(value, pd.DataFrame):
                continue
            df_field = value
            for symbol in df_field.index:
                series = df_field.loc[symbol]
                series.index = pd.to_datetime(series.index)
                symbol_df = frames.setdefault(symbol, pd.DataFrame())
                symbol_df[field] = series

        for symbol, df in frames.items():
            if df.empty:
                continue
            df = df.sort_index()
            df["symbol"] = symbol
            frames[symbol] = df

        return frames

    def _build_time_chunks(
        self,
        period: str,
        start_iso: Optional[str],
        end_iso: Optional[str],
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        if not start_iso or not end_iso:
            return []
        try:
            start_ts = pd.to_datetime(start_iso)
            end_ts = pd.to_datetime(end_iso)
        except Exception:
            return []
        if pd.isna(start_ts) or pd.isna(end_ts) or start_ts >= end_ts:
            return []

        freq = _period_offset(period) or pd.Timedelta(minutes=1)
        max_span = pd.DateOffset(years=1)

        chunks: List[Tuple[Optional[str], Optional[str]]] = []
        current_start = start_ts

        while current_start <= end_ts:
            next_start = current_start + max_span
            chunk_end_ts = next_start - freq
            if chunk_end_ts > end_ts:
                chunk_end_ts = end_ts
            if chunk_end_ts < current_start:
                chunk_end_ts = end_ts

            chunks.append(
                (
                    current_start.strftime("%Y-%m-%d %H:%M:%S"),
                    chunk_end_ts.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )

            current_start = chunk_end_ts + freq
        return chunks

    def build_chunks_for_missing(
        self,
        period: str,
        gap: MissingRange,
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        return self._build_time_chunks(period, gap.start_time, gap.end_time)


class PersistenceService:
    """
    Persist downloaded data into DuckDB via Rust extension.
    """

    def __init__(
        self,
        db_path: str,
        *,
        save_fn: Callable[..., None] = save_klines_rust,
        logger: Optional[logging.Logger] = None,
        price_precision: int = 6,
        volume_precision: int = 2,
    ) -> None:
        self._db_path = db_path
        self._save_fn = save_fn
        self._logger = logger or logging.getLogger(__name__)
        self._price_precision = max(0, price_precision)
        self._volume_precision = max(0, volume_precision)

    def save(self, period: str, symbol_frames: Mapping[str, pd.DataFrame]) -> None:
        for symbol, df in symbol_frames.items():
            if df.empty:
                continue
            bars = self._frames_to_bars(df)
            if not bars:
                continue
            self._logger.info(
                "Saving %s bars into database: symbol=%s period=%s",
                len(bars),
                symbol,
                period,
            )
            self._save_fn(self._db_path, symbol, period, bars, False)

    def _frames_to_bars(self, df: pd.DataFrame) -> List[Dict[str, object]]:
        required_columns = ["open", "high", "low", "close", "volume"]
        df_local = df.copy()
        for col in required_columns:
            if col not in df_local.columns:
                df_local[col] = 0.0

        df_local = df_local[required_columns].copy()
        df_local = df_local.sort_index()
        df_local = df_local.ffill().fillna(0.0)

        bars: List[Dict[str, object]] = []
        for timestamp, row in df_local.iterrows():
            if not isinstance(timestamp, pd.Timestamp):
                timestamp = pd.to_datetime(timestamp)
            open_ = round(float(row["open"]), self._price_precision)
            high = round(float(row["high"]), self._price_precision)
            low = round(float(row["low"]), self._price_precision)
            close = round(float(row["close"]), self._price_precision)
            volume = round(float(row["volume"]), self._volume_precision)
            bars.append(
                {
                    "datetime": _format_for_db(timestamp),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
        return bars


class DataIntegrityChecker:
    """
    Validate and merge data frames.
    """

    def sanitize(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df

    def merge(
        self,
        existing: Optional[pd.DataFrame],
        incoming: pd.DataFrame,
    ) -> pd.DataFrame:
        if existing is None or existing.empty:
            return self.sanitize(incoming)
        combined = pd.concat([existing, incoming])
        return self.sanitize(combined)


class MarketDataService:
    """
    High-level service orchestrating DB reads and xtdata fallback.
    """

    def __init__(
        self,
        config: MarketDataConfig,
        *,
        db_provider: Optional[DBDataProvider] = None,
        xt_provider: Optional[XtDataProvider] = None,
        persistence: Optional[PersistenceService] = None,
        integrity_checker: Optional[DataIntegrityChecker] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._db_provider = db_provider or DBDataProvider(
            config.db_path, logger=self._logger
        )
        self._xt_provider = xt_provider
        if config.xtdata_enabled and xt_provider is None:
            self._xt_provider = XtDataProvider(
                logger=self._logger,
                retry=config.download_retry,
                retry_backoff=config.download_retry_backoff,
                download_batch_size=config.download_batch_size,
                data_dir=config.xtdata_data_dir,
            )
        self._persistence = persistence or PersistenceService(
            config.db_path,
            logger=self._logger,
            price_precision=config.price_precision,
            volume_precision=config.volume_precision,
        )
        self._integrity_checker = integrity_checker or DataIntegrityChecker()

    def fetch(self, request: DataRequest) -> Dict[str, pd.DataFrame]:
        data, missing = self._db_provider.fetch(request)
        if not missing:
            return {sym: self._integrity_checker.sanitize(df) for sym, df in data.items()}

        if not self._xt_provider:
            raise RuntimeError(
                "xtdata provider is not configured but database is missing data."
            )

        fresh_frames: Dict[str, pd.DataFrame] = {}
        for missing_range in missing:
            self._logger.info("Detected missing range: %s", missing_range)
            self._xt_provider.download(request, missing_range)
            sub_request = request.slice(**missing_range.as_request_kwargs())
            fetched = self._xt_provider.get_market_data(sub_request)
            for symbol, df in fetched.items():
                if df.empty:
                    continue
                fresh_frames[symbol] = self._integrity_checker.merge(
                    fresh_frames.get(symbol), df
                )

        self._persistence.save(request.period, fresh_frames)

        final_data, still_missing = self._db_provider.fetch(request)
        if still_missing:
            for gap in still_missing:
                chunked = self._xt_provider.build_chunks_for_missing(request.period, gap) if self._xt_provider else []
                if chunked:
                    formatted = [
                        f"{start or '<auto>'} → {end or '<auto>'}" for start, end in chunked
                    ]
                    self._logger.warning(
                        "Data still missing after download: %s. Remaining chunk plan: %s",
                        gap,
                        formatted,
                    )
                else:
                    self._logger.warning("Data still missing after download: %s", gap)

        return {
            sym: self._integrity_checker.sanitize(df)
            for sym, df in final_data.items()
        }

    def fetch_bars(
        self,
        request: DataRequest,
        *,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, object]]:
        frames = self.fetch(request)
        target_symbol = symbol or (request.symbols[0] if request.symbols else None)
        if target_symbol is None:
            return []
        df = frames.get(target_symbol)
        if df is None or df.empty:
            return []
        bars: List[Dict[str, object]] = []
        for ts, row in df.iterrows():
            bars.append(
                {
                    "datetime": _format_for_db(pd.to_datetime(ts)),
                    "open": float(row.get("open", 0.0)),
                    "high": float(row.get("high", 0.0)),
                    "low": float(row.get("low", 0.0)),
                    "close": float(row.get("close", 0.0)),
                    "volume": float(row.get("volume", 0.0)),
                    "symbol": target_symbol,
                }
            )
        return bars


