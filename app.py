import os
import re
import requests
import json
import time
import pickle
import base64
import sys
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Configuration
USER_AGENT = os.getenv("USER_AGENT")
X_IG_APP_ID = os.getenv("X_IG_APP_ID")
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

if not USER_AGENT or not X_IG_APP_ID:
    raise RuntimeError("Required headers not found in ENV")

class InstagramStoryScraper:
    def __init__(self):
        """Initialize Instagram Story Scraper"""
        self.headers = {
            'x-ig-app-id': X_IG_APP_ID,
            'x-asbd-id': '198387',
            'x-ig-www-claim': '0',
            'origin': 'https://www.instagram.com',
            'accept': '*/*',
            'user-agent': USER_AGENT,
        }
        self.proxies = {'http': '', 'https': ''}
        self.ig_story_regex = r'https?://(?:www\.)?instagram\.com/stories/([^/]+)(?:/(\d+))?/?'
        self.ig_highlights_regex = r'(?:https?://)?(?:www\.)?instagram\.com/s/(\w+)(?:\?story_media_id=(\d+)_(\d+))?'
        self.ig_session = requests.Session()

    def set_proxies(self, http_proxy: str, https_proxy: str) -> None:
        """Set proxy"""
        self.proxies['http'] = http_proxy 
        self.proxies['https'] = https_proxy

    def get_username_storyid(self, ig_story_url: str) -> tuple:
        """Extract username and story ID from URL"""
        if '/s/' in ig_story_url:
            code = re.match(self.ig_highlights_regex, ig_story_url).group(1)
            return 'highlights', str(base64.b64decode(code)).split(':')[1][:-1]

        match = re.match(self.ig_story_regex, ig_story_url)
        if match:
            username = match.group(1)
            story_id = match.group(2) or '3446487468465775665'  # Default story ID if none provided
            return username, story_id
        return None, None

    def get_userid_by_username(self, username: str, story_id: str) -> str:
        """Get user ID by username"""
        if username == 'highlights':
            return f'highlight:{story_id}'
        try:
            response = self.ig_session.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                headers=self.headers,
                proxies=self.proxies,
                allow_redirects=False
            )
            return response.json()['data']['user']['id']
        except Exception:
            return None

    def ig_login(self, your_username: str, your_password: str, cookies_path: str) -> bool:
        """Login to Instagram and save cookies"""
        if os.path.isfile(cookies_path):
            with open(cookies_path, 'rb') as f:
                self.ig_session.cookies.update(pickle.load(f))
            return True

        ig_login_page = self.ig_session.get('https://www.instagram.com/accounts/login', 
                                          headers=self.headers, 
                                          proxies=self.proxies)

        try:
            csrf_token = json.loads('{' + re.search(r'"csrf_token":"(\w+)"', ig_login_page.text).group(0) + '}')['csrf_token']
            rollout_hash = json.loads('{' + re.search(r'"rollout_hash":"(\w+)"', ig_login_page.text).group(0) + '}')['rollout_hash']
        except Exception:
            return False

        login_headers = self.headers.copy()
        login_headers.update({
            'x-requested-with': 'XMLHttpRequest',
            'x-csrftoken': csrf_token,
            'x-instagram-ajax': rollout_hash,
            'referer': 'https://www.instagram.com/',
        })

        login_payload = {
            'enc_password': f'#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{your_password}',
            'username': your_username,
            'queryParams': '{}',
            'optIntoOneTap': 'false',
            'stopDeletionNonce': '',
            'trustedDeviceRecords': '{}',
        }
        
        try:
            response = self.ig_session.post(
                'https://www.instagram.com/accounts/login/ajax/',
                headers=login_headers,
                data=login_payload
            )
            if response.status_code == 200 and response.json().get('authenticated'):
                with open(cookies_path, 'wb') as f:
                    pickle.dump(self.ig_session.cookies, f)
                return True
            return False
        except Exception:
            return False

    def get_ig_stories_data(self, user_id: str) -> dict:
        """Get stories data"""
        ig_stories_endpoint = f'https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}'
        try:
            response = self.ig_session.get(ig_stories_endpoint, headers=self.headers, proxies=self.proxies)
            return response.json()
        except Exception:
            return None

    def get_story_media_info(self, ig_story_json: dict, user_id: str) -> list:
        """Extract media info from stories JSON"""
        media_info = []
        try:
            for item in ig_story_json['reels'][f'{user_id}']['items']:
                if 'video_versions' in item:
                    # Video story
                    media_info.append({
                        "url": item['video_versions'][0]['url'],
                        "preview": item['image_versions2']['candidates'][0]['url'],
                        "ext": "mp4",
                        "format": "reel",
                        "title": "Instagram Story Video",
                        "width": item.get('original_width', 1080),
                        "height": item.get('original_height', 1920),
                        "is_video": True
                    })
                else:
                    # Image story
                    media_info.append({
                        "url": item['image_versions2']['candidates'][0]['url'],
                        "preview": item['image_versions2']['candidates'][0]['url'],
                        "ext": "jpg",
                        "format": "portrait",
                        "title": "Instagram Story Image",
                        "width": item.get('original_width', 1080),
                        "height": item.get('original_height', 1920),
                        "is_video": False
                    })
            return media_info
        except Exception:
            return None

