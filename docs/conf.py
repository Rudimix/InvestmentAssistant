"""Настройки Sphinx."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

project = "Investment Decision Journal"
language = "ru"
extensions = ["sphinx.ext.autodoc"]
exclude_patterns = ["_build"]
html_theme = "alabaster"
html_show_sourcelink = False
html_copy_source = False
html_show_copyright = False
