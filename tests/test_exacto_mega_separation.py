import contextlib
import io
import unittest

from jps_backtest import select_architect
from jps_edge_tool import cmd_analyze


class ExactoMegaSeparationTest(unittest.TestCase):
    def test_analyze_keeps_mega_separate_from_exacto_selection_weights(self):
        draws = []
        for _ in range(12):
            draws.append({"numero": 1, "meganNumero": 99, "in_reventado": 0})
        for _ in range(8):
            draws.append({"numero": 2, "meganNumero": 99, "in_reventado": 1})
        for _ in range(3):
            draws.append({"numero": 99, "meganNumero": 1, "in_reventado": 0})

        with contextlib.redirect_stdout(io.StringIO()):
            report = cmd_analyze(
                object(),
                _draws=draws,
                _no_save=True,
                _mc_iterations=1,
            )

        self.assertIn("weights", report)
        self.assertIn("mega_weights", report)
        self.assertIn("ranked_mega", report)
        self.assertNotIn("combined_weights", report)

    def test_architect_selection_uses_exacto_weights_even_if_legacy_combined_exists(self):
        report = {
            "weights": {"01": 2.0, "02": 1.8, "99": 0.5},
            "combined_weights": {"99": 2.0, "02": 1.0, "01": 0.5},
        }

        self.assertEqual(select_architect(report, 2), [1, 2])


if __name__ == "__main__":
    unittest.main()
