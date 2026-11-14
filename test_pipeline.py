from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from pipeline import MockCatalog, Pipeline

BRIEF = ("Workshop Setup", ["Lay out tools", "Number (and label) each tray"])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.pipeline = Pipeline(root / "pipeline.sqlite")
        self.catalog = MockCatalog(root / "catalog.sqlite")

    def test_batch_isolates_invalid_briefs_and_deduplicates(self):
        results = self.pipeline.batch([BRIEF, ("Invalid", []), BRIEF])
        self.assertEqual([row["state"] for row in results], ["pending", "invalid", "pending"])
        self.assertEqual(results[0]["digest"], results[2]["digest"])
        self.assertTrue(self.pipeline.asset(results[0]["digest"])[1].startswith(b"%PDF-1.4"))

    def test_approval_required_and_discard_is_terminal(self):
        digest = self.pipeline.batch([BRIEF])[0]["digest"]
        with self.assertRaises(ValueError):
            self.pipeline.publish(digest, self.catalog)
        self.pipeline.review(digest, "discarded", "test-reviewer")
        with self.assertRaises(ValueError):
            self.pipeline.review(digest, "approved", "test-reviewer")
        self.assertIsNone(self.catalog.read(digest))

    def test_lost_receipt_reconciles_after_restart(self):
        digest = self.pipeline.batch([BRIEF])[0]["digest"]
        self.pipeline.review(digest, "approved", "test-reviewer")
        self.assertEqual(self.pipeline.publish(digest, self.catalog, lose_receipt=True), "publishing")
        worker = Pipeline(self.pipeline.path)
        self.assertEqual(worker.retry(digest, self.catalog), "published")
        self.assertEqual(worker.retry(digest, self.catalog), "published")
        with closing(sqlite3.connect(self.catalog.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0], 1)

    def test_retry_after_pre_write_failure_and_conflicting_readback(self):
        class Unavailable(MockCatalog):
            def publish(self, *args, **kwargs):
                raise ConnectionError

        digest = self.pipeline.batch([BRIEF])[0]["digest"]
        self.pipeline.review(digest, "approved", "test-reviewer")
        with self.assertRaises(ConnectionError):
            self.pipeline.publish(digest, Unavailable(self.catalog.path))
        self.assertEqual(self.pipeline.retry(digest, self.catalog), "published")

        other = self.pipeline.batch([("Other workshop", BRIEF[1])])[0]["digest"]
        self.pipeline.review(other, "approved", "test-reviewer")
        with self.assertRaises(ConnectionError):
            self.pipeline.publish(other, Unavailable(self.catalog.path))
        self.catalog.publish(other, "Placeholder", b"different asset")
        self.assertEqual(self.pipeline.retry(other, self.catalog), "publishing")
        self.assertEqual(self.catalog.read(other), b"different asset")

    def test_conflicting_title_with_identical_pdf_and_destination_substitution(self):
        digest = self.pipeline.batch([BRIEF])[0]["digest"]
        self.pipeline.review(digest, "approved", "test-reviewer")
        title, pdf = self.pipeline.asset(digest)
        self.catalog.publish(digest, "Unexpected title", pdf)
        self.assertEqual(self.pipeline.publish(digest, self.catalog), "publishing")
        self.assertEqual(self.pipeline.retry(digest, self.catalog), "publishing")
        with closing(sqlite3.connect(self.catalog.path)) as db, db:
            db.execute("UPDATE products SET title = ? WHERE digest = ?", (title, digest))
        self.assertEqual(self.pipeline.reconcile(digest, self.catalog), "published")
        substitute = MockCatalog(self.catalog.path.with_name("replacement.sqlite"))
        with self.assertRaisesRegex(ValueError, "matching catalog"):
            self.pipeline.reconcile(digest, substitute)
        self.assertIsNone(substitute.read(digest))
        self.assertEqual(self.pipeline.reconcile(digest, MockCatalog(self.catalog.path)), "published")
