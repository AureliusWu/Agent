"""Keep the packaged vision surface limited to the formats the product accepts."""

# PyInstaller's bundled Pillow hook collects every *ImagePlugin module. The
# desktop product accepts only JPEG, PNG and WEBP, so carrying the other
# plugins increases one-file extraction time without adding reachable product
# behavior.
hiddenimports = [
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
    "PIL.WebPImagePlugin",
]
