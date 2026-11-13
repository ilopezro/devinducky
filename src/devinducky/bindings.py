"""Slot -> session bindings, persisted to bindings.json."""

import json
import os
import shutil
import tempfile


class Bindings:
    def __init__(self, path, num_slots):
        self.path = path
        self.num_slots = num_slots
        self.slots = {}          # slot index -> session_id

    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
            self.slots = {int(k): v for k, v in raw.items() if 0 <= int(k) < self.num_slots and v}
        except (FileNotFoundError, ValueError):
            self.slots = {}
        return self

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({str(k): v for k, v in sorted(self.slots.items())}, f, indent=1)
        os.replace(tmp, self.path)

    def bind(self, slot, session_id):
        """Bind slot to session_id; if bound elsewhere it moves here. Returns the old slot or None."""
        moved_from = self.slot_of(session_id)
        if moved_from is not None and moved_from != slot:
            del self.slots[moved_from]
        self.slots[slot] = session_id
        return moved_from if moved_from != slot else None

    def unbind(self, slot):
        return self.slots.pop(slot, None)

    def clear(self):
        """Unbind every slot, keeping the saved file as <path>.bak. Returns how many were bound."""
        n = len(self.slots)
        if n and os.path.exists(self.path):
            shutil.copyfile(self.path, self.path + ".bak")
        self.slots = {}
        return n

    def slot_of(self, session_id):
        return next((s for s, sid in self.slots.items() if sid == session_id), None)

    def first_free(self):
        return next((s for s in range(self.num_slots) if s not in self.slots), None)

    def get(self, slot):
        return self.slots.get(slot)

    def ids(self):
        return list(self.slots.values())

    def ordered(self):
        return sorted(self.slots)
