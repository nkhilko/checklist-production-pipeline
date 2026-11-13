"""Generate, review and publish fictional checklist products offline."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import textwrap


def render_pdf(title, items):
    """Render a one-page PDF from validated, printable ASCII text."""
    lines = [title, ""]
    for item in items:
        lines.extend(textwrap.wrap("[ ] " + item, width=78, subsequent_indent="    "))
    if len(lines) > 39:
        raise ValueError("Checklist does not fit the demo template")
    drawing = ["BT /F1 11 Tf 16 TL 48 744 Td"]
    for line in lines:
        escaped = line.replace(chr(92), chr(92) * 2).replace("(", r"\(").replace(")", r"\)")
        drawing.append(f"({escaped}) Tj T*")
    stream = ("\n".join(drawing) + "\nET\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
    ]
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = output.tell()
    output.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return output.getvalue()


def prepare(brief):
    title, notes = brief
    if not isinstance(title, str) or not isinstance(notes, (list, tuple)):
        raise ValueError("Expected a title and a list of research notes")
    if not 2 <= len(notes) <= 10:
        raise ValueError("Expected two to ten research notes")
    for line in (title, *notes):
        if (not isinstance(line, str) or not line.strip() or len(line) > 75
                or not line.isascii() or not line.isprintable()):
            raise ValueError("Expected printable ASCII text of at most 75 characters")
    items = [note.strip().rstrip(".") + "." for note in notes]
    if any(item == "." for item in items):
        raise ValueError("Expected substantive research notes")
    pdf = render_pdf(title.strip(), items)
    return title.strip(), hashlib.sha256(pdf).hexdigest(), pdf


class ReceiptLost(Exception):
    """The mock accepted the product but the response did not arrive."""


class MockCatalog:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS products (digest TEXT PRIMARY KEY, title TEXT, pdf BLOB)")

    def publish(self, digest, title, pdf, *, lose_receipt=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO products VALUES (?, ?, ?)", (digest, title, pdf))
        if lose_receipt:
            raise ReceiptLost

    def read(self, digest):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT pdf FROM products WHERE digest = ?", (digest,)).fetchone()
        return row[0] if row else None


class Pipeline:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS products (
                digest TEXT PRIMARY KEY, title TEXT, pdf BLOB, state TEXT, reviewer TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, digest TEXT, action TEXT
            )""")

    def batch(self, briefs, workers=3):
        if not 1 <= workers <= 8:
            raise ValueError("Expected one to eight workers")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(prepare, brief) for brief in briefs]
            results = []
            for future in futures:
                try:
                    title, digest, pdf = future.result()
                except (TypeError, ValueError) as error:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("INSERT INTO events(digest, action) VALUES (NULL, 'validation_failed')")
                    results.append({"state": "invalid", "reason": str(error)})
                    continue
                with closing(sqlite3.connect(self.path)) as db, db:
                    added = db.execute(
                        "INSERT OR IGNORE INTO products VALUES (?, ?, ?, 'pending', NULL)", (digest, title, pdf)
                    )
                    if added.rowcount:
                        db.execute("INSERT INTO events(digest, action) VALUES (?, 'pending')", (digest,))
                    state = db.execute("SELECT state FROM products WHERE digest = ?", (digest,)).fetchone()[0]
                results.append({"title": title, "digest": digest, "state": state})
            return results

    def review(self, digest, decision, reviewer):
        if decision not in {"approved", "discarded"} or not reviewer.strip():
            raise ValueError("Expected an explicit decision and reviewer")
        self._transition(digest, "pending", decision, reviewer)

    def _transition(self, digest, previous, state, reviewer=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            updated = db.execute(
                "UPDATE products SET state = ?, reviewer = coalesce(?, reviewer) WHERE digest = ? AND state = ?",
                (state, reviewer, digest, previous),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Expected {previous} state for this checklist")
            db.execute("INSERT INTO events(digest, action) VALUES (?, ?)", (digest, state))

    def asset(self, digest):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT title, pdf FROM products WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Unknown checklist")
        return row

    def publish(self, digest, catalog, *, lose_receipt=False):
        self._transition(digest, "approved", "publishing")
        title, pdf = self.asset(digest)
        try:
            catalog.publish(digest, title, pdf, lose_receipt=lose_receipt)
        except ReceiptLost:
            return "publishing"
        return self.reconcile(digest, catalog)

    def retry(self, digest, catalog):
        state = self.reconcile(digest, catalog)
        if state == "published" or catalog.read(digest) is not None:
            return state
        title, pdf = self.asset(digest)
        # The mock guarantees a single product per digest, including concurrent retries.
        catalog.publish(digest, title, pdf)
        return self.reconcile(digest, catalog)

    def reconcile(self, digest, catalog):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT state, pdf FROM products WHERE digest = ?", (digest,)).fetchone()
        if row is None or row[0] not in {"publishing", "published"}:
            raise ValueError("Expected an existing publish attempt")
        if row[0] == "published":
            return "published"
        if catalog.read(digest) != row[1]:
            return "publishing"
        try:
            self._transition(digest, "publishing", "published")
        except ValueError:
            with closing(sqlite3.connect(self.path)) as db:
                if db.execute("SELECT state FROM products WHERE digest = ?", (digest,)).fetchone()[0] != "published":
                    raise
        return "published"


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    from sys import argv

    briefs = [
        ("Community Seed Swap", ("Label seed envelopes by variety", "Set up a table for exchanges")),
        ("Repair Cafe Setup", ("Lay out shared hand tools", "Check the spare parts tray")),
        ("Broken brief", ("Only one note",)),
    ]
    with TemporaryDirectory() as directory:
        root = Path(directory)
        pipeline = Pipeline(root / "pipeline.sqlite")
        catalog = MockCatalog(root / "catalog.sqlite")
        approved, discarded, invalid = pipeline.batch(briefs)
        a, b = approved["digest"], discarded["digest"]
        if len(argv) > 1:
            Path(argv[1]).write_bytes(pipeline.asset(a)[1])
        pipeline.review(a, "approved", "demo-reviewer")
        pipeline.review(b, "discarded", "demo-reviewer")
        attempted = pipeline.publish(a, catalog, lose_receipt=True)
        recovered = Pipeline(pipeline.path).reconcile(a, MockCatalog(catalog.path))
        print(json.dumps({"batch": [approved, discarded, invalid], "decision": "second checklist discarded",
                          "after_lost_receipt": attempted, "after_restart": recovered}, indent=2))
