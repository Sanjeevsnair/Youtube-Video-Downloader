import random
from bs4 import BeautifulSoup
from flask import Flask, json, redirect, render_template, request, send_file, jsonify
import requests
import tempfile
import os
import instaloader
from urllib.parse import urlparse
import re
from io import BytesIO
from PIL import Image
import time
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("instagram_downloader")

# Monkey patch the Session class to add max_redirects functionality
original_send = requests.Session.send




def patched_send(self, request, **kwargs):
    if hasattr(self, "max_redirects"):
        kwargs["allow_redirects"] = False  # We'll handle redirects ourselves
        response = original_send(self, request, **kwargs)

        redirect_count = 0
        max_count = self.max_redirects

        while redirect_count < max_count and response.is_redirect:
            redirect_count += 1
            logger.debug(f"Following redirect {redirect_count}/{max_count}")

            request.url = response.headers["Location"]
            response = original_send(self, request, **kwargs)

        if redirect_count >= max_count and response.is_redirect:
            raise requests.exceptions.TooManyRedirects(
                f"Exceeded {max_count} redirects.", response=response
            )

        return response
    else:
        return original_send(self, request, **kwargs)


# Apply the monkey patch
requests.Session.send = patched_send

app = Flask(__name__)


@app.before_request
def redirect_www_to_non_www():
    host = request.headers.get("Host", "")
    if host.startswith("www."):
        new_url = request.url.replace("://www.", "://", 1)
        if new_url != request.url:
            return redirect(new_url, code=301)


def get_content_type(url):
    if "/reel/" in url or "/reels/" in url:
        return "reel"
    elif "/stories/" in url:
        return "story"
    elif "/p/" in url:
        return "post"
    return None


def clean_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename)


# Initialize instaloader properly
L = instaloader.Instaloader(
    quiet=True,
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
)


def is_instagram_url(url):
    parsed = urlparse(url)
    return parsed.netloc in ("www.instagram.com", "instagram.com")


def get_shortcode_from_url(url):
    """Extract Instagram post shortcode from URL"""
    pattern = r"(?:reel|reels|p|stories)/([a-zA-Z0-9-_]+)/?"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def get_media_with_proxy(url):
    """Fetch media through proxy to avoid Instagram restrictions"""
    try:
        # Set a maximum number of redirects to prevent loops
        session = requests.Session()
        session.max_redirects = 5  # Limit redirects to 5
        response = session.get(
            url,
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            allow_redirects=True,
        )
        if response.status_code == 200:
            img = BytesIO()
            for chunk in response.iter_content(1024):
                img.write(chunk)
            img.seek(0)
            return img
        return None
    except requests.exceptions.TooManyRedirects:
        logger.warning(f"Too many redirects when fetching {url}")
        return None
    except Exception as e:
        logger.error(f"Error fetching media: {str(e)}")
        return None


def download_story(url):
    """Download Instagram story using RapidAPI"""
    try:
        # Extract username from URL
        username_match = re.search(r'instagram\.com/stories/([^/]+)', url)
        if not username_match:
            return None, "Invalid story URL format"
        username = username_match.group(1)
        
        # Extract story ID and remove any query parameters
        story_id = url.split("/")[5].split("?")[0]
        
        logger.info(f"Downloading story from @{username} using RapidAPI")
        
        # First get the user ID using RapidAPI
        user_url = "https://instagram-best-experience.p.rapidapi.com/media"
        user_querystring = {"id": story_id}
        
        headers = {
            "x-rapidapi-key": "89502ff331mshd77d3b1b518b9d4p16ce37jsnaaf63c0fa5f2",
            "x-rapidapi-host": "instagram-best-experience.p.rapidapi.com"
        }
        
        # Get user ID
        user_response = requests.get(user_url, headers=headers, params=user_querystring)
        if user_response.status_code != 200:
            return None, f"Failed to get user info: {user_response.text}"
        
        user_data = user_response.json()
        
        media_list = []
        # Handle single story item
        media_type = user_data.get('media_type')
        if media_type == 1:  # Photo
            candidates = user_data.get('image_versions2', {}).get('candidates', [])
            if candidates:
                best_quality = max(candidates, key=lambda x: x.get('width', 0))
                media_list.append({
                    'url': best_quality['url'],
                    'preview': best_quality['url'],
                    'ext': 'jpg',
                    'is_image': True,
                    'title': f"story_{username}_{user_data.get('id', '')}",
                    'timestamp': user_data.get('taken_at', int(time.time()))
                })
        elif media_type == 2:  # Video
            videos = user_data.get('video_versions', [])
            if videos:
                best_video = max(videos, key=lambda x: x.get('width', 0))
                image_candidates = user_data.get('image_versions2', {}).get('candidates', [])
                preview = image_candidates[0]['url'] if image_candidates else best_video['url']
                media_list.append({
                    'url': best_video['url'],
                    'preview': preview,
                    'ext': 'mp4',
                    'is_image': False,
                    'title': f"story_{username}_{user_data.get('id', '')}",
                    'timestamp': user_data.get('taken_at', int(time.time()))
                })
        
        if media_list:
            return media_list, None
        else:
            return None, "No downloadable media found in stories"
    
    except Exception as e:
        logger.error(f"RapidAPI story download error: {str(e)}")
        return None, f"Unexpected error"


