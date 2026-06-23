import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jps_edge_tool


PAGE_STATS_SAMPLE = """
Frecuencia de Números Ganadores
Número
Cant. de veces que ha salido
Última vez que salió
24
109
02/06/2026
58
103
02/06/2026
88
114
02/06/2026
Mega Números Ganadores
Número
Cant. de veces que ha salido
Última vez que salió
2
40
24/05/2026
19
42
14/05/2026
37
1
02/06/2026
Ver más resultados
"""


class PageStatsParserTest(unittest.TestCase):
    def test_parse_page_stats_text_separates_exacto_and_mega_tables(self):
        parsed = jps_edge_tool.parse_page_stats_text(PAGE_STATS_SAMPLE)

        self.assertEqual(parsed["exacto"]["24"]["count"], 109)
        self.assertEqual(parsed["exacto"]["24"]["last_seen"], "2026-06-02")
        self.assertEqual(parsed["exacto"]["88"]["count"], 114)
        self.assertEqual(parsed["mega"]["19"]["count"], 42)
        self.assertEqual(parsed["mega"]["02"]["count"], 40)
        self.assertEqual(parsed["mega"]["37"]["last_seen"], "2026-06-02")

    def test_import_page_stats_writes_combined_and_prior_files(self):
        with tempfile.TemporaryDirectory() as td:
            sample_path = Path(td) / "page.txt"
            sample_path.write_text(PAGE_STATS_SAMPLE, encoding="utf-8")

            with patch.object(jps_edge_tool, "DATA_DIR", td):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = jps_edge_tool.cmd_import_page_stats(SimpleNamespace(file=str(sample_path)))
                exacto_prior = jps_edge_tool.load_json("global_frequency_prior.json")
                mega_prior = jps_edge_tool.load_json("mega_frequency_prior.json")

        self.assertEqual(result["source"], str(sample_path))
        self.assertEqual(exacto_prior["numbers"]["24"]["count"], 109)
        self.assertEqual(mega_prior["numbers"]["19"]["count"], 42)

    def test_fetch_page_mode_saves_page_response(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(jps_edge_tool, "DATA_DIR", td), patch.object(
                jps_edge_tool, "api_get", return_value=[{"dia": "2026-06-02T00:00:00"}]
            ) as api_get:
                with contextlib.redirect_stdout(io.StringIO()):
                    jps_edge_tool.cmd_fetch(SimpleNamespace(mode="page", days=60))
                saved = jps_edge_tool.load_json("jps_page_data.json")

        api_get.assert_called_once_with("/api/App/nuevostiempos/page")
        self.assertEqual(saved[0]["dia"], "2026-06-02T00:00:00")


if __name__ == "__main__":
    unittest.main()
