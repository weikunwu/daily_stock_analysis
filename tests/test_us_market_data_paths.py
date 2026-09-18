# -*- coding: utf-8 -*-
"""US market data paths: TickFlow US breadth, YFinance GICS sector rankings,
yfinance major-holders institution block, and manager routing.

All tests are offline (mocked providers) and run under
``pytest -m "not network"``.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager
from data_provider.tickflow_fetcher import TickFlowFetcher
from data_provider.yfinance_fetcher import YfinanceFetcher
from data_provider.yfinance_fundamental_adapter import YfinanceFundamentalAdapter

from tests.test_tickflow_fetcher import _FakeClient, _PermissionLikeError, _quote


class TestTickFlowUsMarketStats(unittest.TestCase):
    """TickFlowFetcher.get_market_stats(market='us') aggregates US_Equity universe quotes."""

    def _us_quotes(self):
        return [
            _quote("A.US", last_price=110.0, prev_close=100.0, amount=5e9),  # up
            _quote("B.US", last_price=90.0, prev_close=100.0, amount=3e9),  # down
            _quote("C.US", last_price=50.0, prev_close=50.0, amount=2e9),  # flat
            # prev_close missing: amount still counts toward total, but not a valid breadth row
            {"symbol": "D.US", "last_price": 10.0, "prev_close": None, "amount": 1e9},
            # non-US symbol must be filtered out of US stats
            _quote("600519.SH", last_price=11.0, prev_close=10.0, amount=9e9),
        ]

    def test_us_stats_counts_breadth_and_turnover_in_bn_usd(self):
        fetcher = TickFlowFetcher(api_key="sk-test")
        fetcher._client = _FakeClient(universe_data=self._us_quotes())

        stats = fetcher.get_market_stats(market="us")

        self.assertIsNotNone(stats)
        self.assertEqual(stats["up_count"], 1)
        self.assertEqual(stats["down_count"], 1)
        self.assertEqual(stats["flat_count"], 1)
        self.assertEqual(stats["limit_up_count"], 0)
        self.assertEqual(stats["limit_down_count"], 0)
        # A+B+C+D = 5e9 + 3e9 + 2e9 + 1e9 = 11e9 USD = 11 (十亿美元)
        self.assertAlmostEqual(stats["total_amount"], 11.0)

    def test_us_stats_requests_us_universe_only(self):
        fetcher = TickFlowFetcher(api_key="sk-test")
        fetcher._client = _FakeClient(universe_data=self._us_quotes())

        fetcher.get_market_stats(market="us")

        call = fetcher._client.quotes.calls[0]
        self.assertEqual(call["universes"], ["US_Equity"])

    def test_us_stats_permission_failure_is_negative_cached(self):
        fetcher = TickFlowFetcher(api_key="sk-test")
        fetcher._client = _FakeClient(universe_data=_PermissionLikeError("universe forbidden"))

        self.assertIsNone(fetcher.get_market_stats(market="us"))
        self.assertIsNone(fetcher.get_market_stats(market="us"))
        self.assertEqual(len(fetcher._client.quotes.calls), 1)

    def test_us_stats_empty_universe_returns_none(self):
        fetcher = TickFlowFetcher(api_key="sk-test")
        fetcher._client = _FakeClient(universe_data=[])
        self.assertIsNone(fetcher.get_market_stats(market="us"))

    def test_no_client_returns_none_without_error(self):
        fetcher = TickFlowFetcher(api_key="")
        self.assertIsNone(fetcher.get_market_stats(market="us"))


class TestManagerUsRouting(unittest.TestCase):
    """DataFetcherManager routes market='us' to the US data paths (fail-open)."""

    def test_us_market_stats_routes_to_tickflow(self):
        manager = DataFetcherManager(fetchers=[])
        stats = {"up_count": 1, "down_count": 2, "flat_count": 3,
                 "limit_up_count": 0, "limit_down_count": 0, "total_amount": 450.0}
        fake_tickflow = MagicMock()
        fake_tickflow.get_market_stats.return_value = stats
        with patch.object(manager, "_get_tickflow_fetcher", return_value=fake_tickflow):
            result = manager.get_market_stats(purpose="market_review:us", market="us")
        self.assertEqual(result, stats)
        fake_tickflow.get_market_stats.assert_called_once_with(market="us")

    def test_us_market_stats_fail_open_when_tickflow_missing(self):
        manager = DataFetcherManager(fetchers=[])
        with patch.object(manager, "_get_tickflow_fetcher", return_value=None):
            result = manager.get_market_stats(market="us")
        self.assertEqual(result, {})

    def test_us_market_stats_fail_open_when_tickflow_returns_none(self):
        manager = DataFetcherManager(fetchers=[])
        fake_tickflow = MagicMock()
        fake_tickflow.get_market_stats.return_value = None
        with patch.object(manager, "_get_tickflow_fetcher", return_value=fake_tickflow):
            self.assertEqual(manager.get_market_stats(market="us"), {})

    def test_cn_market_stats_routing_is_untouched(self):
        # CN path: TickFlow is consulted with its default (A-share) semantics
        manager = DataFetcherManager(fetchers=[])
        stats = {"up_count": 0, "down_count": 0, "flat_count": 0}
        fake_tickflow = MagicMock()
        fake_tickflow.get_market_stats.return_value = stats
        with patch.object(manager, "_get_tickflow_fetcher", return_value=fake_tickflow):
            result = manager.get_market_stats(purpose="market_review:cn")
        self.assertEqual(result, stats)
        fake_tickflow.get_market_stats.assert_called_once_with()

    def test_us_sector_rankings_route_to_yfinance_fetcher(self):
        yfinance_fetcher = MagicMock()
        yfinance_fetcher.name = "YfinanceFetcher"
        yfinance_fetcher.get_sector_rankings.return_value = ([{"name": "Technology", "change_pct": 2.1}], [])
        manager = DataFetcherManager(fetchers=[yfinance_fetcher])

        top, bottom = manager.get_sector_rankings(5, market="us")

        self.assertEqual(top, [{"name": "Technology", "change_pct": 2.1}])
        self.assertEqual(bottom, [])
        yfinance_fetcher.get_sector_rankings.assert_called_once_with(5)

    def test_us_sector_rankings_fail_open_without_yfinance_fetcher(self):
        # fetchers 列表非空才不会触发默认初始化（默认会带真实 YfinanceFetcher）；
        # 放一个非 Yfinance 的占位 fetcher，验证找无 Yfinance 时美股行业榜 fail-open 返回空榜
        dummy = MagicMock()
        dummy.name = "DummyFetcher"
        manager = DataFetcherManager(fetchers=[dummy])
        self.assertEqual(manager.get_sector_rankings(5, market="us"), ([], []))


class TestYfinanceSectorRankings(unittest.TestCase):
    """YfinanceFetcher.get_sector_rankings builds GICS sector rows from sector ETFs."""

    def _download_df(self):
        # MultiIndex (field, ticker) columns like yfinance returns for multiple tickers
        cols = pd.MultiIndex.from_tuples(
            [("Open", "XLK"), ("Close", "XLK"), ("Open", "XLE"), ("Close", "XLE")]
        )
        data = [
            [99.0, 100.0, 50.5, 50.0],
            [102.0, 103.0, 48.5, 48.0],
        ]
        return pd.DataFrame(
            data=data,
            index=pd.to_datetime(["2026-09-16", "2026-09-17"]),
            columns=cols,
        )

    def test_ranking_order_and_shape(self):
        fetcher = YfinanceFetcher()
        with patch("yfinance.download", return_value=self._download_df()) as mock_dl:
            top, bottom = fetcher.get_sector_rankings(1)

        self.assertEqual(top[0]["name"], "Technology")
        self.assertEqual(top[0]["ticker"], "XLK")
        self.assertAlmostEqual(top[0]["change_pct"], 3.0, places=1)
        self.assertEqual(bottom[0]["name"], "Energy")
        self.assertAlmostEqual(bottom[0]["change_pct"], -4.0, places=1)
        mock_dl.assert_called_once()

    def test_fail_open_on_download_exception(self):
        fetcher = YfinanceFetcher()
        with patch("yfinance.download", side_effect=RuntimeError("net down")):
            self.assertIsNone(fetcher.get_sector_rankings())

    def test_fail_open_on_empty_download(self):
        fetcher = YfinanceFetcher()
        with patch("yfinance.download", return_value=pd.DataFrame()):
            self.assertIsNone(fetcher.get_sector_rankings())


class _FakeYfTicker:
    """Minimal yfinance Ticker double for the institution block."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.info = {
            "heldPercentInstitutions": 0.663,
            "heldPercentInsiders": 0.016,
            "sector": "Technology",
        }

    @property
    def major_holders(self):
        return pd.DataFrame(
            {"Value": [0.663, 7758.0]},
            index=pd.Index(["institutionsPercentHeld", "institutionsCount"]),
        )

    def __getattr__(self, name):
        raise AttributeError(name)


