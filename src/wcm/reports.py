"""Draft talk-page text for confirmed findings.

The output is a starting point for a human editor, not something to paste as-is.
The reviewer's own note is the summary; the model's explanation is left out on
purpose, because editors increasingly discount talk-page comments that read as
AI-generated.
"""

from sqlalchemy.orm import object_session

from wcm.models import Finding, FindingStatus, Verdict

VERDICT_SENTENCE = {
    Verdict.not_supported: "I could not find this statement in the cited source.",
    Verdict.contradicted: "The cited source appears to say something different.",
    Verdict.partially_supported: (
        "The cited source supports part of this statement, but not all of it."
    ),
}


class DraftNotAllowed(ValueError):
    pass


def _clip(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


SOURCE_RESULT = {
    Verdict.supported: "supports it",
    Verdict.partially_supported: "supports part of it",
    Verdict.not_supported: "does not appear to say this",
    Verdict.contradicted: "appears to say something different",
    Verdict.source_insufficient: "I could not read enough of it to tell",
    Verdict.source_unavailable: "I could not check it online",
}


def talk_page_draft(finding: Finding) -> str:
    from wcm.pipeline import latest_source_verdicts

    if finding.status != FindingStatus.confirmed:
        raise DraftNotAllowed("Only confirmed findings get a talk-page draft.")

    v = finding.verification
    claim = v.claim
    revision = claim.revision
    reviewer_note = next(
        (r.note for r in reversed(finding.reviews) if r.decision == "confirmed" and r.note), ""
    )
    latest = latest_source_verdicts(object_session(finding), claim)
    citations = list(claim.citations)

    lines = [
        f'== Sources do not appear to support: "{_clip(claim.text, 60)}" ==',
        "<!-- DRAFT. Before posting: re-check the sources yourself, rewrite this in your own",
        "     words, and say that you used a citation-checking tool. Do not post several of",
        "     these at once; one issue per thread, and wait for replies. -->",
        (
            f"In [[Special:PermanentLink/{revision.rev_id}|revision {revision.rev_id}]], "
            f'section "{claim.section}", the article says:'
        ),
        f':"{claim.text}"',
        "",
    ]
    if len(citations) == 1:
        lines.append(f"This cites: {citations[0].text}")
    else:
        lines.append(f"This cites {len(citations)} sources:")
    for citation in citations:
        sv = latest.get(citation.id)
        snap = sv.snapshot if sv else None
        link = ""
        if snap:
            url = snap.final_url or snap.requested_url
            link = f" ([{url} archived copy])" if snap.via_archive else f" ([{url} link])"
        result = SOURCE_RESULT.get(sv.verdict, "not checked") if sv else "not checked"
        prefix = f"# {citation.text}" if len(citations) > 1 else "I checked it"
        lines.append(f"{prefix}{link}: {result}.")
    lines.append("")
    if len(citations) > 1:
        lines.append(VERDICT_SENTENCE.get(v.verdict, "").replace("cited source", "sources"))

    number = {c.id: n for n, c in enumerate(citations, start=1)}
    for q in v.quotes:
        if not q.get("verified"):
            continue
        label = "A source says" if q["kind"] == "supporting" else "However, a source says"
        n = number.get(q.get("citation_id") or v.citation_id)
        if n and len(citations) > 1:
            label = label.replace("A source", f"Source {n}").replace("a source", f"source {n}")
        elif len(citations) == 1:
            label = label.replace("A source", "The source").replace("a source", "the source")
        line = f':{label}: "{q["text"]}"'
        if q.get("translation"):
            line += f' (translation: "{q["translation"]}")'
        lines.append(line)

    if v.unsupported_parts:
        lines.append("")
        lines.append("Parts I could not verify in the sources:")
        lines.extend(f"* {p}" for p in v.unsupported_parts)
    if finding.needs_offline_check:
        lines.append("")
        lines.append(
            "I could not check the source(s) that aren't online, so they may cover the rest."
        )

    lines += [
        "",
        reviewer_note or "[Your own short explanation here.]",
        "",
        (
            "Per [[WP:V]], could we either find a source that supports the current wording, or "
            "adjust the wording to match what the sources say? ~~~~"
        ),
    ]
    return "\n".join(lines)
