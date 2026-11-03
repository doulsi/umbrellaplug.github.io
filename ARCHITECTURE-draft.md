# Umbrella (plugin.video.umbrella) — Architecture Draft

Scope: how the Kodi add-on is structured and how requests flow through it, plus a
section highlighting the modifications committed on this fork (branch
`vibe/swr-review-fixes-b3cca4`, draft PR #1, upstream feature request issue #655).

The repository is a Kodi add-on *repository* (Pages-style): it hosts one add-on
per Kodi major version, and each tree is a near-identical copy of the same code:

```
matrix/  nexus/  omega/  piers/          # Kodi 19 / 20 / 21 / 22
└── plugin.video.umbrella/
    ├── umbrella.py                     # plugin entry point
    ├── service.py                      # startup service
    ├── addon.xml                       # extension points
    └── resources/lib/
        ├── menus/                      # navigator + list screens
        ├── indexers/                   # TMDb / fanart.tv / TVmaze API clients
        ├── modules/                    # control, trakt, sources, player, library, ...
        ├── database/                   # cache.py, metacache, providerscache, sync dbs
        ├── windows/                    # dialogs / managers
        ├── debrid/  cloud_scrapers/  internal_scrapers/  externals/  context/
```

The four `plugin.video.umbrella` trees are byte-identical for every file the
feature work touches (verified by checksum), so everything below applies to all
of them and changes are applied once and copied across.

## 1. Entry points

`addon.xml` declares two runtime extension points:

- `xbmc.python.pluginsource` → `umbrella.py` — runs for every `plugin://plugin.video.umbrella/...`
  URL (menu clicks, widgets, skin shortcuts). It calls `router.router(sys.argv[2])`
  and exits unless `Container.PluginName` contains "umbrella".
- `xbmc.service` → `service.py`, `start="startup"` — long-running background
  process: settings checks, account sync, library service, Trakt/Simkl/etc.
  service loops, and a `SettingsMonitor(xbmc.Monitor)` that receives Kodi
  notifications (`onSettingsChanged`, and since this fork `onNotification`).

## 2. Request flow (plugin invocations)

```
plugin:// URL
  └── umbrella.py → router.router(argv2)
        └── parse query params (action, url, imdb/tmdb/tvdb ids, ...)
              └── menus/navigator.py  Navigator.<action>()
                    ├── builds menu items / conditions (_eval_condition_key)
                    └── delegates list building to menus/*.py classes
                          (Movies, TVshows, Seasons, Episodes, Collections)
                                └── indexers/tmdb.py, modules/trakt.py, simkl.py,
                                    mdblist.py, ...  (remote API calls)
                                └── database/cache.py  (all network results cached)
                    └── xbmcplugin.addDirectoryItem(...) → Kodi renders the listing
```

`Navigator` (`resources/lib/menus/navigator.py`) is the dispatcher: `root()` and
per-section methods (`movies()`, `tvshows()`, ...) map `action=` params to the
menu classes in `resources/lib/menus/`. Menu conditions (`has_lib_movies`,
`trakt_credentials`, ...) are evaluated lazily in `_eval_condition_key()`.

`resources/lib/modules/control.py` is the central Kodi facade: `infoLabel`,
`condVisibility`, `homeWindow` (Window 10000), `player`, `monitor`, `refresh()`,
paths (`dataPath`, `cacheFile`, ...), and `trigger_widget_refresh()` which drives
skin widgets via `widgetreload*` window properties plus `UpdateLibrary(video)`.

## 3. Persistence layer (`resources/lib/database/`)

- `cache.py` — the general metadata cache: sqlite `cache.db` in the addon profile,
  keyed by `function-name + md5(args)`. API: `get()`, `get_coalesced()`,
  `get_stale()` (this fork), `cache_get/insert`, `remove()`, `timeout()`.
- `metacache.py`, `providerscache.py`, `watchedcache.py`, `artwork.py`,
  `fanarttv_cache.py` — specialized caches (item metadata, provider results,
  watched state, art).
- `traktsync.py`, `simklsync.py`, `mdbsync.py`, ... — account sync state dbs.
- `menu.py` — user-editable menu definitions driving the navigator.

## 4. Caching model (core of this fork's changes)

Upstream behavior: `cache.get(fn, hours, *args)` blocks on the network whenever
the entry is missing or older than `hours`.

This fork adds **stale-while-revalidate** (`cache.get_stale()`, in `cache.py`):

1. Return the on-disk value immediately, even if stale.
2. If stale and `duration > 0`, spawn a daemon thread that re-fetches, rewrites
   the cache, and — only if the stored value actually changed, the request
   verifiably came from the visible listing (see below), no playback is active,
   and the user is still on the same folder — issues one throttled
   `Container.Refresh` so the open listing updates itself.
3. Cold cache (nothing stored) still fetches synchronously.
4. `duration == 0` entries always fetch synchronously (no stale reads, no loop).
5. `force=True` bypasses the cache entirely (forced refreshes after user edits).

Safety rails added after maintainer review (commit `d0b945f`):

- **No refresh loops / no refresh on failure** — duration-0 handled
  synchronously; refresh only fires when the rewritten cache value differs.
- **Nested caches** — a thread-local flag makes inner `get_stale()` calls during
  a background revalidation fetch fresh, so an outer list can't store stale
  inner data under a fresh timestamp.
- **Widget protection** — refresh origin requires `Container.PluginName` to
  contain the addon **and** the videos window active, not just a `plugin://`
  `Container.FolderPath` prefix (widgets use plugin:// URLs too).
- **Cross-invocation dedup** — revalidation claims and the 2 s auto-refresh
  throttle are persisted in a shared sqlite coordination db
  (`cache.db.revalidate`), so separate Kodi plugin processes share them;
  claims expire after 300 s.
- **Library probe invalidation** — `service.py` `SettingsMonitor.onNotification`
  drops the cached `has_lib_movies` probe on `VideoLibrary.OnUpdate /
  OnRemove / OnScanFinished` (it is otherwise cached 24 h).

## 5. Modifications committed on this fork

Two code commits on top of upstream `master` (plus fork-testing metadata commits):

| Commit | What | Files (× 4 version trees) |
|---|---|---|
| `d62b5d4f` — "Speed up all screens: stale-while-revalidate + lazy library probe" (feature, issue #655, version 6.7.100) | New `cache.get_stale()` + background revalidate helper in `cache.py`; switched all list fetches in `menus/{movies,tvshows,episodes,seasons,collections}.py` and `indexers/tmdb.py` from `cache.get` to `cache.get_stale`; made the JSON-RPC `VideoLibrary.GetMovies` probe lazy + 24 h cached in `menus/navigator.py` (`_eval_has_lib_movies` / `_lib_movies_probe`) | 7 .py files per tree + addon.xml/zips |
| `d0b945f` — "Address maintainer review of stale-while-revalidate" (this branch, PR #1) | The six review fixes listed in §4 plus `service.py` `onNotification` library-probe invalidation and `force=True` on the `_force_fresh` paths in `movies.py`/`tvshows.py` | `database/cache.py`, `menus/movies.py`, `menus/tvshows.py`, `service.py` per tree (16 files, +600/−84) |

Deliberately unchanged by the feature: side-effect-only cache calls
(progress/watched rebuilds), fanart.tv art cache, per-season TMDb meta, and the
existing service-loop background sync (`modules/tools.py`).

## 6. Verification performed

- `python3 -m py_compile` on all changed files in all four trees: pass.
- Headless behavioral test of `cache.py` (stubbed Kodi modules), 17/17 checks:
  cold sync fetch, fresh hit, stale-while-revalidate + single refresh, no
  refresh on failed request, duration-0 synchronous, `force=True` bypass,
  nested-revalidate freshness, widget/window protection, cross-invocation
  throttle and dedup claim/release.
