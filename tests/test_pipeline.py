from pathlib import Path

import pytest
from PIL import Image

from pmax.images import fit_to_ratio, make_pmax_formats, overlay_logo
from pmax.render import render_handoff, write_review_csv
from pmax.validate import ERROR, has_errors, validate_campaign


def minimal_campaign(**overrides) -> dict:
    data = {
        "client": "Testklant",
        "campaign": "Test",
        "business_name": "Testklant",
        "logo": "brand/logo.png",
        "asset_groups": [
            {
                "name": "Thema",
                "final_url": "https://example.com/thema",
                "headlines": ["Een", "Twee", "Drie"],
                "long_headlines": ["Een lange headline"],
                "descriptions": ["Beschrijving een.", "Beschrijving twee."],
            }
        ],
    }
    data.update(overrides)
    return data


# --- tekstvalidatie -------------------------------------------------------

def test_minimal_campaign_passes():
    assert not has_errors(validate_campaign(minimal_campaign()))


def test_headline_over_30_chars_is_error():
    data = minimal_campaign()
    data["asset_groups"][0]["headlines"][0] = "x" * 31
    assert any(i.level == ERROR and "31 tekens" in i.message for i in validate_campaign(data))


def test_too_few_headlines_is_error():
    """De oude validator keek alleen naar maxlengte en miste dit volledig."""
    data = minimal_campaign()
    data["asset_groups"][0]["headlines"] = ["Een", "Twee"]
    assert any(i.level == ERROR and "minimaal 3" in i.message for i in validate_campaign(data))


def test_too_many_headlines_is_error():
    data = minimal_campaign()
    data["asset_groups"][0]["headlines"] = [f"H{i}" for i in range(16)]
    assert any(i.level == ERROR and "maximaal 15" in i.message for i in validate_campaign(data))


def test_single_description_is_error():
    data = minimal_campaign()
    data["asset_groups"][0]["descriptions"] = ["Slechts een."]
    assert any(i.level == ERROR and "minimaal 2" in i.message for i in validate_campaign(data))


def test_missing_business_name_is_error():
    data = minimal_campaign()
    del data["business_name"]
    assert any(i.field == "business_name" and i.level == ERROR for i in validate_campaign(data))


def test_business_name_over_25_chars_is_error():
    data = minimal_campaign(business_name="x" * 26)
    assert any(i.field == "business_name" and i.level == ERROR for i in validate_campaign(data))


def test_relative_landing_page_is_error():
    """Relatieve paden zonder client-resolve zijn ongeldig; PMax wil een absolute URL."""
    data = minimal_campaign()
    data["asset_groups"][0]["final_url"] = "/thema"
    assert any(i.field == "final_url" and i.level == ERROR for i in validate_campaign(data))


def test_duplicate_headline_is_warning():
    data = minimal_campaign()
    data["asset_groups"][0]["headlines"] = ["Een", "een", "Drie"]
    issues = validate_campaign(data)
    assert any(i.level == "warning" and "duplicaat" in i.message for i in issues)
    assert not has_errors(issues)


# --- beeldvalidatie -------------------------------------------------------

@pytest.fixture
def assets(tmp_path: Path) -> Path:
    (tmp_path / "brand").mkdir()
    Image.new("RGB", (1200, 1200), "white").save(tmp_path / "brand/logo.png")
    Image.new("RGB", (1200, 628), "blue").save(tmp_path / "landscape.png")
    Image.new("RGB", (1200, 1200), "blue").save(tmp_path / "square.png")
    Image.new("RGB", (800, 600), "red").save(tmp_path / "wrong_ratio.png")
    return tmp_path


def test_correct_images_pass(assets: Path):
    data = minimal_campaign()
    data["asset_groups"][0]["images"] = {
        "landscape": ["landscape.png"],
        "square": ["square.png"],
    }
    assert not has_errors(validate_campaign(data, asset_root=assets))


