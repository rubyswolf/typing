import json
import re
from collections import Counter, defaultdict
from pathlib import Path

LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
MAX_N = 8
MIN_N = 5
HOUSE_MIN_COUNT = 1000
EXTENDED_MIN_COUNT = 1000
DOMINANCE_BY_SIZE = {5: 0.50, 6: 0.45, 7: 0.40, 8: 0.35}
HOUSE_LIMIT = 80
EXTENDED_LIMIT = 160

ROOT = Path('.')
CORPUS = ROOT / 'allDurf.txt'
HOUSE = ROOT / 'layouts' / 'house.json'
EXTENDED = ROOT / 'layouts' / 'house_extended.json'


def key(entry):
    return (str(entry['room']), tuple(sorted(map(str, entry['keys']))))


def scan():
    words = re.findall(r'[A-Z]+', CORPUS.read_text(encoding='utf-8', errors='ignore').upper())
    counts_by_size = {size: Counter() for size in range(MIN_N, MAX_N + 1)}
    grouped_by_letters = defaultdict(Counter)
    for word in words:
        for size in range(MIN_N, min(MAX_N, len(word)) + 1):
            for start in range(len(word) - size + 1):
                gram = word[start:start + size]
                if len(set(gram)) != size:
                    continue
                counts_by_size[size][gram] += 1
                grouped_by_letters[''.join(sorted(gram))][gram] += 1
    stats = {}
    for letters, counter in grouped_by_letters.items():
        output, count = counter.most_common(1)[0]
        stats[letters] = {
            'output': output,
            'count': count,
            'total': sum(counter.values()),
            'dominance': count / sum(counter.values()),
            'size': len(letters),
        }
    return stats


def lookup(layout):
    out = {}
    for room, keys in layout.items():
        for key_name, letter in keys.items():
            out[letter] = (room, key_name)
    return out


def append_house(data, stats):
    layout = data['layout']
    used = {key(entry) for entry in data['chords']}
    additions = []
    for letters, stat in sorted(stats.items(), key=lambda item: (-item[1]['count'], -item[1]['dominance'], item[1]['output'])):
        size = stat['size']
        if stat['count'] < HOUSE_MIN_COUNT or stat['dominance'] < DOMINANCE_BY_SIZE[size]:
            continue
        rooms = []
        keys = []
        ok = True
        for letter in stat['output']:
            found = False
            for room, key_map in layout.items():
                for key_name, mapped_letter in key_map.items():
                    if mapped_letter == letter:
                        rooms.append(room)
                        keys.append(key_name)
                        found = True
                        break
                if found:
                    break
            if not found:
                ok = False
                break
        if not ok or len(set(rooms)) != 1 or len(set(keys)) != size:
            continue
        room = rooms[0]
        keys = sorted(keys)
        if (room, tuple(keys)) in used:
            continue
        entry = {
            'room': room,
            'keys': keys,
            'output': stat['output'],
            'count': stat['count'],
            'dominance': round(stat['dominance'], 4),
            'added_high_order_ngram': True,
            'source': f'{size}-gram additive candidate',
        }
        data['chords'].append(entry)
        used.add((room, tuple(keys)))
        additions.append(entry)
        if len(additions) >= HOUSE_LIMIT:
            break
    return additions


def append_extended(data, stats):
    layout = data['layout']
    letter_lookup = lookup(layout)
    used = {key(entry) for entry in data['chords']}
    additions = []
    for letters, stat in sorted(stats.items(), key=lambda item: (-item[1]['count'], -item[1]['dominance'], item[1]['output'])):
        size = stat['size']
        if stat['count'] < EXTENDED_MIN_COUNT or stat['dominance'] < DOMINANCE_BY_SIZE[size]:
            continue
        if any(letter not in letter_lookup for letter in stat['output']):
            continue
        keys = [letter_lookup[letter][1] for letter in stat['output']]
        if len(set(keys)) != size:
            continue
        native_rooms = [letter_lookup[letter][0] for letter in stat['output']]
        preferred_rooms = []
        for room in ('H', 'R', 'L'):
            if room in native_rooms:
                preferred_rooms.append(room)
        for room in ('H', 'R', 'L'):
            if room not in preferred_rooms:
                preferred_rooms.append(room)
        for room in preferred_rooms:
            sorted_keys = sorted(keys)
            if (room, tuple(sorted_keys)) in used:
                continue
            entry = {
                'room': room,
                'keys': sorted_keys,
                'output': stat['output'],
                'count': stat['count'],
                'dominance': round(stat['dominance'], 4),
                'house_extended_cross_room': len(set(native_rooms)) > 1,
                'added_high_order_ngram': True,
                'source': f'additive cross-house {size}-gram candidate' if len(set(native_rooms)) > 1 else f'additive {size}-gram candidate',
            }
            data['chords'].append(entry)
            used.add((room, tuple(sorted_keys)))
            additions.append(entry)
            break
        if len(additions) >= EXTENDED_LIMIT:
            break
    return additions


stats = scan()
for path, appender in [(HOUSE, append_house), (EXTENDED, append_extended)]:
    data = json.loads(path.read_text(encoding='utf-8'))
    before = len(data['chords'])
    additions = appender(data, stats)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print(path)
    print('before', before, 'after', len(data['chords']), 'added', len(additions))
    for entry in additions[:20]:
        print(entry['room'], '+'.join(entry['keys']), entry['output'], entry['count'], entry['dominance'])
    print()