@app.route("/download_story", methods=["POST"])
def handle_story_download():
    """Special route for downloading stories"""
    url = request.form.get("url")

    if not url or not is_instagram_url(url) or "/stories/" not in url:
        return jsonify({"error": "Invalid story URL"}), 400

    media_info, error = download_story(url)

    if media_info:
        # Add story-specific format information
        for media in media_info:
            media["format"] = "reel"  # Stories use the 9:16 format like reels

        return jsonify(
            {"success": True, "media_info": media_info, "content_type": "story"}
        )
    else:
        return jsonify({"error": error or "Unknown error downloading story"}), 500


def download_media(url):
    try:
        # Extract shortcode from URL
        shortcode = re.search(r"(?:reel|reels|p|stories)/([a-zA-Z0-9-_]+)", url)
        if not shortcode:
            return None, "Invalid URL format - couldn't extract content ID"

        shortcode = shortcode.group(1)
        logger.info(f"Downloading media with shortcode {shortcode}")

        # Check if it's a story
        is_story = "/stories/" in url

        if is_story:
            # Use our efficient story downloader
            return download_story(url)

        try:
            # For regular posts/reels, try the standard instaloader method first
            post = instaloader.Post.from_shortcode(L.context, shortcode)

            media_list = []

            if post.typename == "GraphImage":
                media_list.append(
                    {
                        "url": post.url,
                        "preview": post.url,
                        "ext": "jpg",
                        "is_image": True,
                        "title": f"insta_{shortcode}",
                    }
                )
            elif post.typename == "GraphVideo":
                media_list.append(
                    {
                        "url": post.video_url,
                        "preview": post.url,  # Thumbnail URL
                        "ext": "mp4",
                        "is_image": False,
                        "title": f"insta_{shortcode}",
                    }
                )
            elif post.typename == "GraphSidecar":
                for node in post.get_sidecar_nodes():
                    if node.is_video:
                        media_list.append(
                            {
                                "url": node.video_url,
                                "preview": node.display_url,
                                "ext": "mp4",
                                "is_image": False,
                                "title": f"insta_{shortcode}_{len(media_list)}",
                            }
                        )
                    else:
                        media_list.append(
                            {
                                "url": node.display_url,
                                "preview": node.display_url,
                                "ext": "jpg",
                                "is_image": True,
                                "title": f"insta_{shortcode}_{len(media_list)}",
                            }
                        )

            # Verify and fix preview URLs
            for media in media_list:
                if not media["preview"].startswith(("http:", "https:")):
                    media["preview"] = f"https://instagram.com{media['preview']}"

            if media_list:
                return media_list, None

        except Exception as e:
            logger.warning(f"Instaloader method failed: {str(e)}")
            # Fall back to alternative method if regular download fails

        # Fall back to alternative method
        return fallback_download(url)

    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        # Fall back to alternative method if regular download fails
        return fallback_download(url)


@app.route("/preview")
def serve_preview():
    """Proxy route to serve preview images"""
    url = request.args.get("url")
    if not url:
        return "", 404

    img = get_media_with_proxy(url)
    if img:
        return send_file(img, mimetype="image/jpeg")
    return "", 404


