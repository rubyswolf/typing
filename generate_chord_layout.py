from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class KeySlot:
    room: str
    key: str
    finger: str
    weight: float


SLOTS = [
    KeySlot("H", "1", "right index", 1.00),
    KeySlot("H", "D", "left index", 0.98),
    KeySlot("H", "2", "right middle", 0.90),
    KeySlot("H", "C", "left middle", 0.88),
    KeySlot("H", "3", "right ring", 0.78),
    KeySlot("H", "B", "left ring", 0.76),
    KeySlot("H", "4", "right pinky", 0.66),
    KeySlot("H", "A", "left pinky", 0.64),
    KeySlot("R", "1", "right index", 0.58),
    KeySlot("R", "D", "left index", 0.56),
    KeySlot("R", "2", "right middle", 0.51),
    KeySlot("R", "C", "left middle", 0.49),
    KeySlot("R", "3", "right ring", 0.43),
    KeySlot("R", "B", "left ring", 0.41),
    KeySlot("R", "4", "right pinky", 0.36),
    KeySlot("R", "A", "left pinky", 0.34),
    KeySlot("L", "1", "right index", 0.52),
    KeySlot("L", "D", "left index", 0.50),
    KeySlot("L", "2", "right middle", 0.46),
    KeySlot("L", "C", "left middle", 0.44),
    KeySlot("L", "3", "right ring", 0.39),
    KeySlot("L", "B", "left ring", 0.37),
    KeySlot("L", "4", "right pinky", 0.33),
    KeySlot("L", "A", "left pinky", 0.31),
]


