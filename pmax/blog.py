"""Blogpagina's van de klant scrapen als bron voor PMax-assets.

Alleen standaardbibliotheek + Pillow. De scraper haalt op wat verifieerbaar is
(titel, intro, tussenkoppen, tekst, contentbeelden, resolutie). Thema's kiezen
en copy schrijven blijft werk voor de agent, net als bij de nieuwsbrief.
"""

from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from PIL import Image

from .specs import NEWSLETTER_SOURCE_MIN_SHORT_SIDE_PX

USER_AGENT = "Mozilla/5.0 (compatible; pmax-pipeline/0.3)"
TIMEOUT = 20

# Beelden die nooit contentbeeld zijn.
_SKIP_IMG = re.compile(
    r"(logo|icon|favicon|avatar|gravatar|sprite|spacer|pixel|badge|emoji|"
    r"placeholder|loader|social|share|facebook|twitter|linkedin|instagram|"
    r"whatsapp|pinterest|youtube|author)",
    re.I,
)
# Links op een blogoverzicht die geen artikel zijn.
_SKIP_LINK = re.compile(
    r"/(page|tag|tags|category|categorie|categorieen|author|auteur|feed|search|zoeken)(/|$)|"
    r"[?&](page|p|s)=|\.(xml|rss|jpg|jpeg|png|gif|webp|pdf)$",
    re.I,
)
_BLOCK_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "figcaption"}
_IGNORE_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}


def fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str) -> str:
    raw = fetch(url)
    return raw.decode("utf-8", errors="replace")


# --- HTML-parsing -----------------------------------------------------------

def _best_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0.0
        if len(bits) > 1:
            m = re.match(r"([\d.]+)[wx]", bits[1])
            w = float(m.group(1)) if m else 0.0
        if w > best_w:
            best, best_w = bits[0], w
    return best


class _PageParser(HTMLParser):
    """Eén pass: meta, links, en tekst/beelden binnen <article> of <main>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.links: list[str] = []
        self.blocks: list[tuple[str, str, bool]] = []  # (tag, tekst, in_content)
        self.images: list[tuple[dict, bool]] = []  # (attrs, in_content)
        self.times: list[str] = []
        self._in_title = False
        self._ignore = 0
        self._content = 0  # diepte binnen article/main
        self._stack: list[str] = []
        self._block: list[str] | None = None
        self._block_tag = ""

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content") and key.lower() not in self.meta:
                self.meta[key.lower()] = a["content"].strip()
            return
        if tag in ("img", "source", "br", "link", "input", "hr"):
            if tag in ("img", "source") and not self._ignore:
                self.images.append((a, self._content > 0))
            return
        self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        elif tag in _IGNORE_TAGS:
            self._ignore += 1
        elif tag in ("article", "main"):
            self._content += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag in _BLOCK_TAGS and self._block is None and not self._ignore:
            self._block, self._block_tag = [], tag

    def handle_endtag(self, tag):
        if tag not in self._stack:
            return
        while self._stack:
            t = self._stack.pop()
            self._close(t)
            if t == tag:
                break

    def _close(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _IGNORE_TAGS:
            self._ignore = max(0, self._ignore - 1)
        elif tag in ("article", "main"):
            self._content = max(0, self._content - 1)
        if tag == self._block_tag and self._block is not None:
            text = re.sub(r"\s+", " ", "".join(self._block)).strip()
            if text:
                self.blocks.append((tag, text, self._content > 0))
            self._block, self._block_tag = None, ""

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._block is not None and not self._ignore:
            self._block.append(data)


def _img_url(attrs: dict, page_url: str) -> str | None:
    for key in ("data-srcset", "srcset"):
        if attrs.get(key):
            best = _best_from_srcset(attrs[key])
            if best:
                return urljoin(page_url, best)
    for key in ("data-src", "data-lazy-src", "data-original", "src"):
        src = attrs.get(key, "").strip()
        if src and not src.startswith("data:"):
            return urljoin(page_url, src)
    return None


def _usable_image(url: str, attrs: dict) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(".svg") or _SKIP_IMG.search(path) or _SKIP_IMG.search(attrs.get("class", "")):
        return False
    for dim in ("width", "height"):
        val = attrs.get(dim, "")
        if val.isdigit() and int(val) < 200:
            return False
    return True


@dataclass
class BlogPost:
    url: str
    title: str
    description: str = ""
    published: str = ""
    headings: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)  # na download: pad + resolutie

    @property
    def slug(self) -> str:
        last = urlparse(self.url).path.rstrip("/").rsplit("/", 1)[-1] or "post"
        return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-")[:60] or "post"


def parse_post(html: str, url: str) -> BlogPost:
    p = _PageParser()
    p.feed(html)
    has_content = any(c for _, _, c in p.blocks)
    blocks = [(t, x) for t, x, c in p.blocks if c or not has_content]

    title = p.meta.get("og:title") or next((x for t, x in blocks if t == "h1"), "") or p.title
    description = p.meta.get("og:description") or p.meta.get("description", "")
    published = (
        p.meta.get("article:published_time")
        or p.meta.get("datepublished")
        or (p.times[0] if p.times else "")
    )

    images: list[str] = []
    if p.meta.get("og:image"):
        images.append(urljoin(url, p.meta["og:image"]))
    has_content_imgs = any(c for _, c in p.images)
    for attrs, in_content in p.images:
        if has_content_imgs and not in_content:
            continue
        src = _img_url(attrs, url)
        if src and _usable_image(src, attrs) and src not in images:
            images.append(src)

    return BlogPost(
        url=url,
        title=title.strip(),
        description=description.strip(),
        published=published.strip(),
        headings=[x for t, x in blocks if t in ("h2", "h3")],
        paragraphs=[x for t, x in blocks if t in ("p", "li", "blockquote") and len(x) > 40],
        image_urls=images,
    )


# --- Artikelen vinden -------------------------------------------------------

def discover_from_index(html: str, index_url: str) -> list[str]:
    """Artikel-URL's op een blogoverzicht: zelfde host, onder het blogpad."""
    p = _PageParser()
    p.feed(html)
    base = urlparse(index_url)
    prefix = base.path.rstrip("/") + "/"
    seen: list[str] = []
    for href in p.links:
        full, _ = urldefrag(urljoin(index_url, href))
        u = urlparse(full)
        if u.netloc != base.netloc or _SKIP_LINK.search(full):
            continue
        path = u.path
        if not path.startswith(prefix) or path.rstrip("/") == base.path.rstrip("/"):
            continue
        if full not in seen:
            seen.append(full)
    return seen


