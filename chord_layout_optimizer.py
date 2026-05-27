from __future__ import annotations

import argparse
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class Slot:
    room: str
    key: str
    label: str
    weight: float


SLOTS = [
    Slot("H", "1", "home right index", 1.00),
    Slot("H", "D", "home left index", 0.98),
    Slot("H", "2", "home right middle", 0.90),
    Slot("H", "C", "home left middle", 0.88),
    Slot("H", "3", "home right ring", 0.78),
    Slot("H", "B", "home left ring", 0.76),
    Slot("H", "4", "home right pinky", 0.66),
    Slot("H", "A", "home left pinky", 0.64),
    Slot("R", "1", "right-room right index", 0.58),
    Slot("R", "D", "right-room left index", 0.56),
    Slot("R", "2", "right-room right middle", 0.51),
    Slot("R", "C", "right-room left middle", 0.49),
    Slot("R", "3", "right-room right ring", 0.43),
    Slot("R", "B", "right-room left ring", 0.41),
    Slot("R", "4", "right-room right pinky", 0.36),
    Slot("R", "A", "right-room left pinky", 0.34),
    Slot("L", "1", "left-room right index", 0.52),
    Slot("L", "D", "left-room left index", 0.50),
    Slot("L", "2", "left-room right middle", 0.46),
    Slot("L", "C", "left-room left middle", 0.44),
    Slot("L", "3", "left-room right ring", 0.39),
    Slot("L", "B", "left-room left ring", 0.37),
    Slot("L", "4", "left-room right pinky", 0.33),
    Slot("L", "A", "left-room left pinky", 0.31),
]


TRANSITION_COST = {
    ("H", "H"): 0.00,
    ("R", "R"): 0.00,
    ("L", "L"): 0.00,
    ("H", "R"): 0.16,
    ("R", "H"): 0.16,
    ("H", "L"): 0.20,
    ("L", "H"): 0.20,
    ("R", "L"): 0.62,
    ("L", "R"): 0.62,
}


@dataclass(frozen=True)
class ChordCandidate:
    mask: int
    letters: tuple[int, ...]
    output: str
    top_count: int
    total_count: int
    dominance: float
    value: float


def char_index(byte: int) -> int | None:
    if 65 <= byte <= 90:
        return byte - 65
    if 97 <= byte <= 122:
        return byte - 97
    return None


def pack(indices: list[int]) -> int:
    value = 0
    for index in indices:
        value = value * 26 + index
    return value


def unpack(value: int, size: int) -> str:
    chars = [""] * size
    for i in range(size - 1, -1, -1):
        value, rem = divmod(value, 26)
        chars[i] = LETTERS[rem]
    return "".join(chars)


def mask_for(indices: list[int]) -> int:
    mask = 0
    for index in indices:
        mask |= 1 << index
    return mask


def letters_for(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(26) if mask & (1 << i))


def scan(path: Path) -> tuple[list[int], list[list[int]], dict[int, Counter[int]], dict[int, Counter[int]], int]:
    letter_counts = [0] * 26
    bigrams = [[0] * 26 for _ in range(26)]
    ngrams: dict[int, Counter[int]] = {2: Counter(), 3: Counter(), 4: Counter()}
    unordered: dict[int, Counter[int]] = defaultdict(Counter)
    total_letters = 0
    last: list[int] = []

    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            for byte in chunk:
                index = char_index(byte)
                if index is None:
                    last.clear()
                    continue

                total_letters += 1
                letter_counts[index] += 1

                if len(last) >= 1:
                    gram = [last[-1], index]
                    packed = pack(gram)
                    bigrams[gram[0]][gram[1]] += 1
                    ngrams[2][packed] += 1
                    if len(set(gram)) == 2:
                        unordered[mask_for(gram)][packed] += 1

                if len(last) >= 2:
                    gram = [last[-2], last[-1], index]
                    packed = pack(gram)
                    ngrams[3][packed] += 1
                    if len(set(gram)) == 3:
                        unordered[mask_for(gram)][packed] += 1

                if len(last) >= 3:
                    gram = [last[-3], last[-2], last[-1], index]
                    packed = pack(gram)
                    ngrams[4][packed] += 1
                    if len(set(gram)) == 4:
                        unordered[mask_for(gram)][packed] += 1

                last.append(index)
                if len(last) > 3:
                    last.pop(0)

    return letter_counts, bigrams, ngrams, dict(unordered), total_letters