def get_media_id(url):
    """Extract Instagram media ID from URL"""
    match = re.search(
        r"instagram\.com\/(?:[A-Za-z0-9_.]+\/)?(p|reels|reel|stories)\/([A-Za-z0-9-_]+)", 
        url
    )
    return match.group(2) if match else None

def get_all_display_resources(display_resources):
    """Return all available display resources sorted by quality"""
    if not display_resources:
        return []
    return sorted(display_resources, key=lambda x: x['config_width'], reverse=True)

def fetch_instagram_data(url):
    """Fetch Instagram media data using GraphQL API"""
    # First check if it's a story URL
    story_scraper = InstagramStoryScraper()
    username, story_id = story_scraper.get_username_storyid(url)
    
    if username and story_id:
        # Handle story URL
        if not IG_USERNAME or not IG_PASSWORD:
            return {"error": "Please Try Again"}
            
        if not story_scraper.ig_login(IG_USERNAME, IG_PASSWORD, 'ig_cookies'):
            return {"error": "Please Try Again"}
            
        user_id = story_scraper.get_userid_by_username(username, story_id)
        if not user_id:
            return {"error": "Failed to get user ID"}
            
        stories_json = story_scraper.get_ig_stories_data(user_id)
        if not stories_json:
            return {"error": "Account is private or Story is expired, Please check account and link then try again"}
            
        media_info = story_scraper.get_story_media_info(stories_json, user_id)
        if not media_info:
            return {"error": "Account is private or Story is expired, Please check account and link then try again"}
            
        return {
            "success": True,
            "content_type": "story",
            "media_info": media_info,
            "caption": "",
            "owner": username
        }
    
    # Handle regular posts (original functionality)
    media_id = get_media_id(url)
    if not media_id:
        return {"error": "Invalid Instagram URL"}

    graphql_url = "https://www.instagram.com/api/graphql"
    params = {
        "variables": json.dumps({"shortcode": media_id}),
        "doc_id": "10015901848480474",
        "lsd": "AVqbxe3J_YA"
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-IG-App-ID": X_IG_APP_ID,
        "X-FB-LSD": "AVqbxe3J_YA",
        "X-ASBD-ID": "129477",
        "Sec-Fetch-Site": "same-origin"
    }

    try:
        response = requests.post(graphql_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return {"error": f"Failed to fetch data"}

    items = data.get("data", {}).get("xdt_shortcode_media")
    if not items:
        return {"error": "No media found in URL"}

    # Determine content type
    if items.get("__typename") == "XDTGraphSidecar":
        content_type = "carousel"
    elif items.get("is_video"):
        content_type = "video"
    else:
        content_type = "image"

    media_info = []
    
    if content_type == "carousel":
        edges = items.get("edge_sidecar_to_children", {}).get("edges", [])
        if not edges:
            edges = [{"node": node} for node in items.get("sidecar", [])]
        
        for edge in edges:
            node = edge.get("node", {})
            display_resources = get_all_display_resources(node.get("display_resources", []))
        
            if node.get("is_video"):
                media_info.append({
                    "url": node.get("video_url"),
                    "display_resources": display_resources,
                    "preview": display_resources[0]['src'] if display_resources else None,
                    "ext": "mp4",
                    "format": "square",
                    "title": f"Instagram Video {len(media_info) + 1}",
                    "width": node.get("dimensions", {}).get("width"),
                    "height": node.get("dimensions", {}).get("height"),
                    "is_video": True
                })
            else:
                media_info.append({
                    "display_resources": display_resources,
                    "url": display_resources[0]['src'] if display_resources else None,
                    "preview": display_resources[0]['src'] if display_resources else None,
                    "ext": "jpg",
                    "format": "square",
                    "title": f"Instagram Image {len(media_info) + 1}",
                    "width": node.get("dimensions", {}).get("width"),
                    "height": node.get("dimensions", {}).get("height"),
                    "is_video": False
                })
    else:
        display_resources = get_all_display_resources(items.get("display_resources", []))
    
        if content_type == "video":
            media_info.append({
                "url": items.get("video_url"),
                "display_resources": display_resources,
                "preview": display_resources[0]['src'] if display_resources else None,
                "ext": "mp4",
                "format": "reel" if items.get("product_type") == "clips" else "square",
                "title": "Instagram Video",
                "width": items.get("dimensions", {}).get("width"),
                "height": items.get("dimensions", {}).get("height"),
                "is_video": True
            })
        else:
            media_info.append({
                "display_resources": display_resources,
                "url": display_resources[0]['src'] if display_resources else None,
                "preview": display_resources[0]['src'] if display_resources else None,
                "ext": "jpg",
                "format": "square",
                "title": "Instagram Photo",
                "width": items.get("dimensions", {}).get("width"),
                "height": items.get("dimensions", {}).get("height"),
                "is_video": False
            })

    # Determine the format for each media item based on aspect ratio
    for media in media_info:
        if media.get("width") and media.get("height"):
            ratio = media["width"] / media["height"]
            if ratio > 1.2:
                media["format"] = "landscape"
            elif ratio < 0.8:
                media["format"] = "portrait"
            elif ratio > 0.9 and ratio < 1.1:
                media["format"] = "square"
            else:
                media["format"] = "reel"

    return {
        "success": True,
        "content_type": content_type,
        "media_info": media_info,
        "caption": (items.get("edge_media_to_caption", {}).get("edges", [{}])[0].get("node", {}).get("text", "") 
            if items.get("edge_media_to_caption", {}).get("edges", []) 
            else ""),
        "owner": items.get("owner", {}).get("username", "")
    }
    

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            return render_template("index.html", error="Please enter a valid Instagram URL")
        
        result = fetch_instagram_data(url)
        if "error" in result:
            return render_template("index.html", error=result["error"])
        
        return render_template(
            "index.html", 
            success=True,
            url=url,
            content_type=result["content_type"],
            media_info=result["media_info"],
            caption=result["caption"],
            owner=result["owner"]
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

@app.route("/preview")
def preview():
    """Proxy for preview images to avoid CORS issues"""
    url = request.args.get("url")
    if not url:
        return "", 400
    
    try:
        response = requests.get(url, stream=True, timeout=10)
        response.raise_for_status()
        return response.content, response.status_code, {"Content-Type": response.headers["Content-Type"]}
    except Exception:
        return "", 500

@app.route("/download", methods=["POST"])
def download():
    """Proxy for downloads to avoid CORS issues"""
    media_url = request.form.get("media_url")
    if not media_url:
        return "Missing media URL", 400
    
    try:
        response = requests.get(media_url, stream=True, timeout=30)
        response.raise_for_status()
        prefix = "StreamSave-"
        ext = request.form.get("ext", "mp4")
        title = request.form.get("title", "instagram_media").replace(" ", "_")
        filename = f"{prefix}{title}.{ext}"
        
        headers = {
            "Content-Type": response.headers["Content-Type"],
            "Content-Disposition": f"attachment; filename={filename}"
        }
        return response.content, response.status_code, headers
    except Exception as e:
        return str(e), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)