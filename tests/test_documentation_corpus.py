"""Integrity and provenance tests for the bundled documentation corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPOSITORY_ROOT / "server" / "resources" / "docs"
MANIFEST_PATH = DOCS_ROOT / "manifest.json"
URL_PATTERN = re.compile(r"https?://[^\s)>\]}”’\"']+")


def _safe_target(root: Path, relative: str) -> Path:
    """Resolve a manifest path and reject absolute or escaping paths."""
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    if relative != posix.as_posix():
        raise ValueError(f"path is not canonical POSIX form: {relative}")
    target = (root / Path(*posix.parts)).resolve()
    target.relative_to(root.resolve())
    return target


class DocumentationCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.documents = cls.manifest["documents"]

    def test_corpus_metadata_is_explicit(self):
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["project"], "MSX-AI")
        self.assertEqual(self.manifest["license"], "GPL-3.0-or-later")
        self.assertEqual(
            self.manifest["corpus_license"], "GPL-3.0-or-later")
        self.assertEqual(
            self.manifest["project_license"], "GPL-3.0-or-later")
        self.assertEqual(self.manifest["authored_by"], "MSX-AI project")
        self.assertEqual(self.manifest["creator"], {
            "name": "Rodrigo Galhardi M. Garcia",
            "role": (
                "Project creator and originator of the MCP-to-real-MSX "
                "integration concept"),
        })
        self.assertEqual(
            self.manifest["provenance"], {
                "origin": "project-authored",
                "repository_url": "https://github.com/mefroio/msx-ai",
                "distribution_version": "0.6.0",
                "source_distribution": "msx_ai-0.6.0.tar.gz",
                "evidence_paths_relative_to": "source distribution root",
            })
        self.assertEqual(self.manifest["reviewed_at"], "2026-08-10")

    def test_manifest_covers_every_markdown_resource_once(self):
        ids = [document["id"] for document in self.documents]
        paths = [document["path"] for document in self.documents]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(
            set(paths), {path.name for path in DOCS_ROOT.glob("*.md")})

    def test_document_metadata_and_hashes(self):
        for document in self.documents:
            with self.subTest(document=document["id"]):
                self.assertEqual(document["license"], "GPL-3.0-or-later")
                self.assertEqual(document["authored_by"], "MSX-AI project")
                self.assertEqual(document["origin"], "project-authored")
                self.assertEqual(
                    document["provenance"]["origin"], "project-authored")
                self.assertEqual(document["reviewed_at"], "2026-08-10")
                self.assertTrue(document["title"])
                self.assertTrue(document["summary"])
                self.assertTrue(document["tags"])
                self.assertTrue(document["audience"])
                self.assertTrue(document["backends"])
                self.assertTrue(document["provenance"]["evidence"])

                target = _safe_target(DOCS_ROOT, document["path"])
                self.assertTrue(target.is_file())
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                self.assertEqual(document["sha256"], digest)

    def test_all_manifest_paths_are_safe_existing_repository_files(self):
        for document in self.documents:
            with self.subTest(document=document["id"]):
                _safe_target(DOCS_ROOT, document["path"])
                for evidence in document["provenance"]["evidence"]:
                    target = _safe_target(REPOSITORY_ROOT, evidence)
                    self.assertTrue(target.is_file(), evidence)

    def test_external_urls_and_origins_must_be_declared(self):
        for document in self.documents:
            with self.subTest(document=document["id"]):
                body = _safe_target(
                    DOCS_ROOT, document["path"]).read_text(encoding="utf-8")
                found_urls = set(URL_PATTERN.findall(body))
                references = document.get("external_references", [])
                declared_urls = {reference["url"] for reference in references}
                self.assertEqual(found_urls, declared_urls)

                origin = document["provenance"]["origin"]
                if origin != "project-authored":
                    self.assertTrue(
                        references,
                        "an external origin requires a declared reference")
                for reference in references:
                    self.assertTrue(reference["origin"])
                    self.assertTrue(reference["purpose"])
                    self.assertTrue(reference["license_status"])


if __name__ == "__main__":
    unittest.main()