def fallback_download(url):
    """Alternative method using requests"""
    try:
        logger.info(f"Using fallback download method for {url}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.instagram.com/",
            "Cookie": "",  # Empty cookie - we're not logged in
        }

        session = requests.Session()
        session.max_redirects = 5  # Limit redirects to 5
        response = session.get(url, headers=headers, timeout=10, allow_redirects=True)

        # Check if we were redirected to login page
        if "login" in response.url.lower():
            logger.warning("Redirected to login page")
            return None, "Instagram requires login to view this content"

        soup = BeautifulSoup(response.text, "html.parser")

        # Find media in meta tags
        media_url = None
        preview_url = None
        is_video = False

        for meta in soup.find_all("meta"):
            if meta.get("property") == "og:video":
                media_url = meta.get("content")
                is_video = True
                break
            elif meta.get("property") == "og:image" and not media_url:
                media_url = meta.get("content")

        if not media_url:
            # Try to find in JSON data
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if "video" in data:
                        media_url = data["video"]["contentUrl"]
                        preview_url = data["video"]["thumbnailUrl"]
                        is_video = True
                        break
                    elif "image" in data:
                        if isinstance(data["image"], list) and len(data["image"]) > 0:
                            media_url = data["image"][0].get("url", "")
                        else:
                            media_url = data["image"].get("url", "")
                        break
                except (json.JSONDecodeError, AttributeError):
                    pass

        if not media_url:
            # Last attempt - look for embeded videos or images
            for script in soup.find_all("script"):
                if script.string and "window.__additionalDataLoaded" in script.string:
                    # Try to extract JSON
                    json_match = re.search(
                        r"window\.__additionalDataLoaded\([^,]+,\s*({.+})\);",
                        script.string,
                    )
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))

                            # Navigate the complex Instagram JSON structure
                            if "graphql" in data:
                                post_data = data["graphql"].get("shortcode_media", {})

                                if post_data.get("is_video"):
                                    media_url = post_data.get("video_url")
                                    preview_url = post_data.get("display_url")
                                    is_video = True
                                else:
                                    media_url = post_data.get("display_url")

                                # Handle carousel/multiple images
                                if (
                                    not media_url
                                    and "edge_sidecar_to_children" in post_data
                                ):
                                    edges = post_data["edge_sidecar_to_children"].get(
                                        "edges", []
                                    )
                                    if edges and len(edges) > 0:
                                        first_node = edges[0]["node"]
                                        if first_node.get("is_video"):
                                            media_url = first_node.get("video_url")
                                            preview_url = first_node.get("display_url")
                                            is_video = True
                                        else:
                                            media_url = first_node.get("display_url")

                            # Also check items array used in some responses
                            elif "items" in data and len(data["items"]) > 0:
                                item = data["items"][0]
                                if (
                                    item.get("video_versions")
                                    and len(item["video_versions"]) > 0
                                ):
                                    media_url = item["video_versions"][0]["url"]
                                    is_video = True
                                    if (
                                        "image_versions2" in item
                                        and "candidates" in item["image_versions2"]
                                    ):
                                        preview_url = item["image_versions2"][
                                            "candidates"
                                        ][0]["url"]
                                elif (
                                    "image_versions2" in item
                                    and "candidates" in item["image_versions2"]
                                ):
                                    media_url = item["image_versions2"]["candidates"][
                                        0
                                    ]["url"]

                        except json.JSONDecodeError:
                            pass

        if not media_url:
            # Try searching for any video or image URLs in the HTML
            video_urls = re.findall(
                r'https://[^"\'\s]+\.cdninstagram\.com/[^"\'\s]+\.mp4[^"\'\s]*',
                response.text,
            )
            if video_urls:
                media_url = video_urls[0]
                is_video = True
            else:
                image_urls = re.findall(
                    r'https://[^"\'\s]+\.cdninstagram\.com/[^"\'\s]+\.jpg[^"\'\s]*',
                    response.text,
                )
                if image_urls:
                    media_url = image_urls[0]

        if not media_url:
            return None, "No media found in this post"

        if not preview_url:
            preview_url = media_url if not is_video else None

        return [
            {
                "url": media_url,
                "preview": preview_url or media_url,
                "title": "instagram_media",
                "ext": "mp4" if is_video else "jpg",
                "is_image": not is_video,
            }
        ], None

    except requests.exceptions.TooManyRedirects as e:
        logger.error(f"Too many redirects: {str(e)}")
        return None, "Error: Too many redirects. Instagram may be blocking access."
    except Exception as e:
        logger.error(f"Fallback download error: {str(e)}")
        return None, f"Error: {str(e)}"


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url")
        if not url or not is_instagram_url(url):
            return render_template(
                "index.html", error="Please enter a valid Instagram URL"
            )

        content_type = get_content_type(url)
        if not content_type:
            return render_template("index.html", error="Unsupported Instagram URL type")

        # Special handling for stories
        if content_type == "story":
            media_info, error = download_story(url)
            if error:
                return render_template(
                    "index.html", error=f"Failed to download story: {error}"
                )
        else:
            # Regular posts and reels
            media_info, error = download_media(url)
            if error:
                return render_template("index.html", error=error)

        # Add format information for proper display
        for media in media_info:
            if content_type == "story":
                media["format"] = "reel"  # Stories use the 9:16 format like reels
            elif content_type == "reel":
                media["format"] = "reel"
            elif content_type == "post":
                # Default to square, template will adjust based on actual dimensions
                media["format"] = "square"

        return render_template(
            "index.html",
            success=True,
            url=url,
            media_info=media_info,
            content_type=content_type,
        )

    return render_template("index.html")

