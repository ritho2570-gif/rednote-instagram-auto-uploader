#!/usr/bin/env python3
import os
import sys
import time
import logging
import argparse
import re
import subprocess
import random
import shutil
import json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

# Load .env (python-dotenv is in requirements.txt but the original never loaded it)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

from downloader.download import download_video_from_url
from uploader.upload import upload_reel

LOG_FILE = os.path.join(BASE_DIR, "video_processor.log")
FAILED_FILE = os.path.join(BASE_DIR, "failed_urls.txt")
UPLOAD_LOG_FILE = os.path.join(BASE_DIR, "uploads.log")
FAILURE_COUNTS_FILE = os.path.join(BASE_DIR, "failure_counts.json")
MAX_FAILURES = 3
IG_MAX_CAPTION = 2200
IG_MAX_HASHTAGS = 30

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


def load_failure_counts():
    """url -> failed attempts, persisted so one-shot runs also count toward MAX_FAILURES."""
    try:
        with open(FAILURE_COUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_failure_counts(counts):
    try:
        with open(FAILURE_COUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(counts, f, indent=2)
    except OSError as e:
        logging.error(f"Could not save failure counts: {e}")


def env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def find_ffmpeg():
    """Use system ffmpeg if present, else the bundled imageio-ffmpeg binary."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def sanitize_filename(filename):
    name, ext = os.path.splitext(filename)
    sanitized = re.sub(r"[#@]", "", name)
    sanitized = re.sub(r"[^\w\s\-\.]", "", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if not sanitized:
        sanitized = "video"
    return f"{sanitized}{ext}"


def convert_video_format(input_path):
    """Convert to Instagram-friendly H.264/AAC 1080x1920 30fps."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        logging.error("ffmpeg not found. Install it or run: pip install imageio-ffmpeg")
        return None

    # Original used str.replace('.mp4', ...), which broke for any other extension
    output_path = os.path.splitext(input_path)[0] + "_converted.mp4"

    cmd = [
        ffmpeg, "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        # cap bitrate instead of mixing -crf with -b:v (they conflict)
        "-maxrate", "4M",
        "-bufsize", "8M",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        output_path,
    ]

    logging.info(f"Converting video: {input_path} -> {output_path}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except Exception as e:
        logging.error(f"Error running ffmpeg: {e}")
        return None

    if result.returncode != 0:
        logging.error(f"FFmpeg conversion failed: {result.stderr[-1000:]}")
        return None

    logging.info("Video conversion successful")
    return output_path


def get_random_hashtags(num_tags=2):
    try:
        with open(os.path.join(BASE_DIR, "tags.txt"), "r", encoding="utf-8") as f:
            hashtags = f.read().split()
        if len(hashtags) >= num_tags:
            return " ".join(random.sample(hashtags, num_tags))
        if hashtags:
            return " ".join(hashtags)
    except Exception as e:
        logging.error(f"Error reading tags.txt: {e}")
    return "#reels #trending"


def build_caption(video_path):
    """Build the caption from the downloaded filename (the downloader stores it there)."""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    caption = stem.replace("_", " ").strip()

    # Original compared against "hs_www.xiaohongshu.com" AFTER replacing underscores,
    # so that check could never match. Detect the placeholder loosely instead.
    if not caption or "xiaohongshu.com" in caption.lower():
        caption = ""

    # Unique hashtags, original order preserved (original sorted them alphabetically)
    tags = list(dict.fromkeys(re.findall(r"#\w+", caption)))
    body = re.sub(r"#\w+", "", caption)
    body = re.sub(r"\s+", " ", body).strip()

    if not tags:
        tags = get_random_hashtags().split()

    tags = tags[:IG_MAX_HASHTAGS]
    tag_str = " ".join(tags)
    caption = f"{body} {tag_str}".strip()

    if len(caption) > IG_MAX_CAPTION:
        keep = IG_MAX_CAPTION - len(tag_str) - 1
        caption = f"{body[:max(keep, 0)].rstrip()} {tag_str}".strip()[:IG_MAX_CAPTION]
    return caption


def read_urls(url_file):
    with open(url_file, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def write_urls(url_file, urls):
    with open(url_file, "w", encoding="utf-8") as f:
        for u in urls:
            f.write(f"{u}\n")


def remove_url_from_file(url_file, url):
    try:
        write_urls(url_file, [u for u in read_urls(url_file) if u != url])
    except Exception as e:
        logging.error(f"Error removing URL from file: {e}")


def handle_failure(url_file, url):
    """Stop one bad link from blocking the queue forever (original always retried urls[0])."""
    counts = load_failure_counts()
    counts[url] = counts.get(url, 0) + 1
    urls = [u for u in read_urls(url_file) if u != url]

    if counts[url] >= MAX_FAILURES:
        logging.error(f"Giving up on {url} after {MAX_FAILURES} failures -> {FAILED_FILE}")
        with open(FAILED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{url}\n")
        counts.pop(url, None)
    else:
        urls.append(url)  # move to the back of the queue
    write_urls(url_file, urls)
    save_failure_counts(counts)


def log_upload(url, video_path, upload_info):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"TIME: {timestamp}\nURL: {url}\nVIDEO: {video_path}\n"
        f"UPLOAD_ID: {upload_info}\n{'-' * 50}\n"
    )
    with open(UPLOAD_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    logging.info(f"Upload logged to {UPLOAD_LOG_FILE}")


def process_url_file(url_file="urls.txt", downloads_dir="downloads", debug=False):
    os.makedirs(downloads_dir, exist_ok=True)

    if not os.path.exists(url_file):
        logging.error(f"URL file not found: {url_file}")
        return False

    urls = read_urls(url_file)
    if not urls:
        logging.warning(f"No URLs found in {url_file}")
        return False

    url = urls[0]
    logging.info(f"Found {len(urls)} URLs. Processing: {url}")
    video_path = None

    try:
        video_path = download_video_from_url(url, downloads_dir, debug=debug)
        if not video_path or not os.path.exists(video_path):
            logging.error("Failed to download video")
            handle_failure(url_file, url)
            return False

        caption = build_caption(video_path)
        logging.info(f"Using caption: {caption}")

        # Safe filename for the uploader
        safe_path = os.path.join(
            os.path.dirname(video_path), sanitize_filename(os.path.basename(video_path))
        )
        if safe_path != video_path:
            os.replace(video_path, safe_path)
            video_path = safe_path

        converted = convert_video_format(video_path)
        if converted:
            os.remove(video_path)
            video_path = converted
        else:
            logging.warning("Conversion failed; trying the original file")

        upload_result = upload_reel(video_path, caption, debug=debug)
        if upload_result:
            log_upload(url, video_path, upload_result)
            remove_url_from_file(url_file, url)
            counts = load_failure_counts()
            if counts.pop(url, None) is not None:
                save_failure_counts(counts)
            return True

        logging.error("Upload failed")
        handle_failure(url_file, url)
        return False

    except Exception as e:
        logging.exception(f"Error processing {url}: {e}")
        try:
            handle_failure(url_file, url)
        except Exception:
            pass
        return False
    finally:
        # Always clean up so failed runs don't pile up video files
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
                logging.info(f"Deleted video: {video_path}")
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Download RedNote videos and upload to Instagram")
    parser.add_argument("-u", "--url-file", default=os.getenv("URL_FILE", "urls.txt"))
    parser.add_argument("-d", "--downloads-dir", default=os.getenv("DOWNLOADS_DIR", "downloads"))
    parser.add_argument("-c", "--continuous", action="store_true",
                        default=env_bool("CONTINUOUS_MODE"))
    parser.add_argument("-i", "--interval", type=int, default=env_int("CHECK_INTERVAL", 3600))
    parser.add_argument("--debug", action="store_true", default=env_bool("DEBUG"))
    args = parser.parse_args()

    if not args.continuous:
        process_url_file(args.url_file, args.downloads_dir, debug=args.debug)
        return

    logging.info(f"Continuous mode: checking every ~{args.interval}s")
    try:
        while True:
            if os.path.exists(args.url_file) and os.path.getsize(args.url_file) > 0:
                process_url_file(args.url_file, args.downloads_dir, debug=args.debug)
            else:
                logging.info("No URLs to process.")
            # +/-10% jitter so posts don't land at perfectly regular intervals
            delay = int(args.interval * random.uniform(0.9, 1.1))
            logging.info(f"Sleeping for {delay} seconds")
            time.sleep(delay)
    except KeyboardInterrupt:
        logging.info("Stopped by user")


if __name__ == "__main__":
    logging.info("Starting video processor")
    main()
    logging.info("Video processor finished")
