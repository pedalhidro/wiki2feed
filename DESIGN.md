# wiki2feed — Design

## Goal

Serve RSS feeds generated from the *Notícias* pages of two Miraheze-hosted
MediaWiki sites — `pedalhidrografi.co` and `bicisampa.info`. Each news entry on a
Notícias page is a dated bullet that links to a wiki page; the corresponding
feed item's body is that linked page's content.

## Decisions (locked)

- **Serving model:** on-demand serverless function, with the response cached.
- **Feeds:** three — one per wiki, plus a combined feed.
- **Item body:** full page HTML, cleaned.
- **Reference format:** bullet titles are `[[wikilinks]]`; the link target is the page to fetch.
- **API base:** `/w/api.php` on both wikis (confirmed; `/api.php` 404s).
- **A missing/empty `Notícias` index is not an error** — it yields a valid, empty
  feed. (As of the dry run, only `pedalhidrografi.co` has the page; `bicisampa.info`
  has no news source yet, so its feed is empty until one exists.)

## Architecture

On request, a serverless function returns cached RSS, or builds it on a
miss/stale. Request flow:

```
GET /<feed>.xml
   │
   ├─ cache hit & fresh ───────────────────────► return cached XML
   │
   └─ miss / stale
        ├─ GET Notícias wikitext   (action=parse&prop=wikitext)
        ├─ parse bullets → [{ date, pageTitle, displayText }]
        ├─ batch GET revids        (action=query&prop=revisions)   → cache keys
        ├─ for each entry: GET page HTML (action=parse&prop=text)   [per-page cache by title+revid]
        ├─ clean HTML (strip edit links / nav / infobox, absolutize URLs)
        ├─ assemble RSS 2.0 (sort by date desc, cap at N)
        └─ store in cache (TTL) → return XML
```

Proposed endpoints:

- `/pedalhidrografi.xml`
- `/bicisampa.xml`
- `/combined.xml` — merges both per-wiki item lists, interleaved by date.

The combined feed reuses the two per-wiki pipelines and merges their results.

## Pipeline detail

1. **Index fetch** — `GET {base}/w/api.php?action=parse&page=Notícias&prop=wikitext&format=json`. Returns the raw wikitext of the news index.
2. **Parse** — match list items of the form `* YYYY-MM-DD: [[Target|Display]]`. Capture the date, the link target (the page to fetch), and optional display text (the item title). A regex suffices for the fixed format; `mwparserfromhell` is the robustness upgrade. Malformed lines are skipped and logged.
3. **Page fetch** — for each entry, `GET .../api.php?action=parse&page={target}&prop=text|revid&format=json`. The revid feeds the cache key.
4. **Clean** — operate on the returned HTML fragment. In practice (dry run) the
   `action=parse` body is already plain prose + figures: no editsection, toc,
   navbox/infobox, script, or inline style — so those strips are defensive, not
   load-bearing. The cleaning that **actually fires** is URL absolutization:
   images sit on the Wikitide CDN as **protocol-relative** `//static.wikitide.net/...`
   on both `<img src>` **and `srcset`** (handle srcset explicitly), and `/wiki/...`
   hrefs are root-relative. Also strip the trailing MediaWiki `<!-- NewPP … -->` /
   parser-cache comments. Goal: portable, reader-friendly HTML.
5. **Assemble** — build RSS 2.0.

## Item mapping

| RSS field | Source |
|---|---|
| `title` | display text (fallback: target page title) |
| `link` | `{base}/wiki/{Target}` (URL-encoded) |
| `guid` | page URL + `#` + revid (stable; changes on edit), `isPermaLink=false` |
| `pubDate` | bullet date at 00:00 **`-0300` (BRT, hardcoded)**, RFC-822. The wikis are configured `timezone=UTC` (`timeoffset=0`), so don't read tz from the API — the audience is São Paulo; pin BRT. |
| `description` / `content:encoded` | cleaned HTML |

Sort items by date descending; cap at N (proposed: 50).

## Caching strategy (key to the on-demand model)

Two layers, so a cold build is rare and Miraheze load stays minimal:

- **Assembled feed** — cached per route with a TTL (proposed: 15 min). Emit `Cache-Control: public, max-age=900` and an `ETag` so readers and CDNs revalidate cheaply.
- **Per-page HTML** — cached by page title + revid (effectively immutable content). Only new or edited pages are refetched. The index-parse step is followed by one `action=query&prop=revisions` batch call across all targets to compare revids against the cache before fetching any page body.
- **stale-while-revalidate** — serve slightly stale XML instantly and refresh in the background, avoiding request-time latency spikes and function timeouts on cold builds.

## Miraheze etiquette & robustness

- Descriptive `User-Agent` identifying the bot and a contact (Miraheze requires this), e.g. `wiki2feed/1.0 (+https://github.com/.../wiki2feed)`.
- Pass `maxlag`; back off on 429 / 5xx.
- Tolerate missing or renamed targets: skip the item, keep the feed valid, log a warning.
- Cap concurrency on cold builds; prefer cached per-page content.

## Tech stack (proposed; open)

- **Platform:** Cloudflare Workers — edge cache + KV for per-page/feed caching, generous free tier, global, fast cold starts. Alternatives: Vercel functions or AWS Lambda + API Gateway.
- **Language/libs:** TypeScript on Workers with a small RSS builder; or Python (`feedgen`, `requests`, `mwparserfromhell`) if targeting Lambda/Vercel-Python. Choose to match the platform.

## Resolved by dry run (2026-06-23, live sites)

Verified end-to-end against both wikis with `simulate_feed.py`:

- **API base** is `/w/api.php` on both (`/api.php` 404s). Both run MediaWiki 1.45.3,
  `articlepath=/wiki/$1`, `lang=pt-br`, `timezone=UTC`.
- **Index page** `Notícias` exists on `pedalhidrografi.co` (pageid 92) and its one
  bullet matches the locked format exactly: `* 2026-06-23: [[Pedal Hidrográfico 97|…]]`.
  On `bicisampa.info` the page is **missing** — the whole wiki has only 3 pages, no
  news source. Pipeline must serve an empty feed there, not fail.
- **Page HTML** from `action=parse` is already clean prose + a figure. The only
  cleaning that does anything is URL absolutization — including `srcset` and the
  `//static.wikitide.net` image CDN — plus dropping trailing parser comments.
- **Timezone** is configured UTC; `pubDate` pins `-0300` by decision (see Item mapping).

## Still open (pick at build time)

- Final cache TTL (proposed 15 min) and item cap (proposed 50).
- Whether to publish `bicisampa.xml` / include it in `combined.xml` while it has no
  source, or hold it back until the wiki has a `Notícias` page.

## Validation / testing plan

- Unit-test the bullet parser against sample wikitext (well-formed and malformed lines).
- Validate generated XML with the W3C Feed Validator; open all three feeds in a real reader (NetNewsWire, Feedly) and confirm titles, dates, links, and rendered bodies.
- Snapshot-test the HTML cleaner on a representative page.
