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

    # -- topic discovery (all read-only) ------------------------------------------------

    def _query(self, **params) -> dict:
        resp = self.http.get(
            f"{self.base}/w/api.php",
            params={"action": "query", "format": "json", "formatversion": "2", **params},
        )
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, limit: int = 50, namespace: int = 0) -> list[str]:
        """Titles from Wikipedia's own search, best match first."""
        data = self._query(
            list="search",
            srsearch=query,
            srnamespace=str(namespace),
            srlimit=str(limit),
            srprop="",
        )
        return [r["title"] for r in data.get("query", {}).get("search", [])]

    def category_members(self, category: str, limit: int = 200) -> tuple[list[str], list[str]]:
        """(articles, subcategories) in a category."""
        data = self._query(
            list="categorymembers",
            cmtitle=category,
            cmtype="page|subcat",
            cmnamespace="0|14",
            cmlimit=str(limit),
        )
        pages, subcats = [], []
        for m in data.get("query", {}).get("categorymembers", []):
            (subcats if m.get("ns") == 14 else pages).append(m["title"])
        return pages, subcats

    def page_links(self, title: str, limit: int = 300) -> list[str]:
        """Articles linked from a page."""
        data = self._query(
            prop="links",
            titles=title,
            plnamespace="0",
            pllimit=str(min(limit, 500)),
            redirects="1",
        )
        pages = data.get("query", {}).get("pages", [])
        return [link["title"] for p in pages for link in p.get("links", [])][:limit]

    def page_info(self, titles: list[str]) -> dict[str, dict]:
        """Size, short description, edit protection and last edit for articles,
        keyed by the title asked for. Missing pages and redirects' targets are
        resolved; disambiguation pages are marked."""
        out: dict[str, dict] = {}
        for i in range(0, len(titles), 50):
            batch = titles[i : i + 50]
            data = self._query(
                prop="info|pageprops|revisions",
                titles="|".join(batch),
                inprop="protection",
                ppprop="wikibase-shortdesc|disambiguation",
                rvprop="timestamp",
                redirects="1",
            )
            q = data.get("query", {})
            renamed = {r["from"]: r["to"] for r in q.get("normalized", []) + q.get("redirects", [])}
            pages = {p["title"]: p for p in q.get("pages", [])}
            for asked in batch:
                title = asked
                while title in renamed:
                    title = renamed[title]
                p = pages.get(title)
                if not p or p.get("missing") or p.get("ns", 0) != 0:
                    continue
                props = p.get("pageprops", {})
                edit = next(
                    (x["level"] for x in p.get("protection", []) if x.get("type") == "edit"),
                    None,
                )
                ts = (p.get("revisions") or [{}])[0].get("timestamp")
                out[asked] = {
                    "title": p["title"],
                    "size": p.get("length"),
                    "description": props.get("wikibase-shortdesc", ""),
                    "disambiguation": "disambiguation" in props,
                    "edit_protection": edit,
                    "last_edited": datetime.fromisoformat(ts) if ts else None,
                }
        return out


def permalink(lang: str, rev_id: int) -> str:
    return f"https://{lang}.wikipedia.org/w/index.php?oldid={rev_id}"
