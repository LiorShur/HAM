from pathlib import Path

from wcm.parsing import is_archive_url, parse_article, split_sentences

FIXTURE = Path(__file__).parent / "fixtures" / "parsoid_sample.html"


def parsed():
    return parse_article(FIXTURE.read_text())


def test_citations_extracted():
    cites = parsed().citations
    built = cites["cite_note-built-1"]
    assert built.urls == ["https://example.org/lighthouses/harbor-point"]
    assert built.archive_url.startswith("https://web.archive.org/")
    assert built.access_date == "5 May 2020"

    book = cites["cite_note-2"]
    assert book.urls == []
    assert "Lights of the Coast" in book.text

    assert cites["cite_note-board-3"].quote == "The Board approved the station in 1868."
    # Protocol-relative URLs are normalised.
    assert cites["cite_note-4"].urls == ["https://museum.example.net/collection.pdf"]


def test_claims_attach_to_the_right_sentences():
    claims = parsed().claims
    by_text = {c.text: c for c in claims}

    lead = by_text["It was completed in 1871 and first lit on 1 May of that year."]
    assert lead.note_ids == ["cite_note-built-1"]
    assert lead.section == "(lead)"
    assert lead.context_before.startswith("Harbor Point Lighthouse is a lighthouse")

    assert by_text["The tower is 24 metres tall."].note_ids == ["cite_note-2"]

    lantern = by_text["Dr. A. Smith, the board's engineer, designed the lantern room."]
    # Explanatory note [a] is not a source and is dropped.
    assert lantern.note_ids == ["cite_note-board-3"]
    assert lantern.section == "History"
    assert "approved by the U.S. Lighthouse Board in 1868" in lantern.context_before

    # Mid-sentence footnote attaches to its sentence; [citation needed] text is removed.
    automated = next(c for c in claims if c.text.startswith("The light was automated"))
    assert automated.note_ids == ["cite_note-built-1"]
    assert "citation needed" not in automated.text

    assert by_text["The original Fresnel lens is on display in the museum."].note_ids == [
        "cite_note-4"
    ]


def test_infobox_is_skipped():
    assert not any(c.text.startswith("Built 1871") for c in parsed().claims)


def test_sentence_splitter_handles_abbreviations():
    text = "Dr. Smith arrived c. 1900 in the U.S. He left. Then approx. three came."
    sentences = [text[s:e] for s, e in split_sentences(text)]
    # "U.S." is treated as an abbreviation, so a sentence ending in it merges with
    # the next one. Over-merging is safer than over-splitting: the judge still sees
    # the full text.
    assert sentences == [
        "Dr. Smith arrived c. 1900 in the U.S. He left.",
        "Then approx. three came.",
    ]


def test_archive_detection():
    assert is_archive_url("https://web.archive.org/web/2020/https://x.org")
    assert is_archive_url("https://archive.ph/abc")
    assert not is_archive_url("https://example.org/archive/page")


def test_citation_text_has_no_stray_spaces():
    text = parsed().citations["cite_note-built-1"].text
    assert text.startswith('"Harbor Point Light". Example Lighthouse Society.')
