import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))
import publisher_engine


class TestPublisherEngine(unittest.TestCase):
    def test_normalize_lf(self):
        self.assertEqual(publisher_engine._normalize_lf("a\r\nb\rc\n"), "a\nb\nc\n")

    def test_extract_sections_from_markdown(self):
        sample_md = """# Test Architecture Study

## Escopo & Problema
This is scope text.

## Método & Trilha de Evidências
This is evidence text.

## Proposta & Resultados
This is findings text.

## Limitações & Ameaças
This is limitations text.

## Referências
This is reference 1.
"""
        parsed = publisher_engine._extract_sections_from_markdown(sample_md)
        self.assertEqual(parsed["title"], "Test Architecture Study")
        self.assertIn("scope text", parsed["scope"])
        self.assertIn("evidence text", parsed["evidence"])
        self.assertIn("findings text", parsed["findings"])
        self.assertIn("limitations text", parsed["limitations"])
        self.assertIn("reference 1", parsed["references"])

    def test_build_canonical_html(self):
        parsed = {
            "title": "My Canonical Study",
            "abstract": "An abstract description.",
            "scope": "Scope details.",
            "evidence": "Evidence details.",
            "findings": "Findings details.",
            "limitations": "Limitation details.",
            "references": "Reference details."
        }
        html = publisher_engine.build_canonical_html(parsed, "RESEARCH BOUNDARY")
        self.assertIn("<!doctype html>", html)
        self.assertIn('<article data-tare-document>', html)
        self.assertIn('<header data-tare-role="document-header">', html)
        self.assertIn('<section id="scope" data-tare-section="scope">', html)
        self.assertIn('<section id="evidence" data-tare-section="evidence">', html)
        self.assertIn('<section id="findings" data-tare-section="findings">', html)
        self.assertIn('<section id="limitations" data-tare-section="limitations">', html)
        self.assertIn('<section id="references" data-tare-section="references">', html)
        self.assertNotIn("\r\n", html)


if __name__ == "__main__":
    unittest.main()
