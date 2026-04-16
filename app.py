from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import requests
import threading
import time
import math

app = Flask(__name__)

jobs = {}
UPLOAD_FOLDER = '/tmp/video_jobs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────────
# Layout constants  (9:16 vertical = 1080x1920)
# ─────────────────────────────────────────────
EQ_CENTER_Y = 0.92   # EQ bar vertical position
DARK_START  = 0.82   # dark gradient band starts here


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


def get_video_duration(video_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
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


# ══════════════════════════════════════════════
# ARTIST WATERMARK  (top-right, gold italic)
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


def build_artist_watermark(font_italic, artist_name="SORLUNE"):
    name    = ffmpeg_escape(artist_name.upper())
    padding = 28
    alpha_expr = "0.875+0.125*sin(6.2832/4.0*t)"

    watermark = (
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
# EQ BAR  (gold animated bars at bottom)
# ══════════════════════════════════════════════

def build_eq_bar(font):
    parts      = []
    bar_count  = 30
    bar_gap    = 14
    half       = bar_count // 2
    center_y   = f"h*{EQ_CENTER_Y}"

    freqs  = [1.3,2.1,2.7,1.9,3.1,2.4,1.7,2.9,2.2,3.5,2.0,2.8,2.1,2.8,2.0,
              3.5,2.2,2.9,1.7,2.4,3.1,1.9,2.7,2.1,1.3,1.8,2.5,3.0,1.6,2.3]
    phases = [0.0,0.5,1.1,1.7,0.3,0.9,1.5,0.2,0.8,1.4,0.6,1.2,0.0,1.2,0.6,
              1.4,0.8,0.2,1.5,0.9,0.3,1.7,1.1,0.5,0.0,0.7,1.3,0.4,1.0,1.6]

    for i in range(bar_count):
        dist      = abs(i - half) / half
        amplitude = int(5 + 36 * math.exp(-2.5 * dist * dist))
        alpha_up  = 0.90 - 0.25 * dist
        alpha_dwn = 0.40 - 0.15 * dist
        offset    = (i - half) * bar_gap
        bar_x     = f"(w/2+({offset})-tw/2)"
        fs_expr   = f"4+{amplitude}*abs(sin(t*{freqs[i]}+{phases[i]}))"

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
# FFMPEG COMMAND  — short video (9:16)
#
# Pipeline:
#  1. Scale + crop input video to 1080x1920
#  2. Loop video if audio is longer
#  3. Light color grade (same as long version)
#  4. Dark overlay band at bottom (covers EQ area)
#  5. Artist watermark (top-right)
#  6. EQ bar animation
#  7. Fade in/out
# ══════════════════════════════════════════════

def build_ffmpeg_command_short(video_path, audio_path, output_path, audio_duration, fps, font, font_italic, artist_name="SORLUNE"):
    fade_out_st = max(audio_duration - 3, audio_duration * 0.85)

    # Scale and crop to 9:16 (1080x1920) — center crop
    scale_crop = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920"
    )

    # Subtle color grade — same feel as long video
    grade_filter = (
        "eq=brightness='0.02*sin(t*2.2+0.3)':contrast='1.03+0.02*sin(t*1.8+1.0)':saturation='1.05+0.06*sin(t*2.5+0.8)',"
        "curves=r='0/0 0.5/0.53 1/1':g='0/0 0.5/0.48 1/0.95':b='0/0 0.5/0.43 1/0.86',"
        "vignette=PI/4.5,"
        "noise=alls=2:allf=t"
    )

    # Dark band at bottom to make EQ bar readable
    dark_overlay = (
        f"drawtext=fontfile={font}:text=' ':fontsize=1:fontcolor=black@0:"
        f"box=1:boxcolor=black@0.55:boxborderw=0:"
        f"x=0:y=h*{DARK_START}:fix_bounds=1"
    )

    fade_filter    = f"fade=t=in:st=0:d=2,fade=t=out:st={fade_out_st:.2f}:d=3"
    artist_filter  = build_artist_watermark(font_italic, artist_name)
    eq_filter      = build_eq_bar(font)

    vf_parts = [
        scale_crop,
        grade_filter,
        "format=yuv420p",
        dark_overlay,
        artist_filter,
        eq_filter,
        fade_filter,
    ]
    vf_chain = ",".join(vf_parts)

    # -stream_loop -1  → loops the video indefinitely so audio always wins
    return [
        'ffmpeg', '-y',
        '-stream_loop', '-1', '-i', video_path,   # looping video input
        '-i', audio_path,                          # audio input
        '-vf', vf_chain,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '22',
        '-c:a', 'aac', '-b:a', '192k',
        '-pix_fmt', 'yuv420p',
        '-t', str(audio_duration),
        '-shortest',
        output_path
    ]


# ══════════════════════════════════════════════
# JOB RUNNER
# ══════════════════════════════════════════════

def generate_short_job(job_id, video_path, audio_path, output_path, artist_name="SORLUNE"):
    try:
        jobs[job_id]['status'] = 'processing'
        audio_duration = get_audio_duration(audio_path)
        font           = get_best_font()
        font_italic    = get_italic_font()

        cmd = build_ffmpeg_command_short(
            video_path, audio_path, output_path,
            audio_duration, 30, font, font_italic,
            artist_name=artist_name
        )

        print(f"[FFmpeg] Starting short video job: {job_id}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if proc.returncode == 0 and os.path.exists(output_path):
            jobs[job_id]['status']    = 'completed'
            jobs[job_id]['video_url'] = f"/videos/{job_id}/{job_id}.mp4"
            print(f"[FFmpeg] Job {job_id} completed successfully")
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
    video_url   = data.get('video_url')          # videoinput.mp4 from WordPress
    api_key     = data.get('api_key', 'default')
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
                if os.path.exists(f):
                    os.remove(f)

            jobs[job_id]['status'] = 'downloading_assets'
            download_file(video_url, video_path)
            download_file(audio_url, audio_path)

            generate_short_job(job_id, video_path, audio_path, output_path, artist_name=artist_name)

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
