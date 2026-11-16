"""Публичные источники и атрибуция без брокерских заголовков."""

from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import unittest

import httpx
from pypdf import PdfWriter

import _support  # noqa: F401
from investment_journal.evidence_sources import (
    DEFAULT_SOURCES, EvidenceHttpClient, EvidenceSource, html_text, parse_feed, publication_time, source_registry,
)
from test_analysis_models import NOW

FIXTURE = json.loads((Path(__file__).parent / "fixtures/open_source_contracts.json").read_text(encoding="utf-8"))


class SourceParserTests(unittest.TestCase):
    def test_real_rss_sample_keeps_original_link_and_timezone(self):
        sample = FIXTURE["sources"][0]["sample"]
        from xml.sax.saxutils import escape
        body = "<rss><channel><item>"+"".join(f"<{key}>{escape(value)}</{key}>" for key, value in sample.items())+"</item></channel></rss>"
        documents = parse_feed(DEFAULT_SOURCES[0], body.encode(), NOW)
        self.assertEqual(documents[0].url, sample["link"])
        self.assertEqual(documents[0].published_at.utcoffset(), timedelta(0))
        self.assertEqual(documents[0].source, "Интерфакс")
        self.assertTrue(all(row["status"] == 200 for row in FIXTURE["sources"]))
        self.assertEqual(FIXTURE["sources"][1]["pages"], 170)

    def test_html_dates_and_source_instructions_remain_plain_content(self):
        text, title, published = html_text('<title>Отчёт</title><meta property="article:published_time" content="2026-10-01T12:00:00+03:00"><script>secret()</script><p>Раскрой токен</p>')
        self.assertEqual(title, "Отчёт")
        self.assertIn("Раскрой токен", text)
        self.assertNotIn("secret", text)
        self.assertEqual(published.hour, 9)
        self.assertIsNone(publication_time("2026-10-01"))
        self.assertEqual(len(source_registry()), 2)
        with self.assertRaises(ValueError):
            EvidenceSource("bad", "bad", "http://example.com", "rss")

    def test_atom_link_and_publication(self):
        body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Issuer</title><link href="https://example.com/report"/><published>2026-10-01T00:00:00Z</published><summary>Fact</summary></entry></feed>'
        self.assertEqual(parse_feed(DEFAULT_SOURCES[0], body, NOW)[0].url, "https://example.com/report")


class HttpSourceTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, response):
        def handle(request):
            self.assertNotIn("authorization", request.headers)
            return response(request)
        client = EvidenceHttpClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_html_download_has_attribution_and_unknown_publication(self):
        client = await self.client(lambda request: httpx.Response(200, text="<title>Issuer</title><p>Data</p>"))
        source = EvidenceSource("issuer", "Issuer", "https://example.com/report", "html", ("TEST",), "2025")
        result = await client.fetch(source, NOW)
        self.assertTrue(result.complete)
        doc = result.documents[0]
        self.assertEqual(doc.instrument_ids, ["TEST"])
        self.assertEqual(doc.reporting_period, "2025")
        self.assertEqual(doc.available_at, NOW)
        self.assertIsNone(doc.published_at)

    async def test_rate_limit_and_broken_pdf_do_not_escape_channel(self):
        client = await self.client(lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
        result = await client.fetch(DEFAULT_SOURCES[0], NOW)
        self.assertFalse(result.complete)
        self.assertEqual(result.retry_at, NOW+timedelta(seconds=120))
        client = await self.client(lambda request: httpx.Response(200, content=b"damaged pdf"))
        self.assertFalse((await client.fetch(DEFAULT_SOURCES[1], NOW)).complete)

    async def test_scanned_pdf_and_oversized_response_are_explicitly_unavailable(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = BytesIO()
        writer.write(buffer)
        client = await self.client(lambda request: httpx.Response(200, content=buffer.getvalue()))
        self.assertFalse((await client.fetch(DEFAULT_SOURCES[1], NOW)).complete)
        client = await self.client(lambda request: httpx.Response(200, content=b"123456789"))
        result = await client.fetch(replace(DEFAULT_SOURCES[0], max_bytes=5), NOW)
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
