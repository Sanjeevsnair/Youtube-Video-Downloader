import random
import traceback
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
    request_timeout=60  # Add this
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


from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import threading

# Add these at the top of your imports
lock = threading.Lock()

def setup_selenium_driver():
    """Configure headless Chrome browser for Instagram scraping"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")  # New headless mode
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Enable automatic downloading
    prefs = {
        "profile.default_content_setting_values.automatic_downloads": 1,
        "download.prompt_for_download": False,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    try:
        # Use webdriver_manager to handle ChromeDriver automatically
        driver = webdriver.Chrome(
            service=webdriver.chrome.service.Service(ChromeDriverManager().install()),
            options=chrome_options
        )
        return driver
    except Exception as e:
        logger.error(f"Failed to initialize Chrome driver: {str(e)}")
        raise

def extract_media_with_selenium(url):
    """Selenium fallback for when Instaloader fails"""
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        
        # Wait for main content to load
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "article[role='presentation']")))
        
        media_info = []
        shortcode = get_shortcode_from_url(url)
        
        # Check for carousel
        carousel = driver.find_elements(By.CSS_SELECTOR, "div[role='button'][aria-label*='carousel']")
        if carousel:
            items = driver.find_elements(By.CSS_SELECTOR, "div._aagv, div._aakz")
            for idx, item in enumerate(items):
                try:
                    # Click to activate carousel item
                    driver.execute_script("arguments[0].click();", item)
                    time.sleep(1)  # Wait for content to load
                    
                    # Check for video
                    video = item.find_elements(By.TAG_NAME, "video")
                    if video:
                        video_url = video[0].get_attribute("src")
                        preview = item.find_element(By.TAG_NAME, "img").get_attribute("src")
                        media_info.append({
                            "url": video_url,
                            "preview": preview,
                            "ext": "mp4",
                            "is_image": False,
                            "title": f"ig_{shortcode}_{idx}",
                            "format": "square"
                        })
                    else:
                        # Handle image
                        img = item.find_element(By.TAG_NAME, "img")
                        img_url = img.get_attribute("src")
                        media_info.append({
                            "url": img_url,
                            "preview": img_url,
                            "ext": "jpg",
                            "is_image": True,
                            "title": f"ig_{shortcode}_{idx}",
                            "format": "square"
                        })
                except:
                    continue
        else:
            # Single post
            video = driver.find_elements(By.TAG_NAME, "video")
            if video:
                video_url = video[0].get_attribute("src")
                preview = driver.find_element(By.CSS_SELECTOR, "img[style*='object-fit']").get_attribute("src")
                media_info.append({
                    "url": video_url,
                    "preview": preview,
                    "ext": "mp4",
                    "is_image": False,
                    "title": f"ig_{shortcode}",
                    "format": "square"
                })
            else:
                img = driver.find_element(By.CSS_SELECTOR, "img[style*='object-fit']")
                img_url = img.get_attribute("src")
                media_info.append({
                    "url": img_url,
                    "preview": img_url,
                    "ext": "jpg",
                    "is_image": True,
                    "title": f"ig_{shortcode}",
                    "format": "square"
                })
        
        return media_info if media_info else None, None
        
    except Exception as e:
        logger.error(f"Selenium error: {str(e)}")
        return None, f"Selenium failed: {str(e)}"
    finally:
        if driver:
            driver.quit()

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

def download_reel_selenium(url):
    """Most reliable current method using headless browser"""
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        
        # Wait for video element and get highest quality source
        video = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//video[contains(@src,'cdninstagram.com')]"))
        )
        video_url = driver.execute_script("""
            const videos = Array.from(document.querySelectorAll('video'));
            const sources = videos.flatMap(v => 
                Array.from(v.querySelectorAll('source')).map(s => s.src)
            );
            return sources.find(url => url.includes('cdninstagram.com')) || 
                   (videos[0] ? videos[0].src : null);
        """)
        
        if not video_url or 'blob:' in video_url:
            raise Exception("No valid video URL found")

        # Get preview image
        preview = driver.find_element(By.CSS_SELECTOR, "img[style*='object-fit']")
        preview_url = preview.get_attribute('src')
        
        return [{
            "url": video_url,
            "preview": preview_url,
            "ext": "mp4",
            "is_image": False,
            "title": f"reel_{get_shortcode_from_url(url)}",
            "format": "reel"
        }], None
        
    except Exception as e:
        logger.error(f"Selenium reel download failed: {str(e)}")
        return None, f"Selenium failed: {str(e)}"
    finally:
        if driver:
            driver.quit()
            
def download_reel_api_fallback(url):
    """Alternative API methods when Selenium fails"""
    shortcode = get_shortcode_from_url(url)
    if not shortcode:
        return None, "Invalid URL"
    
    # Method 1: GraphQL Query
    try:
        api_url = f"https://www.instagram.com/graphql/query/?query_hash=9f8827793ef34641b2fb195d4d41151c&variables=%7B%22shortcode%22%3A%22{shortcode}%22%7D"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-IG-App-ID": "936619743392459"
        }
        response = requests.get(api_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            video_url = data['data']['shortcode_media']['video_url']
            preview_url = data['data']['shortcode_media']['display_url']
            return [{
                "url": video_url,
                "preview": preview_url,
                "ext": "mp4",
                "is_image": False,
                "title": f"reel_{shortcode}",
                "format": "reel"
            }], None
    except:
        pass
    
    # Method 2: Mobile API
    try:
        mobile_url = f"https://i.instagram.com/api/v1/media/{shortcode}/info/"
        headers = {
            "User-Agent": "Instagram 267.0.0.19.301 Android",
            "X-IG-App-ID": "567067343352427"
        }
        response = requests.get(mobile_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            video_url = data['items'][0]['video_versions'][0]['url']
            preview_url = data['items'][0]['image_versions2']['candidates'][0]['url']
            return [{
                "url": video_url,
                "preview": preview_url,
                "ext": "mp4",
                "is_image": False,
                "title": f"reel_{shortcode}",
                "format": "reel"
            }], None
    except:
        pass
    
    return None, "All API methods failed"

def download_reel(url):
    """Main reel download function with smart fallbacks"""
    # Try Selenium first (most reliable)
    result, error = download_reel_selenium(url)
    if result:
        return result, None
    
    # Try API fallbacks
    result, error = download_reel_api_fallback(url)
    if result:
        return result, None
    
    # Ultimate fallback - requires authentication
    try:
        L = instaloader.Instaloader()
        post = instaloader.Post.from_shortcode(L.context, get_shortcode_from_url(url))
        if not post.is_video:
            return None, "Not a video post"
            
        return [{
            "url": post.video_url,
            "preview": post.url,
            "ext": "mp4",
            "is_image": False,
            "title": f"reel_{post.shortcode}",
            "format": "reel"
        }], None
    except Exception as e:
        logger.error(f"Instaloader failed: {str(e)}")
        return None, "All methods failed (including authenticated)"


def download_instagram_post(url):
    """Handles all Instagram post types: single photos, videos, and carousels"""
    shortcode = get_shortcode_from_url(url)
    if not shortcode:
        return None, "Invalid Instagram URL"

    try:
        # Initialize Instaloader
        L = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False
        )
        
        # Get post object
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        media_info = []
        
        # Handle different post types
        if post.typename == 'GraphImage':  # Single image
            media_info.append({
                "url": post.url,
                "preview": post.url,
                "ext": "jpg",
                "is_image": True,
                "title": f"ig_{shortcode}",
                "format": "square" if post.aspect_ratio == 1 else "portrait"
            })
            
        elif post.typename == 'GraphVideo':  # Single video
            media_info.append({
                "url": post.video_url,
                "preview": post.url,
                "ext": "mp4",
                "is_image": False,
                "title": f"ig_{shortcode}",
                "format": "square" if post.aspect_ratio == 1 else "portrait"
            })
            
        elif post.typename == 'GraphSidecar':  # Carousel
            for idx, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video:
                    media_info.append({
                        "url": node.video_url,
                        "preview": node.display_url,
                        "ext": "mp4",
                        "is_image": False,
                        "title": f"ig_{shortcode}_{idx}",
                        "format": "square" if node.aspect_ratio == 1 else "portrait"
                    })
                else:
                    media_info.append({
                        "url": node.display_url,
                        "preview": node.display_url,
                        "ext": "jpg",
                        "is_image": True,
                        "title": f"ig_{shortcode}_{idx}",
                        "format": "square" if node.aspect_ratio == 1 else "portrait"
                    })
        
        if media_info:
            return media_info, None
        else:
            return None, "No media found in post"
            
    except Exception as e:
        logger.error(f"Instaloader error: {str(e)}")
        return None, f"Error downloading post: {str(e)}"


def download_media(url):
    """Main download function that handles all content types"""
    content_type = get_content_type(url)
    
    if content_type == "reel":
        return download_reel(url)
    elif content_type == "story":
        return download_story(url)
    elif content_type == "post":
        # First try Instaloader
        media_info, error = download_instagram_post(url)
        if media_info:
            return media_info, None
            
        # Fallback to Selenium
        media_info, error = extract_media_with_selenium(url)
        if media_info:
            return media_info, None
            
        return None, "All download methods failed for this post"
    else:
        return None, "Unsupported content type"

def extract_media_via_api(url):
    """Try to extract media using Instagram's internal API"""
    try:
        shortcode = get_shortcode_from_url(url)
        if not shortcode:
            return None, "Could not extract shortcode"
            
        api_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }
        
        session = requests.Session()
        session.max_redirects = 3
        response = session.get(api_url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            media_info = []
            
            # Handle different post types
            if 'items' in data:  # GraphQL response
                item = data['items'][0]
                if item['media_type'] == 1:  # Image
                    media_info.append({
                        "url": item['image_versions2']['candidates'][0]['url'],
                        "preview": item['image_versions2']['candidates'][0]['url'],
                        "ext": "jpg",
                        "is_image": True,
                        "title": f"insta_{shortcode}",
                        "format": "square"
                    })
                elif item['media_type'] == 2:  # Video
                    media_info.append({
                        "url": item['video_versions'][0]['url'],
                        "preview": item['image_versions2']['candidates'][0]['url'],
                        "ext": "mp4",
                        "is_image": False,
                        "title": f"insta_{shortcode}",
                        "format": "square"
                    })
                elif item['media_type'] == 8:  # Carousel
                    for carousel_item in item['carousel_media']:
                        if carousel_item['media_type'] == 1:  # Image
                            media_info.append({
                                "url": carousel_item['image_versions2']['candidates'][0]['url'],
                                "preview": carousel_item['image_versions2']['candidates'][0]['url'],
                                "ext": "jpg",
                                "is_image": True,
                                "title": f"insta_{shortcode}_{len(media_info)}",
                                "format": "square"
                            })
                        elif carousel_item['media_type'] == 2:  # Video
                            media_info.append({
                                "url": carousel_item['video_versions'][0]['url'],
                                "preview": carousel_item['image_versions2']['candidates'][0]['url'],
                                "ext": "mp4",
                                "is_image": False,
                                "title": f"insta_{shortcode}_{len(media_info)}",
                                "format": "square"
                            })
            elif 'graphql' in data:  # Alternative API response
                media = data['graphql']['shortcode_media']
                if media['__typename'] == 'GraphImage':
                    media_info.append({
                        "url": media['display_url'],
                        "preview": media['display_url'],
                        "ext": "jpg",
                        "is_image": True,
                        "title": f"insta_{shortcode}",
                        "format": "square"
                    })
                elif media['__typename'] == 'GraphVideo':
                    media_info.append({
                        "url": media['video_url'],
                        "preview": media['display_url'],
                        "ext": "mp4",
                        "is_image": False,
                        "title": f"insta_{shortcode}",
                        "format": "square"
                    })
                elif media['__typename'] == 'GraphSidecar':
                    for edge in media['edge_sidecar_to_children']['edges']:
                        node = edge['node']
                        if node['__typename'] == 'GraphImage':
                            media_info.append({
                                "url": node['display_url'],
                                "preview": node['display_url'],
                                "ext": "jpg",
                                "is_image": True,
                                "title": f"insta_{shortcode}_{len(media_info)}",
                                "format": "square"
                            })
                        elif node['__typename'] == 'GraphVideo':
                            media_info.append({
                                "url": node['video_url'],
                                "preview": node['display_url'],
                                "ext": "mp4",
                                "is_image": False,
                                "title": f"insta_{shortcode}_{len(media_info)}",
                                "format": "square"
                            })
            
            if media_info:
                return media_info, None
            
        return None, "No media found in API response"
    
    except Exception as e:
        logger.warning(f"API extraction failed: {str(e)}")
        return None, str(e)

def extract_media_from_html(url):
    """Extract media from HTML meta tags and JSON scripts"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        session = requests.Session()
        session.max_redirects = 3
        response = session.get(url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            shortcode = get_shortcode_from_url(url)
            media_info = []
            
            # Try to extract from JSON-LD
            ld_json = soup.find('script', type='application/ld+json')
            if ld_json:
                try:
                    data = json.loads(ld_json.string)
                    if isinstance(data, dict):
                        if data.get('@type') == 'VideoObject':
                            media_info.append({
                                "url": data.get('contentUrl'),
                                "preview": data.get('thumbnailUrl'),
                                "ext": "mp4",
                                "is_image": False,
                                "title": f"insta_{shortcode}",
                                "format": "square"
                            })
                        elif data.get('@type') == 'ImageObject':
                            media_info.append({
                                "url": data.get('contentUrl') or data.get('url'),
                                "preview": data.get('contentUrl') or data.get('url'),
                                "ext": "jpg",
                                "is_image": True,
                                "title": f"insta_{shortcode}",
                                "format": "square"
                            })
                except json.JSONDecodeError:
                    pass
            
            # Try to extract from meta tags
            if not media_info:
                video_meta = soup.find('meta', property='og:video')
                if video_meta and video_meta.get('content'):
                    media_info.append({
                        "url": video_meta['content'],
                        "preview": soup.find('meta', property='og:image')['content'] if soup.find('meta', property='og:image') else "",
                        "ext": "mp4",
                        "is_image": False,
                        "title": f"insta_{shortcode}",
                        "format": "square"
                    })
                else:
                    image_meta = soup.find('meta', property='og:image')
                    if image_meta and image_meta.get('content'):
                        media_info.append({
                            "url": image_meta['content'],
                            "preview": image_meta['content'],
                            "ext": "jpg",
                            "is_image": True,
                            "title": f"insta_{shortcode}",
                            "format": "square"
                        })
            
            if media_info:
                return media_info, None
            
        return None, "No media found in HTML"
    
    except Exception as e:
        logger.warning(f"HTML extraction failed: {str(e)}")
        return None, str(e)


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
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        }
        
        session = requests.Session()
        session.max_redirects = 3
        response = session.get(url, headers=headers, timeout=15)
        
        # New pattern matching for Instagram's current HTML
        video_pattern = r'"video_url":"([^"]+)"'
        image_pattern = r'"display_url":"([^"]+)"'
        
        if response.status_code == 200:
            # Try to find video first
            video_match = re.search(video_pattern, response.text)
            if video_match:
                video_url = video_match.group(1).replace('\\', '')
                return [{
                    "url": video_url,
                    "preview": video_url.replace('.mp4', '.jpg'),
                    "ext": "mp4",
                    "is_image": False,
                    "title": "instagram_video"
                }], None
            
            # Then try image
            image_match = re.search(image_pattern, response.text)
            if image_match:
                image_url = image_match.group(1).replace('\\', '')
                return [{
                    "url": image_url,
                    "preview": image_url,
                    "ext": "jpg",
                    "is_image": True,
                    "title": "instagram_image"
                }], None
        
        return None, "No media found in this post"
        
    except Exception as e:
        logger.error(f"Improved fallback error: {str(e)}")
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

@app.route("/privacy-policy", methods=["GET", "POST"])
def privacy():
    return render_template("privacy.html")

@app.route("/contact", methods=["GET", "POST"])
def contact():
    return render_template("contact.html")

@app.route("/about", methods=["GET", "POST"])
def about():
    return render_template("about.html")

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

    except Exception as e:
        logger.error(f"Full download error: {str(e)}\nTraceback: {traceback.format_exc()}")
        return jsonify({"error": "Download failed. Please try again later."}), 500
    except Exception as e:
        logger.error(f"Processing error: {str(e)}")
        return jsonify({"error": f"Processing error: {str(e)}"}), 500
    
def download_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            result, error = download_media(url)
            if result:
                return result, error
            time.sleep(2 ** attempt)  # Exponential backoff
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
    return None, "All download attempts failed"

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)