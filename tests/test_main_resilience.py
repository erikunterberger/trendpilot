import unittest

from google.genai import errors as genai_errors

from src import main


class AnalysisFallback(unittest.TestCase):
    def test_success_passes_through(self):
        notes = []
        out = main.analysis_or_fallback(lambda: {"report": "ok", "ideas": [1]}, notes)
        self.assertEqual(out["ideas"], [1])
        self.assertEqual(notes, [])

    def test_gemini_outage_returns_empty_result_and_a_note(self):
        def boom():
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
        notes = []
        out = main.analysis_or_fallback(boom, notes)
        self.assertEqual((out["ideas"], out["reviews"], out["rejected"]), ([], [], []))
        self.assertTrue(out["failed"])
        self.assertIn("## Executive Summary", out["report"])
        self.assertIn("ServerError", notes[0])

    def test_any_other_failure_is_contained_too(self):
        def boom():
            raise RuntimeError("Missing required config 'GEMINI_API_KEY'")
        notes = []
        self.assertEqual(main.analysis_or_fallback(boom, notes)["ideas"], [])
        self.assertIn("RuntimeError", notes[0])


if __name__ == "__main__":
    unittest.main()
