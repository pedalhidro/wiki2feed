#!/usr/bin/env python3
"""Simula localmente a saída dos feeds do wiki2feed, batendo nos wikis ao vivo.

Implementa o pipeline do DESIGN.md sem servidor nem cache: busca o índice
`Notícias`, faz o parse dos bullets, busca o HTML de cada página, limpa e monta
o RSS 2.0 — exatamente o XML que o Worker serviria. Use para inspecionar o
resultado antes de escrever o serviço, ou para validar mudanças no pipeline.

Uso:
    python simulate_feed.py                  # feed combinado, no stdout
    python simulate_feed.py pedal            # só pedalhidrografi.co
    python simulate_feed.py bicisampa
    python simulate_feed.py combined --limit 50 --out combined.xml
    python simulate_feed.py pedal --html --out preview.html   # pré-visualização

Sem dependências além da stdlib (usa o CA bundle do `certifi` se houver,
senão cai pro `/etc/ssl/cert.pem` do sistema).
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

USER_AGENT = "wiki2feed/0.1 (+https://github.com/pedalhidro/wiki2feed; simulator)"
INDEX_PAGE = "Notícias"
BRT = timezone(timedelta(hours=-3))  # pubDate fixo em -0300 (decisão do DESIGN.md)

WIKIS = {
    "pedal": {
        "base": "https://pedalhidrografi.co",
        "title": "Pedal Hidrográfico — Notícias",
        "description": "Pedais pelos rios e anti-rios de São Paulo.",
    },
    "bicisampa": {
        "base": "https://bicisampa.info",
        "title": "BiciSampa — Notícias",
        "description": "Bicicleta no Sítio Urbano de São Paulo.",
    },
}

# `* YYYY-MM-DD: [[Target|Display]]`  /  `* YYYY-MM-DD: [[Target]]`
BULLET_RE = re.compile(
    r"^\*\s*(?P<date>\d{4}-\d{2}-\d{2})\s*:\s*\[\[(?P<target>[^\]|]+?)(?:\|(?P<display>[^\]]+?))?\]\]",
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")


_CTX = _ssl_context()


def api(base: str, **params) -> dict:
    params.setdefault("format", "json")
    url = f"{base}/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
        return json.loads(r.read())


@dataclass
class Entry:
    date: str          # YYYY-MM-DD
    target: str        # página a buscar
    display: str       # título do item
    base: str
    feed_key: str
    revid: int | None = None
    html: str = ""

    @property
    def link(self) -> str:
        return f"{self.base}/wiki/" + urllib.parse.quote(self.target.replace(" ", "_"))

    @property
    def guid(self) -> str:
        return f"{self.link}#{self.revid}"

    @property
    def pubdate(self) -> str:
        d = datetime.strptime(self.date, "%Y-%m-%d").replace(tzinfo=BRT)
        return format_datetime(d)


def fetch_index_bullets(wiki_key: str) -> list[Entry]:
    """Busca o wikitext de Notícias e extrai os bullets. Página ausente -> []."""
    cfg = WIKIS[wiki_key]
    base = cfg["base"]
    try:
        j = api(base, action="parse", page=INDEX_PAGE, prop="wikitext")
    except urllib.error.HTTPError as e:
        print(f"  [{wiki_key}] índice indisponível (HTTP {e.code}) — feed vazio", file=sys.stderr)
        return []
    if "error" in j:
        # missingtitle etc. — feed válido, vazio
        print(f"  [{wiki_key}] sem página '{INDEX_PAGE}' ({j['error'].get('code')}) — feed vazio", file=sys.stderr)
        return []
    wikitext = j["parse"]["wikitext"]["*"]
    entries: list[Entry] = []
    for ln in wikitext.splitlines():
        if not ln.lstrip().startswith("*"):
            continue
        m = BULLET_RE.match(ln.strip())
        if not m:
            print(f"  [{wiki_key}] bullet ignorado (formato): {ln.strip()!r}", file=sys.stderr)
            continue
        target = m.group("target").strip()
        entries.append(Entry(
            date=m.group("date"),
            target=target,
            display=(m.group("display") or target).strip(),
            base=base,
            feed_key=wiki_key,
        ))
    return entries


def fetch_page(entry: Entry) -> None:
    """Preenche entry.html (limpo) e entry.revid. Página sumida -> item pulado a montante."""
    j = api(entry.base, action="parse", page=entry.target, prop="text|revid")
    if "error" in j:
        raise KeyError(j["error"].get("code", "error"))
    entry.revid = j["parse"]["revid"]
    entry.html = clean_html(j["parse"]["text"]["*"], entry.base)


def clean_html(html: str, base: str) -> str:
    """Absolutiza URLs (inclui srcset + CDN protocol-relative) e remove ruído."""
    # 1) comentários do MediaWiki (NewPP, parser cache, transclusion report)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # 2) defensivo: script/style/editsection (não aparecem hoje, mas custa nada)
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r'<span class="mw-editsection[^>]*>.*?</span>', "", html, flags=re.S | re.I)
    # 3) srcset protocol-relative (cada URL na lista)
    def _fix_srcset(m: re.Match) -> str:
        return 'srcset="' + m.group(1).replace("//", "https://", 1).replace(", //", ", https://") + '"'
    html = re.sub(r'srcset="([^"]*)"', _fix_srcset, html)
    # 4) src/href protocol-relative -> https
    html = re.sub(r'(\b(?:src|href))="//', r'\1="https://', html)
    # 5) src/href root-relative (/wiki/..., /w/...) -> absoluto (não toca em // já tratado)
    html = re.sub(r'(\b(?:src|href))="/(?!/)', rf'\1="{base}/', html)
    return html.strip()


def build_rss(wiki_key: str, entries: list[Entry], limit: int) -> str:
    cfg = WIKIS[wiki_key] if wiki_key in WIKIS else {
        "base": "", "title": "Pedal Hidrográfico + BiciSampa — Notícias",
        "description": "Feed combinado.",
    }
    self_link = (cfg["base"] + f"/{wiki_key}.xml") if cfg["base"] else f"/{wiki_key}.xml"
    items = sorted(entries, key=lambda e: e.date, reverse=True)[:limit]
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{escape(cfg['title'])}</title>",
        f"    <link>{escape(cfg['base'] or 'https://pedalhidrografi.co')}</link>",
        f"    <description>{escape(cfg['description'])}</description>",
        "    <language>pt-br</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        f'    <atom:link href="{escape(self_link)}" rel="self" type="application/rss+xml" />',
    ]
    for e in items:
        parts += [
            "    <item>",
            f"      <title>{escape(e.display)}</title>",
            f"      <link>{escape(e.link)}</link>",
            f'      <guid isPermaLink="false">{escape(e.guid)}</guid>',
            f"      <pubDate>{e.pubdate}</pubDate>",
            f"      <description><![CDATA[{e.html}]]></description>",
            f"      <content:encoded><![CDATA[{e.html}]]></content:encoded>",
            "    </item>",
        ]
    parts += ["  </channel>", "</rss>"]
    return "\n".join(parts) + "\n"


def build_html(wiki_key: str, entries: list[Entry], limit: int) -> str:
    """Página de pré-visualização estilo leitor — mesmo pipeline, saída legível."""
    cfg = WIKIS[wiki_key] if wiki_key in WIKIS else {
        "base": "", "title": "Pedal Hidrográfico + BiciSampa — Notícias",
        "description": "Feed combinado.",
    }
    items = sorted(entries, key=lambda e: e.date, reverse=True)[:limit]
    feed_names = {"pedal": "pedal", "bicisampa": "bicisampa", "combined": "combined"}
    arts = []
    for e in items:
        d = datetime.strptime(e.date, "%Y-%m-%d")
        human = d.strftime("%d/%m/%Y")
        src = f' · <span class="src">{escape(WIKIS[e.feed_key]["title"].split(" — ")[0])}</span>' if wiki_key == "combined" else ""
        arts.append(
            "    <article>\n"
            f'      <h2><a href="{escape(e.link)}">{escape(e.display)}</a></h2>\n'
            f'      <p class="meta"><time datetime="{e.date}">{human}</time>{src}</p>\n'
            f'      <div class="body">{e.html}</div>\n'
            "    </article>"
        )
    empty = "" if arts else '    <p class="empty">Nenhuma notícia ainda.</p>'
    body = "\n".join(arts) if arts else empty
    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(cfg['title'])}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 system-ui, -apple-system, sans-serif; max-width: 42rem;
         margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }}
  header {{ border-bottom: 2px solid currentColor; margin-bottom: 1.5rem; padding-bottom: .75rem; }}
  header h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
  header p {{ margin: 0; opacity: .7; }}
  article {{ border-bottom: 1px solid color-mix(in srgb, currentColor 18%, transparent);
             padding: 1.5rem 0; }}
  article h2 {{ margin: 0 0 .25rem; font-size: 1.3rem; }}
  article h2 a {{ color: inherit; text-decoration: none; }}
  article h2 a:hover {{ text-decoration: underline; }}
  .meta {{ margin: 0 0 .75rem; font-size: .85rem; opacity: .65; }}
  .src {{ font-weight: 600; }}
  .body img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  .body figure {{ margin: 1rem 0; text-align: center; }}
  .empty {{ opacity: .6; font-style: italic; }}
  footer {{ margin-top: 2rem; font-size: .8rem; opacity: .5; }}
</style>
</head>
<body>
  <header>
    <h1>{escape(cfg['title'])}</h1>
    <p>{escape(cfg['description'])}</p>
  </header>
  <main>
{body}
  </main>
  <footer>Pré-visualização gerada por simulate_feed.py · feed <code>{feed_names.get(wiki_key, wiki_key)}</code> · {len(items)} item(ns)</footer>
</body>
</html>
"""


