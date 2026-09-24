#!/usr/bin/env python3
"""Check local href/src references in the static Pages HTML."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.values.append(value)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "site").resolve()
    errors: list[str] = []
    for page in sorted(root.rglob("*.html")):
        parser = References()
        parser.feed(page.read_text(encoding="utf-8"))
        for value in parser.values:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or value.startswith("#"):
                continue
            rel = unquote(parsed.path)
            if not rel:
                target = page
            else:
                target = (page.parent / rel).resolve()
            if not target.is_relative_to(root):
                errors.append(f"{page.relative_to(root)}: path escapes site: {value}")
            elif target.is_dir():
                if not (target / "index.html").is_file():
                    errors.append(f"{page.relative_to(root)}: missing directory index: {value}")
            elif not target.is_file():
                errors.append(f"{page.relative_to(root)}: missing local target: {value}")
    if errors:
        print("Broken local site references:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Checked local links in {len(list(root.rglob('*.html')))} HTML pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