def discover_from_sitemap(xml: str, path_prefix: str = "") -> list[str]:
    """URL's uit een sitemap, nieuwste lastmod eerst. Sitemap-index wordt niet gevolgd."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    rows = []
    for node in root.findall("s:url", ns):
        loc = (node.findtext("s:loc", default="", namespaces=ns) or "").strip()
        mod = (node.findtext("s:lastmod", default="", namespaces=ns) or "").strip()
        if loc and (not path_prefix or urlparse(loc).path.startswith(path_prefix)):
            rows.append((mod, loc))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [loc for _, loc in rows]


# --- Beelden downloaden -----------------------------------------------------

def download_images(post: BlogPost, dest: Path, max_images: int = 6) -> list[dict]:
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for url in post.image_urls:
        if len(saved) >= max_images:
            break
        try:
            raw = fetch(url)
            img = Image.open(BytesIO(raw))
            img.load()
        except Exception as exc:  # netwerk of geen beeld: overslaan, wel melden
            saved.append({"url": url, "error": str(exc)})
            continue
        w, h = img.size
        if min(w, h) < 200:
            continue
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}.get(img.format or "", ".jpg")
        n = sum(1 for s in saved if "path" in s) + 1
        path = dest / f"{post.slug}-{n:02d}{ext}"
        path.write_bytes(raw)
        saved.append({
            "url": url,
            "path": str(path),
            "width": w,
            "height": h,
            "needs_upscale": min(w, h) < NEWSLETTER_SOURCE_MIN_SHORT_SIDE_PX,
        })
    post.images = saved
    return saved


# --- Rapport voor de agent --------------------------------------------------

def write_report(posts: list[BlogPost], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "posts.json"
    json_path.write_text(
        json.dumps([asdict(p) for p in posts], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Blogbronnen voor PMax", ""]
    for p in posts:
        lines += [f"## {p.title or p.url}", "", f"- URL: {p.url}"]
        if p.published:
            lines.append(f"- Datum: {p.published}")
        if p.description:
            lines.append(f"- Intro: {p.description}")
        if p.headings:
            lines.append("- Tussenkoppen: " + " | ".join(p.headings[:10]))
        for img in p.images:
            if "path" in img:
                flag = "  ⚠ upscale nodig" if img["needs_upscale"] else ""
                lines.append(f"- Beeld: {img['path']} ({img['width']}×{img['height']}){flag}")
            else:
                lines.append(f"- Beeld mislukt: {img['url']} ({img['error']})")
        if p.paragraphs:
            lines += ["", "> " + "\n> ".join(p.paragraphs[:3])]
        lines.append("")
    md_path = out_dir / "posts.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
