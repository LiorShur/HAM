"""Turn Parsoid HTML for one article revision into claims and the citations behind them.

A cited claim is a sentence that carries at least one footnote. Footnotes usually
sit at the end of the text they support, and one footnote often covers several
preceding sentences, so uncited sentences before a cited one are treated as covered
by it and kept as `context_before`.

An uncited claim is a sentence that no footnote can be covering: one after the last
footnote in its paragraph, or in a paragraph with no footnotes at all.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

ARCHIVE_HOSTS = (
    "web.archive.org",
    "archive.org",
    "archive.today",
    "archive.ph",
    "archive.is",
    "webcitation.org",
    "ghostarchive.org",
)

# Words that end in a period without ending a sentence.
ABBREVIATIONS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sr",
    "jr",
    "st",
    "gen",
    "col",
    "lt",
    "capt",
    "sgt",
    "rev",
    "hon",
    "no",
    "vol",
    "pp",
    "p",
    "ed",
    "eds",
    "c",
    "ca",
    "cf",
    "e.g",
    "i.e",
    "etc",
    "vs",
    "al",
    "approx",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
    "u.s",
    "u.k",
    "u.n",
    "inc",
    "ltd",
    "co",
    "corp",
    "mt",
    "ft",
}

SKIP_CONTAINERS = ("table", "figure", "style", "script", "math")
SKIP_CLASSES = {
    "navbox",
    "reflist",
    "mw-references",
    "hatnote",
    "infobox",
    "sidebar",
    "metadata",
    "mw-empty-elt",
    "noprint",
}

_RETRIEVED_RE = re.compile(r"Retrieved\s+(?:on\s+)?([^.]+?)\.", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*(?=\s+\S)")


@dataclass
class ParsedCitation:
    note_id: str
    text: str
    urls: list[str] = field(default_factory=list)
    archive_url: str | None = None
    access_date: str | None = None
    quote: str | None = None


@dataclass
class ParsedClaim:
    section: str
    paragraph_index: int
    sentence_index: int
    text: str
    context_before: str
    note_ids: list[str]
    # Wikipedia already marks it with {{citation needed}}.
    citation_needed: bool = False

    @property
    def cited(self) -> bool:
        return bool(self.note_ids)


@dataclass
class ParsedArticle:
    citations: dict[str, ParsedCitation]
    claims: list[ParsedClaim]


def is_archive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ARCHIVE_HOSTS)


def _classes(tag: Tag) -> set[str]:
    return set(tag.get("class") or [])


def _is_skipped(tag: Tag) -> bool:
    for parent in [tag, *tag.parents]:
        if not isinstance(parent, Tag):
            continue
        if parent.name in SKIP_CONTAINERS or _classes(parent) & SKIP_CLASSES:
            return True
    return False


def _note_id_from_ref(sup: Tag) -> str | None:
    """Return the footnote id a <sup class="mw-ref"> points to, or None for
    grouped explanatory notes ({{efn}} etc.), which aren't sources."""
    data_mw = sup.get("data-mw")
    if data_mw:
        try:
            group = json.loads(data_mw).get("attrs", {}).get("group")
        except (ValueError, AttributeError):
            group = None
        if group:
            return None
    link = sup.find("a", href=True)
    if not link:
        return None
    _, _, fragment = link["href"].partition("#")
    return unquote(fragment) or None


def parse_citations(soup: BeautifulSoup) -> dict[str, ParsedCitation]:
    citations: dict[str, ParsedCitation] = {}
    for ol in soup.select("ol.mw-references"):
        for li in ol.find_all("li", id=True, recursive=False):
            body = li.select_one(".mw-reference-text") or li
            urls: list[str] = []
            archive_url = None
            for a in body.select("a.external[href], a[rel~='mw:ExtLink'][href]"):
                href = a["href"]
                if href.startswith("//"):
                    href = "https:" + href
                if not href.startswith(("http://", "https://")):
                    continue
                if is_archive_url(href):
                    archive_url = archive_url or href
                elif href not in urls:
                    urls.append(href)
            text = " ".join(body.get_text().split())
            retrieved = _RETRIEVED_RE.search(text)
            quote_tag = body.select_one("cite q, q")
            citations[li["id"]] = ParsedCitation(
                note_id=li["id"],
                text=text,
                urls=urls,
                archive_url=archive_url,
                access_date=retrieved.group(1).strip() if retrieved else None,
                quote=quote_tag.get_text(" ", strip=True) if quote_tag else None,
            )
    return citations


