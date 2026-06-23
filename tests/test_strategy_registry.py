import unittest

import jps_backtest
import jps_bandit
import jps_predict


class StrategyRegistryTest(unittest.TestCase):
    def test_new_exacto_only_strategies_are_available_in_backtest_and_predict(self):
        expected = {
            "weekday_session_recent30",
            "weekday_recent15",
            "weekday_consensus_exacto",
            "weekday_inverse_recent",
            "exacto_signal_gate",
            "global_frequency_prior",
            "weekday_recent30_plus_global_prior",
            "weekday_session_plus_global_prior",
            "weekday_recent30_session_global",
        }

        backtest_names = {name for name, _selector, _profile in jps_backtest.STRATEGIES}
        predict_names = set(jps_predict.ALL_STRATEGIES)
        bandit_names = set(jps_bandit.ALL_STRATEGIES)

        self.assertTrue(expected.issubset(backtest_names))
        self.assertTrue(expected.issubset(predict_names))
        self.assertTrue(expected.issubset(bandit_names))

    def test_global_frequency_prior_is_a_separate_exacto_signal(self):
        top5 = jps_backtest.select_global_frequency_prior(n=5)

        self.assertEqual(top5, [4, 69, 91, 27, 44])

        target = {"dia": "2026-06-02T00:00:00", "session": "manana"}
        combined = jps_backtest.select_weekday_recent30_session_global([], target, n=5)

        self.assertEqual(combined, top5)


if __name__ == "__main__":
    unittest.main()
