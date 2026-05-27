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


SLOTS: list[Slot] = [
    # Home is deliberately above every modified-room slot. Inside a room:
    # right hand first, then left hand, index -> middle -> ring -> pinky.
    Slot("H", "1", "home right index", 1.00),
    Slot("H", "D", "home left index", 0.98),
    Slot("H", "2", "home right middle", 0.90),
    Slot("H", "C", "home left middle", 0.88),
    Slot("H", "3", "home right ring", 0.78),
    Slot("H", "B", "home left ring", 0.76),
    Slot("H", "4", "home right pinky", 0.66),
    Slot("H", "A", "home left pinky", 0.64),
    Slot("R", "1", "right-room right index", 0.56),
    Slot("R", "D", "right-room left index", 0.55),
    Slot("R", "2", "right-room right middle", 0.50),
    Slot("R", "C", "right-room left middle", 0.49),
    Slot("R", "3", "right-room right ring", 0.43),
    Slot("R", "B", "right-room left ring", 0.42),
    Slot("R", "4", "right-room right pinky", 0.36),
    Slot("R", "A", "right-room left pinky", 0.35),
    Slot("L", "1", "left-room right index", 0.51),
    Slot("L", "D", "left-room left index", 0.50),
    Slot("L", "2", "left-room right middle", 0.46),
    Slot("L", "C", "left-room left middle", 0.45),
    Slot("L", "3", "left-room right ring", 0.39),
    Slot("L", "B", "left-room left ring", 0.38),
    Slot("L", "4", "left-room right pinky", 0.33),
    Slot("L", "A", "left-room left pinky", 0.32),
]


TRANSITION_COST = {
    ("H", "H"): 0.0,
    ("R", "R"): 0.0,
    ("L", "L"): 0.0,
    ("H", "R"): 0.18,
    ("R", "H"): 0.18,
    ("H", "L"): 0.20,
    ("L", "H"): 0.20,
    ("R", "L"): 0.58,
    ("L", "R"): 0.58,
}


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


def unpack(value: int, n: int) -> str:
    out = [""] * n
    for i in range(n - 1, -1, -1):
        value, rem = divmod(value, 26)
        out[i] = LETTERS[rem]
    return "".join(out)


def bitmask(indices: list[int]) -> int:
    mask = 0
    for index in indices:
        mask |= 1 << index
    return mask


def mask_letters(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(26) if mask & (1 << i))


def scan_corpus(path: Path) -> tuple[list[int], list[list[int]], dict[int, float], dict[int, Counter[int]], int]:
    letter_counts = [0] * 26
    bigram_matrix = [[0] * 26 for _ in range(26)]
    hyperedge_weights: defaultdict[int, float] = defaultdict(float)
    ngram_counts: dict[int, Counter[int]] = {2: Counter(), 3: Counter(), 4: Counter()}
    last: list[int] = []
    total_letters = 0

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break

            for byte in chunk:
                index = char_index(byte)
                if index is None:
                    last.clear()
                    continue

                total_letters += 1
                letter_counts[index] += 1

                if len(last) >= 1:
                    gram = [last[-1], index]
                    bigram_matrix[gram[0]][gram[1]] += 1
                    ngram_counts[2][pack(gram)] += 1
                    if len(set(gram)) == 2:
                        hyperedge_weights[bitmask(gram)] += 1.0

                if len(last) >= 2:
                    gram = [last[-2], last[-1], index]
                    ngram_counts[3][pack(gram)] += 1
                    if len(set(gram)) == 3:
                        hyperedge_weights[bitmask(gram)] += 2.0

                if len(last) >= 3:
                    gram = [last[-3], last[-2], last[-1], index]
                    ngram_counts[4][pack(gram)] += 1
                    if len(set(gram)) == 4:
                        hyperedge_weights[bitmask(gram)] += 3.0

                last.append(index)
                if len(last) > 3:
                    del last[0]

    return letter_counts, bigram_matrix, dict(hyperedge_weights), ngram_counts, total_letters


