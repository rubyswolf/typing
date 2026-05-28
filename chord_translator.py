from __future__ import annotations

import argparse
import ctypes
import json
import queue
import threading
import time
import tkinter as tk
from collections import defaultdict, deque
from pathlib import Path
from ctypes import wintypes

import keyboard


ORDER = ["A", "B", "C", "D", "1", "2", "3", "4"]
THUMBS = {"L", "R"}
ROOMS = {"H", "L", "R", "A"}
SHIFT_KEYS = {"shift", "left shift", "right shift"}
WINDOWS_KEYS = {"windows", "left windows", "right windows"}
COACH_PAIR_WINDOW = 3.0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ULONG_PTR = wintypes.WPARAM
USER32 = ctypes.WinDLL("user32", use_last_error=True)
BACKSPACE_CHORD = ("1", "2", "3", "4")


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


USER32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
USER32.SendInput.restype = wintypes.UINT
USER32.GetKeyState.argtypes = (ctypes.c_int,)
USER32.GetKeyState.restype = ctypes.c_short

VK_CAPITAL = 0x14
VK_BACK = 0x08


def normalize_physical_key(key: str) -> str:
    key = str(key).lower()
    if key in {" ", "spacebar"}:
        return "space"
    return key


def caps_lock_on() -> bool:
    return bool(USER32.GetKeyState(VK_CAPITAL) & 1)


