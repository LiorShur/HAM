"""Fetching cited sources and working with their text."""

import hashlib
import io
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader

from wcm.config import get_settings


@dataclass
class FetchResult:
    requested_url: str
    status: str  # ok | thin | http_error | network_error | unsupported_type | too_large | empty
    text: str = ""
    final_url: str | None = None
    via_archive: bool = False
    http_status: int | None = None
    content_type: str | None = None
    title: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "thin")

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.text.encode()).hexdigest() if self.text else None


_PAYWALL_HINTS = (
    "subscribe to continue",
    "subscribers only",
    "sign in to read",
    "create a free account",
    "to continue reading",
    "paywall",
)
_WAYBACK_TS_RE = re.compile(r"(https?://web\.archive\.org/web/)(\d{1,14})(?:[a-z]{2}_)?/(.+)")


def raw_archive_url(url: str) -> str:
    """Wayback URL without the toolbar/link rewriting (the `id_` form)."""
    m = _WAYBACK_TS_RE.match(url)
    return f"{m.group(1)}{m.group(2)}id_/{m.group(3)}" if m else url


def parse_access_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


class SourceFetcher:
    def __init__(self, client: httpx.Client | None = None, min_interval: float = 1.0):
        self.settings = get_settings()
        self.http = client or httpx.Client(
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.wcm_fetch_timeout,
            follow_redirects=True,
        )
        self.min_interval = min_interval
        self._last_hit: dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        wait = self._last_hit.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def fetch(self, url: str, via_archive: bool = False) -> FetchResult:
        target = raw_archive_url(url) if via_archive else url
        self._throttle(target)
        try:
            with self.http.stream("GET", target) as resp:
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if resp.status_code >= 400:
                    return FetchResult(
                        url,
                        "http_error",
                        http_status=resp.status_code,
                        content_type=ctype,
                        via_archive=via_archive,
                        final_url=str(resp.url),
                    )
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.wcm_fetch_max_bytes:
                        return FetchResult(
                            url,
                            "too_large",
                            http_status=resp.status_code,
                            content_type=ctype,
                            via_archive=via_archive,
                        )
                final_url = str(resp.url)
                status_code = resp.status_code
                encoding = resp.encoding
        except httpx.HTTPError as exc:
            return FetchResult(url, "network_error", error=str(exc), via_archive=via_archive)

        result = FetchResult(
            url,
            "ok",
            final_url=final_url,
            via_archive=via_archive,
            http_status=status_code,
            content_type=ctype,
        )
        data = bytes(body)
        if ctype == "application/pdf" or data[:5] == b"%PDF-":
            result.text, result.title = _pdf_text(data)
        elif ctype in ("text/html", "application/xhtml+xml", "") or data.lstrip()[:1] == b"<":
            result.text, result.title = _html_text(data.decode(encoding or "utf-8", "replace"))
        elif ctype.startswith("text/"):
            result.text = data.decode(encoding or "utf-8", "replace")
        else:
            result.status = "unsupported_type"
            return result

        result.text = result.text.strip()
        if not result.text:
            result.status = "empty"
        elif len(result.text) < 1500 and any(h in result.text.lower() for h in _PAYWALL_HINTS):
            result.status = "thin"
        return result

    def find_archive(self, url: str, near: datetime | None) -> str | None:
        """Closest Wayback Machine capture of `url` to `near` (e.g. the citation's access date)."""
        params = {"url": url}
        if near:
            params["timestamp"] = near.strftime("%Y%m%d")
        try:
            resp = self.http.get("https://archive.org/wayback/available", params=params)
            resp.raise_for_status()
            closest = resp.json().get("archived_snapshots", {}).get("closest") or {}
        except (httpx.HTTPError, ValueError):
            return None
        return closest.get("url") if closest.get("available") else None


def _html_text(html: str) -> tuple[str, str | None]:
    text = (
        trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True)
        or ""
    )
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    if not text:
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    return text, title


def _pdf_text(data: bytes) -> tuple[str, str | None]:
    # Malformed PDFs raise a wide range of exceptions from deep inside pypdf.
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return "", None
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            pages.append(f"[page {i}]\n{page.extract_text() or ''}")
        except Exception:  # noqa: BLE001
            pages.append(f"[page {i}]\n")
    title = reader.metadata.title if reader.metadata else None
    return "\n\n".join(pages), title


# --- Quote verification -----------------------------------------------------------

_QUOTE_CHARS = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "–": "-",
        "—": "-",
        "‐": "-",
        " ": " ",
    }
)
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|…|\[\s*\.\.\.\s*\])\s*")


def _normalise_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercased, whitespace-collapsed text plus a map from each output char back
    to its index in the input."""
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_QUOTE_CHARS).lower():
            if c.isspace():
                if prev_space:
                    continue
                c = " "
                prev_space = True
            else:
                prev_space = False
            out.append(c)
            index.append(i)
    return "".join(out), index


def find_quote(quote: str, source_text: str) -> tuple[int, int] | None:
    """Locate `quote` in `source_text`, tolerant of whitespace, case, typographic
    quotes and ellipses. Returns (start, end) in the original text, or None."""
    norm_src, index = _normalise_with_map(source_text)
    segments = [s for s in _ELLIPSIS_RE.split(quote.strip().strip('"')) if s.strip()]
    if not segments:
        return None
    pos = 0
    first_start = last_end = None
    for seg in segments:
        norm_seg, _ = _normalise_with_map(seg)
        norm_seg = norm_seg.strip()
        found = norm_src.find(norm_seg, pos)
        if found < 0:
            return None
        if first_start is None:
            first_start = found
        last_end = found + len(norm_seg)
        pos = last_end
    return index[first_start], index[last_end - 1] + 1


def excerpt(text: str, start: int, end: int, context: int) -> str:
    s, e = max(0, start - context), min(len(text), end + context)
    return ("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "")


# --- Long sources ---------------------------------------------------------------

_WORD_RE = re.compile(r"\w{3,}", re.UNICODE)


def select_passages(text: str, queries: list[str], max_chars: int, chunk: int = 2000) -> str:
    """If `text` is too long to send whole, keep the chunks that share the most
    words with the claims, in document order."""
    if len(text) <= max_chars:
        return text
    terms = {w.lower() for q in queries for w in _WORD_RE.findall(q)}
    chunks = [text[i : i + chunk] for i in range(0, len(text), chunk)]
    scored = sorted(
        range(len(chunks)),
        key=lambda i: -len(terms & {w.lower() for w in _WORD_RE.findall(chunks[i])}),
    )
    keep: set[int] = set()
    budget = max_chars
    for i in scored:
        if budget < len(chunks[i]):
            break
        keep.add(i)
        budget -= len(chunks[i])
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("\n[…]\n")
        out.append(chunks[i])
        prev = i
    return "".join(out)
