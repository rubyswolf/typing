import json
import re
from collections import Counter, defaultdict
from pathlib import Path

MIN_N = 2
MAX_N = 8
MIN_COUNT = {2: 4_000, 3: 4_000, 4: 4_000, 5: 1_000, 6: 1_000, 7: 1_000, 8: 1_000}
DOMINANCE = {2: 0.72, 3: 0.56, 4: 0.44, 5: 0.50, 6: 0.45, 7: 0.40, 8: 0.35}
ROOM_PRIORITY = ('R', 'L')
CORPUS = Path('allDurf.txt')
EXTENDED = Path('layouts/house_extended.json')


def slot_key(entry):
    return str(entry['room']), tuple(sorted(map(str, entry['keys'])))


def lookup(layout):
    result = {}
    for room, key_map in layout.items():
        for key, letter in key_map.items():
            result[letter] = (room, key)
    return result


def scan_candidates():
    words = re.findall(r'[A-Z]+', CORPUS.read_text(encoding='utf-8', errors='ignore').upper())
    grouped = defaultdict(Counter)
    for word in words:
        max_size = min(MAX_N, len(word))
        for size in range(MIN_N, max_size + 1):
            for start in range(len(word) - size + 1):
                gram = word[start:start + size]
                if len(set(gram)) != size:
                    continue
                grouped[''.join(sorted(gram))][gram] += 1

    candidates = []
    for letters, counter in grouped.items():
        output, count = counter.most_common(1)[0]
        size = len(letters)
        total = sum(counter.values())
        dominance = count / total
        if count < MIN_COUNT[size] or dominance < DOMINANCE[size]:
            continue
        candidates.append({
            'letters': letters,
            'output': output,
            'count': count,
            'total_same_keys': total,
            'dominance': dominance,
            'size': size,
        })
    return sorted(candidates, key=lambda item: (-item['count'], -item['dominance'], item['output']))


def target_rooms(native_rooms):
    present = set(native_rooms)
    # Cross-room chords belong in a side room that is part of the phrase.
    # Do not place them in home just because home has a free equivalent key set.
    return [room for room in ROOM_PRIORITY if room in present]


def make_entry(room, keys, stat):
    size = stat['size']
    source = f'cross-room {size}-gram candidate' if size <= 4 else f'additive cross-house {size}-gram candidate'
    return {
        'room': room,
        'keys': keys,
        'output': stat['output'],
        'count': stat['count'],
        'total_same_keys': stat['total_same_keys'],
        'dominance': round(stat['dominance'], 4),
        'house_extended_cross_room': True,
        'source': source,
        'added_cross_room_candidate': True,
    }


def cleanup_invalid_home_cross_room_chords(data, existing_by_slot):
    letter_room = {
        letter: room
        for room, key_map in data['layout'].items()
        for key, letter in key_map.items()
    }
    remove_indexes = set()
    moved = []
    upgraded = []
    removed = []

    for index, entry in list(enumerate(data['chords'])):
        output = str(entry.get('output', ''))
        native_rooms = {letter_room.get(letter) for letter in output}
        if entry.get('room') != 'H' or len(native_rooms) <= 1:
            continue
        if not entry.get('house_extended_cross_room'):
            continue

        keys = tuple(sorted(map(str, entry.get('keys', []))))
        placed = False
        for room in target_rooms([room for room in native_rooms if room]):
            target = (room, keys)
            target_index = existing_by_slot.get(target)
            if target_index is None:
                new_entry = dict(entry)
                new_entry['room'] = room
                new_entry['moved_from_invalid_home_cross_room'] = True
                data['chords'].append(new_entry)
                existing_by_slot[target] = len(data['chords']) - 1
                moved.append((entry, new_entry))
                placed = True
                break

            target_entry = data['chords'][target_index]
            if target_entry.get('house_extended_cross_room') and int(target_entry.get('count') or 0) < int(entry.get('count') or 0):
                new_entry = dict(entry)
                new_entry['room'] = room
                new_entry['moved_from_invalid_home_cross_room'] = True
                new_entry['replaced_cross_room_chord'] = {
                    'output': target_entry.get('output'),
                    'count': target_entry.get('count'),
                    'dominance': target_entry.get('dominance'),
                    'source': target_entry.get('source'),
                }
                data['chords'][target_index] = new_entry
                upgraded.append((target_entry, new_entry))
                placed = True
                break

        remove_indexes.add(index)
        if not placed:
            removed.append(entry)

    if remove_indexes:
        data['chords'] = [entry for index, entry in enumerate(data['chords']) if index not in remove_indexes]

    return moved, upgraded, removed


def main():
    data = json.loads(EXTENDED.read_text(encoding='utf-8'))
    letter_lookup = lookup(data['layout'])
    existing_by_slot = {slot_key(entry): index for index, entry in enumerate(data['chords'])}
    additions = []
    upgrades = []
    skipped_blocked = []

    for stat in scan_candidates():
        if any(letter not in letter_lookup for letter in stat['output']):
            continue
        native_rooms = [letter_lookup[letter][0] for letter in stat['output']]
        if len(set(native_rooms)) <= 1:
            continue
        keys = sorted(letter_lookup[letter][1] for letter in stat['output'])
        if len(set(keys)) != stat['size']:
            continue

        for room in target_rooms(native_rooms):
            skey = (room, tuple(keys))
            existing_index = existing_by_slot.get(skey)
            if existing_index is None:
                entry = make_entry(room, keys, stat)
                data['chords'].append(entry)
                existing_by_slot[skey] = len(data['chords']) - 1
                additions.append(entry)
                break

            existing = data['chords'][existing_index]
            # Only replace an existing cross-room shortcut. Same-room/native chords are deliberately protected.
            if not existing.get('house_extended_cross_room'):
                skipped_blocked.append((stat, existing))
                continue
            if int(existing.get('count') or 0) >= stat['count']:
                continue

            entry = make_entry(room, keys, stat)
            entry['replaced_cross_room_chord'] = {
                'output': existing.get('output'),
                'count': existing.get('count'),
                'dominance': existing.get('dominance'),
                'source': existing.get('source'),
            }
            data['chords'][existing_index] = entry
            upgrades.append((existing, entry))
            break

    moved, cleanup_upgrades, removed = cleanup_invalid_home_cross_room_chords(data, existing_by_slot)
    EXTENDED.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print(
        'added',
        len(additions),
        'upgraded_cross_room',
        len(upgrades),
        'blocked_by_native',
        len(skipped_blocked),
        'moved_invalid_home',
        len(moved),
        'cleaned_upgraded',
        len(cleanup_upgrades),
        'removed_invalid_home',
        len(removed),
        'total',
        len(data['chords']),
    )
    print('top additions')
    for entry in additions[:30]:
        print(entry['room'], '+'.join(entry['keys']), entry['output'], entry['count'], entry['dominance'])
    print('top upgrades')
    for old, new in upgrades[:20]:
        print(new['room'], '+'.join(new['keys']), old.get('output'), old.get('count'), '->', new['output'], new['count'], new['dominance'])
    print('top native blocks')
    for stat, existing in skipped_blocked[:20]:
        print(existing['room'], '+'.join(existing['keys']), existing.get('output'), existing.get('count'), 'blocked', stat['output'], stat['count'])


if __name__ == '__main__':
    main()
