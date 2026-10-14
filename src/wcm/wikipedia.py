"""Read-only Wikipedia access. This tool never edits Wikipedia."""

from dataclasses import dataclass
from datetime import datetime

import httpx

from wcm.config import get_settings


class WikipediaError(RuntimeError):
    pass


@dataclass
class RevisionInfo:
    title: str
    rev_id: int
    timestamp: datetime | None


class WikipediaClient:
    def __init__(self, lang: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.lang = lang or settings.wiki_lang
        self.base = f"https://{self.lang}.wikipedia.org"
        self.http = client or httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.wcm_fetch_timeout,
            follow_redirects=True,
        )

    def revision_info(self, title: str | None = None, rev_id: int | None = None) -> RevisionInfo:
        """Latest revision of `title`, or the metadata for a specific `rev_id`."""
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "ids|timestamp",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        }
        if rev_id is not None:
            params["revids"] = str(rev_id)
        elif title:
            params["titles"] = title
        else:
            raise ValueError("title or rev_id required")

        resp = self.http.get(f"{self.base}/w/api.php", params=params)
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("revisions"):
            raise WikipediaError(f"No such page or revision: {title or rev_id}")
        page = pages[0]
        rev = page["revisions"][0]
        ts = rev.get("timestamp")
        return RevisionInfo(
            title=page["title"],
            rev_id=int(rev["revid"]),
            timestamp=datetime.fromisoformat(ts) if ts else None,
        )

    def revision_html(self, rev_id: int) -> str:
        """Parsoid HTML for an exact revision (MediaWiki core REST API)."""
        resp = self.http.get(f"{self.base}/w/rest.php/v1/revision/{rev_id}/html")
        if resp.status_code == 404:
            raise WikipediaError(f"Revision {rev_id} not found")
        resp.raise_for_status()
        return resp.text

    def permalink(self, rev_id: int) -> str:
        return permalink(self.lang, rev_id)


def permalink(lang: str, rev_id: int) -> str:
    return f"https://{lang}.wikipedia.org/w/index.php?oldid={rev_id}"
