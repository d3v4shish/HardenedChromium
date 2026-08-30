#!/usr/bin/env python3
"""Generate a small Y4M looping camera source for Chromium fake video capture.

Chromium's --use-file-for-fake-video-capture switch expects a YUV4MPEG2 file.
This helper creates a deterministic animated privacy/test-pattern source that
can be used as a virtual camera without OBS or a physical webcam.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile


RED_Y = 78
RED_U = 85
RED_V = 255
GRAY_U = 128
GRAY_V = 128


def is_y4m_file(path: pathlib.Path) -> bool:
  try:
    with path.open("rb") as stream:
      return stream.read(9) == b"YUV4MPEG2"
  except OSError:
    return False


def mark_red_chroma(u_plane: bytearray, v_plane: bytearray, width: int,
                    x_start: int, y_start: int, x_end: int, y_end: int) -> None:
  chroma_width = width // 2
  for cy in range(max(0, y_start // 2), max(0, y_end // 2)):
    offset = cy * chroma_width
    for cx in range(max(0, x_start // 2), max(0, x_end // 2)):
      u_plane[offset + cx] = RED_U
      v_plane[offset + cx] = RED_V


def draw_red_rect(y_plane: bytearray, u_plane: bytearray, v_plane: bytearray,
                  width: int, height: int, x_start: int, y_start: int,
                  x_end: int, y_end: int) -> None:
  x_start = max(0, min(width, x_start))
  x_end = max(0, min(width, x_end))
  y_start = max(0, min(height, y_start))
  y_end = max(0, min(height, y_end))
  if x_start >= x_end or y_start >= y_end:
    return

  for py in range(y_start, y_end):
    row_offset = py * width
    y_plane[row_offset + x_start:row_offset + x_end] = bytes([RED_Y]) * (
        x_end - x_start)
  mark_red_chroma(u_plane, v_plane, width, x_start, y_start, x_end, y_end)


def write_y4m(output: pathlib.Path, width: int, height: int, frames: int,
              fps: int) -> None:
  output.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary_name = tempfile.mkstemp(
      prefix=f"{output.name}.", suffix=".tmp", dir=str(output.parent))

  try:
    with os.fdopen(fd, "wb") as stream:
      stream.write(
          f"YUV4MPEG2 W{width} H{height} F{fps}:1 Ip A1:1 C420jpeg\n".encode(
              "ascii"))

      chroma_width = width // 2
      chroma_height = height // 2

      for frame in range(frames):
        y_plane = bytearray(width * height)
        u_plane = bytearray([GRAY_U]) * (chroma_width * chroma_height)
        v_plane = bytearray([GRAY_V]) * (chroma_width * chroma_height)

        for py in range(height):
          row_offset = py * width
          for px in range(width):
            stripe = ((px + py + frame * 3) // 18) % 2
            sweep = ((px - frame * 5) % width) < max(4, width // 18)
            y_plane[row_offset + px] = 34 + (12 if stripe else 0) + (
                22 if sweep else 0)

        border = max(2, min(width, height) // 80)
        draw_red_rect(y_plane, u_plane, v_plane, width, height, 0, 0, width,
                      border)
        draw_red_rect(y_plane, u_plane, v_plane, width, height, 0,
                      height - border, width, height)
        draw_red_rect(y_plane, u_plane, v_plane, width, height, 0, 0, border,
                      height)
        draw_red_rect(y_plane, u_plane, v_plane, width, height, width - border,
                      0, width, height)

        marker_size = max(14, min(width, height) // 6)
        marker_x = int((width - marker_size) * frame / max(1, frames - 1))
        marker_y = height - marker_size - max(8, border * 3)
        draw_red_rect(y_plane, u_plane, v_plane, width, height, marker_x,
                      marker_y, marker_x + marker_size,
                      marker_y + marker_size)

        stream.write(b"FRAME\n")
        stream.write(y_plane)
        stream.write(u_plane)
        stream.write(v_plane)

    os.replace(temporary_name, output)
  except BaseException:
    try:
      os.unlink(temporary_name)
    except FileNotFoundError:
      pass
    raise


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Generate a Y4M looping video for Chromium fake camera mode.")
  parser.add_argument("--output", required=True, type=pathlib.Path)
  parser.add_argument("--width", type=int, default=320)
  parser.add_argument("--height", type=int, default=180)
  parser.add_argument("--frames", type=int, default=120)
  parser.add_argument("--fps", type=int, default=30)
  parser.add_argument("--force", action="store_true")
  return parser.parse_args()


def main() -> int:
  args = parse_args()

  if args.width <= 0 or args.height <= 0:
    raise SystemExit("width and height must be positive")
  if args.width % 2 or args.height % 2:
    raise SystemExit("width and height must be even for 4:2:0 Y4M")
  if args.frames <= 0:
    raise SystemExit("frames must be positive")
  if args.fps <= 0:
    raise SystemExit("fps must be positive")

  if args.output.exists() and is_y4m_file(args.output) and not args.force:
    print(f"Loop video already exists: {args.output}")
    return 0

  if args.output.exists() and not args.force:
    print(
        f"Refusing to overwrite an existing non-Y4M file: {args.output}",
        file=sys.stderr)
    return 2

  write_y4m(args.output, args.width, args.height, args.frames, args.fps)
  print(f"Generated loop video: {args.output}")
  print(f"  Size: {args.width}x{args.height}")
  print(f"  Frames: {args.frames}")
  print(f"  FPS: {args.fps}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
