#!/usr/bin/env python3
"""
Post-process slack-export-viewer's rendered channel pages:

1. Collapse threads — consecutive <div class="message-container"> elements
   whose message is a thread reply (class="reply") get wrapped in a
   <details> so they're hidden by default, with a "N replies" toggle.
2. Paginate — very long channels get split into channel/<name>/index.html,
   page-2.html, page-3.html, ... with prev/next nav, so a single page
   doesn't have to render (and a browser doesn't have to load) the entire
   channel history at once.

Runs after slack-export-viewer's --html-only build and before Pagefind
indexing, so collapsed/paginated content is still what gets indexed.
"""
import sys
from pathlib import Path
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
CHANNEL_DIR = PUBLIC_DIR / "channel"
MESSAGES_PER_PAGE = 150


def is_reply(message_container):
    """A message-container wraps either a div.message (root) or div.reply (thread reply)."""
    return message_container.find("div", class_="reply") is not None


def collapse_threads(soup):
    """
    Group consecutive reply message-containers under a <details> so threads
    are collapsed by default. Returns the new flat list of top-level items
    (each either a lone message-container or a <details> wrapping a run of
    replies) for the pagination step to chunk.
    """
    messages_div = soup.find("div", class_="messages")
    if messages_div is None:
        return soup, []

    containers = messages_div.find_all("div", class_="message-container", recursive=False)
    items = []
    i = 0
    while i < len(containers):
        c = containers[i]
        if is_reply(c):
            run = [c]
            i += 1
            while i < len(containers) and is_reply(containers[i]):
                run.append(containers[i])
                i += 1
            details = soup.new_tag("details", **{"class": "thread-replies"})
            summary = soup.new_tag("summary")
            summary.string = f"{len(run)} repl{'y' if len(run) == 1 else 'ies'}"
            details.append(summary)
            for r in run:
                details.append(r.extract())
            items.append(details)
        else:
            items.append(c.extract())
            i += 1

    return soup, items


PAGINATION_TMPL = """<nav class="pagination">{prev}<span>Page {page} of {total}</span>{next}</nav>"""


def build_pagination(page, total, base_name):
    def link(target_page, label):
        if target_page < 1 or target_page > total:
            return f"<span class='disabled'>{label}</span>"
        fname = "index.html" if target_page == 1 else f"page-{target_page}.html"
        return f'<a href="./{fname}">{label}</a>'

    prev = link(page - 1, "&larr; Newer") + " "
    next_ = " " + link(page + 1, "Older &rarr;")
    return PAGINATION_TMPL.format(prev=prev, page=page, total=total, next=next_)


def paginate(soup, items, out_dir):
    # Snapshot the pristine template (head/sidebar/etc, empty messages div)
    # BEFORE any page mutates it — reused fresh for every page below.
    messages_div = soup.find("div", class_="messages")
    messages_div.clear()
    template_html = str(soup)

    total_pages = max(1, (len(items) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE)
    item_html = [str(item) for item in items]

    written = []
    for page in range(1, total_pages + 1):
        page_soup = BeautifulSoup(template_html, "html.parser")
        page_messages_div = page_soup.find("div", class_="messages")

        chunk = item_html[(page - 1) * MESSAGES_PER_PAGE : page * MESSAGES_PER_PAGE]
        for html in chunk:
            page_messages_div.append(BeautifulSoup(html, "html.parser"))

        if total_pages > 1:
            nav_html = build_pagination(page, total_pages, "index")
            page_messages_div.insert_before(BeautifulSoup(nav_html, "html.parser"))
            page_messages_div.insert_after(BeautifulSoup(nav_html, "html.parser"))

        fname = "index.html" if page == 1 else f"page-{page}.html"
        (out_dir / fname).write_text(str(page_soup), encoding="utf-8")
        written.append(fname)
    return written


EXTRA_CSS = """<style>
  details.thread-replies { margin: .3rem 0 .3rem 3.2rem; }
  details.thread-replies summary { cursor: pointer; font-size: .85rem; opacity: .7; padding: .2rem 0; }
  details.thread-replies[open] summary { opacity: 1; }
  nav.pagination { display: flex; justify-content: space-between; align-items: center;
    margin: 1rem 0; padding: .6rem 0; font-size: .9rem; }
  nav.pagination .disabled { opacity: .4; }
</style>"""


def inject_css(soup):
    head = soup.find("head")
    if head is not None:
        head.append(BeautifulSoup(EXTRA_CSS, "html.parser"))


def process_channel_page(path):
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    inject_css(soup)
    soup, items = collapse_threads(soup)
    if not items:
        return 0, 1
    written = paginate(soup, items, path.parent)
    return len(items), len(written)


def main():
    if not CHANNEL_DIR.exists():
        print("public/channel/ doesn't exist — nothing to post-process.", file=sys.stderr)
        return

    total_items = total_pages = 0
    for index_html in CHANNEL_DIR.glob("*/index.html"):
        n_items, n_pages = process_channel_page(index_html)
        total_items += n_items
        total_pages += n_pages

    print(f"Post-processed {total_items} top-level item(s) into {total_pages} page(s) across channels")


if __name__ == "__main__":
    main()
