#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Manga Metadata Plugin for Calibre  v1.3
Fetches manga metadata from MyAnimeList (via Jikan API v4) with MangaDex fallback.

Sources:
  - Primary:  MyAnimeList via Jikan API v4  (https://api.jikan.moe/v4)
  - Fallback: MangaDex API                  (https://api.mangadex.org)

Cover priority:
  1. MangaDex volume-exact cover  (matched by volume number)
  2. If no volume match -> both MAL series cover + MangaDex volume covers
     (capped by user config) in Calibre's cover browser.

Config (Preferences > Plugins > Manga Metadata > Customize):
  - Max covers to download   (default 10, range 1-50)
  - Volume window half-width (default  3, range 0-20)
      Covers fetched = only those within ±window of the target volume.
      Set to 0 to fetch only the exact match; set high (e.g. 20) for a
      wide browse window.  Has no effect when no volume number is found
      in the title (in that case max_covers applies globally).
"""

from __future__ import absolute_import, division, print_function, unicode_literals

__license__   = 'GPL v3'
__copyright__ = '2024'
__docformat__ = 'restructuredtext en'

import json
import re
import time
from datetime import datetime
from queue import Queue, Empty
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Source
from calibre.utils.date import utc_tz

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_COVERS    = 10   # maximum images queued for the cover browser
DEFAULT_VOLUME_WINDOW = 3    # half-width: fetch vols (target-N) … (target+N)

# ---------------------------------------------------------------------------
# Config widget  (shown in Preferences > Plugins > Customize)
# ---------------------------------------------------------------------------

class ConfigWidget:                          # noqa: N801  (Calibre convention)
    """Calibre plugin config panel."""

    def __init__(self, plugin):
        from calibre.gui2 import gprefs
        from PyQt5.Qt import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel,
            QSpinBox, QGroupBox, QFormLayout,
        )

        self.plugin = plugin
        self.widget = QWidget()
        outer = QVBoxLayout(self.widget)

        # ---- Cover settings group ----
        grp    = QGroupBox('Cover download settings')
        form   = QFormLayout(grp)

        # Max covers
        self.max_covers_spin = QSpinBox()
        self.max_covers_spin.setRange(1, 50)
        self.max_covers_spin.setValue(plugin.prefs.get('max_covers', DEFAULT_MAX_COVERS))
        self.max_covers_spin.setToolTip(
            'Maximum number of cover images to download and show in the cover browser.\n'
            'Lower = faster; higher = more choices.'
        )
        form.addRow(QLabel('Max covers to download:'), self.max_covers_spin)

        # Volume window
        self.vol_window_spin = QSpinBox()
        self.vol_window_spin.setRange(0, 20)
        self.vol_window_spin.setValue(plugin.prefs.get('volume_window', DEFAULT_VOLUME_WINDOW))
        self.vol_window_spin.setToolTip(
            'When a volume number is detected in the title, only fetch covers\n'
            'within this many volumes either side of the target.\n'
            'Example: target=5, window=3  ->  fetches vols 2 through 8.\n'
            'Set to 0 to fetch only the single exact-match volume.\n'
            'Ignored when no volume number can be parsed from the title.'
        )
        form.addRow(QLabel('Volume window (±):'), self.vol_window_spin)

        outer.addWidget(grp)
        outer.addStretch()

    def commit(self):
        self.plugin.prefs['max_covers']    = self.max_covers_spin.value()
        self.plugin.prefs['volume_window'] = self.vol_window_spin.value()


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class MangaMetadata(Source):
    """Calibre metadata source plugin for manga."""

    name                    = 'Manga Metadata'
    description             = (
        'Downloads manga metadata from MyAnimeList (via Jikan API v4) '
        'with MangaDex as automatic fallback. '
        'Retrieves title, authors/artists, cover(s), genres, synopsis, '
        'publisher, release date, and series/volume information. '
        'Fetches per-volume covers from MangaDex with configurable limits.'
    )
    author                  = 'AshHimura'
    version                 = (1, 3, 0)
    minimum_calibre_version = (5, 0, 0)

    capabilities   = frozenset(['identify', 'cover'])
    touched_fields = frozenset([
        'title', 'authors', 'tags', 'pubdate', 'comments',
        'series', 'series_index', 'identifiers', 'publisher',
    ])

    has_html_comments               = False
    supports_gzip_transfer_encoding = True
    can_get_multiple_covers         = True

    # Public API bases
    JIKAN_BASE    = 'https://api.jikan.moe/v4'
    MANGADEX_BASE = 'https://api.mangadex.org'
    MANGADEX_CDN  = 'https://uploads.mangadex.org/covers'

    # Jikan rate-limit ~3 req/s
    JIKAN_DELAY   = 0.35

    # -----------------------------------------------------------------------
    # Persistent preferences  (JSONConfig backed by Calibre's config dir)
    # -----------------------------------------------------------------------

    @property
    def prefs(self):
        if not hasattr(self, '_prefs'):
            from calibre.utils.config import JSONConfig
            self._prefs = JSONConfig('plugins/manga_metadata')
            self._prefs.defaults['max_covers']    = DEFAULT_MAX_COVERS
            self._prefs.defaults['volume_window'] = DEFAULT_VOLUME_WINDOW
        return self._prefs

    # -----------------------------------------------------------------------
    # Config widget hook (called by Calibre's plugin manager)
    # -----------------------------------------------------------------------

    def customization_help(self, gui=False):
        return (
            'Configure cover download limits via '
            'Preferences > Plugins > Manga Metadata > Customize plugin.'
        )

    def config_widget(self):
        cw = ConfigWidget(self)
        return cw.widget

    def save_settings(self, config_widget):
        # config_widget is the QWidget returned above; we need the wrapper
        # Calibre calls save_settings(widget) — look up our wrapper via plugin ref
        # stored on the widget, or fall back to reading spinboxes directly.
        try:
            config_widget._manga_cfg.commit()
        except AttributeError:
            pass

    # -----------------------------------------------------------------------
    # Low-level HTTP
    # -----------------------------------------------------------------------

    def _http_get(self, url, timeout=30):
        req = Request(url, headers={
            'User-Agent': 'CalibreMangaMetadataPlugin/1.3',
            'Accept':     'application/json',
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception:
            return None

    def _download_image(self, url, timeout=30):
        req = Request(url, headers={
            'User-Agent': 'CalibreMangaMetadataPlugin/1.3',
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Title parsing
    # -----------------------------------------------------------------------

    def _parse_series_and_volume(self, raw_title):
        """
        'Berserk Vol 3'  ->  ('Berserk', 3.0)
        'One Piece'      ->  ('One Piece', None)
        """
        if not raw_title:
            return raw_title, None

        patterns = [
            r'^(.*?)\s+[Vv]ol(?:ume)?\.?\s*(\d+(?:\.\d+)?)',
            r'^(.*?)\s+[Tt]ome?\.?\s*(\d+(?:\.\d+)?)',
            r'^(.*?)\s+[Cc]h(?:apter)?\.?\s*(\d+(?:\.\d+)?)',
            r'^(.*?)\s+#\s*(\d+(?:\.\d+)?)',
            r'^(.*?)\s+(\d+)$',
        ]
        for pat in patterns:
            m = re.match(pat, raw_title.strip())
            if m:
                return m.group(1).strip(), float(m.group(2))
        return raw_title.strip(), None

    # -----------------------------------------------------------------------
    # MAL / Jikan helpers
    # -----------------------------------------------------------------------

    def _jikan_search(self, query, timeout=30):
        url  = '{}/manga?q={}&limit=5&type=manga'.format(self.JIKAN_BASE, quote(query))
        data = self._http_get(url, timeout)
        return data['data'] if data and isinstance(data.get('data'), list) else []

    def _jikan_full(self, mal_id, timeout=30):
        data = self._http_get('{}/manga/{}/full'.format(self.JIKAN_BASE, mal_id), timeout)
        return data.get('data') if data else None

    def _mal_cover_url(self, item):
        jpg = (item.get('images') or {}).get('jpg') or {}
        return jpg.get('large_image_url') or jpg.get('image_url') or ''

    def _mal_item_to_metadata(self, item, series_name=None, series_index=None):
        title = item.get('title_english') or item.get('title') or 'Unknown'
        mi    = Metadata(title)

        mal_id = item.get('mal_id')
        if mal_id:
            mi.set_identifier('mal', str(mal_id))

        authors = []
        for entry in item.get('authors') or []:
            raw = entry.get('name', '').strip()
            if not raw:
                continue
            if ', ' in raw:
                family, given = raw.split(', ', 1)
                raw = '{} {}'.format(given.strip(), family.strip())
            if raw not in authors:
                authors.append(raw)
        mi.authors = authors or ['Unknown']

        tags = []
        for section in ('genres', 'themes', 'demographics'):
            for g in item.get(section) or []:
                name = g.get('name', '').strip()
                if name and name not in tags:
                    tags.append(name)
        if tags:
            mi.tags = tags

        synopsis = re.sub(
            r'\s*\[Written by MAL Rewrite\]', '',
            (item.get('synopsis') or '').strip()
        ).strip()
        if synopsis:
            mi.comments = synopsis

        serials = item.get('serializations') or []
        if serials:
            mi.publisher = serials[0].get('name', '').strip()

        from_str = (item.get('published') or {}).get('from', '')
        if from_str:
            try:
                mi.pubdate = datetime.strptime(from_str[:10], '%Y-%m-%d').replace(tzinfo=utc_tz)
            except (ValueError, TypeError):
                pass

        mi.series = series_name or title
        if series_index is not None:
            mi.series_index = series_index

        cover_url = self._mal_cover_url(item)
        if cover_url:
            mi.set_identifier('mal_cover', cover_url)

        return mi

    # -----------------------------------------------------------------------
    # MangaDex helpers
    # -----------------------------------------------------------------------

    def _mangadex_search(self, query, timeout=30):
        params = {
            'title':      query,
            'limit':      5,
            'includes[]': ['author', 'artist', 'cover_art'],
        }
        url  = '{}/manga?{}'.format(self.MANGADEX_BASE, urlencode(params, doseq=True))
        data = self._http_get(url, timeout)
        return data['data'] if data and isinstance(data.get('data'), list) else []

    def _mangadex_series_cover_url(self, item):
        md_id = item.get('id', '')
        for rel in item.get('relationships') or []:
            if rel.get('type') == 'cover_art':
                file_name = (rel.get('attributes') or {}).get('fileName', '')
                if file_name and md_id:
                    return '{}/{}/{}'.format(self.MANGADEX_CDN, md_id, file_name)
        return ''

    def _mangadex_volume_covers(self, md_id, volume_number=None, timeout=30):
        """
        Fetch cover art entries for *md_id*, filtered and sorted smartly.

        When *volume_number* is given, only entries within the configured
        volume window are returned.  The global max_covers cap is also applied.

        Returns list of {'url', 'volume', 'exact'} dicts.
        """
        max_covers    = self.prefs.get('max_covers',    DEFAULT_MAX_COVERS)
        volume_window = self.prefs.get('volume_window', DEFAULT_VOLUME_WINDOW)

        covers, offset, limit = [], 0, 100

        while True:
            params = {'manga[]': md_id, 'limit': limit, 'offset': offset}
            url    = '{}/cover?{}'.format(self.MANGADEX_BASE, urlencode(params, doseq=True))
            data   = self._http_get(url, timeout)

            if not data or not isinstance(data.get('data'), list):
                break

            batch = data['data']
            if not batch:
                break

            for entry in batch:
                attrs     = entry.get('attributes') or {}
                file_name = attrs.get('fileName', '')
                vol_str   = (attrs.get('volume') or '').strip()

                try:
                    vol_num = float(vol_str) if vol_str else None
                except (ValueError, TypeError):
                    vol_num = None

                if not file_name:
                    continue

                # Apply volume window filter when we have a target volume
                if volume_number is not None and vol_num is not None:
                    if abs(vol_num - volume_number) > volume_window:
                        continue      # outside window — skip

                is_exact = (
                    vol_num is not None
                    and volume_number is not None
                    and abs(vol_num - volume_number) < 0.01
                )
                covers.append({
                    'url':    '{}/{}/{}'.format(self.MANGADEX_CDN, md_id, file_name),
                    'volume': vol_num,
                    'exact':  is_exact,
                })

            total   = data.get('total', 0)
            offset += limit
            if offset >= total:
                break

        # Sort: exact first, then ascending volume, then unlabelled
        def _sort_key(c):
            if c['exact']:               return (0, c['volume'] or 0)
            if c['volume'] is not None:  return (1, c['volume'])
            return (2, 0)

        covers.sort(key=_sort_key)

        # Apply global max_covers cap
        return covers[:max_covers]

    def _mangadex_item_to_metadata(self, item, series_name=None, series_index=None):
        attrs = item.get('attributes') or {}
        md_id = item.get('id', '')

        titles = attrs.get('title') or {}
        title  = (
            titles.get('en')
            or titles.get('ja-ro')
            or next(iter(titles.values()), 'Unknown')
        )
        mi = Metadata(title)

        if md_id:
            mi.set_identifier('mangadex', md_id)

        authors, artists = [], []
        for rel in item.get('relationships') or []:
            name = (rel.get('attributes') or {}).get('name', '').strip()
            if not name:
                continue
            if rel.get('type') == 'author' and name not in authors:
                authors.append(name)
            elif rel.get('type') == 'artist' and name not in artists:
                artists.append(name)
        mi.authors = (authors + [a for a in artists if a not in authors]) or ['Unknown']

        tags = []
        for tag in attrs.get('tags') or []:
            tag_name = ((tag.get('attributes') or {}).get('name') or {}).get('en', '').strip()
            if tag_name and tag_name not in tags:
                tags.append(tag_name)
        if tags:
            mi.tags = tags

        desc = attrs.get('description') or {}
        description = (
            (desc.get('en') or next(iter(desc.values()), ''))
            if isinstance(desc, dict) else str(desc)
        )
        if description:
            mi.comments = description.strip()

        year = attrs.get('year')
        if year:
            try:
                mi.pubdate = datetime(int(year), 1, 1, tzinfo=utc_tz)
            except (ValueError, TypeError):
                pass

        mi.series = series_name or title
        if series_index is not None:
            mi.series_index = series_index

        series_cover = self._mangadex_series_cover_url(item)
        if series_cover:
            mi.set_identifier('mangadex_cover', series_cover)

        return mi

    # -----------------------------------------------------------------------
    # Identify
    # -----------------------------------------------------------------------

    def identify(
        self, log, result_queue, abort,
        title=None, authors=None, identifiers={}, timeout=30
    ):
        if abort.is_set():
            return None

        series_name, series_index = self._parse_series_and_volume(title or '')
        search_query = series_name or title or ''

        if not search_query:
            log.warn('MangaMetadata: No title provided.')
            return None

        results = []

        # ---- 1. MyAnimeList via Jikan ----
        log.info('MangaMetadata: Searching MAL for "{}"'.format(search_query))
        mal_items = self._jikan_search(search_query, timeout)

        if mal_items:
            log.info('MangaMetadata: {} MAL result(s).'.format(len(mal_items)))
            for idx, item in enumerate(mal_items[:3]):
                if abort.is_set():
                    break
                if idx == 0 and item.get('mal_id'):
                    time.sleep(self.JIKAN_DELAY)
                    full = self._jikan_full(item['mal_id'], timeout)
                    if full:
                        item = full
                mi = self._mal_item_to_metadata(item, series_name, series_index)
                mi.source_relevance = idx
                results.append(mi)
                time.sleep(self.JIKAN_DELAY)
        else:
            log.info('MangaMetadata: No MAL results.')

        # ---- 2. Fallback: MangaDex ----
        if not results:
            log.info('MangaMetadata: Falling back to MangaDex for "{}".'.format(search_query))
            md_items = self._mangadex_search(search_query, timeout)
            if md_items:
                log.info('MangaMetadata: {} MangaDex result(s).'.format(len(md_items)))
                for idx, item in enumerate(md_items[:3]):
                    if abort.is_set():
                        break
                    mi = self._mangadex_item_to_metadata(item, series_name, series_index)
                    mi.source_relevance = idx
                    results.append(mi)
            else:
                log.warn('MangaMetadata: No results found anywhere.')

        for mi in results:
            if abort.is_set():
                break
            result_queue.put(mi)

        return None

    # -----------------------------------------------------------------------
    # Cover download  (multi-cover, config-limited)
    # -----------------------------------------------------------------------

    def download_cover(
        self, log, result_queue, abort,
        title=None, authors=None, identifiers={}, timeout=30,
        get_best_cover=False
    ):
        """
        Download cover(s) and push (plugin, image_bytes) into result_queue.

        Priority
        ─────────────────────────────────────────────────────────────────────
        A) Volume found AND MangaDex exact match exists
              -> Push that one cover immediately (fast path)

        B) No exact match:
              -> MangaDex covers within ±volume_window of target  (or all if
                 no volume found), capped at max_covers
              -> MangaDex series cover
              -> MAL series cover
        """
        if abort.is_set():
            return

        max_covers = self.prefs.get('max_covers', DEFAULT_MAX_COVERS)

        # --- Collect identifiers ---
        ids = dict(identifiers)
        if not any(ids.get(k) for k in ('mangadex', 'mal', 'mal_cover', 'mangadex_cover')):
            log.info('MangaMetadata: Running identify to collect IDs …')
            dummy_q = Queue()
            self.identify(
                log, dummy_q, abort,
                title=title, authors=authors, identifiers=ids, timeout=timeout,
            )
            while True:
                try:
                    mi = dummy_q.get_nowait()
                    for k, v in mi.get_identifiers().items():
                        if k not in ids:
                            ids[k] = v
                except Empty:
                    break

        _, volume_number = self._parse_series_and_volume(title or '')
        log.info('MangaMetadata: Volume={}, max_covers={}, window=±{}'.format(
            volume_number,
            max_covers,
            self.prefs.get('volume_window', DEFAULT_VOLUME_WINDOW),
        ))

        # Resolve MangaDex UUID
        md_id = ids.get('mangadex') or ''
        if not md_id:
            m = re.search(
                r'/covers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/',
                ids.get('mangadex_cover', '')
            )
            if m:
                md_id = m.group(1)

        pushed_urls  = []
        exact_pushed = False

        # ── Path A: exact volume cover ────────────────────────────────────
        if md_id and volume_number is not None:
            log.info('MangaMetadata: Fetching windowed MangaDex covers for {} …'.format(md_id))
            covers = self._mangadex_volume_covers(md_id, volume_number, timeout)

            for entry in covers:
                if abort.is_set():
                    return
                if entry['exact']:
                    log.info('MangaMetadata: ✓ Exact vol {} cover.'.format(volume_number))
                    img = self._download_image(entry['url'], timeout)
                    if img:
                        result_queue.put((self, img))
                        pushed_urls.append(entry['url'])
                        exact_pushed = True
                    break

            if not exact_pushed:
                log.info('MangaMetadata: No exact match in window — offering all windowed covers.')

        # ── Path B: no exact match ────────────────────────────────────────
        if not exact_pushed:
            if md_id:
                covers = self._mangadex_volume_covers(md_id, volume_number, timeout)
                for entry in covers:
                    if abort.is_set():
                        return
                    if entry['url'] in pushed_urls:
                        continue
                    if len(pushed_urls) >= max_covers:
                        log.info('MangaMetadata: Reached max_covers cap ({}).'.format(max_covers))
                        break
                    log.info('MangaMetadata: vol {} → {}'.format(entry['volume'], entry['url']))
                    img = self._download_image(entry['url'], timeout)
                    if img:
                        result_queue.put((self, img))
                        pushed_urls.append(entry['url'])

            # MangaDex series cover
            md_series = ids.get('mangadex_cover', '')
            if md_series and md_series not in pushed_urls and len(pushed_urls) < max_covers:
                log.info('MangaMetadata: MangaDex series cover.')
                img = self._download_image(md_series, timeout)
                if img:
                    result_queue.put((self, img))
                    pushed_urls.append(md_series)

            # MAL series cover
            mal_cover = ids.get('mal_cover', '')
            if mal_cover and mal_cover not in pushed_urls and len(pushed_urls) < max_covers:
                log.info('MangaMetadata: MAL series cover.')
                img = self._download_image(mal_cover, timeout)
                if img:
                    result_queue.put((self, img))
                    pushed_urls.append(mal_cover)

            count = len(pushed_urls)
            if count:
                log.info('MangaMetadata: {} cover(s) queued.'.format(count))
            else:
                log.warn('MangaMetadata: No covers could be downloaded.')


# ---------------------------------------------------------------------------
# Quick manual test  (calibre-debug __init__.py)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from calibre.ebooks.metadata.sources.test import (
        test_identify_plugin, test_cover_plugin,
    )
    test_identify_plugin(MangaMetadata, [
        ({'title': 'Berserk Vol 1'},      []),
        ({'title': 'One Piece Vol 100'},  []),
        ({'title': 'Vagabond'},           []),
    ])
    test_cover_plugin(MangaMetadata, [
        ({'title': 'Berserk Vol 3'},     []),
        ({'title': 'One Piece Vol 100'}, []),
        ({'title': 'Vagabond'},          []),
    ])
