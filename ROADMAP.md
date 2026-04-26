# Roadmap

Forward-looking plans for TagStudio — tag-based photo/file organization. Alpha today, tracking toward 1.0.

## Planned Features

### Library & Schema
- Schema versioning with forward/backward migration tests (Alembic-style) so alpha libraries survive 1.0
- Multi-root libraries (a single library spanning several mount points / external drives)
- Per-folder library overrides — ignore rules, field defaults, auto-tag rules
- Portable / relocatable `.TagStudio` — relative paths only, survives moving the drive letter
- Optional sidecar export (XMP, JSON) so tags round-trip with files outside TagStudio

### Tagging & Search
- Saved searches / smart collections (materialized as pinned sidebar items)
- Boolean + nested tag grammar (`cat AND (outdoor OR porch) NOT blurry`) with autocomplete
- Bulk tag editor — rename, merge, split, reparent tags across the whole library
- Tag inheritance / implication rules (`kitten` implies `cat` implies `animal`)
- OCR + face clustering as opt-in workers that write back to fields/tags

### Performance
- Virtualized thumbnail grid — constant memory for 500k+ entries
- Incremental scanner on `inotify` / `ReadDirectoryChangesW` so the library stays live without a manual rescan
- Hash-based dedupe pass (blake3) surfacing duplicates as a dedicated view
- Preview cache eviction policy (LRU by size cap, configurable per library)

### Interop
- Plugin API for preview handlers + metadata extractors (so RAW, PSD, DCM, GLB can be community-maintained)
- Watch-folder rules that auto-apply tags based on path / filename regex
- Import from Lightroom catalogs, digiKam DB, Hydrus, ExifTool sidecars
- Export to Immich / PhotoPrism / Nextcloud as read-only mirror
- CLI (`tagstudio query`, `tagstudio tag`, `tagstudio export`) for scripting pipelines

### UX
- Compact / dense layout toggle, keyboard-driven navigation
- Split-view: grid + detail inspector + map pane simultaneously
- Map view using EXIF GPS (MapLibre tiles) with tag-colored clusters
- Timeline view keyed on EXIF DateTimeOriginal with zoom (year/month/day)
- Per-field histograms (camera model, focal length, rating) as facets

## Competitive Research

- **digiKam**: the mature reference — face tags, geolocation, database-backed. TagStudio wins on modern UI and non-destructive library model; borrow their face-clustering pipeline.
- **Hydrus Network**: tag-centric, hash-based, handles 500k+ files. Worth studying their parent/sibling tag graph and PTR (public tag repo) model for our implication rules.
- **Eagle / Pixave**: commercial, polished, pay-per-seat. TagStudio's free + portable + non-proprietary pitch is the differentiator — keep positioning that way.
- **Immich / PhotoPrism**: server-first, AI-heavy. Complement rather than compete — offer one-way export so users can combine TagStudio's curation with Immich's mobile sync.

## Nice-to-Haves

- Mobile companion app (read-only browse + quick tag) over a local HTTP bridge
- AI-assisted auto-tagging using local CLIP / moondream models (opt-in, no cloud)
- Shared library mode (SQLite in WAL on a SMB share, read-mostly)
- Color-histogram and perceptual-hash similarity search
- Versioned "outfits" of tags (save/restore per-project tag bundles)
- Built-in script console (Python) for power users to write one-off library transforms

## Open-Source Research (Round 2)

### Related OSS Projects
- https://github.com/tagspaces/tagspaces — offline tag-based doc manager; filename-encoded tags approach
- https://github.com/spacedriveapp/spacedrive — Rust virtual distributed filesystem, cloud+local unified
- https://github.com/hydrusnetwork/hydrus — heavyweight tag-based media manager with tag siblings/parents
- https://github.com/jhc13/taggui — image-dataset tag manager with sidecar `.txt` convention
- https://github.com/Deiwulf/AI-image-auto-tagger — wd-vit-tagger-v3 model integration reference
- https://github.com/bit-bots/imagetagger — collaborative labeling web platform
- https://github.com/allusion-app/Allusion — reference-image visual library, multi-root watched folders
- https://github.com/photoprism/photoprism — AI-tagged self-hosted photo manager, classification ideas
- https://github.com/immich-app/immich — ML-powered self-hosted alternative to Google Photos

### Features to Borrow
- Sidecar `.txt` tag format convention for interop with training pipelines (taggui)
- wd-vit-tagger-v3 / DeepDanbooru auto-tag model integration with confidence threshold slider (AI-image-auto-tagger)
- Tag siblings + tag parents graph relationships (hydrus) — more expressive than flat tags
- Filename-encoded tags as a portable fallback (`file[tag1 tag2].ext`) for systems without a sidecar DB (tagspaces)
- Multi-root library with per-root settings (Allusion) — solves current single-root limitation
- Perceptual-hash near-duplicate detection using `imagehash` (photoprism pattern)
- CLIP-embedding semantic search ("cat on red couch") via local ONNX model (immich pattern)
- Collaborative tagging over a LAN/WebDAV (imagetagger) — optional multi-user mode
- EXIF/IPTC/XMP round-trip so tags survive export to third-party tools (photoprism)
- Smart-album rules engine (dynamic queries saved as views) (immich smart-search)

### Patterns & Architectures Worth Studying
- Rust-core + multi-frontend split (spacedrive) — engine reusable from CLI, TUI, mobile
- Sidecar-first vs DB-first debate: hybrid approach (authoritative DB, periodic sidecar export for durability)
- Event-sourced library mutations — every tag change logged, enabling undo and branch/merge of tag sets
- Plugin API: untrusted Python sandboxed via RestrictedPython for user "library transforms"
- Watched-folder + inotify/fsevents live sync (Allusion) with conflict-resolution on moves/renames
