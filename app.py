from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import requests
import threading
import time
import math
import uuid
import json
import re

app = Flask(__name__)

jobs = {}
UPLOAD_FOLDER = '/tmp/video_jobs'
AUDIO_SEGMENTS_FOLDER = '/tmp/audio_segments'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_SEGMENTS_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
# Layout constants (9:16 = 720x1280)
# ─────────────────────────────────────────────
EQ_CENTER_Y = 0.92
DARK_START  = 0.78
LYRICS_Y    = 0.84


# ══════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════

def download_file(url, dest_path):
    headers = {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    r = requests.get(f"{url}?nocache={int(time.time())}", timeout=120, stream=True, headers=headers)
    if r.status_code != 200:
        r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path


def get_audio_duration(audio_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def get_best_font():
    for path in [
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]:
        if os.path.exists(path):
            return path
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def get_italic_font():
    for path in [
        '/usr/share/fonts/truetype/freefont/FreeSerifBoldItalic.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf',
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-BI.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf',
    ]:
        if os.path.exists(path):
            return path
    return get_best_font()


def get_lyrics_font():
    for path in [
        '/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',
    ]:
        if os.path.exists(path):
            return path
    return get_best_font()


# ══════════════════════════════════════════════
# BEST PART DETECTION
# Analyzes audio volume to find the most
# energetic/loud segment of the song
# ══════════════════════════════════════════════

def find_best_segment(audio_path, segment_duration=58):
    """Find the start time of the most energetic segment using ffmpeg volumedetect."""
    total_duration = get_audio_duration(audio_path)
    if total_duration <= segment_duration:
        return 0.0

    # Sample volume every 2 seconds across the whole track
    step       = 2.0
    window     = segment_duration
    best_start = 0.0
    best_score = -1.0

    # Use ffmpeg to get RMS volume for each 2s chunk
    num_chunks = int(total_duration / step)
    volumes    = []

    for i in range(num_chunks):
        t = i * step
        result = subprocess.run([
            'ffmpeg', '-y', '-ss', str(t), '-t', str(step),
            '-i', audio_path,
            '-af', 'volumedetect',
            '-f', 'null', '/dev/null'
        ], capture_output=True, text=True, timeout=10)

        # Parse mean_volume from stderr
        match = re.search(r'mean_volume:\s*([-\d.]+)\s*dB', result.stderr)
        vol   = float(match.group(1)) if match else -60.0
        volumes.append(vol)

    if not volumes:
        return 0.0

    # Slide a window of (segment_duration / step) chunks and find max average volume
    window_chunks = int(window / step)
    best_start    = 0.0
    best_score    = -999.0

    for i in range(len(volumes) - window_chunks + 1):
        score = sum(volumes[i:i + window_chunks]) / window_chunks
        if score > best_score:
            best_score = score
            best_start = i * step

    print(f"[BestSegment] start={best_start:.1f}s score={best_score:.1f}dB total={total_duration:.1f}s")
    return best_start


# ══════════════════════════════════════════════
# FFMPEG ESCAPE
# ══════════════════════════════════════════════

def ffmpeg_escape(text):
    text = text.replace('\\', '\\\\')
    text = text.replace("'", "\u2019")
    text = text.replace(':', '\\:')
    text = text.replace('%', '\\%')
    text = text.replace('[', '\\[')
    text = text.replace(']', '\\]')
    text = text.replace(',', '\\,')
    return text


# ══════════════════════════════════════════════
# ARTIST WATERMARK
# ══════════════════════════════════════════════

def build_artist_watermark(font_italic, artist_name="SORLUNE"):
    name       = ffmpeg_escape(artist_name.upper())
    padding    = 28
    alpha_expr = "0.875+0.125*sin(6.2832/4.0*t)"
    watermark  = (
        f"drawtext=fontfile={font_italic}:text='{name}':"
        f"fontsize=34:fontcolor=0xD4AF37@1.0:"
        f"borderw=2:bordercolor=black@0.80:"
        f"shadowcolor=black@0.70:shadowx=2:shadowy=2:"
        f"x=w-text_w-{padding}:y={padding}:alpha='{alpha_expr}'"
    )
    underline = (
        f"drawtext=fontfile={font_italic}:text='\u2014\u2014\u2014\u2014\u2014\u2014\u2014':"
        f"fontsize=14:fontcolor=0xD4AF37@1.0:"
        f"x=w-text_w-{padding}:y={padding+42}:alpha='{alpha_expr}'"
    )
    return ",".join([watermark, underline])


# ══════════════════════════════════════════════
# EQ BAR
# ══════════════════════════════════════════════

def build_eq_bar(font):
    parts     = []
    bar_count = 24
    bar_gap   = 12
    half      = bar_count // 2
    center_y  = f"h*{EQ_CENTER_Y}"

    freqs  = [1.3,2.1,2.7,1.9,3.1,2.4,1.7,2.9,2.2,3.5,2.0,2.8,
              2.8,2.0,3.5,2.2,2.9,1.7,2.4,3.1,1.9,2.7,2.1,1.3]
    phases = [0.0,0.5,1.1,1.7,0.3,0.9,1.5,0.2,0.8,1.4,0.6,1.2,
              1.2,0.6,1.4,0.8,0.2,1.5,0.9,0.3,1.7,1.1,0.5,0.0]

    for i in range(bar_count):
        dist      = abs(i - half) / half
        amplitude = int(4 + 28 * math.exp(-2.5 * dist * dist))
        alpha_up  = 0.88 - 0.22 * dist
        alpha_dwn = 0.38 - 0.12 * dist
        offset    = (i - half) * bar_gap
        bar_x     = f"(w/2+({offset})-tw/2)"
        fs_expr   = f"3+{amplitude}*abs(sin(t*{freqs[i]}+{phases[i]}))"

        parts.append(
            f"drawtext=fontfile={font}:text='|':fontsize={fs_expr}:"
            f"fontcolor=0xD4AF37@{alpha_up:.2f}:x={bar_x}:y=({center_y})-text_h"
        )
        parts.append(
            f"drawtext=fontfile={font}:text='|':fontsize={fs_expr}:"
            f"fontcolor=0xB8860B@{alpha_dwn:.2f}:x={bar_x}:y={center_y}"
        )
    return ",".join(parts)


# ══════════════════════════════════════════════
# SUBSCRIBE ANIMATION
# Centered at 80% height — high conversion
# ══════════════════════════════════════════════

def build_subscribe_animation(font):
    alpha     = "if(lt(t,2),0,if(lt(t,3),(t-2),0.85+0.15*abs(sin(3.14159*t))))"
    arr_alpha = "if(lt(t,3),0,0.7+0.3*abs(sin(2.8*t)))"
    # ✅ ffmpeg uses trunc() not int()
    arr_y     = "trunc(h*0.22)+36+trunc(6*abs(sin(2.8*t)))"

    box = (
        "drawbox=x=200:y=trunc(h*0.22)-34:w=320:h=62:"
        "color=0xFF1111@1.0:t=fill:"
        "enable='gte(t,2)'"
    )
    white_border = (
        "drawbox=x=200:y=trunc(h*0.22)-34:w=320:h=62:"
        "color=white@1.0:t=3:"
        "enable='gte(t,2)'"
    )
    sub = (
        f"drawtext=fontfile={font}:text='SUBSCRIBE':"
        f"fontsize=36:fontcolor=white@1.0:"
        f"shadowcolor=0x990000@0.8:shadowx=1:shadowy=1:"
        f"x=(w-text_w)/2:y=trunc(h*0.22)-16:"
        f"alpha='{alpha}'"
    )
    arrow = (
        f"drawtext=fontfile={font}:text='\u25BC  \u25BC':"
        f"fontsize=20:fontcolor=white@1.0:"
        f"x=(w-text_w)/2:y={arr_y}:"
        f"alpha='{arr_alpha}'"
    )
    return ",".join([box, white_border, sub, arrow])


# ══════════════════════════════════════════════
# LYRICS / KARAOKE
# ══════════════════════════════════════════════

_SECTION_WORDS = r'verse|chorus|bridge|hook|outro|intro|pre[\-\s]?chorus|post[\-\s]?chorus|refrain|interlude|instrumental|spoken|rap|breakdown|solo|ad[\-\s]?lib|vamp|coda|tag|skit|fade'
SECTION_REGEX  = [re.compile(p, re.IGNORECASE) for p in [
    r'^\[.*\]$', r'^\(.*\)$',
    rf'^({_SECTION_WORDS})\s*[\d:.\-]*\s*$',
    rf'^({_SECTION_WORDS})\s*\d*\s*:$',
    r'^[\d\s\.\)\(\:\-]+$'
]]

def is_section_label(line):
    s = line.strip()
    return any(p.match(s) or p.match(s.rstrip(':').strip()) for p in SECTION_REGEX)

def split_lyrics_lines(text):
    if not text: return []
    return [l.strip() for l in text.replace('\r\n','\n').replace('\r','\n').split('\n')
            if l.strip() and not is_section_label(l.strip())]

def normalize_word(w):
    return re.sub(r"[^\w']", "", (w or "").lower()).strip()

def transcribe_audio_words_with_whisper(audio_path, openai_api_key):
    if not openai_api_key or not os.path.exists(audio_path): return []
    try:
        with open(audio_path, "rb") as audio_file:
            response = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_api_key}"},
                files={"file": audio_file},
                data={"model": "whisper-1", "response_format": "verbose_json",
                      "timestamp_granularities[]": "word"},
                timeout=300
            )
        if response.status_code != 200: return []
        data    = response.json()
        cleaned = []
        for w in data.get("words", []):
            word_text = (w.get("word") or "").strip()
            start     = w.get("start")
            end       = w.get("end")
            if not word_text or start is None or end is None: continue
            start, end = float(start), float(end)
            if end <= start: continue
            cleaned.append({"word": word_text, "norm": normalize_word(word_text), "start": start, "end": end})
        if cleaned: return cleaned
        seg_words = []
        for seg in data.get("segments", []):
            text  = (seg.get("text") or "").strip()
            start = seg.get("start")
            end   = seg.get("end")
            if not text or start is None or end is None: continue
            seg_words.append({"word": text, "norm": normalize_word(text), "start": float(start), "end": float(end)})
        return seg_words
    except Exception as e:
        print(f"[Whisper] Error: {e}"); return []

