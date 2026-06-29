# Calibre Manga Metadata Plugin

A metadata source plugin for [Calibre](https://calibre-ebook.com/) that fetches manga metadata and per-volume cover art using the MyAnimeList (via Jikan API v4) and MangaDex APIs.

---

## What It Does

Calibre's built-in metadata sources aren't designed for manga. This plugin fills that gap by:

- Fetching manga metadata from **MyAnimeList via the Jikan API v4** (primary)
- Automatically falling back to **MangaDex** when MAL results are unavailable
- Downloading **per-volume cover art** from MangaDex, matched by volume number
- Offering a **configurable volume window** to control how many nearby volumes are included in cover results
- Capping cover downloads with a **configurable max covers** setting to keep things fast

---

## Screenshots

### Metadata Download
> Plugin retrieving full metadata from MyAnimeList — title, authors, series, genres, synopsis, and cover art.

<img src="images/Manga_Metadata_1.png" width="900" alt="Metadata download result in Calibre" />

### Cover Browser
> Per-volume cover art matched and returned by the plugin.

<img src="images/Manga_Metadata_cover.png" width="400" alt="Cover browser showing matched volume cover" />

### Plugin Settings
> Plugin listed as `Manga Metadata (1.3.0) by AshHimura` with the configurable cover download settings panel.

<img src="images/Manga_Metadata_Plugin_Settings.png" width="700" alt="Plugin settings panel in Calibre preferences" />

---

## Metadata Retrieved

| Field | Source |
|---|---|
| Title | MAL / MangaDex |
| Authors & Artists | MAL / MangaDex |
| Synopsis / Description | MAL / MangaDex |
| Tags / Genres | MAL / MangaDex |
| Publisher & Release Date | MAL / MangaDex |
| Series Name & Volume Number | Parsed from filename |
| Cover Art | MangaDex (per-volume) + MAL (series fallback) |

---

## How It Works

```
Calibre: Download Metadata
    └── Parse series name + volume number from title
    └── Search Jikan API (MyAnimeList)
            └── Success: return top 3 results as metadata candidates
            └── Failure / no results:
                    └── Fallback: search MangaDex API
                            └── Return top 3 MangaDex results

Calibre: Download Cover
    └── Path A — Volume number found + MangaDex exact match exists
            └── Push exact volume cover immediately (fast path)
    └── Path B — No exact match
            └── Push all MangaDex covers within ±volume_window of target
            └── MangaDex series cover (if under max_covers cap)
            └── MAL series cover (if under max_covers cap)
```

---

## Tech Stack

| Component | Technology |
|---|---|
| Plugin Framework | Python 3, Calibre Plugin API |
| Primary Metadata | [Jikan API v4](https://jikan.moe/) (MyAnimeList) |
| Fallback Metadata | [MangaDex API](https://api.mangadex.org/) |
| Cover Art | MangaDex CDN (per-volume matching) |
| Config UI | PyQt5 (native Calibre preferences panel) |

---

## Requirements

- [Calibre](https://calibre-ebook.com/) 5.0+
- Python 3 (bundled with Calibre)
- Internet connection for API access

---

## Installation

### Option A — Install from ZIP (recommended)

1. Download the latest release ZIP from the [Releases](https://github.com/AshHimura/calibre-manga-metadata/releases) page
2. Open Calibre
3. Go to **Preferences → Plugins → Load plugin from file**
4. Select the downloaded ZIP
5. Restart Calibre

### Option B — Install from source

```bash
git clone https://github.com/AshHimura/calibre-manga-metadata.git
cd calibre-manga-metadata
zip MangaMetadata.zip __init__.py
```

Then in Calibre: **Preferences → Plugins → Load plugin from file** → select the ZIP.

---

## Configuration

Access plugin settings via:

**Preferences → Plugins → Metadata Sources → Manga Metadata → Customize plugin**

| Setting | Default | Range | Description |
|---|---|---|---|
| Max covers to download | 10 | 1–50 | Hard cap on cover images queued in the cover browser. Lower = faster; higher = more choices. |
| Volume window (±) | 3 | 0–20 | When a volume number is detected, only fetch covers within this many volumes either side of the target. Set to `0` for exact-match only. |

**Example:** Searching for `One Piece Vol 100` with a window of `3` fetches covers for volumes 97–103, capped at max covers.

Settings persist across Calibre restarts via Calibre's built-in config system.

---

## Usage

1. Select one or more manga volumes in your Calibre library
2. Right-click → **Download metadata and covers**
3. Ensure **Manga Metadata** is checked in the sources list
4. Calibre searches MAL first, falling back to MangaDex automatically
5. Review and apply the returned metadata and cover art

**Tip:** Files named in the format `Series Name Vol X.cbz` (e.g. `Berserk Vol 3.cbz`) give the best results — the plugin parses the series name and volume number directly from the title.

---

## Cover Priority Logic

| Situation | Result |
|---|---|
| Volume number found + exact MangaDex match | Exact volume cover pushed immediately |
| Volume number found, no exact match | All covers within ±window pushed (up to max cap) |
| No volume number found | All available MangaDex covers pushed (up to max cap) |
| MangaDex unavailable | MAL series cover used as final fallback |

---

## Known Limitations

- Jikan API enforces rate limiting (~3 requests/second) — large batch searches may be slower than expected
- Cover art availability depends on what MangaDex has indexed for a given volume
- Series with non-standard volume numbering (omnibus, deluxe editions) may require manual adjustment after import
- No volume number in the filename disables the volume window feature

---

## Roadmap

- [ ] AniList API as a secondary fallback
- [ ] Improved volume number parsing for omnibus and deluxe editions
- [ ] Cached API responses to reduce redundant requests on large libraries

---

## License

GPL v3
