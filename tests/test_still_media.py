from __future__ import annotations

import unittest

from shotcut_mcp.media import STILL_MAX_SECONDS, media_duration


def probe(format_name: str, duration: str | None, codec: str = "mjpeg") -> dict:
    payload: dict = {"format": {"format_name": format_name}, "streams": []}
    if duration is not None:
        payload["format"]["duration"] = duration
        payload["streams"].append(
            {"codec_type": "video", "codec_name": codec, "duration": duration}
        )
    return payload


class StillImageDurationTests(unittest.TestCase):
    def test_still_image_reports_no_duration(self) -> None:
        """A JPEG is one frame long; FFprobe still reports 1/fps as duration."""

        self.assertIsNone(media_duration(probe("image2", "0.040000")))

    def test_still_threshold_is_the_one_frame_mark(self) -> None:
        self.assertIsNone(
            media_duration(probe("image2", f"{STILL_MAX_SECONDS:.6f}"))
        )

    def test_slightly_longer_image_is_a_real_clip(self) -> None:
        value = STILL_MAX_SECONDS + 0.001
        self.assertAlmostEqual(
            media_duration(probe("image2", f"{value:.6f}")) or 0.0, value, places=6
        )

    def test_real_video_keeps_its_duration(self) -> None:
        self.assertAlmostEqual(
            media_duration(probe("mov,mp4,m4a,3gp,3g2,mj2", "12.500000", "h264")) or 0.0,
            12.5,
        )

    def test_short_clip_outside_image_container_is_not_a_still(self) -> None:
        """A genuine one-frame video must not be mistaken for a still image."""

        self.assertAlmostEqual(
            media_duration(probe("avi", "0.040000", "rawvideo")) or 0.0, 0.04
        )

    def test_missing_duration_stays_none(self) -> None:
        self.assertIsNone(media_duration({"format": {}, "streams": []}))


if __name__ == "__main__":
    unittest.main()
