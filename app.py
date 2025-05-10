import os
import re
import requests
import json
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Configuration
USER_AGENT = os.getenv("USER_AGENT")
X_IG_APP_ID = os.getenv("X_IG_APP_ID")

if not USER_AGENT or not X_IG_APP_ID:
    raise RuntimeError("Required headers not found in ENV")

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
    
    # Sort by width descending (highest quality first)
    return sorted(display_resources, key=lambda x: x['config_width'], reverse=True)

def fetch_instagram_data(url):
    """Fetch Instagram media data using GraphQL API"""
    media_id = get_media_id(url)
    if not media_id:
        return {"error": "Invalid Instagram URL"}

    graphql_url = "https://www.instagram.com/api/graphql"
    params = {
        "variables": json.dumps({"shortcode": media_id}),
        "doc_id": "10015901848480474",  # This doc_id might need updating periodically
        "lsd": "AVqbxe3J_YA"  # This LSD token might need updating
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
        return {"error": f"Failed to fetch data: {str(e)}"}

    items = data.get("data", {}).get("xdt_shortcode_media")
    if not items:
        return {"error": "No media data found"}

    # Determine content type
    if items.get("__typename") == "XDTGraphSidecar":
        content_type = "carousel"
    elif items.get("is_video"):
        content_type = "video"
    else:
        content_type = "image"

    media_info = []
    
    # Handle carousel posts
    # Handle carousel posts
    if content_type == "carousel":
        edges = items.get("edge_sidecar_to_children", {}).get("edges", [])
        if not edges:
            # Fallback to check if there's a "sidecar" field directly in items
            edges = [{"node": node} for node in items.get("sidecar", [])]
        
        for edge in edges:
            node = edge.get("node", {})
            display_resources = get_all_display_resources(node.get("display_resources", []))
        
            if node.get("is_video"):
                # Video in carousel
                media_info.append({
                    "url": node.get("video_url"),
                    "display_resources": display_resources,
                    "preview": display_resources[0]['src'] if display_resources else None,
                    "ext": "mp4",
                    "format": "square",
                    "title": f"Instagram Video {len(media_info) + 1}",
                    "width": node.get("dimensions", {}).get("width"),
                    "height": node.get("dimensions", {}).get("height")
                })
            else:
                # Image in carousel
                media_info.append({
                    "display_resources": display_resources,
                    "url": display_resources[0]['src'] if display_resources else None,
                    "preview": display_resources[0]['src'] if display_resources else None,
                    "ext": "jpg",
                    "format": "square",
                    "title": f"Instagram Image {len(media_info) + 1}",
                    "width": node.get("dimensions", {}).get("width"),
                    "height": node.get("dimensions", {}).get("height")
                })
    else:
        # Handle single media posts
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
                "height": items.get("dimensions", {}).get("height")
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
                "height": items.get("dimensions", {}).get("height")
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
        
        # Determine filename
        ext = request.form.get("ext", "mp4")
        title = request.form.get("title", "instagram_media").replace(" ", "_")
        filename = f"{title}.{ext}"
        
        headers = {
            "Content-Type": response.headers["Content-Type"],
            "Content-Disposition": f"attachment; filename={filename}"
        }
        return response.content, response.status_code, headers
    except Exception as e:
        return str(e), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)