def test_wrong_aspect_ratio_is_error(assets: Path):
    data = minimal_campaign()
    data["asset_groups"][0]["images"] = {
        "landscape": ["wrong_ratio.png"],
        "square": ["square.png"],
    }
    issues = validate_campaign(data, asset_root=assets)
    assert any("ASPECT_RATIO_NOT_ALLOWED" in i.message for i in issues)


def test_missing_square_image_is_error(assets: Path):
    data = minimal_campaign()
    data["asset_groups"][0]["images"] = {"landscape": ["landscape.png"]}
    issues = validate_campaign(data, asset_root=assets)
    assert any(i.field == "square" and i.level == ERROR for i in issues)


# --- beeldpijplijn --------------------------------------------------------

def test_fit_to_ratio_returns_exact_dimensions():
    assert fit_to_ratio(Image.new("RGB", (3000, 1000)), 1200, 1200).size == (1200, 1200)


def test_fit_to_ratio_crops_from_center():
    img = Image.new("RGB", (300, 100), "black")
    img.putpixel((150, 50), (255, 0, 0))
    assert fit_to_ratio(img, 100, 100).getpixel((50, 50)) == (255, 0, 0)


def test_overlay_logo_runs_and_keeps_size():
    """Regressietest: de oude apply_logo_overlay.py gaf hier een TypeError."""
    base = Image.new("RGB", (1200, 628), "blue")
    logo = Image.new("RGBA", (300, 100), (255, 0, 0, 255))
    out = overlay_logo(base, logo)
    assert out.size == (1200, 628)
    assert out.mode == "RGB"


