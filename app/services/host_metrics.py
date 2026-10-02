import os
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class HostStats:
    cpu_percent: float
    ram_used_bytes: int
    ram_total_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int
    net_out_mbps: float | None
    net_in_mbps: float | None


class HostMetrics:
    """CPU, memory, disk and network figures for the machine the app runs on.

    Network rates are the byte delta since the previous sample, so the first
    sample has none. Loopback is left out.
    """

    clock = staticmethod(time.monotonic)

    def __init__(self) -> None:
        self._last_net: tuple[float, int, int] | None = None

    def sample(self) -> HostStats:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getenv("MEDIA_ROOT") or "/")
        out_mbps, in_mbps = self._net_rates()
        return HostStats(
            cpu_percent=psutil.cpu_percent(interval=None),
            ram_used_bytes=memory.total - memory.available,
            ram_total_bytes=memory.total,
            disk_free_bytes=disk.free,
            disk_total_bytes=disk.total,
            net_out_mbps=out_mbps,
            net_in_mbps=in_mbps,
        )

    def _net_rates(self) -> tuple[float | None, float | None]:
        counters = [c for nic, c in psutil.net_io_counters(pernic=True).items() if nic != "lo"]
        now = self.clock()
        sent = sum(c.bytes_sent for c in counters)
        received = sum(c.bytes_recv for c in counters)
        previous, self._last_net = self._last_net, (now, sent, received)
        if previous is None or now <= previous[0]:
            return None, None
        elapsed = now - previous[0]
        return (
            max(sent - previous[1], 0) * 8 / elapsed / 1_000_000,
            max(received - previous[2], 0) * 8 / elapsed / 1_000_000,
        )


host_metrics = HostMetrics()
