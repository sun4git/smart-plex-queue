#!/usr/bin/env python3
"""
Smart Plex Queue - Listener Service

Listens for Plex webhooks and intelligently queues the next song using
MediaSage's playlist generation based on the current track context.

This service does NOT require a Plex API token. Instead, it relies entirely
on MediaSage's API endpoints which handle all Plex authentication internally.

Architecture:
  Plex Server → (Webhook) → This Listener → (MediaSage API) → Plex Player

Usage:
    python3 queue_listener.py [PORT]  # Default port: 8000

Environment Variables:
    MEDIASAGE_HOST   - MediaSage API base URL (default: http://192.168.1.100:5765/api)

Toggle File:
    ../.smart-queue-enabled
    When the file exists, feature is active.
    When absent, webhooks are logged but ignored.
"""

import os
import sys
import json
import time
import random
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

# --- Configuration ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(SCRIPT_DIR) # This reliably points to the parent folder

TOGGLE_FILE = os.path.join(WORKSPACE_DIR, ".smart-queue-enabled")
LOG_FILE = os.path.join(WORKSPACE_DIR, "logs", "smart-queue.log")
MEDIASAGE_API = os.getenv(
    "MEDIASAGE_HOST",
    "http://192.168.1.100:5765/api"
)

# Track recently processed tracks to avoid duplicates (keyed by client_id)
recent_tracks = {}

# Track the last known shuffle state per client to detect manual toggles
last_shuffle_state = {}

# Track the last seen play queue ID per client to detect manual context changes
last_queue_id = {}

STATE_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()  # for thread-safe logging

# Number of track changes seen since the last AI generation, keyed by
# client_id. Initialized to the threshold so the first webhook triggers
# generation immediately.
songs_since_generation = {}

# Per-client skip threshold: how many track changes to wait before the
# next AI generation. 2 on LAN (refreshPlayQueue works immediately),
# 1 on cellular (AI tracks land after the pre-buffered next track, so
# generate one song earlier to keep the stream continuous).
# Defaults to 2 (safe/conservative). Updated after each successful queue call.
generation_threshold = {}


