import httpx
import respx

from wcm.sources import (
    SourceFetcher,
    find_quote,
    parse_access_date,
    raw_archive_url,
    select_passages,
)


def test_find_quote_is_tolerant_of_formatting():
    src = "The Board approved   the “station” in 1868.\nWork began in 1869."
    span = find_quote('the board approved the "station" in 1868.', src)
    assert span is not None
    assert src[span[0] : span[1]].startswith("The Board")


def test_find_quote_with_ellipsis_requires_order():
    src = "Alpha beta gamma. Delta epsilon zeta."
    assert find_quote("Alpha beta ... epsilon zeta", src)
    assert find_quote("epsilon zeta … Alpha beta", src) is None


def test_find_quote_rejects_paraphrase():
    assert (
        find_quote("The board approved it in 1868", "The Board approved the station in 1868.")
        is None
    )


def test_select_passages_keeps_relevant_chunks():
    filler = "lorem ipsum dolor " * 500
    text = filler + " The lighthouse was automated in 1972. " + filler
    out = select_passages(text, ["The light was automated in 1965"], max_chars=4000, chunk=1000)
    assert "automated in 1972" in out
    assert len(out) <= 4000 + 20


def test_raw_archive_url():
    assert (
        raw_archive_url("https://web.archive.org/web/20200101000000/https://x.org/a")
        == "https://web.archive.org/web/20200101000000id_/https://x.org/a"
    )
    assert raw_archive_url("https://x.org/a") == "https://x.org/a"


def test_parse_access_date():
    assert parse_access_date("5 May 2020").year == 2020
    assert parse_access_date("May 5, 2020").day == 5
    assert parse_access_date("sometime") is None


@respx.mock
def test_fetch_html_and_errors():
    respx.get("https://ok.example/a").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>T</title></head><body><article><p>"
            + "The station was approved in 1868. " * 20
            + "</p></article></body></html>",
        )
    )
    respx.get("https://gone.example/b").mock(return_value=httpx.Response(404))
    f = SourceFetcher(min_interval=0)
    ok = f.fetch("https://ok.example/a")
    assert ok.status == "ok" and "approved in 1868" in ok.text and ok.title == "T"
    assert ok.sha256
    gone = f.fetch("https://gone.example/b")
    assert gone.status == "http_error" and gone.http_status == 404


@respx.mock
def test_find_archive():
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(
            200,
            json={
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "url": "http://web.archive.org/web/2020/https://x.org",
                    }
                }
            },
        )
    )
    assert SourceFetcher(min_interval=0).find_archive("https://x.org", None).endswith("x.org")


def test_corrupt_pdf_yields_no_text_instead_of_raising():
    from wcm.sources import _pdf_text

    assert _pdf_text(b"%PDF-1.4 garbage that is not a real pdf") == ("", None)
    # Valid header and xref but a broken /Pages tree: the case seen in practice,
    # where the error only appears when the pages are counted.
    broken = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj 42 endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )
    assert _pdf_text(broken)[0] == ""
