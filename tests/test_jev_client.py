"""验证 scripts/jev_client.py：请求契约、图片尺寸归一化与坏图跳过、长上下文头、重试边界与单元测试离线隔离。"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from typing import Any, Mapping
import unittest
import urllib.error
import zlib

SPEC = importlib.util.spec_from_file_location(
    "jev_client", Path(__file__).resolve().parents[1] / "scripts/jev_client.py"
)
assert SPEC is not None and SPEC.loader is not None
JC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = JC
SPEC.loader.exec_module(JC)


def make_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = b"\x00" + b"\xf0\xf4\xf8" * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str], float]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout_s: float) -> Mapping[str, Any]:
        self.calls.append((url, json.loads(body.decode("utf-8")), dict(headers), timeout_s))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class JevClientTest(unittest.TestCase):
    def test_choice_score_and_noul_roundtrip(self) -> None:
        transport = FakeTransport([
            {
                "model": "jev-1.14.0",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "relay",
                        "confidence": 0.91,
                        "probabilities": {"relay": 0.94, "create": 0.06},
                    },
                    "urgent": {"type": "noul", "noul": 0.87},
                    "risk": {
                        "type": "score",
                        "score": 1.6,
                        "confidence": 0.72,
                        "probabilities": [0.0, 0.4, 0.6],
                    },
                },
                "usage": {"input_tokens": 320, "output_tokens": 48},
            }
        ])
        client = JC.JevClient({"api_key": "ts-test-key"}, transport=transport, sleep_fn=lambda _: None)
        res = client.decide(
            state="484 先暂停测试",
            questions={
                "route": JC.Choice("Action?", {"relay": "forward", "create": "new"}),
                "urgent": JC.Noul("Urgent?", {"true": "yes", "false": "no"}),
                "risk": JC.Score("Risk?", ["low", "mid", "high"]),
            },
        )
        self.assertEqual(res.choice("route")[0], "relay")
        self.assertAlmostEqual(res.choice("route")[1], 0.91)
        self.assertAlmostEqual(res.noul("urgent"), 0.87)
        self.assertAlmostEqual(res.score("risk")[0], 1.6)
        self.assertEqual((res.input_tokens, res.output_tokens), (320, 48))

        _, payload, headers, _ = transport.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer ts-test-key")
        self.assertNotIn("TypeSafe-Danger-Zone-Images", headers)
        self.assertNotIn("TypeSafe-Danger-Zone-Long-Context", headers)
        self.assertEqual(payload["model"], "jev-1.14.0")

    def test_valid_image_passes_through_and_tiny_image_pads_to_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok_img = Path(tmp) / "ok.png"
            ok_img.write_bytes(make_png(300, 300))
            tiny_img = Path(tmp) / "tiny.png"
            tiny_img.write_bytes(make_png(32, 24))
            corrupt_img = Path(tmp) / "corrupt.png"
            corrupt_img.write_bytes(b"not-an-image")

            transport = FakeTransport([
                {"model": "jev-1.14.0", "answers": {"blank": {"type": "noul", "noul": 0.04}}, "usage": {}}
            ])
            client = JC.JevClient({"api_key": "ts-key"}, transport=transport, sleep_fn=lambda _: None)
            res = client.decide(
                state={"text": "Check screenshots"},
                questions={"blank": JC.Noul("Is the page blank?")},
                images=[ok_img, tiny_img, corrupt_img],
                skip_invalid_images=True,
            )
            self.assertAlmostEqual(res.noul("blank"), 0.04)
            _, payload, headers, _ = transport.calls[0]
            self.assertEqual(headers.get("TypeSafe-Danger-Zone-Images"), "true")
            self.assertEqual(len(payload["assets"]), 2)
            attached = payload["state"]["_attached_images"]
            self.assertEqual(len(attached), 2)
            # 第二张小图应当已被补白到至少 280x280
            padded_bytes = base64.b64decode(payload["assets"][attached[1]]["data_base64"])
            dims = JC._inspect_png(padded_bytes)
            self.assertIsNotNone(dims)
            assert dims is not None
            self.assertGreaterEqual(dims[0], 280)
            self.assertGreaterEqual(dims[1], 280)

            with self.assertRaises(JC.JevImageError):
                client.decide("Check", {"blank": JC.Noul("Blank?")}, images=[corrupt_img])

    def test_oversized_image_downscales_within_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            huge = Path(tmp) / "retina.png"
            huge.write_bytes(make_png(2000, 1200))
            fmt, out_bytes = JC.detect_or_convert_image(huge)
            self.assertEqual(fmt, "png")
            dims = JC._inspect_png(out_bytes)
            self.assertIsNotNone(dims)
            assert dims is not None
            self.assertLessEqual(dims[0], 1568)
            self.assertLessEqual(dims[1], 1568)
            self.assertGreaterEqual(dims[0], 280)
            self.assertGreaterEqual(dims[1], 280)

    def test_long_context_header_enabled_above_threshold(self) -> None:
        transport = FakeTransport([
            {"model": "jev-1.14.0", "answers": {"flag": {"type": "noul", "noul": 0.1}}, "usage": {}}
        ])
        client = JC.JevClient({"api_key": "ts-key"}, transport=transport, sleep_fn=lambda _: None)
        client.decide(state="x" * 75_000, questions={"flag": JC.Noul("Has feature flag?")})
        _, _, headers, _ = transport.calls[0]
        self.assertEqual(headers.get("TypeSafe-Danger-Zone-Long-Context"), "true")

    def test_retries_transient_503_once_but_not_401(self) -> None:
        err_503 = urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", 503, "Unavailable", {}, io.BytesIO(b"busy"))
        transport = FakeTransport([
            err_503,
            {"model": "jev-1.14.0", "answers": {"ok": {"type": "noul", "noul": 0.9}}, "usage": {}},
        ])
        client = JC.JevClient({"api_key": "ts-key", "retries": 1}, transport=transport, sleep_fn=lambda _: None)
        res = client.decide("hello", {"ok": JC.Noul("Is ok?")})
        self.assertAlmostEqual(res.noul("ok"), 0.9)
        self.assertEqual(len(transport.calls), 2)

        err_401 = urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", 401, "Unauthorized", {}, io.BytesIO(b"bad token"))
        bad_transport = FakeTransport([err_401])
        bad_client = JC.JevClient({"api_key": "ts-bad", "retries": 2}, transport=bad_transport, sleep_fn=lambda _: None)
        with self.assertRaises(JC.JevUnavailableError):
            bad_client.decide("hello", {"ok": JC.Noul("Is ok?")})
        self.assertEqual(len(bad_transport.calls), 1)

    def test_default_client_under_unittest_is_offline_by_default(self) -> None:
        client = JC.JevClient()
        with self.assertRaises(JC.JevUnavailableError):
            client.decide("hello", {"ok": JC.Noul("Is ok?")})


if __name__ == "__main__":
    unittest.main()
