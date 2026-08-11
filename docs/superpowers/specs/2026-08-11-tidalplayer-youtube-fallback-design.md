# TidalPlayer YouTube fallback design

## Goal

`tplay` will accept single-video YouTube links, prefer a high-confidence Tidal recording, and queue the YouTube source through Red LavaLink when Tidal has no confident match.

The batch also removes duplicate URL parsing and shares queue admission between Tidal and YouTube tracks.

## URL boundary

`parse_provider_url()` remains the source of truth for provider URLs. It will recognize these HTTPS YouTube forms:

- `youtube.com/watch?v=<video-id>` and the `www`, `m`, and `music` subdomains;
- `youtu.be/<video-id>`;
- `youtube.com/shorts/<video-id>`;
- `youtube.com/live/<video-id>`;
- `youtube.com/embed/<video-id>`;
- the existing playlist and watch-with-`list` forms.

The parser will require an 11-character YouTube video ID made from ASCII letters, digits, `_`, or `-`. A valid `list` parameter keeps playlist precedence. The parser will strip surrounding whitespace and one Discord angle-bracket pair before it validates the URL. It will reject HTTP, embedded credentials, unsupported hosts, malformed IDs, and unsupported paths.

The dispatcher will pass the parsed `ProviderURL` to YouTube handlers. The handlers will use `identifier` instead of parsing the URL with a second regular expression. Spotify routing and behavior stay unchanged. Tidal album and mix parsing also stay unchanged until a failing production URL supplies a reproducible path shape.

## Single-video data flow

1. `tplay` parses the link and routes `content_type == "video"` to a single-video handler.
2. The handler asks YouTube Data API `videos.list(part="snippet", id=video_id, maxResults=1)` for title, channel, and thumbnails when the client exists.
3. If API metadata is unavailable, missing, or malformed, the handler loads the canonical YouTube URL through LavaLink once and uses the returned track title and author as matching metadata. It retains that loaded track for a possible fallback.
4. The handler creates a catalog query from the available video metadata, then calls the existing bounded Tidal search path.
5. A new matching helper accepts a candidate when the normalized Tidal title appears as a complete phrase in the cleaned video title and the normalized Tidal artist matches the cleaned channel or appears as a complete phrase in the video title.
6. A confident candidate uses the existing Tidal stream and metadata path. The handler will not use the first Tidal result as a fallback.
7. With no confident match, the handler queues the retained LavaLink track or loads the canonical YouTube URL once if it has not been loaded yet. Red LavaLink owns that request timeout. The cog queues the first returned track.

The handler can build fallback metadata from the loaded track title, author, `length`, `thumbnail`, and `uri`. It will not retry either provider call and will never load the same YouTube URL twice within one command.

## Queue and metadata boundary

The cog will extract the common loaded-track admission steps from `_load_and_queue_track()` into one private helper. Both sources will use this helper after they resolve a LavaLink track and build `TrackMeta`. The helper will preserve current idle detection, queue order, rollback, controller creation, and queue-confirmation behavior.

`TrackMeta` will gain an optional `source` field. Existing Tidal metadata will omit it and keep the current default. YouTube fallback metadata will set:

- `source`: `YouTube`;
- `quality`: `YouTube`;
- `share_url`: the canonical watch URL;
- `duration`: Red LavaLink `length` converted from milliseconds to seconds;
- `track_id`: `None`;
- `album`: `None`;
- `image`: the YouTube thumbnail when available.

The embed factories and Components V2 controller will read `source`. They will render `Playing from YouTube` and `Open in YouTube` for fallback tracks. Tidal output will retain its current text and links. Autoplay history will continue to use the normalized artist/title signature when `track_id` is absent.

## Task-registry validation

Inspection found that temporary queue-message tasks already remove themselves from `_tasks` in `_delete_after()` and Last.fm session closure already attaches a discard callback. Regression coverage will preserve that bounded lifecycle. No extra task abstraction will be added because it would not improve speed or memory use.

## Errors and logs

The parser will return the existing invalid-link response for malformed YouTube URLs. Missing or private API metadata will use LavaLink metadata for the Tidal match attempt before falling back to the retained YouTube track. A LavaLink empty or failed result will send a YouTube playback error.

Logs may include provider, operation, guild ID, video ID, result type, and elapsed time. Logs will exclude the full request URL, query parameters, API keys, and raw provider exception text.

Cancellation will propagate. The handler will not convert cancellation into a fallback or error message.

## Verification

Tests will cover:

- each supported YouTube URL shape and malformed-ID rejection;
- playlist precedence for watch URLs containing `list`;
- Discord angle-bracket normalization;
- confident Tidal selection without a YouTube LavaLink load;
- cover or uncertain-match rejection;
- direct YouTube fallback with one LavaLink load;
- fallback after missing or malformed YouTube API metadata;
- source-aware embeds and controller text;
- millisecond-to-second duration conversion;
- shared queue admission for idle and queued players;
- finished temporary tasks leaving `_tasks`;
- cancellation propagation and credential-safe logs.

Each behavior change starts with a failing regression test. The final gate runs the complete TidalPlayer suite, Ruff, compilation, diff validation, and a scope review. This batch leaves Spotify, Tidal album and mix handlers, autoplay admission, node recovery, signed-URL timing, and cog teardown unchanged.
