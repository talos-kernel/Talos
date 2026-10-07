#!/usr/bin/env python3
"""Check the public FAQ's search metadata and crawlable paths without network access."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
ORIGIN = "https://talos-agent.ch"


class Page(HTMLParser):
    def __init__(self, path: Path):
        super().__init__(convert_charrefs=True)
        self.nodes: list[tuple[str, dict]] = []
        self.ids: list[str] = []
        self.schemas: list[dict] = []
        self.schema_text: str | None = None
        self.title = ""
        self.in_title = False
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        self.nodes.append((tag, fields))
        if "id" in fields:
            self.ids.append(fields["id"])
        if tag == "title":
            self.in_title = True
        if tag == "script" and fields.get("type") == "application/ld+json":
            self.schema_text = ""

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.schema_text is not None:
            self.schema_text += data

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.schema_text is not None:
            self.schemas.append(json.loads(self.schema_text))
            self.schema_text = None

    def attrs(self, tag):
        return [fields for name, fields in self.nodes if name == tag]


def check() -> None:
    faq = Page(SITE / "faq/index.html")
    assert len(faq.attrs("h1")) == 1, "FAQ must have one H1"
    assert faq.attrs("html")[0].get("lang") == "en"
    assert 25 <= len(faq.title) <= 65 and "Talos" in faq.title and "FAQ" in faq.title
    assert len(faq.ids) == len(set(faq.ids)), "Duplicate FAQ ids"
    assert len(faq.attrs("details")) >= 15, "Expected substantive question coverage"
    assert len(faq.attrs("summary")) == len(faq.attrs("details"))
    metadata = {item.get("name", item.get("property")): item.get("content", "")
                for item in faq.attrs("meta")}
    assert 80 <= len(metadata["description"]) <= 170
    assert "noindex" not in metadata.get("robots", "")
    assert metadata["og:url"] == ORIGIN + "/faq/"
    assert metadata["twitter:card"] == "summary_large_image"
    assert metadata["og:image"].startswith(ORIGIN + "/")
    canonical = [item["href"] for item in faq.attrs("link") if item.get("rel") == "canonical"]
    assert canonical == [ORIGIN + "/faq/"]
    graph = faq.schemas[0]["@graph"]
    assert graph[0]["url"] == canonical[0]
    assert graph[1]["@type"] == "BreadcrumbList"
    assert graph[1]["itemListElement"][-1]["item"] == canonical[0]
    assert not faq.attrs("form") and not faq.attrs("iframe"), "FAQ must stay static"

    # Resolve actual local files and fragments, not just strings that look like URLs.
    for tag, fields in faq.nodes:
        target = fields.get("href") if tag in ("a", "link") else fields.get("src")
        if not target:
            continue
        url = urlsplit(target)
        if url.netloc and url.netloc != "talos-agent.ch":
            continue
        assert not url.scheme or url.scheme == "https", target
        path = url.path or "/faq/"
        assert path.startswith("/"), target
        local = SITE / unquote(path).lstrip("/")
        if local.is_dir():
            local /= "index.html"
        assert local.is_file(), f"Missing link target: {target}"
        if url.fragment:
            assert unquote(url.fragment) in Page(local).ids, f"Missing fragment: {target}"

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [node.text for node in ElementTree.parse(SITE / "sitemap.xml").findall("s:url/s:loc", ns)]
    assert locations.count(ORIGIN + "/faq/") == 1
    for name in ("index.html", "docs/index.html"):
        page = Page(SITE / name)
        assert any(link.get("href") == "/faq/" for link in page.attrs("a")), name
    assert ORIGIN + "/faq/" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert ORIGIN + "/faq/" in (SITE / "llms.txt").read_text(encoding="utf-8")
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: " + ORIGIN + "/sitemap.xml" in robots
    assert "Disallow: /faq" not in robots
    print(f"Site discovery checks passed: metadata, schema, {len(faq.attrs('details'))} FAQ entries, assets, fragments and discovery links.")


if __name__ == "__main__":
    check()
