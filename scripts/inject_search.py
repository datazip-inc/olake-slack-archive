#!/usr/bin/env python3
"""
slack-export-viewer's templates know nothing about Pagefind. This walks the
rendered public/ output and injects a Pagefind search box into every page —
must run AFTER slack-export-viewer generates the HTML and BEFORE `pagefind`
builds the index, so the index picks up pages that already contain the
search widget's (empty, harmless) markup.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"

HEAD_TMPL = (
    '<link rel="stylesheet" href="{prefix}pagefind/pagefind-ui.css">\n'
    '<style>\n'
    '  #olake-search {{ max-width: 40rem; margin: 1rem auto; }}\n'
    '  #olake-search .pagefind-ui__result-title {{ font-size: 1rem; }}\n'
    '</style>\n'
    "</head>"
)

BODY_TMPL = (
    "{open_tag}\n"
    '<div id="olake-search"></div>\n'
    '<script src="{prefix}pagefind/pagefind-ui.js"></script>\n'
    "<script>\n"
    "  window.addEventListener('DOMContentLoaded', function () {{\n"
    "    new PagefindUI({{ element: '#olake-search', showSubResults: true }});\n"
    "  }});\n"
    "</script>\n"
)


def inject(path, prefix):
    html = path.read_text(encoding="utf-8")
    if "olake-search" in html:
        return False  # already injected (re-run safety)

    if "</head>" in html:
        html = html.replace("</head>", HEAD_TMPL.format(prefix=prefix), 1)

    # Insert right after the opening <body ...> tag, however it's attributed.
    idx = html.find("<body")
    if idx == -1:
        return False
    end = html.find(">", idx)
    if end == -1:
        return False
    open_tag = html[idx : end + 1]
    html = html[: idx] + BODY_TMPL.format(open_tag=open_tag, prefix=prefix) + html[end + 1 :]

    path.write_text(html, encoding="utf-8")
    return True


def main():
    if not PUBLIC_DIR.exists():
        print("public/ doesn't exist — nothing to inject search into.", file=sys.stderr)
        return

    count = 0
    for html_file in PUBLIC_DIR.rglob("*.html"):
        depth = len(html_file.relative_to(PUBLIC_DIR).parts) - 1
        prefix = "../" * depth
        if inject(html_file, prefix):
            count += 1

    print(f"Injected search widget into {count} page(s)")


if __name__ == "__main__":
    main()