class ChordTranslator:
    def __init__(self, layout_path: Path) -> None:
        data = json.loads(layout_path.read_text(encoding="utf-8"))
        self.layout_path = layout_path
        self.name = data.get("name", layout_path.stem)
        self.layout = data["layout"]
        self.bindings = data.get(
            "bindings",
            {"A": "q", "B": "w", "C": "e", "D": "r", "1": "u", "2": "i", "3": "o", "4": "p", "L": "c", "R": " "},
        )
        self.physical_to_logical = {
            normalize_physical_key(physical): logical
            for logical, physical in self.bindings.items()
            if physical
        }
        self.mapped_physical = set(self.physical_to_logical)
        self.chords: dict[tuple[str, tuple[str, ...]], str] = {}
        self.chord_counts: dict[tuple[str, tuple[str, ...]], int] = {}
        self.output_to_chords: dict[str, list[tuple[str, tuple[str, ...], int]]] = defaultdict(list)
        for entry in data.get("chords", data.get("chord_map", [])):
            room = entry["room"]
            keys = tuple(sorted(map(str, entry["keys"])))
            output = entry.get("text") or entry.get("output")
            if room in ROOMS and keys and output:
                output_text = str(output).lower()
                self.chords[(room, keys)] = output_text
                self.chord_counts[(room, keys)] = int(entry.get("count") or 0)
                self.output_to_chords[output_text].append((room, keys, int(entry.get("count") or 0)))
        for entries in self.output_to_chords.values():
            entries.sort(key=lambda item: item[2], reverse=True)
        self.letter_counts = data.get("letter_counts") or data.get("letterCounts") or {}
        self.space_count = int(data.get("space_count") or data.get("spaceCount") or 0)

        self.lock = threading.RLock()
        self.enabled = False
        self.down: set[str] = set()
        self.shift_down = False
        self.stroke: dict[str, object] | None = None
        self.suppression_hooks: list[object] = []
        self.injecting = False
        self.recent_strokes: deque[dict[str, object]] = deque(maxlen=8)
        self.output_lengths: list[int] = []
        self.pending_tip: tuple[str, str, str] | None = None
        self.last_toggle_time = 0.0
        self.pending_windows: set[str] = set()
        self.forwarded_windows: set[str] = set()
        self.win_space_combo_active = False
        self.next_press_id = 1
        self.key_press_ids: dict[str, int] = {}
        self.thumb_press_ids: dict[str, int] = {}
        self.stop_event = threading.Event()
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.overlay_queue: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self.output_thread = threading.Thread(target=self.output_worker, daemon=True)
        self.overlay_thread = threading.Thread(target=run_overlay, args=(self.overlay_queue, self.stop_event), daemon=True)

    def run(self) -> None:
        self.output_thread.start()
        self.overlay_thread.start()
        keyboard.hook(self.handle_toggle_event, suppress=True)
        keyboard.hook(self.handle_passive_event, suppress=False)
        keyboard.add_hotkey("ctrl+alt+esc", self.stop, suppress=False)

        print(f"Loaded: {self.name}")
        print(f"Layout: {self.layout_path}")
        print("Toggle: Win+Space")
        print("Exit: Ctrl+Alt+Esc")
        try:
            while not self.stop_event.is_set():
                time.sleep(0.1)
        finally:
            self.disable_suppression()
            keyboard.unhook_all()
            keyboard.unhook_all_hotkeys()
            print("Stopped")

    def stop(self) -> None:
        self.stop_event.set()

    def enable_suppression(self) -> None:
        self.disable_suppression()
        for physical in sorted(self.mapped_physical):
            def callback(event: keyboard.KeyboardEvent, _physical: str = physical) -> None:
                self.handle_enabled_event(event)

            self.suppression_hooks.append(keyboard.hook_key(physical, callback, suppress=True))

    def disable_suppression(self) -> None:
        for hook in self.suppression_hooks:
            try:
                keyboard.unhook(hook)
            except (KeyError, ValueError):
                pass
        self.suppression_hooks.clear()

    def any_windows_key_down(self) -> bool:
        return bool(self.pending_windows or self.forwarded_windows) or any(
            key in self.down or keyboard.is_pressed(key) for key in WINDOWS_KEYS
        )

    def handle_toggle_event(self, event: keyboard.KeyboardEvent) -> bool:
        physical = normalize_physical_key(event.name or "")
        if self.injecting:
            return True

        if physical in WINDOWS_KEYS:
            if event.event_type == "down":
                if physical not in self.pending_windows and physical not in self.forwarded_windows:
                    self.pending_windows.add(physical)
                return False

            if event.event_type == "up":
                if physical in self.forwarded_windows:
                    self.forwarded_windows.discard(physical)
                    self.replay_key_up(physical)
                    return False
                if physical in self.pending_windows:
                    self.pending_windows.discard(physical)
                    if not self.win_space_combo_active:
                        self.replay_key_tap(physical)
                    elif not self.pending_windows:
                        self.win_space_combo_active = False
                    return False
            return True

        if physical == "space" and event.event_type == "down" and self.pending_windows:
            self.win_space_combo_active = True
            self.toggle_hotkey()
            return False

        if physical == "space" and event.event_type == "up" and self.win_space_combo_active:
            return False

        if event.event_type == "down" and self.pending_windows and not self.win_space_combo_active:
            self.forward_pending_windows()

        return True

    def forward_pending_windows(self) -> None:
        for physical in sorted(self.pending_windows):
            self.replay_key_down(physical)
            self.forwarded_windows.add(physical)
        self.pending_windows.clear()

    def replay_key_down(self, physical: str) -> None:
        self.injecting = True
        try:
            keyboard.press(physical)
        finally:
            self.injecting = False

    def replay_key_up(self, physical: str) -> None:
        self.injecting = True
        try:
            keyboard.release(physical)
        finally:
            self.injecting = False

    def replay_key_tap(self, physical: str) -> None:
        self.injecting = True
        try:
            keyboard.press_and_release(physical)
        finally:
            self.injecting = False

    def toggle_hotkey(self) -> None:
        now = time.monotonic()
        if now - self.last_toggle_time < 0.35:
            return
        self.last_toggle_time = now
        self.toggle()

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.stroke = None
        self.output_lengths.clear()
        self.down.difference_update(self.mapped_physical)
        if self.enabled:
            self.enable_suppression()
        else:
            self.disable_suppression()
        self.update_overlay()
        self.overlay_queue.put(
            (
                "switch",
                "House" if self.enabled else "QWERTY",
                "Win+Space",
                "enabled" if self.enabled else "disabled",
            )
        )

    def update_down(self, event: keyboard.KeyboardEvent) -> str | None:
        physical = normalize_physical_key(event.name or "")
        if physical not in self.mapped_physical:
            return None
        if event.event_type == "down":
            self.down.add(physical)
        elif event.event_type == "up":
            self.down.discard(physical)
        return physical

    def handle_passive_event(self, event: keyboard.KeyboardEvent) -> None:
        if self.injecting:
            return
        with self.lock:
            physical_name = normalize_physical_key(event.name or "")
            if physical_name in SHIFT_KEYS:
                self.shift_down = event.event_type == "down"
                return
            if self.enabled:
                return
            physical = self.update_down(event)
            if physical is None:
                return

    def begin_stroke(self) -> None:
        if self.stroke is not None:
            return
        keys: set[str] = set()
        order: list[str] = []
        thumbs: set[str] = set()
        for physical in self.down:
            logical = self.physical_to_logical.get(physical)
            if logical in THUMBS:
                thumbs.add(logical)
            elif logical in ORDER and logical not in keys:
                keys.add(logical)
                order.append(logical)
        self.stroke = {"keys": keys, "order": order, "thumbs": thumbs}

    def active_room(self) -> str:
        self.reconcile_down_state()
        thumbs = set()
        if self.stroke:
            thumbs.update(self.stroke["thumbs"])  # type: ignore[arg-type]
        for physical in self.down:
            logical = self.physical_to_logical.get(physical)
            if logical in THUMBS:
                thumbs.add(logical)
        if "L" in thumbs and "R" not in thumbs:
            return "L"
        if "R" in thumbs and "L" not in thumbs:
            return "R"
        if "L" in thumbs and "R" in thumbs:
            return "A"
        return "H"

    def reconcile_down_state(self) -> None:
        stale = {
            physical
            for physical in self.down
            if physical in self.mapped_physical and not keyboard.is_pressed(physical)
        }
        if not stale:
            return

        self.down.difference_update(stale)
        if self.stroke:
            for physical in stale:
                logical = self.physical_to_logical.get(physical)
                if logical in THUMBS:
                    self.stroke["thumbs"].discard(logical)  # type: ignore[union-attr]
                elif logical in ORDER:
                    self.stroke["keys"].discard(logical)  # type: ignore[union-attr]
                    self.stroke["order"] = [  # type: ignore[index]
                        key for key in self.stroke["order"] if key != logical  # type: ignore[index]
                    ]

    def handle_enabled_event(self, event: keyboard.KeyboardEvent) -> None:
        if self.injecting:
            return
        with self.lock:
            physical = normalize_physical_key(event.name or "")
            if physical in SHIFT_KEYS:
                self.shift_down = event.event_type == "down"
                return
            logical = self.physical_to_logical.get(physical)
            if logical is None:
                return

            if event.event_type == "down":
                self.reconcile_down_state()
                if physical in self.down:
                    return
                self.down.add(physical)
                if logical in ORDER:
                    self.key_press_ids[logical] = self.next_press_id
                    self.next_press_id += 1
                elif logical in THUMBS:
                    self.thumb_press_ids[logical] = self.next_press_id
                    self.next_press_id += 1
                self.begin_stroke()
                if logical in THUMBS:
                    self.stroke["thumbs"].add(logical)  # type: ignore[union-attr]
                elif logical in ORDER and logical not in self.stroke["keys"]:  # type: ignore[operator]
                    self.stroke["keys"].add(logical)  # type: ignore[union-attr]
                    self.stroke["order"].append(logical)  # type: ignore[union-attr]
                self.update_overlay()
                return

            if event.event_type != "up":
                return

            self.down.discard(physical)
            self.reconcile_down_state()
            if self.resolve_thumb_space_if_ready(logical):
                self.update_overlay()
                return
            if self.stroke and logical in THUMBS and self.stroke["keys"]:  # type: ignore[index]
                self.resolve_stroke()
                self.update_overlay()
                return
            if self.stroke and logical in ORDER and not self.any_finger_held():
                self.resolve_stroke()
                self.update_overlay()
                return
            if not self.down and self.stroke:
                self.resolve_stroke()
            self.update_overlay()

    def any_finger_held(self) -> bool:
        for physical in self.down:
            logical = self.physical_to_logical.get(physical)
            if logical in ORDER:
                return True
        return False

    def resolve_thumb_space_if_ready(self, logical: str) -> bool:
        if logical not in THUMBS or not self.stroke:
            return False
        keys = self.stroke["keys"]  # type: ignore[assignment]
        thumbs = self.stroke["thumbs"]  # type: ignore[assignment]
        if keys or not {"L", "R"}.issubset(thumbs):
            return False
        self.stroke = None
        self.emit(" ")
        self.record_stroke(" ", "H", (), (), ("L", "R"), True)
        return True

    def resolve_stroke(self) -> None:
        if not self.stroke:
            return
        room = self.active_room()
        keys = tuple(sorted(self.stroke["keys"]))  # type: ignore[arg-type]
        order = tuple(self.stroke["order"])  # type: ignore[arg-type]
        thumbs = tuple(sorted(self.stroke["thumbs"]))  # type: ignore[arg-type]
        output = ""
        used_chord = False
        if keys:
            if keys == BACKSPACE_CHORD:
                self.stroke = None
                self.emit("\b")
                self.update_overlay()
                return
            output = self.chords.get((room, keys), "")
            used_chord = bool(output)
            if not used_chord and room != "A":
                output = "".join(self.layout[room][key].lower() for key in self.stroke["order"])  # type: ignore[index]
        self.stroke = None
        if output:
            raw_output = output
            if self.shift_down ^ caps_lock_on():
                output = output.upper()
            self.emit(output)
            self.record_stroke(raw_output, room, keys, order, thumbs, used_chord)

    def emit(self, text: str) -> None:
        self.output_queue.put(text)

    def backspace_count(self) -> int:
        if self.output_lengths:
            return self.output_lengths.pop()
        return 1

    def record_stroke(
        self,
        output: str,
        room: str,
        keys: tuple[str, ...],
        order: tuple[str, ...],
        thumbs: tuple[str, ...],
        used_chord: bool,
    ) -> None:
        stroke = {
            "output": output.lower(),
            "room": room,
            "keys": keys,
            "key_ids": tuple(self.key_press_ids.get(key, 0) for key in keys),
            "order": order,
            "thumbs": thumbs,
            "thumb_ids": tuple(self.thumb_press_ids.get(thumb, 0) for thumb in thumbs),
            "used_chord": used_chord,
            "time": time.monotonic(),
        }
        self.recent_strokes.append(stroke)
        self.detect_inefficient_stroke(stroke)

    def detect_inefficient_stroke(self, stroke: dict[str, object]) -> None:
        if self.detect_changed_keys():
            return
        if self.detect_missed_chord(stroke):
            return
        self.detect_changed_rooms()

    def detect_missed_chord(self, stroke: dict[str, object]) -> bool:
        if stroke["output"] == " ":
            return False
        recent = list(self.recent_strokes)
        for length in range(min(4, len(recent)), 1, -1):
            segment = recent[-length:]
            if any(item["output"] == " " for item in segment):
                continue
            text = "".join(str(item["output"]) for item in segment)
            entries = self.output_to_chords.get(text)
            if not entries:
                continue
            room, keys, count = entries[0]
            self.queue_tip(
                "Missed Chord",
                f"{text.upper()} is {room} {'+'.join(keys)}",
                f"{count:,}" if count else "",
            )
            return True
        return False

    def detect_changed_rooms(self) -> bool:
        if len(self.recent_strokes) < 2:
            return False
        previous, current = self.recent_strokes[-2], self.recent_strokes[-1]
        if time.monotonic() - float(previous["time"]) > COACH_PAIR_WINDOW:
            return False
        if previous["used_chord"] or current["used_chord"]:
            return False
        if previous["room"] != current["room"] or previous["room"] not in {"L", "R"}:
            return False
        if len(previous["keys"]) != 1 or len(current["keys"]) != 1:  # type: ignore[arg-type]
            return False
        if not previous["thumbs"] or not current["thumbs"]:
            return False
        if previous["thumbs"] != current["thumbs"]:
            return False
        previous_thumb_id = previous.get("thumb_ids", (0,))[0]  # type: ignore[index]
        current_thumb_id = current.get("thumb_ids", (0,))[0]  # type: ignore[index]
        if previous_thumb_id and previous_thumb_id == current_thumb_id:
            return False
        room = str(current["room"])
        combined = f"{previous['output']}{current['output']}".upper()
        self.queue_tip(
            "Changed Rooms",
            f"Stay in {room}: hold {room}, tap {previous['keys'][0]} then {current['keys'][0]}",  # type: ignore[index]
            combined,
        )
        return True

    def detect_changed_keys(self) -> bool:
        if len(self.recent_strokes) < 2:
            return False
        previous, current = self.recent_strokes[-2], self.recent_strokes[-1]
        if time.monotonic() - float(previous["time"]) > COACH_PAIR_WINDOW:
            return False
        if previous["used_chord"] or current["used_chord"]:
            return False
        if {previous["room"], current["room"]} != {"L", "R"}:
            return False
        if len(previous["keys"]) != 1 or len(current["keys"]) != 1:  # type: ignore[arg-type]
            return False
        if previous["keys"] != current["keys"]:
            return False
        key = previous["keys"][0]  # type: ignore[index]
        previous_key_id = previous.get("key_ids", (0,))[0]  # type: ignore[index]
        current_key_id = current.get("key_ids", (0,))[0]  # type: ignore[index]
        if previous_key_id and previous_key_id == current_key_id:
            return False
        first_room = str(previous["room"])
        second_room = str(current["room"])
        text = f"{previous['output']}{current['output']}".upper()
        self.queue_tip(
            "Changed Keys",
            f"Hold {key}, tap {first_room} then {second_room}",
            text,
        )
        return True

    def queue_tip(self, title: str, detail: str, meta: str = "") -> None:
        self.pending_tip = (title, detail, meta)

    def update_overlay(self) -> None:
        if not self.enabled:
            self.overlay_queue.put(("hide", "", "", ""))
            return

        if not self.stroke:
            if self.pending_tip:
                title, detail, meta = self.pending_tip
                self.pending_tip = None
                self.overlay_queue.put(("tip", title, detail, meta))
                return
            self.overlay_queue.put(("idle", "", "", ""))
            return

        room = self.active_room()
        keys = tuple(sorted(self.stroke["keys"]))  # type: ignore[arg-type]
        thumbs = self.stroke["thumbs"]  # type: ignore[assignment]

        if not keys and {"L", "R"}.issubset(thumbs):
            self.overlay_queue.put(("show", "SPACE", "L+R", f"{self.space_count:,}"))
            return

        if not keys:
            self.overlay_queue.put(("idle", "", "", ""))
            return

        if keys == BACKSPACE_CHORD:
            self.overlay_queue.put(("chord", "BACKSPACE", "any 1+2+3+4", ""))
            return

        output = self.chords.get((room, keys))
        if output:
            count = self.chord_counts.get((room, keys), 0)
            self.overlay_queue.put(("chord", output.upper(), f"{room} {'+'.join(keys)}", f"{count:,}"))
            return

        if len(keys) == 1 and room != "A":
            letter = self.layout[room][keys[0]]
            count = int(self.letter_counts.get(letter, 0))
            self.overlay_queue.put(("single", letter, f"{room} {keys[0]}", f"{count:,}"))
            return

        if room == "A":
            self.overlay_queue.put(("miss", "ATTIC", f"{room} {'+'.join(keys)}", "no chord"))
            return

        sequential = "".join(self.layout[room][key] for key in self.stroke["order"])  # type: ignore[index]
        self.overlay_queue.put(("miss", sequential, f"{room} {'+'.join(keys)}", "no chord"))

    def output_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                text = self.output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self.injecting = True
            try:
                if text == "\b":
                    send_virtual_key(VK_BACK, self.backspace_count())
                else:
                    send_unicode_text(text)
                    self.output_lengths.append(len(text))
            except OSError as error:
                print(f"SendInput failed for {text!r}: {error}")
            finally:
                self.injecting = False


