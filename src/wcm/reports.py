"""Draft talk-page text for confirmed findings.

The output is a starting point for a human editor, not something to paste as-is.
The reviewer's own note is the summary; the model's explanation is left out on
purpose, because editors increasingly discount talk-page comments that read as
AI-generated.
"""

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


def talk_page_draft(finding: Finding) -> str:
    if finding.status != FindingStatus.confirmed:
        raise DraftNotAllowed("Only confirmed findings get a talk-page draft.")

    v = finding.verification
    claim = v.claim
    citation = v.citation
    revision = claim.revision
    snap = v.snapshot
    reviewer_note = next(
        (r.note for r in reversed(finding.reviews) if r.decision == "confirmed" and r.note), ""
    )

    lines = [
        f'== Source does not appear to support: "{_clip(claim.text, 60)}" ==',
        "<!-- DRAFT. Before posting: re-check the source yourself, rewrite this in your own",
        "     words, and say that you used a citation-checking tool. Do not post several of",
        "     these at once; one issue per thread, and wait for replies. -->",
        (
            f"In [[Special:PermanentLink/{revision.rev_id}|revision {revision.rev_id}]], "
            f'section "{claim.section}", the article says:'
        ),
        f':"{claim.text}"',
        "",
        f"This cites: {citation.text}",
        "",
    ]
    if snap:
        where = (
            f"an [{snap.final_url or snap.requested_url} archived copy]"
            if snap.via_archive
            else f"the [{snap.final_url or snap.requested_url} source]"
        )
        lines.append(f"I checked {where}. {VERDICT_SENTENCE.get(v.verdict, '')}")
    else:
        lines.append(VERDICT_SENTENCE.get(v.verdict, ""))

    for q in v.quotes:
        if not q.get("verified"):
            continue
        label = "The source says" if q["kind"] == "supporting" else "However, the source says"
        line = f':{label}: "{q["text"]}"'
        if q.get("translation"):
            line += f' (translation: "{q["translation"]}")'
        lines.append(line)

    if v.unsupported_parts:
        lines.append("")
        lines.append("Parts I could not verify in the source:")
        lines.extend(f"* {p}" for p in v.unsupported_parts)

    lines += [
        "",
        reviewer_note or "[Your own short explanation here.]",
        "",
        (
            "Per [[WP:V]], could we either find a source that supports the current wording, or "
            "adjust the wording to match what this source says? ~~~~"
        ),
    ]
    return "\n".join(lines)