@app.route("/terms-and-conditions", methods=["GET", "POST"])
def TermsAndCondition():
    return render_template("termsandcondition.html")

# ✅ Fix: Handle GET /download gracefully to avoid 404 for crawlers
@app.route("/download", methods=["GET"])
def download_get_redirect():
    return redirect("/", code=302)

@app.route("/download", methods=["POST"])
def download():
    url = request.form.get("url")
    media_url = request.form.get("media_url")
    ext = request.form.get("ext", "mp4")
    title = request.form.get("title", "instagram_media")
    is_image = request.form.get("is_image", "false").lower() == "true"

    if not url or not is_instagram_url(url) or not media_url:
        return jsonify({"error": "Invalid request"}), 400

    try:
        # For stories, we might need to add additional headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
        }

        # Special handling for Instagram story URLs, which might have short expiration times
        if "/stories/" in url:
            # Try to refresh the media URL if it's from a story
            content_type = get_content_type(url)
            if content_type == "story":
                media_info, error = download_story(url)
                if media_info and not error:
                    # Use the freshest URL
                    for item in media_info:
                        if (is_image and item["is_image"]) or (
                            not is_image and not item["is_image"]
                        ):
                            media_url = item["url"]
                            break

        # Set max redirects to avoid redirection loops
        session = requests.Session()
        session.max_redirects = 5  # Limit to 5 redirects
        response = session.get(
            media_url, stream=True, timeout=15, headers=headers, allow_redirects=True
        )
        response.raise_for_status()

        mem_file = BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            mem_file.write(chunk)
        mem_file.seek(0)

        if is_image or ext in ("jpg", "jpeg", "png", "webp"):
            try:
                img = Image.open(mem_file)
                output = BytesIO()
                img.convert("RGB").save(output, "JPEG", quality=95)
                output.seek(0)
                ext = "jpg"
                mem_file = output
            except Exception as e:
                # If image processing fails, just return the original file
                mem_file.seek(0)
                logger.error(f"Image processing error: {str(e)}")

        filename = f"{clean_filename(title)}.{ext}"

        return send_file(
            mem_file,
            as_attachment=True,
            download_name=filename,
            mimetype="video/mp4" if ext == "mp4" else "image/jpeg",
        )
        

    except requests.exceptions.TooManyRedirects:
        logger.error(f"Too many redirects when downloading {media_url}")
        return (
            jsonify(
                {
                    "error": "Download failed: Too many redirects. Try refreshing the page."
                }
            ),
            500,
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Download request error: {str(e)}")
        return jsonify({"error": f"Download failed: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Processing error: {str(e)}")
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
