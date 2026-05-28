from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path


CORPUS = Path("allDurf.txt")
OUTPUT = Path("word_chain_top1k.json")
WORD_RE = re.compile(r"[a-z]+", re.IGNORECASE)


def main() -> None:
    text = CORPUS.read_text(encoding="utf-8", errors="ignore").lower()
    words = [
        word
        for word in WORD_RE.findall(text)
        if len(word) > 1 or word in {"a", "i"}
    ]
    counts = Counter(words)
    top_words = [word for word, _ in counts.most_common(1000)]
    top_set = set(top_words)

    next_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for left, right in zip(words, words[1:]):
        if left in top_set:
            next_counts[left][right] += 1

    payload = {
        "schema": "house.word_chain.v1",
        "name": "Top 1k word chain",
        "source": str(CORPUS),
        "word_count": len(words),
        "vocabulary_size": len(counts),
        "random_jump_probability": 0.2,
        "words": [
            {
                "word": word,
                "count": counts[word],
                "next": [
                    {"word": next_word, "count": next_count}
                    for next_word, next_count in next_counts[word].most_common(4)
                ],
            }
            for word in top_words
        ],
    }

    OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(top_words)} words from {len(words)} tokens")


if __name__ == "__main__":
    main()