ROOM_COST = {
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


ROOM_CAPACITY = {"H": 8, "R": 8, "L": 8, "X": 2}
ROOM_BASE = {"H": 0.825, "R": 0.46, "L": 0.415, "X": -0.62}


@dataclass(frozen=True)
class Chord:
    letters: str
    output: str
    count: int
    total_same_keys: int
    dominance: float
    value: float


def pack(indices: list[int]) -> int:
    value = 0
    for index in indices:
        value = value * 26 + index
    return value


def unpack(value: int, size: int) -> str:
    chars = [""] * size
    for position in range(size - 1, -1, -1):
        value, index = divmod(value, 26)
        chars[position] = LETTERS[index]
    return "".join(chars)


def bitmask(indices: list[int]) -> int:
    mask = 0
    for index in indices:
        mask |= 1 << index
    return mask


def mask_to_letters(mask: int) -> str:
    return "".join(LETTERS[i] for i in range(26) if mask & (1 << i))


def scan_corpus(path: Path) -> tuple[list[int], list[list[int]], dict[int, Counter[int]], dict[int, Counter[int]], int]:
    data = path.read_bytes().upper()
    words = re.findall(rb"[A-Z]+", data)
    byte_counts = Counter(data)
    letter_counts = [0] * 26
    bigrams = [[0] * 26 for _ in range(26)]
    byte_ngrams: dict[int, Counter[bytes]] = {2: Counter(), 3: Counter(), 4: Counter()}
    ngrams: dict[int, Counter[int]] = {2: Counter(), 3: Counter(), 4: Counter()}
    unordered: dict[int, Counter[int]] = defaultdict(Counter)

    for index, byte in enumerate(range(65, 91)):
        letter_counts[index] = byte_counts[byte]
    total_letters = sum(letter_counts)

    for word in words:
        length = len(word)
        for size in (2, 3, 4):
            if length < size:
                continue
            byte_ngrams[size].update(word[start : start + size] for start in range(length - size + 1))

    for size, counter in byte_ngrams.items():
        for gram, count in counter.items():
            indices = [byte - 65 for byte in gram]
            packed = pack(indices)
            ngrams[size][packed] = count
            if size == 2:
                bigrams[indices[0]][indices[1]] = count
            if len(set(indices)) == size:
                unordered[bitmask(indices)][packed] += count

    return letter_counts, bigrams, ngrams, dict(unordered), total_letters


def build_chords(
    unordered: dict[int, Counter[int]],
    total_letters: int,
    ambiguity_penalty: float,
    min_count: int,
    dominance_by_size: dict[int, float],
) -> list[Chord]:
    chords = []
    for mask, counts in unordered.items():
        size = mask.bit_count()
        if size not in dominance_by_size:
            continue

        packed, count = counts.most_common(1)[0]
        total = sum(counts.values())
        dominance = count / total
        other = total - count
        raw_value = (count - ambiguity_penalty * other) * (size - 1)

        if count < min_count or dominance < dominance_by_size[size] or raw_value <= 0:
            continue

        chords.append(
            Chord(
                letters=mask_to_letters(mask),
                output=unpack(packed, size),
                count=count,
                total_same_keys=total,
                dominance=dominance,
                value=raw_value / total_letters,
            )
        )

    return sorted(chords, key=lambda chord: (-chord.value, -chord.count, chord.output))


def initial_rooms(letter_counts: list[int]) -> list[str]:
    ranked = sorted(range(26), key=lambda index: (-letter_counts[index], LETTERS[index]))
    rooms = ["X"] * 26
    for room, start in [("H", 0), ("R", 8), ("L", 16)]:
        for letter in ranked[start : start + 8]:
            rooms[letter] = room
    return rooms


def room_counts(rooms: list[str]) -> dict[str, int]:
    return {room: rooms.count(room) for room in ROOM_CAPACITY}


def is_direct_side_room_pair(room_a: str, room_b: str) -> bool:
    return {room_a, room_b} == {"L", "R"}


def key_assignment_for_rooms(rooms: list[str], letter_freq: list[float]) -> list[str | None]:
    key_by_letter: list[str | None] = [None] * 26
    for room in ("H", "R", "L"):
        room_slots = [slot for slot in SLOTS if slot.room == room]
        room_letters = [index for index, assigned_room in enumerate(rooms) if assigned_room == room]
        room_letters.sort(key=lambda index: (-letter_freq[index], LETTERS[index]))
        for slot, letter in zip(room_slots, room_letters):
            key_by_letter[letter] = slot.key
    return key_by_letter


def score_rooms(
    rooms: list[str],
    letter_freq: list[float],
    bigrams: list[list[float]],
    chords: list[Chord],
    chord_weight: float,
    transition_weight: float,
    drop_base: float,
    same_key_room_switch_penalty: float,
) -> float:
    score = 0.0
    key_by_letter = key_assignment_for_rooms(rooms, letter_freq)

    for letter, room in enumerate(rooms):
        base = drop_base if room == "X" else ROOM_BASE[room]
        score += letter_freq[letter] * base

    for a in range(26):
        room_a = rooms[a]
        if room_a == "X":
            continue
        for b in range(26):
            room_b = rooms[b]
            if room_b == "X":
                continue
            score -= transition_weight * bigrams[a][b] * ROOM_COST[(room_a, room_b)]
            if is_direct_side_room_pair(room_a, room_b) and key_by_letter[a] == key_by_letter[b]:
                score -= same_key_room_switch_penalty * bigrams[a][b]

    for chord in chords:
        chord_rooms = {rooms[LETTERS.index(letter)] for letter in chord.letters}
        if len(chord_rooms) == 1 and "X" not in chord_rooms:
            score += chord_weight * chord.value

    return score


def optimize_rooms(
    letter_counts: list[int],
    bigram_counts: list[list[int]],
    chords: list[Chord],
    total_letters: int,
    steps: int,
    restarts: int,
    seed: int,
    chord_weight: float,
    transition_weight: float,
    drop_base: float,
    same_key_room_switch_penalty: float,
) -> tuple[list[str], float]:
    rng = random.Random(seed)
    letter_freq = [count / total_letters for count in letter_counts]
    bigrams = [[count / total_letters for count in row] for row in bigram_counts]
    best_rooms = initial_rooms(letter_counts)
    best_score = score_rooms(
        best_rooms,
        letter_freq,
        bigrams,
        chords,
        chord_weight,
        transition_weight,
        drop_base,
        same_key_room_switch_penalty,
    )

    for restart in range(restarts):
        rooms = initial_rooms(letter_counts)
        if restart:
            kept = [i for i, room in enumerate(rooms) if room != "X"]
            rare = sorted(range(26), key=lambda index: letter_counts[index])[:10]
            for letter in rng.sample(kept, 4):
                other = rng.choice(rare)
                rooms[letter], rooms[other] = rooms[other], rooms[letter]
            labels = rooms[:]
            rng.shuffle(labels)
            if room_counts(labels) == ROOM_CAPACITY:
                rooms = labels

        current_score = score_rooms(
            rooms,
            letter_freq,
            bigrams,
            chords,
            chord_weight,
            transition_weight,
            drop_base,
            same_key_room_switch_penalty,
        )

        for step in range(steps):
            a, b = rng.sample(range(26), 2)
            if rooms[a] == rooms[b]:
                continue
            rooms[a], rooms[b] = rooms[b], rooms[a]
            next_score = score_rooms(
                rooms,
                letter_freq,
                bigrams,
                chords,
                chord_weight,
                transition_weight,
                drop_base,
                same_key_room_switch_penalty,
            )
            delta = next_score - current_score
            temperature = 0.004 * (1 - step / max(1, steps - 1)) + 0.0001

            if delta >= 0 or rng.random() < math.exp(delta / temperature):
                current_score = next_score
                if current_score > best_score:
                    best_score = current_score
                    best_rooms = rooms[:]
            else:
                rooms[a], rooms[b] = rooms[b], rooms[a]

    improved = True
    while improved:
        improved = False
        for a in range(26):
            for b in range(a + 1, 26):
                if best_rooms[a] == best_rooms[b]:
                    continue
                trial = best_rooms[:]
                trial[a], trial[b] = trial[b], trial[a]
                trial_score = score_rooms(
                    trial,
                    letter_freq,
                    bigrams,
                    chords,
                    chord_weight,
                    transition_weight,
                    drop_base,
                    same_key_room_switch_penalty,
                )
                if trial_score > best_score:
                    best_rooms = trial
                    best_score = trial_score
                    improved = True
                    break
            if improved:
                break

    return best_rooms, best_score


def layout_score(
    layout: dict[str, dict[str, str]],
    letter_counts: list[int],
    bigrams: list[list[int]],
    same_key_room_switch_reward: float,
) -> float:
    total_letters = sum(letter_counts)
    score = 0.0
    letter_pos: dict[str, tuple[str, str, float]] = {}
    for slot in SLOTS:
        letter = layout[slot.room][slot.key]
        letter_pos[letter] = (slot.room, slot.key, slot.weight)
        score += (letter_counts[LETTERS.index(letter)] / total_letters) * slot.weight

    if same_key_room_switch_reward:
        for a, letter_a in enumerate(LETTERS):
            pos_a = letter_pos.get(letter_a)
            if not pos_a:
                continue
            room_a, key_a, _ = pos_a
            for b, letter_b in enumerate(LETTERS):
                pos_b = letter_pos.get(letter_b)
                if not pos_b:
                    continue
                room_b, key_b, _ = pos_b
                if is_direct_side_room_pair(room_a, room_b) and key_a == key_b:
                    score += same_key_room_switch_reward * (bigrams[a][b] / total_letters)
    return score


def assign_slots(
    rooms: list[str],
    letter_counts: list[int],
    bigrams: list[list[int]],
    same_key_room_switch_reward: float,
) -> dict[str, dict[str, str]]:
    layout: dict[str, dict[str, str]] = {}
    for room in ("H", "R", "L"):
        room_slots = [slot for slot in SLOTS if slot.room == room]
        room_letters = [index for index, assigned_room in enumerate(rooms) if assigned_room == room]
        room_letters.sort(key=lambda index: (-letter_counts[index], LETTERS[index]))
        layout[room] = {
            slot.key: LETTERS[letter]
            for slot, letter in zip(room_slots, room_letters)
        }

    if same_key_room_switch_reward <= 0:
        return layout

    best_score = layout_score(layout, letter_counts, bigrams, same_key_room_switch_reward)
    improved = True
    while improved:
        improved = False
        for room in ("H", "R", "L"):
            for key_a, key_b in combinations(sorted(layout[room]), 2):
                trial = {r: keys.copy() for r, keys in layout.items()}
                trial[room][key_a], trial[room][key_b] = trial[room][key_b], trial[room][key_a]
                trial_score = layout_score(trial, letter_counts, bigrams, same_key_room_switch_reward)
                if trial_score > best_score + 1e-12:
                    layout = trial
                    best_score = trial_score
                    improved = True
                    break
            if improved:
                break
    return layout


def slot_lookup(layout: dict[str, dict[str, str]]) -> dict[str, tuple[str, str]]:
    out = {}
    for room, keys in layout.items():
        for key, letter in keys.items():
            out[letter] = (room, key)
    return out


def select_chord_map(
    rooms: list[str],
    layout: dict[str, dict[str, str]],
    chords: list[Chord],
    limit: int,
) -> list[dict[str, object]]:
    lookup = slot_lookup(layout)
    selected = []
    for chord in chords:
        assigned_rooms = {rooms[LETTERS.index(letter)] for letter in chord.letters}
        if len(assigned_rooms) != 1 or "X" in assigned_rooms:
            continue
        room = assigned_rooms.pop()
        keys = sorted(lookup[letter][1] for letter in chord.letters)
        selected.append(
            {
                "room": room,
                "keys": keys,
                "letters": chord.letters,
                "output": chord.output,
                "count": chord.count,
                "total_same_keys": chord.total_same_keys,
                "dominance": round(chord.dominance, 4),
                "value": round(chord.value, 8),
            }
        )
        if len(selected) >= limit:
            break
    return selected


def add_dropped_letter_chords(
    rooms: list[str],
    layout: dict[str, dict[str, str]],
    chord_map: list[dict[str, object]],
    bigrams: list[list[int]],
) -> list[dict[str, object]]:
    dropped = [LETTERS[index] for index, room in enumerate(rooms) if room == "X"]
    used_pairs = {
        (str(entry["room"]), tuple(sorted(map(str, entry["keys"]))))
        for entry in chord_map
        if len(entry.get("keys", [])) == 2
    }

    candidates = []
    for room, key_map in layout.items():
        for key_a, key_b in combinations(sorted(key_map), 2):
            pair = (room, tuple(sorted((key_a, key_b))))
            if pair in used_pairs:
                continue

            letter_a = key_map[key_a]
            letter_b = key_map[key_b]
            index_a = LETTERS.index(letter_a)
            index_b = LETTERS.index(letter_b)
            traffic = bigrams[index_a][index_b] + bigrams[index_b][index_a]
            candidates.append(
                {
                    "room": room,
                    "keys": list(pair[1]),
                    "letters": "".join(sorted((letter_a, letter_b))),
                    "traffic": traffic,
                    "bigrams": {
                        letter_a + letter_b: bigrams[index_a][index_b],
                        letter_b + letter_a: bigrams[index_b][index_a],
                    },
                }
            )

    candidates.sort(key=lambda item: (item["traffic"], item["room"], item["keys"]))
    additions = []
    for letter, candidate in zip(dropped, candidates):
        entry = {
            "room": candidate["room"],
            "keys": candidate["keys"],
            "letters": candidate["letters"],
            "output": letter,
            "count": 0,
            "total_same_keys": candidate["traffic"],
            "dominance": 0.0,
            "value": 0.0,
            "fallback_for_dropped_letter": True,
            "sacrificed_bigram_traffic": candidate["traffic"],
            "sacrificed_bigrams": candidate["bigrams"],
        }
        chord_map.append(entry)
        used_pairs.add((entry["room"], tuple(entry["keys"])))
        additions.append(entry)

    return additions


def write_outputs(
    output_base: Path,
    corpus: Path,
    total_letters: int,
    score: float,
    rooms: list[str],
    layout: dict[str, dict[str, str]],
    chord_map: list[dict[str, object]],
    ngrams: dict[int, Counter[int]],
    chords: list[Chord],
) -> None:
    dropped = [LETTERS[index] for index, room in enumerate(rooms) if room == "X"]
    result = {
        "corpus": str(corpus),
        "letters_counted": total_letters,
        "score": score,
        "dropped_letters": dropped,
        "layout": layout,
        "chord_map": chord_map,
        "dropped_letter_chords": [
            entry for entry in chord_map if entry.get("fallback_for_dropped_letter")
        ],
    }
    output_base.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    chorded_bigrams = {entry["output"] for entry in chord_map if len(str(entry["output"])) == 2}
    lines = [
        f"Corpus: {corpus}",
        f"Letters counted: {total_letters:,}",
        f"Score: {score:.6f}",
        f"Dropped letters: {', '.join(dropped)}",
        "",
        "Layout:",
    ]
    for room, label in [("H", "Home"), ("R", "Right room"), ("L", "Left room")]:
        keys = layout[room]
        lines.append(f"{label}: " + "  ".join(f"{key}:{keys[key]}" for key in ["1", "D", "2", "C", "3", "B", "4", "A"]))

    lines.extend(["", "Chord map:"])
    for entry in chord_map:
        key_text = "+".join(entry["keys"])
        suffix = " fallback" if entry.get("fallback_for_dropped_letter") else ""
        lines.append(
            f'{entry["room"]} {key_text:<9} {entry["letters"]:<4} -> {entry["output"]:<4} '
            f'count={entry["count"]:<8} dominance={entry["dominance"]:.4f}{suffix}'
        )

    lines.extend(["", "Top bigrams:"])
    for packed, count in ngrams[2].most_common(25):
        gram = unpack(packed, 2)
        lines.append(f"{gram:<4} {count:>9}  {'chord' if gram in chorded_bigrams else 'sequential'}")

    output_base.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="allDurf.txt")
    parser.add_argument("--output", default="generated_layout")
    parser.add_argument("--steps", type=int, default=35_000)
    parser.add_argument("--restarts", type=int, default=6)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--chord-weight", type=float, default=1.45)
    parser.add_argument("--transition-weight", type=float, default=0.90)
    parser.add_argument("--drop-base", type=float, default=-0.62)
    parser.add_argument("--same-key-room-switch-penalty", type=float, default=0.0)
    parser.add_argument("--same-key-room-switch-reward", type=float, default=0.0)
    parser.add_argument("--ambiguity-penalty", type=float, default=1.15)
    parser.add_argument("--min-count", type=int, default=4_000)
    parser.add_argument("--bigram-dominance", type=float, default=0.72)
    parser.add_argument("--trigram-dominance", type=float, default=0.56)
    parser.add_argument("--quadgram-dominance", type=float, default=0.44)
    parser.add_argument("--chord-limit", type=int, default=90)
    parser.add_argument("--candidate-limit", type=int, default=1_200)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    corpus = Path(args.text)
    letter_counts, bigrams, ngrams, unordered, total_letters = scan_corpus(corpus)
    chords = build_chords(
        unordered,
        total_letters,
        ambiguity_penalty=args.ambiguity_penalty,
        min_count=args.min_count,
        dominance_by_size={
            2: args.bigram_dominance,
            3: args.trigram_dominance,
            4: args.quadgram_dominance,
        },
    )
    rooms, score = optimize_rooms(
        letter_counts,
        bigrams,
        chords[: args.candidate_limit],
        total_letters,
        steps=args.steps,
        restarts=args.restarts,
        seed=args.seed,
        chord_weight=args.chord_weight,
        transition_weight=args.transition_weight,
        drop_base=args.drop_base,
        same_key_room_switch_penalty=args.same_key_room_switch_penalty,
    )
    layout = assign_slots(rooms, letter_counts, bigrams, args.same_key_room_switch_reward)
    chord_map = select_chord_map(rooms, layout, chords, args.chord_limit)
    dropped_letter_chords = add_dropped_letter_chords(rooms, layout, chord_map, bigrams)
    output_base = Path(args.output)
    write_outputs(output_base, corpus, total_letters, score, rooms, layout, chord_map, ngrams, chords)

    dropped = [LETTERS[index] for index, room in enumerate(rooms) if room == "X"]
    print(f"Wrote {output_base.with_suffix('.json')} and {output_base.with_suffix('.txt')}")
    print(f"Dropped: {', '.join(dropped)}")
    for room, label in [("H", "Home"), ("R", "Right room"), ("L", "Left room")]:
        keys = layout[room]
        print(f"{label}: " + "  ".join(f"{key}:{keys[key]}" for key in ["1", "D", "2", "C", "3", "B", "4", "A"]))
    print("Top chords:")
    for entry in chord_map[:20]:
        print(
            f'{entry["room"]} {"+".join(entry["keys"]):<9} {entry["letters"]:<4} -> '
            f'{entry["output"]:<4} dominance={entry["dominance"]:.4f} count={entry["count"]}'
        )
    if dropped_letter_chords:
        print("Dropped-letter fallback chords:")
        for entry in dropped_letter_chords:
            print(
                f'{entry["output"]}: {entry["room"]} {"+".join(entry["keys"])} '
                f'traffic={entry["sacrificed_bigram_traffic"]}'
            )


if __name__ == "__main__":
    main()