def gather(wiki_key: str) -> list[Entry]:
    entries = fetch_index_bullets(wiki_key)
    out: list[Entry] = []
    for e in entries:
        try:
            fetch_page(e)
            out.append(e)
        except (KeyError, urllib.error.HTTPError) as err:
            print(f"  [{wiki_key}] alvo ausente/erro '{e.target}' ({err}) — item pulado", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Simula a saída dos feeds wiki2feed localmente.")
    ap.add_argument("feed", nargs="?", default="combined",
                    choices=["pedal", "bicisampa", "combined"],
                    help="qual feed gerar (default: combined)")
    ap.add_argument("--limit", type=int, default=50, help="máx. de itens (default: 50)")
    ap.add_argument("--format", choices=["rss", "html"], default="rss",
                    help="rss (default) ou html (página de pré-visualização estilo leitor)")
    ap.add_argument("--html", action="store_const", const="html", dest="format",
                    help="atalho para --format html")
    ap.add_argument("--out", help="grava no arquivo em vez do stdout")
    args = ap.parse_args()

    keys = ["pedal", "bicisampa"] if args.feed == "combined" else [args.feed]
    print(f"# Simulando feed '{args.feed}' (limite {args.limit})…", file=sys.stderr)
    entries: list[Entry] = []
    for k in keys:
        got = gather(k)
        print(f"  [{k}] {len(got)} item(ns)", file=sys.stderr)
        entries += got

    render = build_html if args.format == "html" else build_rss
    out = render(args.feed, entries, args.limit)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"# escrito em {args.out} ({len(out)} bytes, {min(len(entries), args.limit)} itens, {args.format})", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
