import json
import re
from collections import Counter, defaultdict
from pathlib import Path

LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
MIN_N = 2
MAX_N = 8
MIN_COUNT = {2: 4_000, 3: 4_000, 4: 4_000, 5: 1_000, 6: 1_000, 7: 1_000, 8: 1_000}
DOMINANCE = {2: 0.72, 3: 0.56, 4: 0.44, 5: 0.50, 6: 0.45, 7: 0.40, 8: 0.35}
CORPUS = Path('allDurf.txt')
EXTENDED = Path('layouts/house_extended.json')


def slot_key(entry):
    return (str(entry['room']), tuple(sorted(map(str, entry['keys']))))


def lookup(layout):
    out = {}
    for room, keys in layout.items():
        for key_name, letter in keys.items():
            out[letter] = (room, key_name)
    return out


def scan():
    words = re.findall(r'[A-Z]+', CORPUS.read_text(encoding='utf-8', errors='ignore').upper())
    grouped = defaultdict(Counter)
    for word in words:
        for size in range(MIN_N, min(MAX_N, len(word)) + 1):
            for start in range(len(word) - size + 1):
                gram = word[start:start + size]
                if len(set(gram)) != size:
                    continue
                grouped[''.join(sorted(gram))][gram] += 1
    stats = []
    for letters, counter in grouped.items():
        output, count = counter.most_common(1)[0]
        total = sum(counter.values())
        size = len(letters)
        if count < MIN_COUNT[size]:
            continue
        dominance = count / total
        if dominance < DOMINANCE[size]:
            continue
        stats.append({
            'letters': letters,
            'output': output,
            'count': count,
            'total_same_keys': total,
            'dominance': dominance,
            'size': size,
        })
    return sorted(stats, key=lambda item: (-item['count'], -item['dominance'], item['output']))


def candidate_entry(room, keys, stat, native_rooms, action):
    size = stat['size']
    return {
        'room': room,
        'keys': keys,
        'output': stat['output'],
        'count': stat['count'],
        'total_same_keys': stat['total_same_keys'],
        'dominance': round(stat['dominance'], 4),
        'house_extended_cross_room': True,
        'source': f'cross-room {size}-gram candidate' if size <= 4 else f'additive cross-house {size}-gram candidate',
        action: True,
    }


def main():
    data = json.loads(EXTENDED.read_text(encoding='utf-8'))
    letter_lookup = lookup(data['layout'])
    existing_by_slot = {slot_key(entry): index for index, entry in enumerate(data['chords'])}
    additions = []
    replacements = []

    for stat in scan():
        if any(letter not in letter_lookup for letter in stat['output']):
            continue
        native_rooms = [letter_lookup[letter][0] for letter in stat['output']]
        if len(set(native_rooms)) <= 1:
            continue
        keys = sorted(letter_lookup[letter][1] for letter in stat['output'])
        if len(set(keys)) != stat['size']:
            continue

        # Prefer a room already touched by the phrase, then any remaining room.
        rooms = []
        for room in ('H', 'R', 'L'):
            if room in native_rooms:
                rooms.append(room)
        for room in ('H', 'R', 'L'):
            if room not in rooms:
                rooms.append(room)

        placed = False
        for room in rooms:
            skey = (room, tuple(keys))
            index = existing_by_slot.get(skey)
            if index is None:
                entry = candidate_entry(room, keys, stat, native_rooms, 'added_cross_room_candidate')
                data['chords'].append(entry)
                existing_by_slot[skey] = len(data['chords']) - 1
                additions.append(entry)
                placed = True
                break

            existing = data['chords'][index]
            if existing.get('house_extended_cross_room'):
                continue
            if int(existing.get('count') or 0) >= stat['count']:
                continue

            entry = candidate_entry(room, keys, stat, native_rooms, 'replaced_weaker_single_room_candidate')
            entry['replaced_single_room_chord'] = {
                'output': existing.get('output'),
                'count': existing.get('count'),
                'dominance': existing.get('dominance'),
                'source': existing.get('source'),
            }
            data['chords'][index] = entry
            replacements.append((existing, entry))
            placed = True
            break

    EXTENDED.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print('added', len(additions), 'replaced', len(replacements), 'total', len(data['chords']))
    print('top additions')
    for entry in additions[:30]:
        print(entry['room'], '+'.join(entry['keys']), entry['output'], entry['count'], entry['dominance'])
    print('top replacements')
    for old, new in replacements[:30]:
        print(new['room'], '+'.join(new['keys']), old.get('output'), old.get('count'), '->', new['output'], new['count'], new['dominance'])


if __name__ == '__main__':
    main()