class Optimizer:
    def __init__(
        self,
        letter_counts: list[int],
        bigram_matrix: list[list[int]],
        hyperedge_weights: dict[int, float],
        total_letters: int,
        chord_weight: float,
        transition_weight: float,
        drop_weight: float,
    ) -> None:
        self.letter_counts = letter_counts
        self.bigram_matrix = bigram_matrix
        self.hyperedges = [(mask, mask_letters(mask), weight / total_letters) for mask, weight in hyperedge_weights.items()]
        self.total_letters = total_letters
        self.chord_weight = chord_weight
        self.transition_weight = transition_weight
        self.drop_weight = drop_weight
        self.letter_norm = [count / total_letters for count in letter_counts]
        self.bigram_norm = [
            [(bigram_matrix[a][b] / total_letters) for b in range(26)]
            for a in range(26)
        ]
        self.edges_by_letter: list[list[int]] = [[] for _ in range(26)]
        for edge_index, (_mask, letters, _weight) in enumerate(self.hyperedges):
            for letter in letters:
                self.edges_by_letter[letter].append(edge_index)

    def initial_assignment(self, rng: random.Random, randomized: bool) -> list[int]:
        ranked = sorted(range(26), key=lambda i: (-self.letter_counts[i], LETTERS[i]))
        assigned = ranked[:24]
        if randomized:
            # Keep the common letters in play, but let the search explore dropped rare letters.
            tail = ranked[20:]
            rng.shuffle(tail)
            assigned = ranked[:20] + tail[:4]

        positions = [-1] * 26
        for letter, slot_index in zip(assigned, range(24)):
            positions[letter] = slot_index

        if randomized:
            slot_indices = [positions[i] for i in range(26) if positions[i] != -1]
            rng.shuffle(slot_indices)
            cursor = 0
            for i in range(26):
                if positions[i] != -1:
                    positions[i] = slot_indices[cursor]
                    cursor += 1

        return positions

    def room(self, positions: list[int], letter: int) -> str | None:
        slot_index = positions[letter]
        if slot_index == -1:
            return None
        return SLOTS[slot_index].room

    def unigram_contribution(self, positions: list[int], letter: int) -> float:
        slot_index = positions[letter]
        if slot_index == -1:
            return -self.drop_weight * self.letter_norm[letter]
        return self.letter_norm[letter] * SLOTS[slot_index].weight

    def transition_contribution(self, positions: list[int], a: int, b: int) -> float:
        count = self.bigram_norm[a][b]
        if count == 0:
            return 0.0
        room_a = self.room(positions, a)
        room_b = self.room(positions, b)
        if room_a is None or room_b is None:
            return 0.0
        return -self.transition_weight * count * TRANSITION_COST[(room_a, room_b)]

    def edge_contribution(self, positions: list[int], edge_index: int) -> float:
        _mask, letters, weight = self.hyperedges[edge_index]
        first_room = self.room(positions, letters[0])
        if first_room is None:
            return 0.0
        for letter in letters[1:]:
            if self.room(positions, letter) != first_room:
                return 0.0
        return self.chord_weight * weight

    def score(self, positions: list[int]) -> float:
        total = sum(self.unigram_contribution(positions, letter) for letter in range(26))
        for a in range(26):
            for b in range(26):
                total += self.transition_contribution(positions, a, b)
        for edge_index in range(len(self.hyperedges)):
            total += self.edge_contribution(positions, edge_index)
        return total

    def swap_delta(self, positions: list[int], a: int, b: int) -> float:
        transition_pairs = set()
        edge_indices = set(self.edges_by_letter[a])
        edge_indices.update(self.edges_by_letter[b])

        for x in range(26):
            transition_pairs.add((a, x))
            transition_pairs.add((x, a))
            transition_pairs.add((b, x))
            transition_pairs.add((x, b))

        before = self.unigram_contribution(positions, a) + self.unigram_contribution(positions, b)
        before += sum(self.transition_contribution(positions, x, y) for x, y in transition_pairs)
        before += sum(self.edge_contribution(positions, edge_index) for edge_index in edge_indices)

        positions[a], positions[b] = positions[b], positions[a]

        after = self.unigram_contribution(positions, a) + self.unigram_contribution(positions, b)
        after += sum(self.transition_contribution(positions, x, y) for x, y in transition_pairs)
        after += sum(self.edge_contribution(positions, edge_index) for edge_index in edge_indices)

        positions[a], positions[b] = positions[b], positions[a]
        return after - before

    def optimize(self, steps: int, restarts: int, seed: int) -> tuple[list[int], float]:
        rng = random.Random(seed)
        best_positions: list[int] | None = None
        best_score = -math.inf

        for restart in range(restarts):
            positions = self.initial_assignment(rng, randomized=restart > 0)
            current_score = self.score(positions)

            for step in range(steps):
                a, b = rng.sample(range(26), 2)
                if positions[a] == positions[b]:
                    continue

                delta = self.swap_delta(positions, a, b)
                progress = step / max(1, steps - 1)
                temperature = 0.008 * (1.0 - progress) + 0.0003

                if delta >= 0 or rng.random() < math.exp(delta / temperature):
                    positions[a], positions[b] = positions[b], positions[a]
                    current_score += delta

                    if current_score > best_score:
                        best_positions = positions[:]
                        best_score = current_score

        assert best_positions is not None
        return self.polish(best_positions, best_score)

    def polish(self, positions: list[int], current_score: float) -> tuple[list[int], float]:
        improved = True
        while improved:
            improved = False
            for a in range(26):
                for b in range(a + 1, 26):
                    delta = self.swap_delta(positions, a, b)
                    if delta > 1e-12:
                        positions[a], positions[b] = positions[b], positions[a]
                        current_score += delta
                        improved = True
                        break
                if improved:
                    break
        return positions, current_score