def build_lines_from_words(words, max_gap=0.45, max_words=6, max_duration=3.0):
    if not words: return []
    lines   = []
    current = [words[0]]

    def flush(lw):
        if not lw: return None
        text = " ".join(w["word"] for w in lw).strip()
        return {"start": round(lw[0]["start"], 2), "end": round(lw[-1]["end"], 2), "text": text} if text else None

    for w in words[1:]:
        prev = current[-1]
        if (w["start"] - prev["end"] > max_gap or
                len(current) >= max_words or
                w["end"] - current[0]["start"] > max_duration):
            item = flush(current)
            if item: lines.append(item)
            current = [w]
        else:
            current.append(w)
    item = flush(current)
    if item: lines.append(item)

    cleaned = []
    for seg in lines:
        start = float(seg["start"]); end = float(seg["end"]); text = seg["text"].strip()
        if not text: continue
        min_dur = max(0.60, min(1.40, len(text.split()) * 0.22))
        if end - start < min_dur: end = start + min_dur
        if cleaned and start < cleaned[-1]["end"]:
            start = round(cleaned[-1]["end"] + 0.03, 2)
            end   = max(end, start + min_dur)
        cleaned.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return cleaned

def transcribe_lyrics_with_whisper(audio_path, openai_api_key, lyrics_text=""):
    return build_lines_from_words(transcribe_audio_words_with_whisper(audio_path, openai_api_key))