def run_overlay(
    overlay_queue: queue.Queue[tuple[str, str, str, str]],
    stop_event: threading.Event,
) -> None:
    root = tk.Tk()
    root.withdraw()

    live = tk.Toplevel(root)
    live.withdraw()
    live.overrideredirect(True)
    live.attributes("-topmost", True)
    live.configure(bg="#15110d")

    live_frame = tk.Frame(live, bg="#15110d", padx=14, pady=10)
    live_frame.pack()
    live_title = tk.Label(live_frame, text="", bg="#15110d", fg="#f8f2e8", font=("Segoe UI", 28, "bold"))
    live_title.pack(anchor="e")
    live_subtitle = tk.Label(live_frame, text="", bg="#15110d", fg="#d8cbb9", font=("Segoe UI", 11))
    live_subtitle.pack(anchor="e")
    live_count = tk.Label(live_frame, text="", bg="#15110d", fg="#8fc7b0", font=("Consolas", 10))
    live_count.pack(anchor="e")

    tip = tk.Toplevel(root)
    tip.withdraw()
    tip.overrideredirect(True)
    tip.attributes("-topmost", True)
    tip.configure(bg="#4b3511")

    tip_frame = tk.Frame(tip, bg="#4b3511", padx=18, pady=12)
    tip_frame.pack()
    tip_title = tk.Label(tip_frame, text="", bg="#4b3511", fg="#fff7dc", font=("Segoe UI", 18, "bold"))
    tip_title.pack(anchor="center")
    tip_subtitle = tk.Label(tip_frame, text="", bg="#4b3511", fg="#f1d9a2", font=("Segoe UI", 11))
    tip_subtitle.pack(anchor="center")
    tip_count = tk.Label(tip_frame, text="", bg="#4b3511", fg="#ffd166", font=("Consolas", 10))
    tip_count.pack(anchor="center")

    colors = {
        "show": ("#15110d", "#f8f2e8"),
        "single": ("#14323a", "#f8f2e8"),
        "chord": ("#123323", "#f8f2e8"),
        "miss": ("#5a231d", "#fff3d7"),
    }
    tip_until = 0.0

    def place_top_right() -> None:
        live.update_idletasks()
        width = live.winfo_reqwidth()
        height = live.winfo_reqheight()
        x = live.winfo_screenwidth() - width - 18
        y = 18
        live.geometry(f"{width}x{height}+{x}+{y}")

    def place_top_center() -> None:
        tip.update_idletasks()
        width = tip.winfo_reqwidth()
        height = tip.winfo_reqheight()
        x = (tip.winfo_screenwidth() - width) // 2
        y = 18
        tip.geometry(f"{width}x{height}+{x}+{y}")

    def apply(kind: str, main: str, sub: str, meta: str) -> None:
        nonlocal tip_until
        if kind == "idle":
            live.withdraw()
            return
        if kind == "hide":
            live.withdraw()
            tip.withdraw()
            tip_until = 0.0
            return
        if kind in {"tip", "switch"}:
            tip_until = time.monotonic() + 2.4
            if kind == "switch":
                bg = "#16253f" if meta == "enabled" else "#2b2f36"
                fg = "#f7fbff"
                sub_fg = "#b8c7e0"
                meta_fg = "#8fd3ff" if meta == "enabled" else "#c8cdd5"
            else:
                bg = "#4b3511"
                fg = "#fff7dc"
                sub_fg = "#f1d9a2"
                meta_fg = "#ffd166"
            tip.configure(bg=bg)
            tip_frame.configure(bg=bg)
            tip_title.configure(bg=bg, fg=fg)
            tip_subtitle.configure(bg=bg, fg=sub_fg)
            tip_count.configure(bg=bg, fg=meta_fg)
            tip_title.configure(text=main)
            tip_subtitle.configure(text=sub)
            tip_count.configure(text=meta)
            place_top_center()
            tip.deiconify()
            return
        else:
            live.lift()
        bg, fg = colors.get(kind, colors["show"])
        live.configure(bg=bg)
        live_frame.configure(bg=bg)
        live_title.configure(text=main, bg=bg, fg=fg)
        live_subtitle.configure(text=sub, bg=bg)
        live_count.configure(text=meta, bg=bg)
        place_top_right()
        live.deiconify()

    def poll() -> None:
        nonlocal tip_until
        if stop_event.is_set():
            root.destroy()
            return
        latest = None
        try:
            while True:
                latest = overlay_queue.get_nowait()
        except queue.Empty:
            pass
        if latest:
            apply(*latest)
        elif tip_until and time.monotonic() >= tip_until:
            tip.withdraw()
            tip_until = 0.0
        root.after(30, poll)

    root.after(30, poll)
    root.mainloop()


def send_unicode_text(text: str) -> None:
    inputs = []
    for char in text:
        code = ord(char)
        inputs.append(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
        inputs.append(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    if not inputs:
        return
    array_type = INPUT * len(inputs)
    sent = USER32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(error, f"SendInput sent {sent}/{len(inputs)} inputs")


def send_virtual_key(vk: int, count: int = 1) -> None:
    inputs = []
    for _ in range(max(1, count)):
        inputs.append(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0))))
        inputs.append(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))))
    array_type = INPUT * len(inputs)
    sent = USER32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        raise OSError(error, f"SendInput sent {sent}/{len(inputs)} inputs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="System-wide translator for the chord typing layout.")
    parser.add_argument("layout", nargs="?", default="layouts/house_extended.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ChordTranslator(Path(args.layout)).run()


if __name__ == "__main__":
    main()
