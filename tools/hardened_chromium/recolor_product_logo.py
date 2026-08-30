#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Recolors Chromium product-logo assets for the hardened build."""

from __future__ import annotations

import colorsys
from pathlib import Path

from PIL import Image


SOURCE_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_LOGOS = (
    "chrome/app/theme/chromium/linux/product_logo_24.png",
    "chrome/app/theme/chromium/linux/product_logo_48.png",
    "chrome/app/theme/chromium/linux/product_logo_64.png",
    "chrome/app/theme/chromium/linux/product_logo_128.png",
    "chrome/app/theme/chromium/linux/product_logo_256.png",
    "chrome/app/theme/chromium/product_logo_16.png",
    "chrome/app/theme/chromium/product_logo_24.png",
    "chrome/app/theme/chromium/product_logo_48.png",
    "chrome/app/theme/chromium/product_logo_64.png",
    "chrome/app/theme/chromium/product_logo_128.png",
    "chrome/app/theme/chromium/product_logo_256.png",
    "chrome/app/theme/default_100_percent/chromium/linux/product_logo_16.png",
    "chrome/app/theme/default_100_percent/chromium/linux/product_logo_32.png",
    "chrome/app/theme/default_100_percent/chromium/product_logo_16.png",
    "chrome/app/theme/default_100_percent/chromium/product_logo_32.png",
    "chrome/app/theme/default_200_percent/chromium/product_logo_16.png",
    "chrome/app/theme/default_200_percent/chromium/product_logo_32.png",
)
PRODUCT_LOGO_SVGS = (
    "chrome/app/theme/chromium/product_logo.svg",
    "chrome/app/theme/chromium/product_logo_animation.svg",
)
SVG_COLOR_REPLACEMENTS = {
    "#1967D2": "#D21919",
    "#1A73E8": "#E81A1A",
    "#669DF6": "#F66666",
    "#AECBFA": "#FAAEAE",
}


def recolor_pixel(pixel: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    red, green, blue, alpha = pixel
    _, lightness, saturation = colorsys.rgb_to_hls(
        red / 255, green / 255, blue / 255
    )
    if saturation < 0.02:
        return pixel

    red, green, blue = colorsys.hls_to_rgb(0, lightness, saturation)
    return round(red * 255), round(green * 255), round(blue * 255), alpha


def main() -> None:
    for relative_path in PRODUCT_LOGOS:
        path = SOURCE_ROOT / relative_path
        with Image.open(path) as source:
            image = source.convert("RGBA")
        image.putdata(
            [recolor_pixel(pixel) for pixel in image.get_flattened_data()]
        )
        image.save(path, optimize=True)

    for relative_path in PRODUCT_LOGO_SVGS:
        path = SOURCE_ROOT / relative_path
        contents = path.read_text()
        for original, replacement in SVG_COLOR_REPLACEMENTS.items():
            contents = contents.replace(original, replacement)
        path.write_text(contents)


if __name__ == "__main__":
    main()