def room_for(positions: list[int], letter: int) -> str | None:
    slot_index = positions[letter]
    if slot_index == -1:
        return None
    return SLOTS[slot_index].room


def is_chordable(positions: list[int], gram: str) -> bool:
    if len(set(gram)) != len(gram):
        return False
    rooms = {room_for(positions, LETTERS.index(ch)) for ch in gram}
    return len(rooms) == 1 and None not in rooms


def print_layout(positions: list[int]) -> None:
    dropped = [LETTERS[i] for i, slot_index in enumerate(positions) if slot_index == -1]
    print("Dropped letters:", ", ".join(dropped))
    print()
    print("Layout, in priority order inside each room:")
    for room, room_name in [("H", "Home"), ("R", "Right thumb room"), ("L", "Left thumb room")]:
        cells = []
        for slot_index, slot in enumerate(SLOTS):
            if slot.room != room:
                continue
            letter = next((LETTERS[i] for i, pos in enumerate(positions) if pos == slot_index), ".")
            cells.append(f"{slot.key}:{letter}")
        print(f"{room_name}: " + "  ".join(cells))


def print_diagnostics(positions: list[int], ngram_counts: dict[int, Counter[int]]) -> None:
    print()
    print("Top chordable bigrams:")
    shown = 0
    for packed, count in ngram_counts[2].most_common():
        gram = unpack(packed, 2)
        if is_chordable(positions, gram):
            room = room_for(positions, LETTERS.index(gram[0]))
            print(f"{gram:4} {count:>9}  {room}")
            shown += 1
            if shown == 20:
                break

    print()
    print("Top overall bigrams, with chord status:")
    for packed, count in ngram_counts[2].most_common(30):
        gram = unpack(packed, 2)
        status = "chord" if is_chordable(positions, gram) else "no"
        print(f"{gram:4} {count:>9}  {status}")

    print()
    print("Top chordable trigrams:")
    shown = 0
    for packed, count in ngram_counts[3].most_common():
        gram = unpack(packed, 3)
        if is_chordable(positions, gram):
            room = room_for(positions, LETTERS.index(gram[0]))
            print(f"{gram:4} {count:>9}  {room}")
            shown += 1
            if shown == 15:
                break

    print()
    print("Top chordable quadgrams:")
    shown = 0
    for packed, count in ngram_counts[4].most_common():
        gram = unpack(packed, 4)
        if is_chordable(positions, gram):
            room = room_for(positions, LETTERS.index(gram[0]))
            print(f"{gram:4} {count:>9}  {room}")
            shown += 1
            if shown == 10:
                break


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize a 3-room, 8-key chorded typing layout.")
    parser.add_argument("text", nargs="?", default="allDurf.txt", help="Text corpus to scan.")
    parser.add_argument("--steps", type=int, default=70_000, help="Annealing steps per restart.")
    parser.add_argument("--restarts", type=int, default=8, help="Number of search restarts.")
    parser.add_argument("--seed", type=int, default=17, help="Random seed.")
    parser.add_argument("--chord-weight", type=float, default=1.15, help="Reward for same-room n-gram chordability.")
    parser.add_argument("--transition-weight", type=float, default=0.75, help="Penalty weight for changing rooms inside words.")
    parser.add_argument("--drop-weight", type=float, default=1.25, help="Penalty for omitting a letter from the 24 slots.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.text)
    letter_counts, bigram_matrix, hyperedge_weights, ngram_counts, total_letters = scan_corpus(path)

    optimizer = Optimizer(
        letter_counts=letter_counts,
        bigram_matrix=bigram_matrix,
        hyperedge_weights=hyperedge_weights,
        total_letters=total_letters,
        chord_weight=args.chord_weight,
        transition_weight=args.transition_weight,
        drop_weight=args.drop_weight,
    )
    positions, score = optimizer.optimize(args.steps, args.restarts, args.seed)

    print(f"Corpus: {path}")
    print(f"Letters counted: {total_letters:,}")
    print(f"Score: {score:.6f}")
    print(f"Weights: chord={args.chord_weight}, transition={args.transition_weight}, drop={args.drop_weight}")
    print()
    print_layout(positions)
    print_diagnostics(positions, ngram_counts)


if __name__ == "__main__":
    main()