def test_make_pmax_formats_produces_all_ratios(assets: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    made = make_pmax_formats(assets / "landscape.png", out_dir, "thema", logo=assets / "brand/logo.png")
    assert set(made) == {"landscape", "square", "portrait"}
    for paths in made.values():
        assert len(paths) == 2  # met en zonder logo
        assert all(p.exists() for p in paths)


def test_generated_images_pass_validation(assets: Path, tmp_path: Path):
    """De output van de beeldpijplijn moet per definitie door de validator komen."""
    out_dir = tmp_path / "out"
    made = make_pmax_formats(assets / "landscape.png", out_dir, "thema")
    data = minimal_campaign()
    data["asset_groups"][0]["images"] = {
        slot: [str(p.relative_to(tmp_path)) for p in paths] for slot, paths in made.items()
    }
    data["logo"] = "brand/logo.png"
    (tmp_path / "brand").mkdir(exist_ok=True)
    Image.new("RGB", (1200, 1200), "white").save(tmp_path / "brand/logo.png")
    assert not has_errors(validate_campaign(data, asset_root=tmp_path))


# --- render ---------------------------------------------------------------

def test_handoff_mentions_disclaimer_and_all_groups():
    data = minimal_campaign()
    data["asset_groups"].append({**data["asset_groups"][0], "name": "Tweede"})
    text = render_handoff(data, drive_link="https://drive.example.com/x")
    assert "ter review" in text
    assert "Thema" in text and "Tweede" in text
    assert "https://drive.example.com/x" in text


def test_review_csv_has_row_per_text_asset(tmp_path: Path):
    out = write_review_csv(minimal_campaign(), tmp_path / "review.csv")
    rows = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1 + 3 + 1 + 2  # header + headlines + long + descriptions


def test_yaml_colon_trap_is_caught():
    """Regressie: in de originele copy.yaml werd een description met ': ' door
    YAML als dict geparsed. De oude len()-check zag dat niet."""
    data = minimal_campaign()
    data["asset_groups"][0]["descriptions"] = [
        {"Luxe of budget": "wij regelen het voor je."},
        "Een geldige beschrijving.",
    ]
    issues = validate_campaign(data)
    assert any(i.level == ERROR and "geen tekst maar dict" in i.message for i in issues)


# --- blog-scraper ---------------------------------------------------------

from pmax import blog as blogmod

POST_HTML = """<html><head><title>Fallback | Klant</title>
<meta property="og:title" content="Wandelen in de Dolomieten">
<meta name="description" content="Vijf routes voor beginners.">
<meta property="article:published_time" content="2026-10-01T08:00:00Z">
<meta property="og:image" content="/img/hero.jpg">
</head><body>
<header><img src="/img/logo.png"><nav><a href="/blog/">Blog</a></nav></header>
<main><article><h1>Wandelen in de Dolomieten</h1>
<p>Een inleiding die lang genoeg is om als echte alinea te tellen in de scraper.</p>
<h2>Route 1: Seceda</h2>
<p>De Seceda is een klassieker met uitzicht over de grillige pieken van de Odle.</p>
<img src="/img/seceda-small.jpg" srcset="/img/seceda-800.jpg 800w, /img/seceda-1600.jpg 1600w">
<img src="/img/icon-share.png" width="24" height="24">
<img data-src="/img/lago.jpg" src="data:image/gif;base64,R0lGOD">
</article></main>
<footer><p>Footertekst die we niet willen meenemen in de alinea's van dit artikel.</p></footer>
</body></html>"""


def test_parse_post_extracts_text_and_content_images():
    post = blogmod.parse_post(POST_HTML, "https://klant.nl/blog/dolomieten/")
    assert post.title == "Wandelen in de Dolomieten"
    assert post.description == "Vijf routes voor beginners."
    assert post.published.startswith("2026-10-01")
    assert post.headings == ["Route 1: Seceda"]
    assert len(post.paragraphs) == 2 and not any("Footer" in p for p in post.paragraphs)
    assert post.image_urls == [
        "https://klant.nl/img/hero.jpg",
        "https://klant.nl/img/seceda-1600.jpg",
        "https://klant.nl/img/lago.jpg",
    ]
    assert post.slug == "dolomieten"


def test_discover_from_index_keeps_only_articles():
    html = """<a href="/blog/a/">A</a><a href="/blog/b#x">B</a><a href="/blog/a/">A</a>
    <a href="/blog/">home</a><a href="/blog/page/2/">2</a><a href="/blog/tag/zon/">tag</a>
    <a href="https://ander.nl/blog/c/">ext</a><a href="/over-ons/">over</a>"""
    assert blogmod.discover_from_index(html, "https://klant.nl/blog/") == [
        "https://klant.nl/blog/a/",
        "https://klant.nl/blog/b",
    ]


def test_discover_from_sitemap_newest_first_and_filtered():
    xml = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://klant.nl/blog/oud</loc><lastmod>2025-01-01</lastmod></url>
    <url><loc>https://klant.nl/blog/nieuw</loc><lastmod>2026-10-01</lastmod></url>
    <url><loc>https://klant.nl/contact</loc><lastmod>2026-11-01</lastmod></url>
    </urlset>"""
    assert blogmod.discover_from_sitemap(xml, "/blog/") == [
        "https://klant.nl/blog/nieuw",
        "https://klant.nl/blog/oud",
    ]


def test_download_images_flags_upscale(tmp_path: Path, monkeypatch):
    from io import BytesIO

    def fake_fetch(url, timeout=0):
        size = (1600, 900) if "big" in url else (500, 400) if "mid" in url else (50, 50)
        buf = BytesIO()
        Image.new("RGB", size, "green").save(buf, "JPEG")
        return buf.getvalue()

    monkeypatch.setattr(blogmod, "fetch", fake_fetch)
    post = blogmod.BlogPost(
        url="https://klant.nl/blog/x", title="X",
        image_urls=["https://k/big.jpg", "https://k/mid.jpg", "https://k/tiny.jpg"],
    )
    saved = blogmod.download_images(post, tmp_path)
    assert [s["needs_upscale"] for s in saved] == [False, True]
    assert all(Path(s["path"]).exists() for s in saved)
    json_path, md_path = blogmod.write_report([post], tmp_path)
    assert "upscale nodig" in md_path.read_text(encoding="utf-8")