def _is_citation_needed(tag: Tag) -> bool:
    return "Template-Fact" in _classes(tag) or "citation needed" in tag.get_text().lower()


def _flatten(node: Tag) -> tuple[str, list[tuple[int, str]], list[int]]:
    """Plain text of a block, (offset, note_id) for each footnote marker, and the
    offsets of {{citation needed}} tags."""
    parts: list[str] = []
    markers: list[tuple[int, str]] = []
    needed: list[int] = []
    length = 0

    def walk(n: Tag) -> None:
        nonlocal length
        for child in n.children:
            if isinstance(child, NavigableString):
                s = str(child)
                parts.append(s)
                length += len(s)
            elif isinstance(child, Tag):
                classes = _classes(child)
                if child.name == "sup" and "mw-ref" in classes:
                    note_id = _note_id_from_ref(child)
                    if note_id:
                        markers.append((length, note_id))
                    continue
                if child.name in ("sup", "span") and _is_citation_needed(child):
                    needed.append(length)
                    continue
                if child.name in ("style", "script") or classes & {"noprint", "mw-empty-elt"}:
                    continue
                walk(child)

    walk(node)
    return "".join(parts), markers, needed


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences in `text`."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        end = m.end()
        word = re.search(r"([\w.]+)[.!?][\"'”’)\]]*$", text[start:end])
        if word:
            w = word.group(1).lower()
            if w in ABBREVIATIONS or (len(w) == 1 and w.isalpha()):
                continue
        # Don't split before a lowercase continuation ("approx. three").
        nxt = text[end:].lstrip()[:1]
        if nxt.islower():
            continue
        spans.append((start, end))
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _blocks(soup: BeautifulSoup):
    """Yield (section heading, block tag) for article prose paragraphs and list items."""
    body = soup.body or soup
    section = "(lead)"
    for el in body.find_all(["h2", "h3", "h4", "p", "li"]):
        if el.name in ("h2", "h3", "h4"):
            section = el.get_text(" ", strip=True)
            continue
        if _is_skipped(el):
            continue
        if el.name == "li" and (
            el.find_parent("ol", class_="mw-references") or el.find(["p", "li"])
        ):
            continue
        yield section, el


# Sections that list links or sources rather than making claims.
SKIP_SECTIONS = {
    "see also",
    "references",
    "notes",
    "footnotes",
    "citations",
    "sources",
    "bibliography",
    "works cited",
    "further reading",
    "external links",
}


def _sentence_at(spans: list[tuple[int, int]], offset: int) -> int:
    """The sentence a marker at `offset` belongs to (markers follow their text)."""
    idx = 0
    for i, (s, _) in enumerate(spans):
        if s < offset:
            idx = i
    return idx


def _is_statement(sentence: str) -> bool:
    """Skip fragments: very short text and lines that only introduce a list."""
    return len(sentence) >= 25 and not sentence.endswith(":")


def parse_article(html: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "lxml")
    citations = parse_citations(soup)
    claims: list[ParsedClaim] = []

    for paragraph_index, (section, block) in enumerate(_blocks(soup)):
        if section.strip().lower() in SKIP_SECTIONS:
            continue
        text, markers, needed = _flatten(block)
        if not text.strip():
            continue
        spans = split_sentences(text)
        notes_by_sentence: dict[int, list[str]] = {}
        for offset, note_id in markers:
            ids = notes_by_sentence.setdefault(_sentence_at(spans, offset), [])
            if note_id not in ids:
                ids.append(note_id)
        tagged = {_sentence_at(spans, offset) for offset in needed}
        last_cited = max(notes_by_sentence, default=-1)

        pending: list[str] = []  # uncited sentences since the last cited one
        previous: list[str] = []  # every sentence so far, for context
        for i, (s, e) in enumerate(spans):
            sentence = " ".join(text[s:e].split())
            if i in notes_by_sentence:
                claims.append(
                    ParsedClaim(
                        section=section,
                        paragraph_index=paragraph_index,
                        sentence_index=i,
                        text=sentence,
                        context_before=" ".join(pending),
                        note_ids=notes_by_sentence[i],
                        citation_needed=i in tagged,
                    )
                )
                pending = []
            elif i > last_cited and _is_statement(sentence):
                claims.append(
                    ParsedClaim(
                        section=section,
                        paragraph_index=paragraph_index,
                        sentence_index=i,
                        text=sentence,
                        context_before=" ".join(previous[-2:]),
                        note_ids=[],
                        citation_needed=i in tagged,
                    )
                )
            else:
                pending.append(sentence)
            previous.append(sentence)

    return ParsedArticle(citations=citations, claims=claims)