def wrap_lyric_line(text, max_chars=32):
    if len(text) <= max_chars: return [text]
    words      = text.split()
    best_split = len(words) // 2
    best_diff  = float('inf')
    for i in range(1, len(words)):
        p1, p2 = " ".join(words[:i]), " ".join(words[i:])
        diff   = abs(len(p1) - len(p2))
        if diff < best_diff and len(p1) <= max_chars and len(p2) <= max_chars:
            best_diff, best_split = diff, i
    return [" ".join(words[:best_split]), " ".join(words[best_split:])]

def build_karaoke_filter(segments, font, lyrics_font=None):
    if lyrics_font is None: lyrics_font = font
    if not segments: return ""
    parts       = []
    FONT_SIZE   = 36
    LINE_HEIGHT = 44
    MAX_CHARS   = 32

    for seg in segments:
        start, end, raw_text = seg["start"], seg["end"], seg["text"]
        dur       = max(end - start, 0.5)
        fade_dur  = min(0.18, dur / 5)
        alpha_expr = (
            f"if(between(t,{start},{start+fade_dur}),(t-{start})/{fade_dur},"
            f"if(between(t,{start+fade_dur},{end-fade_dur}),1,"
            f"if(between(t,{end-fade_dur},{end}),({end}-t)/{fade_dur},0)))"
        )
        lines = wrap_lyric_line(raw_text, max_chars=MAX_CHARS)
        if len(lines) == 1:
            parts.append(
                f"drawtext=fontfile={lyrics_font}:text='{ffmpeg_escape(lines[0])}':"
                f"fontsize={FONT_SIZE}:fontcolor=white@1.0:"
                f"borderw=3:bordercolor=black@1.0:"
                f"shadowcolor=black@0.95:shadowx=2:shadowy=2:"
                f"x=(w-text_w)/2:y=h*{LYRICS_Y}:alpha='{alpha_expr}'"
            )
        else:
            base_y = LYRICS_Y - 0.04
            for li, line in enumerate(lines):
                parts.append(
                    f"drawtext=fontfile={lyrics_font}:text='{ffmpeg_escape(line)}':"
                    f"fontsize={FONT_SIZE}:fontcolor=white@1.0:"
                    f"borderw=3:bordercolor=black@1.0:"
                    f"shadowcolor=black@0.95:shadowx=2:shadowy=2:"
                    f"x=(w-text_w)/2:y=h*{base_y}+{li*LINE_HEIGHT}:alpha='{alpha_expr}'"
                )
    return ",".join(parts)


