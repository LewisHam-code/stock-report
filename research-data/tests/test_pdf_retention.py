import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_research import core, financial_evidence, pdf_retention, sources
from stock_research.core import ServiceError, Store


class PdfRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.site.joinpath("report-details").mkdir(parents=True)
        self.patches = [patch.object(core, "ROOT", self.root),
                        patch.object(core, "VAULT", self.root/"vault"),
                        patch.object(financial_evidence, "ROOT", self.root),
                        patch.object(pdf_retention, "ROOT", self.root),
                        patch.object(pdf_retention, "SITE", self.site)]
        for p in self.patches:
            p.start()
        self.store = Store(self.root / "test.sqlite")
        self.today = date(2026, 10, 18)

    def tearDown(self):
        self.store.conn.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def add_complete(self, ident, code="600000", created="2026-09-01", disclosed="2026-01-01",
                     quality="complete", recommended=False):
        raw = b"%PDF-1.4\nretention-fixture\n"
        sha = hashlib.sha256(raw).hexdigest()
        relative = f"filings/SH/{code}/2026Q3/{ident}.pdf"
        pdf = self.root / relative
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(raw)
        text = self.root / "text" / f"{ident}.json"
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text(json.dumps({"sha256": sha, "pages": [{"page": 1, "text": "财报原文"}]}))
        doc = dict(id=ident, code=code, company="样例公司", market="SH", period="2026Q3",
                   title="2026年第三季度报告", published_at=disclosed, source_url="https://static.cninfo.com.cn/example.pdf")
        self.store.add_document(doc)
        self.store.execute("UPDATE documents SET pdf=?,sha256=?,text_file=?,state='complete' WHERE id=?",
                           (relative, sha, str(text.relative_to(self.root)), ident))
        report_id = "auto-" + ident
        self.store.execute("INSERT INTO reports VALUES(?,?,?,?,?)", (report_id, ident, "v1", "{}", created + "T12:00:00+08:00"))
        scoring = dict(status="calculated", coverage_pct=90, score=75, dimensions=[dict(name="成长", covered_weight=25, points=20)])
        report = dict(id=report_id, rating="buy" if recommended else "unrated",
                      evidence_status="sufficient" if recommended else "insufficient", scoring=scoring,
                      decision=dict(growth=dict(revenue_yoy_pct=30, adjusted_profit_yoy_pct=40), red_flags=[]),
                      financial_analysis=dict(data_quality=dict(status=quality)))
        self.site.joinpath("report-details", report_id + ".json").write_text(json.dumps(report))
        metrics = [dict(metric=name, scope=scope, verified=True, standard_value=1)
                   for name, scope in financial_evidence.CORE_FIELDS.items()]
        from stock_research.financial_facts import CONTRACT,IMPLEMENTATION
        proof = dict(status="checked", version=financial_evidence.VERSION, metrics=metrics,
                     conflicts=[], checks=[], pages_scanned=1,contract=CONTRACT,implementation_sha256=IMPLEMENTATION,
                     identity={"id":ident,"code":code,"market":"SH","period":"2026Q3","sha256":sha})
        record=json.loads(text.read_text());record['pdf_proof']=proof;text.write_text(json.dumps(record))
        return pdf, text, proof

    def test_completed_ordinary_pdf_removed_after_30_days_but_proof_survives(self):
        pdf, text, proof = self.add_complete("1225000001")
        result = pdf_retention.cleanup_pdfs(self.store, {}, self.today)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(pdf.exists())
        doc = self.store.rows("SELECT * FROM documents WHERE id='1225000001'")[0]
        self.assertIsNone(doc["pdf"])
        self.assertEqual(json.loads(text.read_text())["pdf_proof"], proof)
        self.store.execute("DELETE FROM cache")
        self.assertEqual(financial_evidence.primary_metrics(self.store, doc), proof)
        restored = financial_evidence.enrich_extraction(self.store, doc, {"metrics": []})
        self.assertEqual(restored["data_quality"]["status"], "complete")

    def test_recommended_and_watched_have_longer_windows(self):
        recommended, _, _ = self.add_complete("1225000002", code="600001", created="2026-08-01", recommended=True)
        watched, _, _ = self.add_complete("1225000003", code="600002", created="2026-01-01", disclosed="2025-01-01")
        result = pdf_retention.cleanup_pdfs(self.store, {"watchlist": [{"code": "600002"}]}, self.today)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(recommended.exists())
        self.assertTrue(watched.exists())
        self.assertEqual(pdf_retention.cleanup_pdfs(self.store, {"watchlist": [{"code": "600002"}]}, date(2027, 1, 1))["removed"], 2)

    def test_unfollow_grace_and_incomplete_evidence_block_removal(self):
        recent_unfollow, _, _ = self.add_complete("1225000004", code="600003")
        review, _, _ = self.add_complete("1225000005", code="600004", quality="review")
        cfg = {"pdf_unwatched_at": {"600003": "2026-10-01T10:00:00+08:00"}}
        self.assertEqual(pdf_retention.cleanup_pdfs(self.store, cfg, self.today)["removed"], 0)
        self.assertTrue(recent_unfollow.exists())
        self.assertTrue(review.exists())
        self.assertEqual(pdf_retention.cleanup_pdfs(self.store, cfg, date(2026, 11, 1))["removed"], 1)
        self.assertTrue(review.exists())

    def test_changed_remote_original_is_rejected_before_rearchiving(self):
        doc = dict(id="1225000006", code="600000", market="SH", period="2026Q3",
                   source_url="https://static.cninfo.com.cn/example.pdf", sha256="0" * 64)
        with patch.object(sources, "ROOT", self.root), patch.object(sources, "request", return_value=b"%PDF-1.4\nchanged"):
            with self.assertRaises(ServiceError) as caught:
                sources.archive_pdf(doc)
        self.assertEqual(caught.exception.code, "pdf_changed")
        self.assertEqual(list((self.root / "filings").rglob("*.pdf")), [])

    def test_outdated_quality_claim_does_not_override_current_proof(self):
        pdf, text, proof = self.add_complete("1225000007")
        proof["metrics"] = []
        record=json.loads(text.read_text());record['pdf_proof']=proof;text.write_text(json.dumps(record))
        result = pdf_retention.cleanup_pdfs(self.store, {}, self.today)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(pdf.exists())

    def test_reanalysis_can_refetch_original_without_retaining_pdf(self):
        pdf, text, _ = self.add_complete("1225000008")
        pdf.unlink()
        record=json.loads(text.read_text());record.pop('pdf_proof',None);text.write_text(json.dumps(record))
        self.store.execute("DELETE FROM cache")
        doc = self.store.rows("SELECT * FROM documents WHERE id='1225000008'")[0]
        raw = b"%PDF-1.4\nretention-fixture\n"
        with patch.object(sources, "request", return_value=raw), \
             patch("pdfplumber.open", return_value=nullcontext(SimpleNamespace(pages=[]))), \
             patch("stock_research.statement_evidence.statement_metrics", return_value=([], [], [])):
            proof = financial_evidence.primary_metrics(self.store, doc, allow_refetch=True)
        self.assertEqual(proof["status"], "checked")
        self.assertEqual(proof["pages_scanned"], 0)
        from stock_research.financial_facts import artifact
        self.assertEqual(json.loads(artifact(doc).read_text()),proof)


if __name__ == "__main__":
    unittest.main()
