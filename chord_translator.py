from __future__ import annotations

import argparse
import ctypes
import json
import queue
import threading
import time
from pathlib import Path
from ctypes import wintypes

import keyboard


ORDER = ["A", "B", "C", "D", "1", "2", "3", "4"]
THUMBS = {"L", "R"}
ROOMS = {"H", "L", "R"}
SHIFT_KEYS = {"shift", "left shift", "right shift"}
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ULONG_PTR = wintypes.WPARAM
USER32 = ctypes.WinDLL("user32", use_last_error=True)


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


class ChordTranslator:
    def __init__(self, layout_path: Path) -> None:
        data = json.loads(layout_path.read_text(encoding="utf-8"))
        self.layout_path = layout_path
        self.name = data.get("name", layout_path.stem)
        self.layout = data["layout"]
        self.bindings = data.get(
            "bindings",
            {"A": "q", "B": "w", "C": "e", "D": "r", "1": "i", "2": "o", "3": "p", "4": "[", "L": "c", "R": "m"},
        )
        self.physical_to_logical = {
            str(physical).lower(): logical
            for logical, physical in self.bindings.items()
            if physical
        }
        self.mapped_physical = set(self.physical_to_logical)
        self.toggle_physical = {
            self.bindings["D"].lower(),
            self.bindings["1"].lower(),
            self.bindings["L"].lower(),
            self.bindings["R"].lower(),
        }
        self.chords: dict[tuple[str, tuple[str, ...]], str] = {}
        for entry in data.get("chords", data.get("chord_map", [])):
            room = entry["room"]
            keys = tuple(sorted(map(str, entry["keys"])))
            output = entry.get("text") or entry.get("output")
            if room in ROOMS and keys and output:
                self.chords[(room, keys)] = str(output).lower()

        self.lock = threading.RLock()
        self.enabled = False
        self.down: set[str] = set()
        self.shift_down = False
        self.stroke: dict[str, object] | None = None
        self.suppression_hooks: list[object] = []
        self.ignore_until_toggle_release = False
        self.injecting = False
        self.stop_event = threading.Event()
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.output_thread = threading.Thread(target=self.output_worker, daemon=True)

    def run(self) -> None:
        combo = "+".join(self.bindings[item] for item in ["D", "1", "L", "R"])
        self.output_thread.start()
        keyboard.hook(self.handle_passive_event, suppress=False)
        keyboard.add_hotkey("ctrl+alt+esc", self.stop, suppress=False)

        print(f"Loaded: {self.name}")
        print(f"Layout: {self.layout_path}")
        print(f"Toggle: D+1+L+R ({combo})")
        print("Exit: Ctrl+Alt+Esc")
        print("State: disabled")
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

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.stroke = None
        self.ignore_until_toggle_release = True
        if self.enabled:
            self.enable_suppression()
        else:
            self.disable_suppression()
        print(f"State: {'enabled' if self.enabled else 'disabled'}")

    def update_down(self, event: keyboard.KeyboardEvent) -> str | None:
        physical = str(event.name or "").lower()
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
            physical_name = str(event.name or "").lower()
            if physical_name in SHIFT_KEYS:
                self.shift_down = event.event_type == "down"
                return
            if self.enabled and not self.ignore_until_toggle_release:
                return
            physical = self.update_down(event)
            if physical is None:
                return
            if self.ignore_until_toggle_release:
                if not self.toggle_physical.intersection(self.down):
                    self.ignore_until_toggle_release = False
                return
            if event.event_type == "down" and self.toggle_physical.issubset(self.down):
                self.toggle()

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
            physical = str(event.name or "").lower()
            if physical in SHIFT_KEYS:
                self.shift_down = event.event_type == "down"
                return
            logical = self.physical_to_logical.get(physical)
            if logical is None:
                return

            if self.ignore_until_toggle_release:
                if event.event_type == "up":
                    self.down.discard(physical)
                    if not self.toggle_physical.intersection(self.down):
                        self.ignore_until_toggle_release = False
                elif event.event_type == "down":
                    self.down.add(physical)
                return

            if event.event_type == "down":
                self.reconcile_down_state()
                if physical in self.down:
                    return
                self.down.add(physical)
                if self.toggle_physical.issubset(self.down):
                    self.toggle()
                    return
                self.begin_stroke()
                if logical in THUMBS:
                    self.stroke["thumbs"].add(logical)  # type: ignore[union-attr]
                elif logical in ORDER and logical not in self.stroke["keys"]:  # type: ignore[operator]
                    self.stroke["keys"].add(logical)  # type: ignore[union-attr]
                    self.stroke["order"].append(logical)  # type: ignore[union-attr]
                return

            if event.event_type != "up":
                return

            self.down.discard(physical)
            self.reconcile_down_state()
            if self.resolve_thumb_space_if_ready(logical):
                return
            if self.stroke and logical in THUMBS and self.stroke["keys"]:  # type: ignore[index]
                self.resolve_stroke()
                return
            if self.stroke and logical in ORDER and not self.any_finger_held():
                self.resolve_stroke()
                return
            if not self.down and self.stroke:
                self.resolve_stroke()

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
        return True

    def resolve_stroke(self) -> None:
        if not self.stroke:
            return
        room = self.active_room()
        keys = tuple(sorted(self.stroke["keys"]))  # type: ignore[arg-type]
        output = ""
        if keys:
            output = self.chords.get((room, keys), "")
            if not output:
                output = "".join(self.layout[room][key].lower() for key in self.stroke["order"])  # type: ignore[index]
        self.stroke = None
        if output:
            if self.shift_down:
                output = output.upper()
            self.emit(output)

    def emit(self, text: str) -> None:
        self.output_queue.put(text)

    def output_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                text = self.output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self.injecting = True
            try:
                send_unicode_text(text)
            except OSError as error:
                print(f"SendInput failed for {text!r}: {error}")
            finally:
                self.injecting = False


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="System-wide translator for the chord typing layout.")
    parser.add_argument("layout", nargs="?", default="layouts/house_extended.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ChordTranslator(Path(args.layout)).run()


if __name__ == "__main__":
    main()
