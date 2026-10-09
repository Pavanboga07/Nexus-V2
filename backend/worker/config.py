"""Worker configuration (documented bounds, no hidden behavior)."""

from __future__ import annotations

from dataclasses import dataclass

#: Headroom a claim lease must keep beyond one full send attempt
#: (handshake wait + acknowledgement wait). Leases that expire
#: mid-send let a peer recover — and possibly duplicate — live work.
LEASE_SAFETY_MARGIN_S = 30.0


@dataclass(frozen=True)
class WorkerConfig:
    """Tuning for one dispatcher instance."""

    batch_size: int = 10
    poll_interval_s: float = 1.0
    # A claim lease must comfortably outlive one full send attempt
    # (handshake + transmit + acknowledgement timeout); enforced below.
    lease_seconds: float = 120.0
    ack_timeout_s: float = 30.0
    # Handshake wait owned here so the lease invariant below reflects
    # the actual worst case; forwarded to clients this worker creates.
    handshake_timeout_s: float = 10.0
    max_attempts: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 300.0

    def __post_init__(self) -> None:
        worst_case = (
            self.handshake_timeout_s
            + self.ack_timeout_s
            + LEASE_SAFETY_MARGIN_S
        )
        if self.lease_seconds <= worst_case:
            raise ValueError(
                "lease_seconds must exceed handshake_timeout_s + "
                f"ack_timeout_s + {LEASE_SAFETY_MARGIN_S}s safety margin "
                f"(needs > {worst_case}s, got {self.lease_seconds}s)"
            )