# ══════════════════════════════════════════════
# FFMPEG COMMAND — short video 720x1280
# ══════════════════════════════════════════════

def build_ffmpeg_command_short(video_path, audio_path, output_path, audio_duration,
                                font, font_italic, lyrics_font=None,
                                lyrics_segments=None, artist_name="SORLUNE"):
    fade_out_st = max(audio_duration - 3, audio_duration * 0.85)

    # 720x1280 (9:16) — memory efficient
    scale_crop = (
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280"
    )

    grade_filter = (
        "eq=brightness=0.02:contrast=1.03:saturation=1.05,"
        "curves=r='0/0 0.5/0.53 1/1':g='0/0 0.5/0.48 1/0.95':b='0/0 0.5/0.43 1/0.86'"
    )

    dark_overlay = (
        f"drawtext=fontfile={font}:text=' ':fontsize=1:fontcolor=black@0:"
        f"box=1:boxcolor=black@0.60:boxborderw=0:"
        f"x=0:y=h*{DARK_START}:fix_bounds=1"
    )

    fade_filter   = f"fade=t=in:st=0:d=2,fade=t=out:st={fade_out_st:.2f}:d=3"
    artist_filter = build_artist_watermark(font_italic, artist_name)
    eq_filter     = build_eq_bar(font)
    subscribe_filter = build_subscribe_animation(font)

    vf_parts = [
        scale_crop,
        grade_filter,
        "format=yuv420p",
        dark_overlay,
        artist_filter,
    ]

    # Add karaoke if lyrics available
    if lyrics_segments:
        karaoke = build_karaoke_filter(lyrics_segments, font, lyrics_font=lyrics_font)
        if karaoke:
            vf_parts.append(karaoke)

    vf_parts.append(subscribe_filter)  # ✅ subscribe animation at 80%
    vf_parts.append(eq_filter)
    vf_parts.append(fade_filter)

    vf_chain = ",".join(vf_parts)

    return [
        'ffmpeg', '-y',
        '-stream_loop', '-1', '-i', video_path,
        '-i', audio_path,
        '-vf', vf_chain,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26',
        '-threads', '1',
        '-c:a', 'aac', '-b:a', '128k',
        '-pix_fmt', 'yuv420p',
        '-t', str(audio_duration),
        '-shortest',
        output_path
    ]


