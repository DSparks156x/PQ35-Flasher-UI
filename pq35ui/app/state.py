"""Shared app state: the selected interface and module, the identified ECU,
and the one hardware job allowed at a time."""
from PySide6.QtCore import QObject, Signal

from .storage import EcuRecord, ecu_key


class AppState(QObject):
    changed = Signal()
    progress = Signal(dict)
    busy_changed = Signal(bool)
    open_in_flash = Signal(str)
    open_patched_in_flash = Signal(str, str)  # patched image, the image it was patched from
    refresh_interfaces = Signal()
    start_recovery = Signal()

    def __init__(self, pool, settings, parent=None):
        super().__init__(parent)
        self.pool = pool
        self.settings = settings
        self.adapter = None
        self.family = None
        self.families = []
        self.identity = None
        self.record = None
        self.busy = False
        self.hello = {}
        pool.event.connect(self._event)

    def _event(self, message):
        if message.get("event") == "progress":
            self.progress.emit(message)

    @staticmethod
    def same_adapter(a, b) -> bool:
        keys = ("adapter", "dll", "channel", "serial")
        return bool(a and b) and all(a.get(k) == b.get(k) for k in keys)

    def set_adapter(self, adapter):
        if self.same_adapter(adapter, self.adapter):
            # Same device, fresher details (e.g. a SocketCAN link came up).
            self.adapter = adapter
            self.changed.emit()
            return
        if adapter != self.adapter:
            self.adapter = adapter
            self.identity = None
            self.record = None
            self.changed.emit()

    def set_family(self, family):
        if family != self.family:
            self.family = family
            self.identity = None
            self.record = None
            self.changed.emit()

    def select_module(self, family, identity=None):
        """Pick the module to work on (from the Connect page), optionally with
        the identification a scan already read from it."""
        self.settings.set("module", family["name"] if family else None)
        self.family = family
        self.identity = None
        self.record = None
        if identity:
            self.set_identity(identity)  # emits changed
        else:
            self.changed.emit()

    def family_by_name(self, name):
        return next((f for f in self.families if f["name"] == name), None)

    def set_identity(self, identity):
        self.identity = identity
        self.record = EcuRecord(ecu_key(self.family["name"], identity)) if identity else None
        self.changed.emit()

    def part(self) -> str:
        if not self.identity:
            return ""
        return (self.identity.get("software_part_number") or self.identity.get("part_number") or "").strip()

    def run(self, operation, params=None, on_done=None, on_error=None):
        """Start a hardware job. Returns False (and reports) if one can't start."""
        if self.busy:
            if on_error:
                on_error({"message": "Another operation is still running."})
            return False
        if not self.adapter or (not self.family and operation != "scan"):
            if on_error:
                on_error({"message": "Choose an interface and a module first."})
            return False
        try:
            client = self.pool.for_adapter(self.adapter)
        except RuntimeError as exc:
            if on_error:
                on_error({"message": str(exc)})
            return False
        adapter = {k: v for k, v in self.adapter.items() if k in ("adapter", "dll", "channel", "serial", "bus")}
        request = dict(params or {}, operation=operation, adapter=adapter)
        if self.family:
            request["module"] = self.family["alias"]
        self._set_busy(True)

        def done(result):
            self._set_busy(False)
            if on_done:
                on_done(result)

        def failed(error):
            self._set_busy(False)
            if on_error:
                on_error(error)

        client.call("run", request, done, failed)
        return True

    def offline(self, method, params, on_done=None, on_error=None):
        self.pool.native().call(method, params, on_done, on_error)

    def _set_busy(self, busy):
        self.busy = busy
        self.busy_changed.emit(busy)