def build_chord_candidates(
    unordered: dict[int, Counter[int]],
    total_letters: int,
    ambiguity_penalty: float,
    min_count: int,
    min_dominance: dict[int, float],
) -> list[ChordCandidate]:
    candidates = []
    for mask, counts in unordered.items():
        size = mask.bit_count()
        if size not in (2, 3, 4):
            continue

        output_packed, top_count = counts.most_common(1)[0]
        total_count = sum(counts.values())
        dominance = top_count / total_count
        other_count = total_count - top_count
        raw_value = (top_count - ambiguity_penalty * other_count) * (size - 1)

        if top_count < min_count:
            continue
        if dominance < min_dominance[size]:
            continue
        if raw_value <= 0:
            continue

        candidates.append(
            ChordCandidate(
                mask=mask,
                letters=letters_for(mask),
                output=unpack(output_packed, size),
                top_count=top_count,
                total_count=total_count,
                dominance=dominance,
                value=raw_value / total_letters,
            )
        )

    return sorted(candidates, key=lambda c: (-c.value, -c.top_count, c.output))


class LayoutSearch:
    def __init__(
        self,
        letter_counts: list[int],
        bigrams: list[list[int]],
        candidates: list[ChordCandidate],
        total_letters: int,
        chord_weight: float,
        transition_weight: float,
        drop_weight: float,
    ) -> None:
        self.letter_freq = [count / total_letters for count in letter_counts]
        self.bigrams = [[count / total_letters for count in row] for row in bigrams]
        self.candidates = candidates
        self.chord_weight = chord_weight
        self.transition_weight = transition_weight
        self.drop_weight = drop_weight

    def room(self, positions: list[int], letter: int) -> str | None:
        slot_index = positions[letter]
        if slot_index == -1:
            return None
        return SLOTS[slot_index].room

    def score(self, positions: list[int]) -> float:
        room_cache = [self.room(positions, letter) for letter in range(26)]
        total = 0.0

        for letter, slot_index in enumerate(positions):
            if slot_index == -1:
                total -= self.drop_weight * self.letter_freq[letter]
            else:
                total += self.letter_freq[letter] * SLOTS[slot_index].weight

        for a in range(26):
            room_a = room_cache[a]
            if room_a is None:
                continue
            for b in range(26):
                room_b = room_cache[b]
                if room_b is None:
                    continue
                total -= self.transition_weight * self.bigrams[a][b] * TRANSITION_COST[(room_a, room_b)]

        for candidate in self.candidates:
            first_room = room_cache[candidate.letters[0]]
            if first_room is None:
                continue
            if all(room_cache[letter] == first_room for letter in candidate.letters[1:]):
                total += self.chord_weight * candidate.value

        return total

    def initial(self, rng: random.Random, randomized: bool) -> list[int]:
        ranked = sorted(range(26), key=lambda i: (-self.letter_freq[i], LETTERS[i]))
        kept = ranked[:24]
        if randomized:
            tail = ranked[16:]
            rng.shuffle(tail)
            kept = ranked[:16] + tail[:8]

        positions = [-1] * 26
        slot_indices = list(range(24))
        if randomized:
            rng.shuffle(slot_indices)

        for letter, slot_index in zip(kept, slot_indices):
            positions[letter] = slot_index
        return positions

    def optimize(self, steps: int, restarts: int, seed: int) -> tuple[list[int], float]:
        rng = random.Random(seed)
        best_positions: list[int] | None = None
        best_score = -math.inf

        for restart in range(restarts):
            positions = self.initial(rng, randomized=restart > 0)
            current_score = self.score(positions)
            if current_score > best_score:
                best_positions = positions[:]
                best_score = current_score

            for step in range(steps):
                a, b = rng.sample(range(26), 2)
                if positions[a] == positions[b]:
                    continue

                positions[a], positions[b] = positions[b], positions[a]
                next_score = self.score(positions)
                delta = next_score - current_score
                progress = step / max(1, steps - 1)
                temperature = 0.006 * (1.0 - progress) + 0.00015

                if delta >= 0 or rng.random() < math.exp(delta / temperature):
                    current_score = next_score
                    if current_score > best_score:
                        best_positions = positions[:]
                        best_score = current_score
                else:
                    positions[a], positions[b] = positions[b], positions[a]

        assert best_positions is not None
        return self.polish(best_positions, best_score)

    def polish(self, positions: list[int], best_score: float) -> tuple[list[int], float]:
        improved = True
        while improved:
            improved = False
            for a in range(26):
                for b in range(a + 1, 26):
                    if positions[a] == positions[b]:
                        continue
                    positions[a], positions[b] = positions[b], positions[a]
                    next_score = self.score(positions)
                    if next_score > best_score + 1e-12:
                        best_score = next_score
                        improved = True
                        break
                    positions[a], positions[b] = positions[b], positions[a]
                if improved:
                    break
        return positions, best_score


