#!/usr/bin/env python3
"""常驻轮询进程的瞬时网络错误识别、阶梯/指数退避与重复报错抑制器。

供 `feishu-bridge.py` 与 `gitlab-inbox.py` 共用：
- 夜间休眠、DNS 超时、TLS 握手超时或网关瞬断时，将不同接口路径上的同类网络错误归并为同一签名，
  断网期间只打首条告警（及可选稀疏心跳），恢复后打印一行汇总跳过轮数；
- 支持按固定阶梯（如 30s -> 60s -> 180s）或指数上限放大轮询等待，成功即刻恢复基础间隔。
"""

from __future__ import annotations

import re
from typing import Sequence

MAX_POLL_BACKOFF_S = 300
FAILURE_HEARTBEAT_EVERY = 10

_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dns-timeout", re.compile(r"lookup\s+\S+.*?(?:i/o timeout|no such host|server misbehaving)|temporary failure in name resolution|nodename nor servname|no such host", re.I)),
    ("tls-timeout", re.compile(r"tls handshake timeout", re.I)),
    ("gateway-5xx", re.compile(r"\b(?:502 bad gateway|503 service unavailable|504 gateway timeout)\b|http\s+50[234]\b", re.I)),
    ("conn-refused", re.compile(r"connection refused", re.I)),
    ("conn-reset", re.compile(r"connection reset by peer|broken pipe", re.I)),
    ("unreachable", re.compile(r"network is unreachable|no route to host", re.I)),
    ("eof", re.compile(r"unexpected eof|\beof\b", re.I)),
    ("timeout", re.compile(r"i/o timeout|context deadline exceeded|timed out|超时|\d+\s*秒没返回|\"subtype\"\s*:\s*\"timeout\"", re.I)),
    ("network", re.compile(r"\"type\"\s*:\s*\"network\"|dial tcp|net/http:\s*request canceled", re.I)),
)


def transient_error_kind(error: BaseException | str) -> str | None:
    """识别瞬时网络/超时错误并返回稳定分类名；非瞬时网络错误返回 None。"""
    flat = " ".join(str(error).split())
    for kind, pattern in _TRANSIENT_PATTERNS:
        if pattern.search(flat):
            return kind
    return None


def is_transient_network_error(error: BaseException | str) -> bool:
    return transient_error_kind(error) is not None


def normalize_error_signature(error: BaseException | str) -> str:
    """把跨 URL 的同类瞬时网络错误折叠为同一签名，避免不同接口路径轮流触发刷屏。"""
    kind = transient_error_kind(error)
    if kind is not None:
        return f"transient-network:{kind}"
    return " ".join(str(error).split())


def compute_poll_backoff(
    base_interval: int,
    consecutive_failures: int,
    max_backoff: int = MAX_POLL_BACKOFF_S,
) -> int:
    """连续失败时按指数退避放大等待间隔（如 60 -> 60 -> 120 -> 240 -> 300s），成功立即恢复基础间隔。"""
    floor = max(int(base_interval), 1)
    if consecutive_failures <= 0:
        return floor
    cap = max(floor, int(max_backoff))
    return min(floor * (2 ** min(consecutive_failures - 1, 3)), cap)


def compute_step_backoff(
    base_interval: int,
    consecutive_failures: int,
    steps: Sequence[int] = (30, 60, 180),
) -> int:
    """按给定阶梯（如 30s -> 60s -> 180s）放大失败重试间隔，成功立即恢复基础间隔。"""
    floor = max(int(base_interval), 1)
    if consecutive_failures <= 0 or not steps:
        return floor
    chosen = int(steps[min(consecutive_failures - 1, len(steps) - 1)])
    return max(floor, chosen)


class PollFailureTracker:
    """抑制断网期间同类错误每轮刷屏，并在网络恢复后打印一行汇总计数。"""

    def __init__(self, heartbeat_every: int = FAILURE_HEARTBEAT_EVERY, *, label: str = "poll") -> None:
        self.heartbeat_every = max(int(heartbeat_every), 0)
        self.label = label
        self.consecutive_failures = 0
        self.last_signature = ""
        self.last_summary = ""

    def record_failure(self, error: BaseException) -> str | None:
        summary = " ".join(str(error).split())
        signature = normalize_error_signature(error)
        self.consecutive_failures += 1
        if self.consecutive_failures == 1 or signature != self.last_signature:
            self.last_signature = signature
            self.last_summary = summary
            return f"{self.label} failed: {error}"
        if self.heartbeat_every > 0 and self.consecutive_failures % self.heartbeat_every == 0:
            return f"{self.label} still failing ({self.consecutive_failures} consecutive rounds): {summary[:240]}"
        return None

    def record_success(self) -> str | None:
        if self.consecutive_failures <= 0:
            return None
        count = self.consecutive_failures
        self.consecutive_failures = 0
        self.last_signature = ""
        self.last_summary = ""
        return f"{self.label} recovered after {count} consecutive failure(s)"