class TestYfinanceInstitutionPayload(unittest.TestCase):
    def test_us_symbol_gets_institution_payload(self):
        with patch("yfinance.Ticker", return_value=_FakeYfTicker("AAPL")):
            bundle = YfinanceFundamentalAdapter().get_fundamental_bundle("AAPL")

        inst = bundle.get("institution")
        self.assertIsInstance(inst, dict)
        self.assertNotEqual(inst, {})
        self.assertAlmostEqual(inst["institutional_ownership_pct"], 66.3, places=1)
        self.assertAlmostEqual(inst["insider_ownership_pct"], 1.6, places=1)
        self.assertEqual(inst["institutions_count"], 7758.0)
        self.assertIn("institution:yfinance", bundle["source_chain"])

    def test_hk_symbol_keeps_institution_empty(self):
        with patch("yfinance.Ticker", return_value=_FakeYfTicker("00700.HK")):
            bundle = YfinanceFundamentalAdapter().get_fundamental_bundle("0700.HK")
        self.assertEqual(bundle.get("institution"), {})


_US_INST_PAYLOAD = {
    "institutional_ownership_pct": 66.3,
    "insider_ownership_pct": 1.6,
    "institutions_count": 7758.0,
    "source": "yfinance.major_holders",
}


class TestUsInstitutionBlockAssembly(unittest.TestCase):
    """Offshore fundamental assembly: US institution block from the yfinance bundle."""

    def _context_with_bundle(self, bundle):
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        manager = DataFetcherManager(fetchers=[])
        tw_method = "data_provider.tw_institutional_fetcher.TwInstitutionalFetcher.get_institutional_net"
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ), \
                patch(tw_method, return_value=None) as tw_mock:
            ctx = manager.get_fundamental_context("AAPL")
        return ctx, tw_mock

    def _us_bundle(self, with_institution=True):
        return {
            "status": "partial",
            "growth": {},
            "earnings": {},
            "institution": dict(_US_INST_PAYLOAD) if with_institution else {},
            "boards": {},
            "belong_boards": [],
            "source_chain": ["institution:yfinance"],
            "errors": [],
        }

    def test_us_institution_ok_when_bundle_has_payload(self):
        ctx, tw_mock = self._context_with_bundle(self._us_bundle())
        self.assertEqual(ctx["market"], "us")
        self.assertEqual(ctx["coverage"]["institution"], "ok")
        self.assertEqual(ctx["institution"]["data"]["institutions_count"], 7758.0)
        self.assertEqual(ctx["institution"]["data"]["source"], "yfinance.major_holders")
        self.assertEqual(tw_mock.call_count, 0)

    def test_us_institution_not_supported_without_payload(self):
        ctx, _ = self._context_with_bundle(self._us_bundle(with_institution=False))
        self.assertEqual(ctx["coverage"]["institution"], "not_supported")
        self.assertEqual(ctx["institution"].get("data"), {})


if __name__ == "__main__":
    unittest.main()
