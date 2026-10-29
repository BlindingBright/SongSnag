# Changelog

## 1.1.0 (2026-10-29)

### New

- **Whole albums, artists and playlists.** *Get an album or artist…* takes an artist name, an album title, or a
  YouTube / YouTube Music link (album, playlist, channel or `@handle`) and shows how many songs it found before
  downloading anything. Songs come from YouTube Music with full album details, queue behind whatever you're playing
  now, and download a few seconds apart. Songs you already have are left out.
- **Find an artist.** *Find an artist…* (tray) and *Find artist online…* (Library) search YouTube Music for any
  artist, including ones not in your library yet. Each result says how many of their songs you already have, so
  artists who share a name are easy to tell apart. Download their music, or just follow them.
- **Follow artists.** Followed artists' new albums and singles are checked daily and downloaded automatically.
  Follow from the confirmation, *Find an artist…*, or a song's right-click menu in the Library; manage the list in
  *Followed artists…*.
- **Discover new music.** Suggestions based on the songs you play most (recent plays count more), from YouTube
  Music radio. A song is only suggested when several of your favourites lead to it or it's by an artist you already
  play, which keeps one radio's drift out. *Listen on YouTube* previews without downloading; *Not interested* hides
  a song for good. Settings can download the top few each week (off by default).
- **More browsers.** Firefox (including its newer `~/.config/mozilla/firefox` location, Flatpak and Snap), Vivaldi,
  Opera and Chromium are watched alongside Brave, Chrome and Edge, and can provide the YouTube login.
- **Merge duplicate artist folders…** finds artists filed under more than one spelling (`＂Weird Al＂ Yankovic`,
  `'Weird' Al Yankovic`, `Weird Al Yankovic`) and, after you confirm, moves their songs into one folder. Only files
  SongSnag saved are moved; anything else in those folders stays put.
- **Library**
  - The queue is part of the list: the song downloading now first (highlighted), then *Waiting #1, #2…* in download
    order, and retries with their time. Queued album songs show their artist, title and album straight away.
  - *Checking: …* / *Downloading: …* in the bottom bar.
  - A **#** column with track numbers; sorting by it keeps each album together in order.
  - One **Export** menu (playlist or CSV). Exported playlists list albums in track order.
  - *Download anyway* / *Retry download* moved to the right-click menu, for skipped and failed songs.
  - Right-click a song to get its whole album, everything by its artist, or follow the artist.
- **Progress window for lookups.** Looking up an album or artist opens a window straight away ("Reading album 12 of
  50: …") with Cancel, instead of appearing to do nothing until the confirmation.

### Better

- **Track numbers and album tags.** Songs downloaded from an album keep the exact release and position they came
  from: every song of an album gets the same album name, its own track number, and the album's own title and
  credits ("5318008 (feat. Lil Broccoli)" rather than a video's "5318008 OFFICIAL VIDEO"). Each album is filed
  under its album artist, so it stays in one folder. *Fill in missing album info* uses the exact release too.
- **No duplicate downloads.** The same song from another upload (the official video, the Topic track, a lyric
  video) is saved once and skipped after that, with the existing file named in the note. Notes like
  "(Official Video)", "(Parody of …)" and "(feat. …)" are ignored, but "(Live)", "(Remix)", "(Acoustic)" and the
  like count as different recordings. Plays of the other uploads still count toward the saved song.
- **One folder per artist.** `"` no longer becomes a full-width `＂` in folder names, a new song goes into the
  existing folder whatever the spelling, and YouTube Music's spelling of the artist is used when an album matches.
  Re-filing a song removes its old folder once empty.
- **The Library handles big libraries.** It was rebuilt on Qt's model/view classes. With 51,620 songs it opens in
  0.5 s, sorts any column in about 0.2 s, and a refresh with nothing new takes 54 ms. Only rows that changed are
  reloaded while downloads run. Your selection, current row and scroll position survive refreshes and sorting, and a
  closed Library frees its rows.
- **Faster lookups.** Albums are read four at a time: an artist with 50 releases takes about 7 s instead of 27 s.
- **Quieter in the background.**
  - On Linux and macOS an idle tray no longer wakes up twice a second (signals now wake it through a socket).
  - The tray icon only changes when the state does: still while idle, grey while paused, and a gentle flash only
    while downloading, importing or looking something up. Some tray hosts (KDE Plasma) visibly blinked on every
    repeated icon update.
  - The download worker rereads settings only when the file changes.
- The tray tooltip is just the status, without a "SongSnag:" prefix.
- Imports and whole-album downloads are paced gently; songs you play live still go first.

### Fixed

- **Sorting the Library froze or crashed SongSnag** (segfault in PySide6 when the table called back into Python for
  every comparison), and so did changing the status filter afterwards.
- **Quitting while an album/artist lookup was running** showed an error that blocked SongSnag from closing.
- Upper-case "FEAT" / "FT" in artist names wasn't recognised as a featured artist.
- The "finished" notification called album and artist downloads a "History import".
- A second copy started with `SONGSNAG_HOME` set (a portable copy) found the regular copy and exited.

### Under the hood

- Catalog additions, all applied automatically on first start and compatible with 1.0's file: a song key for
  duplicate detection, the release and track number a song came from, a per-row change stamp maintained by SQLite
  triggers, and tables for followed artists and suggestions.
- New modules: `collect.py` (albums, artists, playlists, new releases), `discover.py` (suggestions), `tidy.py`
  (merging artist folders) and `ui/music.py` (their windows). Like downloads, all YouTube Music lookups run in the
  short-lived worker process, so the tray never loads that code.
- Tests: 75 → 111, including the Library window on an offscreen Qt, Firefox history (with its write-ahead log),
  duplicate detection, followed artists and suggestions. Verified on Python 3.10 and 3.14.

### Known limitations

- Full-concert videos such as NPR Tiny Desk sets are still filed under the channel ("NPR Music").
- Videos that fail with "Requested format is not available" are still retried five times before giving up.
- Copying the browser's history file is still the largest background cost while you browse (about 8 MB every
  15 seconds for a large Brave profile); the browser locks the file, so it can't be read in place.