def same_room(positions: list[int], letters: tuple[int, ...]) -> bool:
    rooms = []
    for letter in letters:
        slot_index = positions[letter]
        if slot_index == -1:
            return False
        rooms.append(SLOTS[slot_index].room)
    return len(set(rooms)) == 1


def chord_key(positions: list[int], letters: tuple[int, ...]) -> str:
    items = sorted((SLOTS[positions[letter]].key, LETTERS[letter]) for letter in letters)
    return "+".join(f"{key}({letter})" for key, letter in items)


def print_layout(positions: list[int]) -> None:
    dropped = [LETTERS[i] for i, slot_index in enumerate(positions) if slot_index == -1]
    print("Dropped:", ", ".join(dropped))
    for room, label in [("H", "Home"), ("R", "Right room"), ("L", "Left room")]:
        parts = []
        for index, slot in enumerate(SLOTS):
            if slot.room != room:
                continue
            letter = next((LETTERS[i] for i, pos in enumerate(positions) if pos == index), ".")
            parts.append(f"{slot.key}:{letter}")
        print(f"{label}: " + "  ".join(parts))


def print_chord_map(positions: list[int], candidates: list[ChordCandidate], limit: int) -> None:
    print()
    print("Chord map:")
    printed = 0
    for candidate in candidates:
        if not same_room(positions, candidate.letters):
            continue
        room = SLOTS[positions[candidate.letters[0]]].room
        print(
            f"{room} {chord_key(positions, candidate.letters):28} -> "
            f"{candidate.output:<4} count={candidate.top_count:<8} "
            f"dominance={candidate.dominance:.3f}"
        )
        printed += 1
        if printed >= limit:
            break


def print_bigram_check(positions: list[int], ngrams: dict[int, Counter[int]], candidates: list[ChordCandidate]) -> None:
    chorded_outputs = {
        candidate.output
        for candidate in candidates
        if len(candidate.output) == 2 and same_room(positions, candidate.letters)
    }
    print()
    print("Top bigrams:")
    for packed, count in ngrams[2].most_common(25):
        gram = unpack(packed, 2)
        print(f"{gram:4} {count:>9}  {'chord' if gram in chorded_outputs else 'ordered'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="allDurf.txt")
    parser.add_argument("--steps", type=int, default=18_000)
    parser.add_argument("--restarts", type=int, default=5)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--chord-weight", type=float, default=1.35)
    parser.add_argument("--transition-weight", type=float, default=0.85)
    parser.add_argument("--drop-weight", type=float, default=1.35)
    parser.add_argument("--ambiguity-penalty", type=float, default=1.15)
    parser.add_argument("--min-count", type=int, default=4000)
    parser.add_argument("--bigram-dominance", type=float, default=0.72)
    parser.add_argument("--trigram-dominance", type=float, default=0.56)
    parser.add_argument("--quadgram-dominance", type=float, default=0.44)
    parser.add_argument("--chord-limit", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.text)
    letter_counts, bigrams, ngrams, unordered, total_letters = scan(path)
    candidates = build_chord_candidates(
        unordered=unordered,
        total_letters=total_letters,
        ambiguity_penalty=args.ambiguity_penalty,
        min_count=args.min_count,
        min_dominance={2: args.bigram_dominance, 3: args.trigram_dominance, 4: args.quadgram_dominance},
    )

    search = LayoutSearch(
        letter_counts=letter_counts,
        bigrams=bigrams,
        candidates=candidates[:1200],
        total_letters=total_letters,
        chord_weight=args.chord_weight,
        transition_weight=args.transition_weight,
        drop_weight=args.drop_weight,
    )
    positions, score = search.optimize(args.steps, args.restarts, args.seed)

    print(f"Corpus: {path}")
    print(f"Letters: {total_letters:,}")
    print(f"Usable chord candidates: {len(candidates):,}")
    print(f"Score: {score:.6f}")
    print(
        "Weights: "
        f"chord={args.chord_weight}, transition={args.transition_weight}, "
        f"drop={args.drop_weight}, ambiguity={args.ambiguity_penalty}"
    )
    print()
    print_layout(positions)
    print_chord_map(positions, candidates, args.chord_limit)
    print_bigram_check(positions, ngrams, candidates)


if __name__ == "__main__":
    main()
