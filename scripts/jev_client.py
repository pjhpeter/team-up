#!/usr/bin/env python3
"""TypeSafe Jev (/v1/systemone) 公共客户端：统一管理密钥、图片尺寸归一化、长上下文头与有界重试。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Optional, Sequence, Union
import urllib.error
import urllib.request
import uuid

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.14.0"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_RETRIES = 1
RETRY_BACKOFF_S = 0.4
# 超过约 2.4 万词元（按 ~3 字符/词元保守折算）时必须显式声明长上下文头，否则官方按默认窗口截断或拒绝。
LONG_CONTEXT_CHAR_THRESHOLD = 72_000
RETRYABLE_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})

# 官方图片硬门禁：单边必须在 [280, 1568] 像素闭区间内，动态 PNG 与超限尺寸都会直接返回 400/413。
MIN_IMAGE_SIDE = 280
MAX_IMAGE_SIDE = 1568
MAX_IMAGE_BYTES = 16 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

Transport = Callable[[str, bytes, Mapping[str, str], float], Mapping[str, Any]]
KeyLoader = Callable[[Mapping[str, str]], str]

_KEY_CACHE: dict[tuple[str, str], str] = {}


class JevError(RuntimeError):
    """Jev 客户端基础异常。"""


class JevUnavailableError(JevError):
    """密钥缺失、网络超时、上游 5xx 或响应结构不合法时抛出，供调用方执行确定性兜底。"""


class JevImageError(JevUnavailableError):
    """图片不存在、损坏或无法归一化到官方尺寸范围时抛出。"""


@dataclass(frozen=True)
class Choice:
    instructions: Any
    criteria: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        if not self.criteria:
            raise ValueError("Choice criteria must not be empty")
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Score:
    instructions: Any
    criteria: Sequence[Any]

    def to_dict(self) -> dict[str, Any]:
        if len(self.criteria) < 2 or len(self.criteria) > 10:
            raise ValueError("Score criteria must have between 2 and 10 ordered levels")
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.criteria)}


@dataclass(frozen=True)
class Noul:
    instructions: Any
    criteria: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = dict(self.criteria)
        return out


@dataclass(frozen=True)
class Box:
    instructions: Any

    def to_dict(self) -> dict[str, Any]:
        return {"type": "box", "instructions": self.instructions}


QuestionSpec = Union[Choice, Score, Noul, Box, Mapping[str, Any]]


@dataclass(frozen=True)
class JevResult:
    model: str
    answers: dict[str, dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "JevResult":
        if not isinstance(data, Mapping) or not isinstance(data.get("answers"), Mapping):
            raise JevUnavailableError("Jev response missing 'answers' object")
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        answers: dict[str, dict[str, Any]] = {}
        for key, val in data["answers"].items():
            if not isinstance(val, Mapping):
                raise JevUnavailableError(f"Jev answer {key!r} is not an object")
            answers[str(key)] = dict(val)
        return cls(
            model=str(data.get("model") or ""),
            answers=answers,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    def noul(self, key: str) -> float:
        ans = self._require(key, "noul")
        val = ans.get("noul")
        if not isinstance(val, (int, float)):
            raise JevUnavailableError(f"Jev noul answer {key!r} missing numeric value")
        return float(val)

    def choice(self, key: str) -> tuple[str, float, dict[str, float]]:
        ans = self._require(key, "choice")
        selected = ans.get("choice")
        if not isinstance(selected, str) or not selected:
            raise JevUnavailableError(f"Jev choice answer {key!r} missing selected option")
        conf = float(ans.get("confidence") or 0.0)
        probs_raw = ans.get("probabilities")
        probs = {str(k): float(v) for k, v in probs_raw.items()} if isinstance(probs_raw, Mapping) else {}
        return selected, conf, probs

    def score(self, key: str) -> tuple[float, float, list[float]]:
        ans = self._require(key, "score")
        val = ans.get("score")
        if not isinstance(val, (int, float)):
            raise JevUnavailableError(f"Jev score answer {key!r} missing numeric score")
        conf = float(ans.get("confidence") or 0.0)
        probs_raw = ans.get("probabilities")
        probs = (
            [float(x) for x in probs_raw]
            if isinstance(probs_raw, Sequence) and not isinstance(probs_raw, (str, bytes))
            else []
        )
        return float(val), conf, probs

    def box(self, key: str) -> tuple[Optional[dict[str, float]], Optional[float]]:
        ans = self._require(key, "box")
        raw_box = ans.get("box")
        box_val = {str(k): float(v) for k, v in raw_box.items()} if isinstance(raw_box, Mapping) else None
        raw_conf = ans.get("confidence")
        conf = float(raw_conf) if isinstance(raw_conf, (int, float)) else None
        return box_val, conf

    def _require(self, key: str, expected_type: str) -> dict[str, Any]:
        ans = self.answers.get(key)
        if ans is None:
            raise JevUnavailableError(f"Jev response missing question key {key!r}")
        actual = ans.get("type")
        if actual and actual != expected_type:
            raise JevUnavailableError(f"Jev answer {key!r} has type {actual!r}, expected {expected_type!r}")
        return ans


def load_keychain_secret(spec: Mapping[str, str]) -> str:
    service = str(spec.get("service") or "").strip()
    account = str(spec.get("account") or "").strip()
    if not service:
        return ""
    cache_key = (service, account)
    if cache_key in _KEY_CACHE:
        return _KEY_CACHE[cache_key]
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd.extend(["-a", account])
    cmd.append("-w")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    secret = (res.stdout or "").strip() if res.returncode == 0 else ""
    if secret:
        # 缓存密钥避免高频分流每次拉起 security 子进程。
        _KEY_CACHE[cache_key] = secret
    return secret


def _inspect_png(raw: bytes) -> Optional[tuple[int, int, bool]]:
    """返回 (width, height, is_animated)；头损坏时返回 None。"""
    if len(raw) < 24 or not raw.startswith(PNG_MAGIC) or raw[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    if width <= 0 or height <= 0:
        return None
    pos = 8
    animated = False
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        chunk_type = raw[pos + 4 : pos + 8]
        if chunk_type == b"acTL":
            animated = True
            break
        if chunk_type == b"IDAT" or chunk_type == b"IEND":
            break
        pos += 12 + length
    return width, height, animated


def _inspect_jpeg(raw: bytes) -> Optional[tuple[int, int]]:
    """扫描 JPEG SOF 段提取宽高；非标准段或损坏时返回 None 交由 sips 兜底。"""
    if len(raw) < 4 or not raw.startswith(JPEG_MAGIC):
        return None
    pos = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3,
        0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB,
        0xCD, 0xCE, 0xCF,
    }
    while pos + 4 <= len(raw):
        if raw[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(raw) and raw[pos] == 0xFF:
            pos += 1
        if pos >= len(raw):
            break
        marker = raw[pos]
        pos += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(raw):
            break
        seg_len = struct.unpack(">H", raw[pos : pos + 2])[0]
        if seg_len < 2 or pos + seg_len > len(raw):
            break
        if marker in sof_markers and pos + 7 <= len(raw):
            height, width = struct.unpack(">HH", raw[pos + 3 : pos + 7])
            if width > 0 and height > 0:
                return width, height
            return None
        pos += seg_len
    return None


def _within_limits(width: int, height: int, byte_len: int) -> bool:
    return (
        MIN_IMAGE_SIDE <= width <= MAX_IMAGE_SIDE
        and MIN_IMAGE_SIDE <= height <= MAX_IMAGE_SIDE
        and byte_len <= MAX_IMAGE_BYTES
    )


def _sips_dimensions(path: Path) -> tuple[int, int]:
    try:
        proc = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JevImageError(f"Failed to inspect image {path.name} via sips: {exc}") from exc
    if proc.returncode != 0:
        raise JevImageError(f"Invalid or unreadable image {path.name}")
    width = 0
    height = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(":")
        if len(parts) != 2:
            continue
        key, val = parts[0].strip(), parts[1].strip()
        if key == "pixelWidth" and val.isdigit():
            width = int(val)
        elif key == "pixelHeight" and val.isdigit():
            height = int(val)
    if width <= 0 or height <= 0:
        raise JevImageError(f"Image {path.name} reported non-positive dimensions ({width}x{height})")
    return width, height


def detect_or_convert_image(path: Path) -> tuple[str, bytes]:
    """将图片归一化为符合官方 [280, 1568] 像素约束的静态 PNG/JPEG。"""
    if not path.is_file():
        raise JevImageError(f"Image file not found: {path}")
    raw = path.read_bytes()
    if not raw:
        raise JevImageError(f"Image file is empty: {path}")

    png_info = _inspect_png(raw)
    if png_info is not None:
        w, h, animated = png_info
        if not animated and _within_limits(w, h, len(raw)):
            return "png", raw

    jpeg_info = _inspect_jpeg(raw)
    if jpeg_info is not None:
        w, h = jpeg_info
        if _within_limits(w, h, len(raw)):
            return "jpeg", raw

    with tempfile.TemporaryDirectory(prefix="hwf-jev-img-") as tmp:
        stage = Path(tmp) / "stage.png"
        try:
            conv = subprocess.run(
                ["sips", "-s", "format", "png", str(path), "--out", str(stage)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise JevImageError(f"sips unavailable for {path.name}: {exc}") from exc
        if conv.returncode != 0 or not stage.is_file():
            raise JevImageError(f"Failed to convert {path.name} to static PNG")

        w, h = _sips_dimensions(stage)
        # Retina 大图先等比缩到最长边 1568，避免直接触发 413。
        if max(w, h) > MAX_IMAGE_SIDE:
            scale = MAX_IMAGE_SIDE / float(max(w, h))
            w = max(1, round(w * scale))
            h = max(1, round(h * scale))
            res = subprocess.run(
                ["sips", "-z", str(h), str(w), str(stage), "--out", str(stage)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if res.returncode != 0:
                raise JevImageError(f"Failed to downscale {path.name} via sips")

        # 小图标或窄条截图补白到至少 280x280，既满足官方下限又不拉伸文字。
        if w < MIN_IMAGE_SIDE or h < MIN_IMAGE_SIDE:
            pad_w = max(w, MIN_IMAGE_SIDE)
            pad_h = max(h, MIN_IMAGE_SIDE)
            pad = subprocess.run(
                ["sips", "-p", str(pad_h), str(pad_w), "--padColor", "FFFFFF", str(stage), "--out", str(stage)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if pad.returncode != 0:
                raise JevImageError(f"Failed to pad {path.name} to minimum size via sips")

        normalized = stage.read_bytes()
        if len(normalized) > MAX_IMAGE_BYTES:
            jpg_stage = Path(tmp) / "stage.jpg"
            jpg = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", str(stage), "--out", str(jpg_stage)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if jpg.returncode == 0 and jpg_stage.is_file():
                jpg_bytes = jpg_stage.read_bytes()
                if len(jpg_bytes) <= MAX_IMAGE_BYTES:
                    return "jpeg", jpg_bytes
            raise JevImageError(f"Normalized image {path.name} still exceeds 16 MiB")

        if not normalized.startswith(PNG_MAGIC):
            raise JevImageError(f"Normalized output is not valid PNG for {path.name}")
        return "png", normalized


def default_http_transport(url: str, body: bytes, headers: Mapping[str, str], timeout_s: float) -> Mapping[str, Any]:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise JevUnavailableError("Jev returned non-JSON payload") from exc
    if not isinstance(parsed, Mapping):
        raise JevUnavailableError("Jev top-level JSON is not an object")
    return parsed


def load_local_typesafe_config(home: Path = HWF_HOME) -> dict[str, Any]:
    """合并本机 local 配置中的 typesafe 段；未写配置时沿用官方默认参数与默认钥匙串项。"""
    merged: dict[str, Any] = {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "timeout_s": DEFAULT_TIMEOUT_S,
        "retries": DEFAULT_RETRIES,
        "api_key_keychain": {"service": "typesafe-jev-api", "account": "default"},
    }
    for name in ("gitlab-inbox.local.json", "feishu-bridge.local.json"):
        path = home / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping) and isinstance(data.get("typesafe"), Mapping):
            merged.update(data["typesafe"])
            break
    return merged


def _running_under_unittest() -> bool:
    return "unittest" in sys.modules


class JevClient:
    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        transport: Optional[Transport] = None,
        key_loader: Optional[KeyLoader] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        # 单元测试未注入假 transport/显式配置时禁止隐式读取本机真实钥匙串打外网，保证离线回归确定性。
        isolated_unittest = config is None and transport is None and key_loader is None and _running_under_unittest()
        cfg = {} if isolated_unittest else dict(load_local_typesafe_config() if config is None else config)
        self.base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).strip()
        self.model = str(cfg.get("model") or DEFAULT_MODEL).strip()
        self.timeout_s = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self.retries = max(int(cfg.get("retries", DEFAULT_RETRIES)), 0)
        self._explicit_key = str(cfg.get("api_key") or "").strip()
        self._isolated_unittest = isolated_unittest
        self._keychain_spec = cfg.get("api_key_keychain")
        if not isolated_unittest and self._keychain_spec is None and "api_key" not in cfg:
            self._keychain_spec = {"service": "typesafe-jev-api", "account": "default"}
        self._transport = transport or default_http_transport
        self._key_loader = key_loader or load_keychain_secret
        self._sleep = sleep_fn

    def _resolve_api_key(self) -> str:
        if self._isolated_unittest:
            return ""
        if self._explicit_key:
            return self._explicit_key
        env_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if env_key:
            return env_key
        if isinstance(self._keychain_spec, Mapping):
            return self._key_loader(self._keychain_spec)
        return ""

    def decide(
        self,
        state: Any,
        questions: Mapping[str, QuestionSpec],
        *,
        images: Sequence[Union[str, Path]] = (),
        skip_invalid_images: bool = False,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> JevResult:
        if not questions:
            raise ValueError("questions must not be empty")
        api_key = self._resolve_api_key()
        if not api_key:
            raise JevUnavailableError("TypeSafe API key is not configured in config, env, or macOS Keychain")

        q_payload: dict[str, dict[str, Any]] = {}
        has_box = False
        for qid, spec in questions.items():
            q_dict = spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)  # type: ignore[arg-type]
            if q_dict.get("type") == "box":
                has_box = True
            q_payload[str(qid)] = q_dict

        assets: dict[str, dict[str, str]] = {}
        placeholders: list[str] = []
        for raw_img in images:
            try:
                fmt, img_bytes = detect_or_convert_image(Path(raw_img))
            except JevImageError:
                if skip_invalid_images:
                    continue
                raise
            # 官方要求每个 asset 键必须作为纯文本出现在请求里，用唯一 UUID 防止误撞业务正文。
            token = f"<<jev-asset-{uuid.uuid4().hex}>>"
            placeholders.append(token)
            assets[token] = {
                "type": "image",
                "format": fmt,
                "data_base64": base64.b64encode(img_bytes).decode("ascii"),
            }

        final_state = state
        if placeholders:
            serialized_check = json.dumps({"s": state, "q": q_payload}, ensure_ascii=False)
            missing = [tok for tok in placeholders if tok not in serialized_check]
            if missing:
                if isinstance(state, str):
                    final_state = state + "\n\nAttached images: " + " ".join(missing)
                elif isinstance(state, Mapping):
                    final_state = {**state, "_attached_images": missing}
                elif isinstance(state, list):
                    final_state = [*state, {"_attached_images": missing}]
                else:
                    final_state = {"value": state, "_attached_images": missing}

        payload: dict[str, Any] = {
            "model": model or self.model,
            "state": final_state,
            "questions": q_payload,
        }
        if assets:
            payload["assets"] = assets

        body_str = json.dumps(payload, ensure_ascii=False)
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "hwf-jev-client/1.0",
        }
        if assets or has_box:
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        state_chars = len(json.dumps(final_state, ensure_ascii=False))
        max_q_chars = max(len(json.dumps(q, ensure_ascii=False)) for q in q_payload.values())
        if state_chars + max_q_chars >= LONG_CONTEXT_CHAR_THRESHOLD:
            headers["TypeSafe-Danger-Zone-Long-Context"] = "true"

        body_bytes = body_str.encode("utf-8")
        effective_timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                raw_resp = self._transport(self.base_url, body_bytes, headers, effective_timeout)
                return JevResult.from_payload(raw_resp)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:240]
                except Exception:
                    pass
                last_error = JevUnavailableError(f"Jev HTTP {exc.code}: {detail}")
                if exc.code not in RETRYABLE_HTTP_CODES or attempt >= self.retries:
                    raise last_error from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = JevUnavailableError(f"Jev transport error: {exc}")
                if attempt >= self.retries:
                    raise last_error from None
            except JevUnavailableError as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
            self._sleep(RETRY_BACKOFF_S * (attempt + 1))

        assert last_error is not None
        raise last_error


_DEFAULT_CLIENT: Optional[JevClient] = None


def default_client() -> JevClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = JevClient()
    return _DEFAULT_CLIENT


def decide(
    state: Any,
    questions: Mapping[str, QuestionSpec],
    *,
    images: Sequence[Union[str, Path]] = (),
    skip_invalid_images: bool = False,
    model: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> JevResult:
    return default_client().decide(
        state,
        questions,
        images=images,
        skip_invalid_images=skip_invalid_images,
        model=model,
        timeout_s=timeout_s,
    )
