"""disciple-scribe -- the reference disciple.

A complete worker. It wakes, tells the Sect it exists, takes one mission tagged
``summarize``, does it, reports the result, and exits. That is the whole lifecycle,
and it is what a GitHub Actions cron job runs.

To make your own disciple: copy this directory into a new repo, change ``ART`` and
``handle``, register the disciple once with the master key, and put the token in the
repo's secrets. Nothing else about this file needs to change.

The summarizer below is deliberately dependency-free and unclever. The Sect does not
care how a disciple does its work -- swap this for an LLM call, a shell out to ffmpeg,
or an HTTP request to something else entirely, and the plumbing is unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

from sect import Disciple, Mission, PermanentFailure

#: The one art this disciple claims to have.
ART = "summarize"

#: Words too common to say anything about what a text is about.
_STOPWORD_TEXT = (
    "a an and are as at be but by for from has have if in into is it its of on or "
    "that the their then there these they this to was were will with"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def summarize(text: str, sentence_count: int = 2) -> dict[str, object]:
    """Extractive summary: keep the sentences densest in the text's own key words."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    words = re.findall(r"[a-z']+", text.lower())
    meaningful = [w for w in words if w not in STOPWORDS and len(w) > 2]
    frequencies = Counter(meaningful)

    def score(sentence: str) -> float:
        tokens = [w for w in re.findall(r"[a-z']+", sentence.lower()) if w in frequencies]
        return sum(frequencies[w] for w in tokens) / (len(tokens) or 1)

    ranked = sorted(sentences, key=score, reverse=True)[:sentence_count]
    # Put the chosen sentences back in the order the author wrote them.
    summary = " ".join(sorted(ranked, key=sentences.index))

    return {
        "summary": summary,
        "words": len(words),
        "sentences": len(sentences),
        "keywords": [word for word, _ in frequencies.most_common(5)],
    }


def handle(mission: Mission) -> dict[str, object]:
    """Do the work. Whatever this returns becomes the mission's result.

    Raising anything reports a retryable failure and puts the mission back on the
    board. Raising PermanentFailure says retrying cannot help -- use it for bad input,
    never for a flaky network.
    """
    text = mission.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PermanentFailure("payload.text must be a non-empty string")
    return summarize(text, int(mission.payload.get("sentences", 2)))


def main() -> int:
    with Disciple(
        name=os.environ.get("SECT_DISCIPLE_NAME", "scribe"),
        arts=[ART],
        display_name="The Scribe",
        description="Turns long documents into short ones.",
        repo_url="https://github.com/bijay-odyssey/disciple-scribe",
        # Free on GitHub Actions, and it tells you which build did the work.
        agent_version=os.environ.get("GITHUB_SHA", "local")[:12],
    ) as disciple:
        mission = disciple.run_once(handle)

    if mission is None:
        print(f"No open missions requiring '{ART}'. Returning to cultivation.")
        return 0

    print(f"{mission.status}: {mission.title}  [{mission.id}]")
    if mission.status == "completed":
        print(json.dumps(mission.result, indent=2))
        return 0

    print(mission.error or "(no detail)", file=sys.stderr)
    # A retryable failure is ordinary operation -- the mission is back on the board and
    # will be picked up again, so the job did its job. Only a terminal failure is worth
    # turning the Actions run red.
    return 1 if mission.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
