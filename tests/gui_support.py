"""Small GUI test helpers, without application behavior reimplementation."""

from PySide6.QtCore import QEventLoop, QTimer


def wait_until(predicate, timeout=20000):
    """Wait on the Qt event loop so real worker completions are delivered."""
    if predicate():
        return
    loop = QEventLoop()
    poll, deadline = QTimer(), QTimer()
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start(10)
    deadline.start(timeout)
    try:
        loop.exec()
    finally:
        poll.stop()
        deadline.stop()
    assert predicate(), "GUI operation did not finish"


class FakeCredentialStore:
    def __init__(self) -> None:
        self.passwords: dict[object, str] = {}
        self.set_calls: list[tuple[object, str]] = []
        self.delete_calls: list[object] = []

    def get_password(self, target: object) -> str | None:
        return self.passwords.get(target)

    def set_password(self, target: object, password: str) -> None:
        self.set_calls.append((target, password))
        self.passwords[target] = password

    def delete_password(self, target: object) -> None:
        self.delete_calls.append(target)
        self.passwords.pop(target, None)
