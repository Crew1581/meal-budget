import asyncio


class GenerationSlot:
    """Общий слот worker/HTTP; только один event loop и один процесс."""

    def __init__(self):
        self.busy = False
        self.changed = asyncio.Event()

    def try_acquire(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        return True

    async def acquire(self) -> None:
        while not self.try_acquire():
            self.changed.clear()
            await self.changed.wait()

    def release(self) -> None:
        self.busy = False
        self.changed.set()