def log(message):
    """Simple logging to console and file."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    with LOG_LOCK:
        print(line, flush=True)

        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def is_enabled():
    """Check if the smart queue feature is enabled via flag file."""
    return os.path.exists(TOGGLE_FILE)


def parse_plex_webhook(data, headers=None):
    """Parse incoming Plex webhook payload."""
    content_type = ""
    if headers:
        content_type = headers.get("Content-Type", "")

    # Handle multipart/form-data (Plex's preferred format)
    if "multipart/form-data" in content_type and headers:
        boundary = None

        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
                break

        if boundary:
            boundary_bytes = ("--" + boundary).encode("utf-8")
            lines = (
                data.split(boundary_bytes)
                if isinstance(data, bytes)
                else data.split(boundary_bytes.decode("utf-8"))
            )

            log(
                f"DEBUG - Multipart boundary found, "
                f"split into {len(lines)} parts"
            )

            for i, line in enumerate(lines):
                if isinstance(line, bytes):
                    line_str = line.decode("utf-8", errors="replace")
                else:
                    line_str = line

                # Look for any form field containing JSON
                if "name=" in line_str and "{" in line_str:
                    log(
                        f"DEBUG - Multipart part {i} contains JSON payload "
                        f"(length: {len(line_str)})"
                    )

                    json_start = line_str.find("{")
                    json_end = line_str.rfind("}") + 1

                    if json_start != -1 and json_end > json_start:
                        payload_str = line_str[json_start:json_end]

                        try:
                            payload = json.loads(payload_str)

                            if isinstance(payload, dict):
                                log(
                                    "DEBUG - Successfully parsed multipart "
                                    f"payload, event: "
                                    f"{payload.get('event', 'unknown')}"
                                )
                                return payload

                        except json.JSONDecodeError as e:
                            log(
                                f"DEBUG - JSON parse error in multipart: "
                                f"{e}, payload preview: "
                                f"{payload_str[:100]}"
                            )

                elif "name=" in line_str:
                    log(
                        f"DEBUG - Multipart form field found but "
                        f"no JSON in part {i}"
                    )

    # Convert bytes to string for further parsing
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    # First try parsing as raw JSON
    try:
        payload = json.loads(data)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    # Fall back to URL-encoded form data (payload=...)
    parsed = parse_qs(data, keep_blank_values=True)
    payload_str = parsed.get("payload", ["{}"])[0]

    try:
        payload = json.loads(payload_str)
        return payload
    except json.JSONDecodeError:
        log(
            "Failed to parse webhook payload. "
            f"Content-Type: {content_type}, "
            f"Raw data (first 200 chars): {data[:200]}"
        )
        return None


def get_plex_clients():
    """Get available Plex clients from MediaSage."""
    try:
        resp = requests.get(
            f"{MEDIASAGE_API}/plex/clients",
            timeout=5
        )

        if resp.status_code == 200:
            data = resp.json()

            # Handle both {"clients": [...]} and [...] responses
            if isinstance(data, list):
                return data

            return data.get("clients", [])

    except Exception as e:
        log(f"Error getting clients from MediaSage: {e}")

    return []


def get_current_playback_info():
    """
    Get currently playing track info via MediaSage.

    We use MediaSage's own Plex integration rather than calling Plex directly.
    """
    try:
        resp = requests.get(
            f"{MEDIASAGE_API}/plex/players",
            timeout=5
        )

        if resp.status_code == 200:
            return resp.json()

    except Exception as e:
        log(f"Error getting playback info from MediaSage: {e}")

    return None


def get_shuffle_state(client_id):
    """Get the authoritative Plex PlayQueue shuffle state via MediaSage.

    Returns the raw dict which includes 'shuffle', 'queue_id', and 'error'.
    """
    if not client_id:
        return {}

    try:
        resp = requests.get(
            f"{MEDIASAGE_API}/plex/shuffle-state",
            params={"client_id": client_id},
            timeout=5,
        )

        if resp.status_code != 200:
            log(
                f"⚠️ Shuffle-state lookup failed: "
                f"HTTP {resp.status_code} - {resp.text[:300]}"
            )
            return {}

        data = resp.json()
        log(
            f"🎲 Shuffle state raw: client={client_id} "
            f"queue_id={data.get('queue_id')} shuffle={data.get('shuffle')} "
            f"error={data.get('error') or 'none'}"
        )
        return data

    except Exception as e:
        log(f"⚠️ Shuffle-state lookup error: {e}")
        return {}


def lookup_mediasage_rating_key(context):
    """
    Translate a Plex rating_key to MediaSage's internal rating key.

    Even though MediaSage syncs from Plex, it may assign its own keys.
    """
    plex_rating_key = context.get("ratingKey", "")
    track_title = context.get("title", "")
    artist_name = (
        context.get("grandparentTitle")
        or context.get("artist", "")
    )

    # Keep variables for compatibility/context; search uses track title.
    _ = artist_name

    # Use just the track title for search - MediaSage's search works better
    # with shorter queries.
    search_query = track_title.strip()

    try:
        resp = requests.get(
            f"{MEDIASAGE_API}/library/search",
            params={"q": search_query},
            timeout=5
        )

        if resp.status_code == 200:
            results = resp.json()

            if isinstance(results, list):
                tracks = results
            else:
                tracks = results.get(
                    "results",
                    results.get("tracks", [])
                )

            if tracks and len(tracks) > 0:
                mediasage_key = tracks[0].get(
                    "rating_key",
                    tracks[0].get("ratingKey", "")
                )

                if mediasage_key:
                    log(
                        f"Found MediaSage rating_key for "
                        f"'{track_title}': {mediasage_key}"
                    )
                    return str(mediasage_key)

            log(
                f"No MediaSage match found for '{search_query}', "
                f"using Plex key: {plex_rating_key}"
            )

    except Exception as e:
        log(f"MediaSage lookup error: {e}")

    return plex_rating_key


def generate_smart_recommendations(context):
    """
    Use MediaSage API to generate recommendations based on the current track.

    Returns a list of track rating keys suitable for queuing.
    """
    # Build prompt for MediaSage
    prompt_parts = []

    if context.get("artist"):
        prompt_parts.append(
            f"based on artist '{context['artist']}'"
        )

    if context.get("title"):
        prompt_parts.append(
            f"and song '{context['title']}'"
        )

    seed_prompt = (
        " ".join(prompt_parts)
        if prompt_parts
        else "random music"
    )

    full_prompt = (
        f"Find 5 songs similar to {seed_prompt} "
        f"that would make good follow-ups."
    )

    # Step 1: Analyze the context through MediaSage
    # (optional, for logging only)
    try:
        analyze_resp = requests.post(
            f"{MEDIASAGE_API}/analyze/prompt",
            json={"prompt": full_prompt},
            timeout=15
        )

        if analyze_resp.status_code == 200:
            log("Analysis successful")
        else:
            log(f"Analyze note: {analyze_resp.status_code}")

    except Exception as e:
        log(f"MediaSage analyze skipped: {e}")

    # Step 2: Generate tracks via MediaSage streaming API
    # MediaSage /generate/stream requires genres and decades arrays.
    # seed_track must be a dict/object, not a string.

    # Lookup the MediaSage-valid rating key for this track
    mediasage_rating_key = lookup_mediasage_rating_key(context)

    seed_track_data = {
        "rating_key": mediasage_rating_key,
        "selected_dimensions": ["genre", "decade", "mood"]
    }

    generated_tracks = []

    # Helper to parse MediaSage SSE streams.
    # Current MediaSage format sends recommendations as:
    # event: tracks
    # data: {"batch": [{"rating_key": "...", ...}, ...]}
    def parse_generation_stream(response):
        tracks = []

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            # Debug only. This can be removed later if desired.
            log(f"DEBUG - STREAM LINE: {line}")

            if not line.startswith("data:"):
                continue

            raw_data = line[5:].strip()

            if not raw_data:
                continue

            try:
                event_data = json.loads(raw_data)

                # Debug only. This confirms the actual response schema.
                log(
                    f"DEBUG - STREAM JSON: "
                    f"{json.dumps(event_data)}"
                )

                # MediaSage recommendation event:
                # {"batch": [{"rating_key": "...", ...}, ...]}
                if (
                    isinstance(event_data, dict)
                    and "batch" in event_data
                    and isinstance(event_data["batch"], list)
                ):
                    for item in event_data["batch"]:
                        if not isinstance(item, dict):
                            continue

                        rating_key = item.get(
                            "ratingKey",
                            item.get("rating_key")
                        )

                        if not rating_key:
                            continue

                        track = {
                            "ratingKey": str(rating_key),
                            "title": item.get("title", "Unknown"),
                            "artist": item.get("artist", "Unknown"),
                            "album": item.get("album", "")
                        }

                        tracks.append(track)

                        log(
                            f"Generated recommendation: "
                            f"{track['artist']} - {track['title']}"
                        )

                # Keep support for a direct track response too.
                elif (
                    isinstance(event_data, dict)
                    and (
                        "ratingKey" in event_data
                        or "rating_key" in event_data
                    )
                ):
                    track = {
                        "ratingKey": str(
                            event_data.get(
                                "ratingKey",
                                event_data.get("rating_key")
                            )
                        ),
                        "title": event_data.get("title", "Unknown"),
                        "artist": event_data.get("artist", "Unknown"),
                        "album": event_data.get("album", "")
                    }

                    tracks.append(track)

                    log(
                        f"Generated recommendation: "
                        f"{track['artist']} - {track['title']}"
                    )

            except json.JSONDecodeError as e:
                log(
                    f"DEBUG - Invalid SSE JSON: {e}; "
                    f"raw={raw_data[:500]}"
                )

        return tracks

    # Step 2a: Try generating with seed_track
    # This provides better recommendations.
    gen_payload = {
        "prompt": full_prompt,
        "seed_track": seed_track_data,
        "track_count": 5,
        "additional_notes": (
            "Pick songs that flow well after the seed track"
        ),
        "genres": [],
        "decades": []
    }

    try:
        gen_resp = requests.post(
            f"{MEDIASAGE_API}/generate/stream",
            json=gen_payload,
            timeout=45,
            stream=True
        )

        # DEBUG: confirm the streaming response itself
        log(
            f"DEBUG - /generate/stream status: "
            f"{gen_resp.status_code}"
        )

        if gen_resp.status_code == 200:
            generated_tracks = parse_generation_stream(gen_resp)

            log(
                "Generation complete with seed track: "
                f"{len(generated_tracks)} tracks"
            )

            # Important:
            # A 200 response can still contain zero parsed tracks.
            # If that happens, retry without seed_track.
            if not generated_tracks:
                log(
                    "Seed track returned empty (200, 0 tracks), "
                    "falling back to prompt-only generation"
                )

                fallback_payload = {
                    "prompt": full_prompt,
                    "track_count": 5,
                    "additional_notes": (
                        "Pick songs that flow well after the current "
                        f"track: {seed_prompt}"
                    ),
                    "genres": [],
                    "decades": []
                }

                gen_resp2 = requests.post(
                    f"{MEDIASAGE_API}/generate/stream",
                    json=fallback_payload,
                    timeout=45,
                    stream=True
                )

                log(
                    f"DEBUG - Fallback /generate/stream status: "
                    f"{gen_resp2.status_code}"
                )

                if gen_resp2.status_code == 200:
                    generated_tracks = parse_generation_stream(gen_resp2)

                    log(
                        "Fallback generation complete: "
                        f"{len(generated_tracks)} tracks"
                    )
                else:
                    log(
                        "Fallback generation failed: "
                        f"{gen_resp2.status_code} - "
                        f"{gen_resp2.text[:200]}"
                    )

        elif gen_resp.status_code == 404:
            # MediaSage doesn't recognize the seed track,
            # retry without it.
            log(
                "Seed track not found "
                f"({gen_resp.text[:100]}), "
                "falling back to prompt-only generation"
            )

            fallback_payload = {
                "prompt": full_prompt,
                "track_count": 5,
                "additional_notes": (
                    "Pick songs that flow well after the current "
                    f"track: {seed_prompt}"
                ),
                "genres": [],
                "decades": []
            }

            gen_resp2 = requests.post(
                f"{MEDIASAGE_API}/generate/stream",
                json=fallback_payload,
                timeout=45,
                stream=True
            )

            log(
                f"DEBUG - Fallback /generate/stream status: "
                f"{gen_resp2.status_code}"
            )

            if gen_resp2.status_code == 200:
                generated_tracks = parse_generation_stream(gen_resp2)

                log(
                    "Fallback generation complete: "
                    f"{len(generated_tracks)} tracks"
                )
            else:
                log(
                    "Fallback generation failed: "
                    f"{gen_resp2.status_code} - "
                    f"{gen_resp2.text[:200]}"
                )

        else:
            log(
                f"Generation failed: {gen_resp.status_code} - "
                f"{gen_resp.text[:200]}"
            )

    except Exception as e:
        log(f"MediaSage generation error: {e}")

    return generated_tracks


def queue_next_tracks(tracks, client_identifier):
    """Insert up to three recommendations as the next songs in Plexamp.

    MediaSage generates five candidates. We randomly select three distinct
    candidates and send them together to MediaSage's Play Next API. The
    MediaSage backend preserves the supplied order when inserting multiple
    tracks immediately after the current song.
    """
    if not tracks:
        log("No tracks to queue")
        return False

    if not client_identifier:
        log("No client ID available")
        return False

    # Remove duplicate rating keys while preserving order.
    unique_tracks = []
    seen_keys = set()

    for track in tracks:
        if not isinstance(track, dict):
            continue

        rating_key = track.get(
            "ratingKey",
            track.get("rating_key", "")
        )

        if not rating_key:
            continue

        rating_key = str(rating_key)
        if rating_key in seen_keys:
            continue

        seen_keys.add(rating_key)
        unique_tracks.append(track)

    if not unique_tracks:
        log("No valid rating keys found for recommendations")
        return False

    # Select exactly three when possible; otherwise use whatever valid
    # recommendations MediaSage returned.
    queue_count = min(3, len(unique_tracks))
    selected_tracks = random.sample(unique_tracks, queue_count)

    log(
        f"Selected {queue_count} random recommendations for Play Next: "
        + " | ".join(
            f"{t.get('artist', 'Unknown')} - {t.get('title', 'Unknown')}"
            for t in selected_tracks
        )
    )

    rating_keys = [
        str(
            track.get(
                "ratingKey",
                track.get("rating_key", "")
            )
        )
        for track in selected_tracks
    ]

    try:
        resp = requests.post(
            f"{MEDIASAGE_API}/play-queue",
            json={
                "rating_keys": rating_keys,
                "client_id": client_identifier,
                "mode": "play_next"
            },
            timeout=10
        )

        if resp.status_code == 200:
            log(
                f"DEBUG - Play Next response: "
                f"{resp.text}"
            )

            try:
                result = resp.json()

                if result.get("success"):
                    client_reachable = result.get("client_reachable", True)
                    log(
                        f"✅ Play Next succeeded: "
                        f"{queue_count} tracks queued | "
                        f"tracks_queued={result.get('tracks_queued')} | "
                        f"client_reachable={client_reachable}"
                    )
                    # Return a tuple so the caller knows whether the client
                    # was reachable (LAN) or not (cellular/remote).
                    return True, client_reachable

                log(
                    f"❌ MediaSage reported Play Next failure: "
                    f"{result}"
                )
                return False, True  # assume reachable on failure (safe default)

            except Exception:
                log(
                    f"⚠️ Play Next returned HTTP 200 but unexpected response: "
                    f"{resp.text}"
                )
                return False, True

        log(
            f"Failed to Play Next: "
            f"{resp.status_code} - {resp.text[:500]}"
        )
        return False, True

    except Exception as e:
        log(f"Play Next error: {e}")
        return False, True


def process_track_change(track_info, player_info=None):
    """Main handler when a new track starts playing or resumes."""
    if not is_enabled():
        log("🔕 Smart Queue disabled - ignoring webhook")
        return

    # Extract client_id early so we can isolate state per-client
    player_info = player_info or {}
    client_id = (
        player_info.get("uuid")
        or player_info.get("machineIdentifier")
        or player_info.get("clientIdentifier")
    )

    if not client_id:
        log(
            f"ERROR - Plex webhook did not provide a client UUID. "
            f"Player: {json.dumps(player_info)}"
        )
        return

    client_name = player_info.get("title", "unknown")
    client_product = player_info.get("product", "unknown")

    log(
        f"🎯 Webhook target client: "
        f"{client_name} ({client_product}) | "
        f"client_id={client_id} | "
        f"local={player_info.get('local')}"
    )

    # 1. Fetch shuffle state BEFORE duplicate check
    state_data = get_shuffle_state(client_id)
    shuffle_state = state_data.get("shuffle")
    current_queue_id = state_data.get("queue_id")

    track_id = track_info.get("ratingKey")

    # 2. Duplicate check and Shuffle Nudge Detection
    with STATE_LOCK:
        # Detect if shuffle was just toggled on
        prev_shuffle = last_shuffle_state.get(client_id)
        shuffle_toggled_on = (shuffle_state is True and prev_shuffle is False)
        last_shuffle_state[client_id] = shuffle_state

        if client_id not in recent_tracks:
            recent_tracks[client_id] = []

        is_duplicate = track_id in recent_tracks[client_id]

        if is_duplicate:
            if shuffle_toggled_on:
                log("🔀 Shuffle toggled ON mid-track! Bypassing duplicate check to nudge AI.")
            else:
                log(f"Duplicate track detected for client {client_name} - skipping")
                return
        else:
            recent_tracks[client_id].append(track_id)
            if len(recent_tracks[client_id]) > 10:
                recent_tracks[client_id].pop(0)
                
        # If the user nudged the AI, force the generation counter to its threshold 
        # so it generates immediately instead of waiting.
        threshold = generation_threshold.get(client_id, 2)
        if shuffle_toggled_on:
            songs_since_generation[client_id] = threshold

    # 3. Abort if shuffle is off
    if shuffle_state is not True:
        if shuffle_state is False:
            log(
                "🔀 Shuffle is OFF - skipping AI generation for this client"
            )
        else:
            log(
                "🔀 Shuffle state unavailable - skipping AI generation for safety"
            )
        return

    # Extract context from webhook data
    context = {
        "title": track_info.get("title", ""),
        "artist": track_info.get("grandparentTitle", ""),
        "album": track_info.get("parentTitle", ""),
        "guid": track_info.get("guid", ""),
        "ratingKey": track_info.get("ratingKey", "")
    }

    log(
        f"🎵 Now playing: "
        f"{context['artist']} - {context['title']}"
    )

    # Generate only after enough songs have started since the previous AI
    # batch. The threshold is 2 on LAN and 1 on cellular (set after each
    # successful queue call). The counter starts at the threshold so the
    # first webhook triggers generation immediately.
    with STATE_LOCK:
        threshold = generation_threshold.get(client_id, 2)

        # Detect if the user manually changed the playlist/album (new queue_id)
        previous_queue_id = last_queue_id.get(client_id)
        if current_queue_id and current_queue_id != previous_queue_id:
            log(f"🔄 Queue changed (from {previous_queue_id} to {current_queue_id}) - resetting generation counter")
            songs_since_generation[client_id] = threshold
            last_queue_id[client_id] = current_queue_id

        count = songs_since_generation.get(client_id, threshold)

        if count < threshold:
            count += 1
            songs_since_generation[client_id] = count
            log(
                f"⏳ Waiting for next AI generation: "
                f"{count}/{threshold} songs since last generation"
            )
            return

        # Reserve/reset the counter before the potentially slow AI call.
        # If generation succeeds, it remains at zero. If generation fails,
        # allow the next webhook to retry rather than waiting for the full
        # threshold again.
        songs_since_generation[client_id] = 0

    log(
        "🤖 Threshold reached (or new context detected) - generating next batch of "
        "recommendations"
    )

    # MediaSage generates five candidates; queue three random candidates
    # as Play Next.
    tracks = generate_smart_recommendations(context)

    if tracks:
        queued, client_reachable = queue_next_tracks(tracks, client_id)
        if queued:
            # On LAN (client_reachable=True): refreshPlayQueue is sent and
            # Plexamp adopts the new tracks immediately — normal 2-song window.
            #
            # On cellular (client_reachable=False): AI tracks are inserted after
            # the already-buffered next item, not after current. This creates a
            # one-track gap at the very start (the buffered non-AI track plays
            # first). To close that gap, we fire the next generation after just
            # 1 song instead of 2 — so batch 2 lands right after the buffered
            # track finishes and AI2 starts playing.
            #
            # After that first catch-up, the "next" item is always an AI track
            # from the previous batch (already buffered but still AI), so no
            # further gaps appear. We revert to threshold=2 to avoid pushing
            # AI tracks down on subsequent generations.
            if not client_reachable:
                current_threshold = generation_threshold.get(client_id, 2)
                next_threshold = 1 if current_threshold == 2 else 2
            else:
                next_threshold = 2
            with STATE_LOCK:
                generation_threshold[client_id] = next_threshold
            log(
                f"✅ Queued 3 recommendations as Play Next; "
                f"next AI generation will occur after {next_threshold} track "
                f"change(s) (client_reachable={client_reachable})"
            )
        else:
            # Do not lose the generation opportunity if queueing failed.
            # Reset counter to current threshold so next webhook retries.
            with STATE_LOCK:
                threshold = generation_threshold.get(client_id, 2)
                songs_since_generation[client_id] = threshold
            log(
                "❌ Failed to queue recommendations; "
                "next webhook will retry AI generation"
            )
    else:
        # Do not consume the threshold window if AI generation failed.
        # Reset counter to current threshold so next webhook retries.
        with STATE_LOCK:
            threshold = generation_threshold.get(client_id, 2)
            songs_since_generation[client_id] = threshold
        log(
            "No recommendations generated; "
            "next webhook will retry AI generation"
        )


# --- HTTP Server ---

class QueueHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(
            self.headers.get("Content-Length", 0)
        )
        body = self.rfile.read(content_length)

        log(f"📥 Webhook received: {self.path}")
        log(
            f"DEBUG - Raw body length: {len(body)}, "
            f"Content-Type: "
            f"{self.headers.get('Content-Type', 'unknown')}"
        )

        payload = parse_plex_webhook(body, self.headers)

        if payload:

            # TEMP DEBUG: capture the complete Plex webhook payload
            # log(f"DEBUG - FULL WEBHOOK: {json.dumps(payload)}")

            event_type = payload.get("event", "")
            metadata = payload.get("Metadata", {})

            # DEBUG: Log raw payload for troubleshooting
            log(
                f"DEBUG - Event: {event_type}, "
                f"Metadata keys: {list(metadata.keys())}"
            )

            if metadata:
                log(
                    f"DEBUG - Metadata: "
                    f"{json.dumps(metadata)[:500]}"
                )

            # Handle relevant playback events
            if event_type in ["media.play", "media.resume"]:
                if metadata.get("Metadata"):
                    track_meta = metadata["Metadata"]
                else:
                    track_meta = metadata

                # MediaSage Smart Queue is for music tracks only.
                # Ignore movies, TV episodes, and other non-track playback.
                media_type = str(track_meta.get("type", "")).lower()
                if media_type != "track":
                    log(
                        f"🎬 Non-music playback detected "
                        f"(type={media_type or 'unknown'}, "
                        f"title={track_meta.get('title', 'unknown')}) - ignoring"
                    )
                    return

                # Keep the existing client/player information unchanged.
                # Offload to a background thread so the webhook returns 200 OK instantly.
                threading.Thread(
                    target=process_track_change,
                    args=(track_meta, payload.get("Player", {})),
                    daemon=True
                ).start()
                log(f"⚡ Spun up background thread for track processing. Server is free.")

            elif event_type == "media.stop":
                log("Playback stopped")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        """Health check / status endpoint."""
        status = "ENABLED" if is_enabled() else "DISABLED"
        log_tail = ""

        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
                log_tail = "".join(lines[-50:])
        except Exception:
            log_tail = "No recent logs available."

        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Smart Plex Queue - Status</title></head>
        <body style="font-family: sans-serif; padding: 40px;">
            <h1>Smart Plex Queue</h1>
            <p>
                <strong>Status:</strong>
                <span style="color: {'green' if is_enabled() else 'gray'}">
                    {status}
                </span>
            </p>
            <p><strong>Latest Log Output:</strong></p>
            <pre style="background: #f4f4f4; padding: 10px;
                        border-radius: 4px; max-height: 300px;
                        overflow-y: scroll;">{log_tail}</pre>
            <hr>
            <p><small>Endpoints:</small></p>
            <ul>
                <li><code>/webhook</code> - Plex webhook receiver</li>
                <li><code>/status</code> - This status page</li>
            </ul>
        </body>
        </html>
        """

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        """Override to suppress default logging."""
        pass


def run_server(port=8000):
    server = HTTPServer(("0.0.0.0", port), QueueHandler)

    log(
        f"🚀 Smart Plex Queue Listener started on port {port}"
    )

    log(
        f"{'🟢' if is_enabled() else '⚪'} "
        f"Feature is {'ENABLED' if is_enabled() else 'DISABLED'}"
    )

    server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    run_server(port)