# ══════════════════════════════════════════════
# JOB RUNNER
# ══════════════════════════════════════════════

def generate_short_job(job_id, video_path, audio_path, output_path,
                       lyrics_segments=None, artist_name="SORLUNE"):
    try:
        jobs[job_id]['status'] = 'processing'
        audio_duration = get_audio_duration(audio_path)
        font           = get_best_font()
        font_italic    = get_italic_font()
        lyrics_font    = get_lyrics_font()

        cmd = build_ffmpeg_command_short(
            video_path, audio_path, output_path,
            audio_duration, font, font_italic,
            lyrics_font=lyrics_font,
            lyrics_segments=lyrics_segments,
            artist_name=artist_name
        )

        print(f"[FFmpeg] Starting short job: {job_id}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if proc.returncode == 0 and os.path.exists(output_path):
            jobs[job_id]['status']    = 'completed'
            jobs[job_id]['video_url'] = f"/videos/{job_id}/{job_id}.mp4"
            print(f"[FFmpeg] Job {job_id} completed")
        else:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error']  = proc.stderr[-3000:]
            print(f"[FFmpeg ERROR]\n{proc.stderr[-3000:]}")

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error']  = str(e)
        print(f"[Job ERROR] {job_id}: {e}")


# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.route('/generate', methods=['POST'])
def generate_video():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400

    audio_url   = data.get('audio_url')
    video_url   = data.get('video_url')
    api_key     = data.get('api_key', 'default')
    lyrics_text = data.get('lyrics', '').strip()
    openai_key  = data.get('openai_key', '').strip()
    artist_name = data.get('artist', 'SORLUNE').strip()

    if not audio_url or not video_url:
        return jsonify({'error': 'Missing audio_url or video_url'}), 400

    job_id     = api_key
    job_folder = os.path.join(UPLOAD_FOLDER, job_id)
    os.makedirs(job_folder, exist_ok=True)

    video_path  = os.path.join(job_folder, 'videoinput.mp4')
    audio_path  = os.path.join(job_folder, 'audio.mp3')
    output_path = os.path.join(job_folder, f'{job_id}.mp4')

    jobs[job_id] = {'status': 'pending', 'video_url': None}

    def run():
        try:
            for f in [video_path, audio_path, output_path]:
                if os.path.exists(f): os.remove(f)

            jobs[job_id]['status'] = 'downloading_assets'
            download_file(video_url, video_path)
            download_file(audio_url, audio_path)

            # Transcribe lyrics with Whisper if key provided
            lyrics_segments = []
            if openai_key:
                try:
                    jobs[job_id]['status'] = 'transcribing_lyrics'
                    lyrics_segments = transcribe_lyrics_with_whisper(audio_path, openai_key, lyrics_text)
                    print(f"[Lyrics] Got {len(lyrics_segments)} segments from Whisper")
                except Exception as e:
                    print(f"[Lyrics] Whisper failed: {e}")
                    lyrics_segments = []

            # Fallback: use raw lyrics text with time estimation
            if not lyrics_segments and lyrics_text:
                duration = get_audio_duration(audio_path)
                lines    = split_lyrics_lines(lyrics_text)
                if lines:
                    step    = max(duration / len(lines), 1.8)
                    current = 0.0
                    for line in lines:
                        lyrics_segments.append({
                            "start": round(current, 2),
                            "end":   round(min(current + step, duration), 2),
                            "text":  line
                        })
                        current += step

            generate_short_job(
                job_id, video_path, audio_path, output_path,
                lyrics_segments=lyrics_segments,
                artist_name=artist_name
            )

        except Exception as e:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error']  = str(e)
            print(f"[Run ERROR] {job_id}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'status': 'started', 'job_id': job_id}), 200


@app.route('/status/<api_key>', methods=['GET'])
def check_status(api_key):
    job = jobs.get(api_key)
    if not job:
        return jsonify({'status': 'not_found'}), 200
    response = {'status': job['status']}
    if job['status'] == 'completed':
        response['video_url'] = request.host_url.rstrip('/') + f'/videos/{api_key}/{api_key}.mp4'
    if job.get('error'):
        response['error'] = job['error']
    return jsonify(response), 200


@app.route('/videos/<job_id>/<filename>', methods=['GET'])
def serve_video(job_id, filename):
    return send_from_directory(os.path.join(UPLOAD_FOLDER, job_id), filename)


@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    data    = request.get_json()
    api_key = data.get('api_key') if data else None
    if api_key:
        jobs.pop(api_key, None)
        import shutil
        job_folder = os.path.join(UPLOAD_FOLDER, api_key)
        if os.path.exists(job_folder):
            shutil.rmtree(job_folder, ignore_errors=True)
    return jsonify({'status': 'cleared'}), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Short video server running'}), 200


# ══════════════════════════════════════════════
# PROCESS AUDIO — find best energetic segment
# ══════════════════════════════════════════════

@app.route('/process-audio', methods=['POST'])
def process_audio():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400

    audio_url        = data.get('url')
    segment_duration = int(data.get('segment_duration', 58))

    if not audio_url:
        return jsonify({'error': 'Missing url'}), 400

    session_id = str(uuid.uuid4())[:8]
    audio_path = os.path.join(AUDIO_SEGMENTS_FOLDER, f'{session_id}_input.mp3')

    try:
        download_file(audio_url, audio_path)
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500

    try:
        total_duration = get_audio_duration(audio_path)
    except:
        return jsonify({'error': 'Could not read audio duration'}), 500

    # Find the best (most energetic) part
    best_start = find_best_segment(audio_path, segment_duration)

    # Cut only that one best segment
    seg_fn   = f'{session_id}_seg000.mp3'
    seg_path = os.path.join(AUDIO_SEGMENTS_FOLDER, seg_fn)

    proc = subprocess.run([
        'ffmpeg', '-y', '-i', audio_path,
        '-ss', str(best_start),
        '-t', str(segment_duration),
        '-c:a', 'libmp3lame', '-b:a', '128k',
        seg_path
    ], capture_output=True, timeout=120)

    os.remove(audio_path)

    if proc.returncode != 0 or not os.path.exists(seg_path):
        return jsonify({'error': 'Segment extraction failed'}), 500

    print(f"[ProcessAudio] Best segment: {best_start:.1f}s → {best_start+segment_duration:.1f}s")
    return jsonify({'segments': [seg_fn]}), 200


@app.route('/audio_segments/<filename>', methods=['GET'])
def serve_audio_segment(filename):
    return send_from_directory(AUDIO_SEGMENTS_FOLDER, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
