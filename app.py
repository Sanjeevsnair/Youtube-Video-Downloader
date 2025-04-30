import random
from bs4 import BeautifulSoup
from flask import Flask, json, render_template, request, send_file, jsonify
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
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('instagram_downloader')

# Monkey patch the Session class to add max_redirects functionality
# since not all versions of requests support it directly
original_send = requests.Session.send
def patched_send(self, request, **kwargs):
    if hasattr(self, 'max_redirects'):
        kwargs['allow_redirects'] = False  # We'll handle redirects ourselves
        response = original_send(self, request, **kwargs)
        
        redirect_count = 0
        max_count = self.max_redirects
        
        while redirect_count < max_count and response.is_redirect:
            redirect_count += 1
            logger.debug(f"Following redirect {redirect_count}/{max_count}")
            
            request.url = response.headers['Location']
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
    if '/reel/' in url or '/reels/' in url:
        return 'reel'
    elif '/stories/' in url:
        return 'story'
    elif '/p/' in url:
        return 'post'
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
    save_metadata=False
)

def is_instagram_url(url):
    parsed = urlparse(url)
    return parsed.netloc in ('www.instagram.com', 'instagram.com')

def get_shortcode_from_url(url):
    """Extract Instagram post shortcode from URL"""
    pattern = r'(?:reel|reels|p|stories)/([a-zA-Z0-9-_]+)/?'
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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
            allow_redirects=True
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

def download_story_updated(url):
    """Updated method to download Instagram stories without requiring login"""
    try:
        # Extract username and story ID from URL
        match = re.search(r'instagram\.com/stories/([^/]+)/([^/]+)', url)
        if not match:
            return None, "Invalid story URL format"
            
        username = match.group(1)
        story_id = match.group(2)
        
        logger.info(f"Attempting to download story from user {username}, story ID {story_id}")
        
        # Add more robust API endpoints with better fallback options
        api_endpoints = [
            f"https://api.storiesdown.com/v2/stories/{username}",
            f"https://stories-downloader.net/api/stories/{username}",
            f"https://storydownload.app/api/stories/user/{username}",
            f"https://instastories.watch/api/stories/{username}",
            f"https://storiesig.info/api/ig/stories/{username}"
        ]
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Referer': 'https://storiesdown.com/users/{username}'
        }
        
        for endpoint in api_endpoints:
            try:
                logger.info(f"Trying endpoint: {endpoint}")
                
                # Limit redirects to prevent redirect loops
                session = requests.Session()
                session.max_redirects = 5  # Limit to 5 redirects
                
                # Add random delay to prevent rate limiting
                time.sleep(random.uniform(0.5, 1.5))
                
                response = session.get(
                    endpoint, 
                    headers=headers, 
                    timeout=15,  # Increased timeout
                    allow_redirects=True
                )
                
                # Skip if not successful
                if response.status_code != 200:
                    logger.warning(f"Endpoint {endpoint} returned status code {response.status_code}")
                    continue
                    
                try:
                    data = response.json()
                    
                    # Different APIs may have different response structures
                    stories = None
                    if 'stories' in data:
                        stories = data['stories']
                    elif 'data' in data:
                        if 'stories' in data['data']:
                            stories = data['data']['stories']
                        elif 'reels' in data['data']:
                            stories = data['data']['reels']
                        elif isinstance(data['data'], list):
                            stories = data['data']
                    
                    # Extra fallback for nested structures
                    if not stories and 'user' in data and 'stories' in data['user']:
                        stories = data['user']['stories']
                    
                    if not stories or len(stories) == 0:
                        logger.warning(f"No stories found in response from {endpoint}")
                        continue
                    
                    # Process stories data
                    media_list = []
                    for i, story in enumerate(stories[:5]):  # Limit to 5 stories
                        # Extract media URLs based on various API formats
                        media_url = None
                        preview_url = None
                        is_video = False
                        
                        # Try different field names used by various APIs
                        if isinstance(story, dict):
                            # Check for video content
                            for video_key in ['video_url', 'video_src', 'video_versions', 'videoUrl', 'video']:
                                if video_key in story:
                                    if video_key == 'video_versions' and isinstance(story[video_key], list):
                                        media_url = story[video_key][0].get('url')
                                    else:
                                        media_url = story[video_key]
                                    is_video = True
                                    break
                            
                            # If no video found, look for image
                            if not media_url:
                                for img_key in ['image_url', 'image_src', 'display_url', 'thumbnail_url', 'imageUrl', 'image']:
                                    if img_key in story:
                                        media_url = story[img_key]
                                        break
                                        
                                # Try to get from image_versions2 structure
                                if not media_url and 'image_versions2' in story:
                                    if 'candidates' in story['image_versions2'] and len(story['image_versions2']['candidates']) > 0:
                                        media_url = story['image_versions2']['candidates'][0].get('url')
                            
                            # Get preview URL
                            for preview_key in ['thumbnail_url', 'display_url', 'image_url', 'thumbnailUrl']:
                                if preview_key in story:
                                    preview_url = story[preview_key]
                                    break
                            
                            if not preview_url and 'image_versions2' in story:
                                if 'candidates' in story['image_versions2'] and len(story['image_versions2']['candidates']) > 0:
                                    preview_url = story['image_versions2']['candidates'][0].get('url')
                        
                        # If we found a valid media URL, add it to our list
                        if media_url:
                            media_list.append({
                                'url': media_url,
                                'preview': preview_url or media_url,
                                'ext': 'mp4' if is_video else 'jpg',
                                'is_image': not is_video,
                                'title': f"story_{username}_{i}"
                            })
                    
                    if media_list:
                        logger.info(f"Successfully found {len(media_list)} stories from {endpoint}")
                        return media_list, None
                
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON from {endpoint}: {str(e)}")
                    continue
                    
            except requests.exceptions.TooManyRedirects:
                logger.warning(f"Too many redirects when accessing {endpoint}")
                continue
            except requests.exceptions.RequestException as e:
                logger.error(f"Request failed for {endpoint}: {str(e)}")
                continue
        
        # Enhanced fallback method - try different proxy services
        proxy_endpoints = [
            f"https://www.instagramsave.com/instagram-story-downloader.php?url={url}",
            f"https://storiesig.net/stories/{username}",
            f"https://www.instadp.com/instagram-stories/{username}"
        ]
        
        for proxy_url in proxy_endpoints:
            try:
                logger.info(f"Trying proxy service: {proxy_url}")
                
                # Random delay to prevent rate limiting
                time.sleep(random.uniform(0.8, 2.0))
                
                session = requests.Session()
                session.max_redirects = 5
                
                # Use different user agents to avoid detection
                user_agents = [
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
                    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ]
                
                proxy_headers = {
                    'User-Agent': random.choice(user_agents),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Referer': 'https://www.google.com/'
                }
                
                response = session.get(
                    proxy_url,
                    headers=proxy_headers,
                    timeout=15,
                    allow_redirects=True
                )
                
                if response.status_code != 200:
                    continue
                
                # Parse HTML content
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Look for story media in common formats
                media_urls = []
                
                # Pattern 1: Look for video elements
                for video in soup.find_all('video'):
                    src = video.get('src')
                    if src and ('instagram' in src or 'cdninstagram' in src):
                        media_urls.append({
                            'url': src,
                            'is_video': True
                        })
                
                # Pattern 2: Look for image elements
                for img in soup.find_all('img'):
                    src = img.get('src')
                    if src and ('instagram' in src or 'cdninstagram' in src) and not src.endswith(('.ico', '.svg')):
                        media_urls.append({
                            'url': src,
                            'is_video': False
                        })
                
                # Pattern 3: Look for download links
                for a in soup.find_all('a', href=True):
                    href = a.get('href')
                    if href and ('cdninstagram' in href or '/download' in href) and (href.endswith('.mp4') or href.endswith('.jpg')):
                        media_urls.append({
                            'url': href,
                            'is_video': href.endswith('.mp4')
                        })
                
                # Pattern 4: Look in scripts for JSON data
                for script in soup.find_all('script'):
                    if script.string:
                        # Find URLs in the script content
                        img_urls = re.findall(r'https://[^"\']+\.cdninstagram\.com/[^"\']+\.(jpg|jpeg|png|webp)', script.string)
                        for url in img_urls:
                            media_urls.append({
                                'url': url,
                                'is_video': False
                            })
                        
                        video_urls = re.findall(r'https://[^"\']+\.cdninstagram\.com/[^"\']+\.mp4[^"\']*', script.string)
                        for url in video_urls:
                            media_urls.append({
                                'url': url,
                                'is_video': True
                            })
                
                # If we found media URLs, return them
                if media_urls:
                    media_list = []
                    seen_urls = set()  # To avoid duplicates
                    
                    for i, media in enumerate(media_urls[:5]):  # Limit to 5
                        if media['url'] in seen_urls:
                            continue
                            
                        seen_urls.add(media['url'])
                        
                        media_list.append({
                            'url': media['url'],
                            'preview': media['url'] if not media['is_video'] else '',
                            'ext': 'mp4' if media['is_video'] else 'jpg',
                            'is_image': not media['is_video'],
                            'title': f"story_{username}_{i}"
                        })
                    
                    if media_list:
                        logger.info(f"Found {len(media_list)} media items via proxy service")
                        return media_list, None
                
            except Exception as e:
                logger.error(f"Proxy service error: {str(e)}")
                continue
        
        # Try a fallback scraping approach
        try:
            logger.info("Attempting direct scraping fallback method")
            
            story_url = f"https://www.instagram.com/stories/{username}/{story_id}/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml',
                'Referer': 'https://www.instagram.com/',
                # Add some cookies to make it look more like a real browser
                'Cookie': 'ig_did=SOME_RANDOM_VALUE; csrftoken=SOME_RANDOM_VALUE; mid=SOME_RANDOM_VALUE;'
            }
            
            session = requests.Session()
            session.max_redirects = 5  # Limit to 5 redirects
            response = session.get(
                story_url,
                headers=headers,
                timeout=15,
                allow_redirects=True
            )
            
            # Check if we were redirected to login page
            if "login" in response.url.lower():
                logger.warning("Redirected to login page")
                
                # Try with a more sophisticated approach to bypass login requirement
                # This adds additional headers and cookies that might help bypass restrictions
                enhanced_headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'TE': 'trailers',
                    'Cookie': f'sessionid=RANDOM{int(time.time())}; ig_did=RANDOM{int(time.time())}; mid=RANDOM{int(time.time())}'
                }
                
                time.sleep(random.uniform(1.0, 2.0))
                
                # Try accessing the profile page first which might set some cookies
                profile_url = f"https://www.instagram.com/{username}/"
                session.get(profile_url, headers=enhanced_headers, timeout=15)
                
                # Now try the story URL again
                response = session.get(story_url, headers=enhanced_headers, timeout=15)
                
                if "login" in response.url.lower():
                    logger.warning("Still redirected to login page after enhanced attempt")
                    # We might need to try a different approach or use authentication
            
            # Try to extract media URLs from HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Look for media in meta tags and scripts
            media_urls = []
            for meta in soup.find_all('meta'):
                if meta.get('property') in ('og:image', 'og:video:secure_url', 'og:video'):
                    media_urls.append({
                        'url': meta.get('content'),
                        'is_video': 'video' in meta.get('property', '')
                    })
            
            # Also look in the page's JavaScript for media URLs
            for script in soup.find_all('script'):
                script_text = script.string if script.string else ''
                
                # Look for image URLs
                img_matches = re.findall(r'https://[^"\']+\.cdninstagram\.com/[^"\']+', script_text)
                for match in img_matches:
                    if '.jpg' in match or '.webp' in match:
                        media_urls.append({
                            'url': match,
                            'is_video': False
                        })
                
                # Look for video URLs
                video_matches = re.findall(r'https://[^"\']+\.cdninstagram\.com/[^"\']+\.mp4[^"\']*', script_text)
                for match in video_matches:
                    media_urls.append({
                        'url': match,
                        'is_video': True
                    })
                    
                # Also look for JSON data that might contain media URLs
                json_pattern = r'\{.*?"url"\s*:\s*"(https://[^"]+\.cdninstagram\.com/[^"]+)".*?\}'
                json_matches = re.findall(json_pattern, script_text)
                for match in json_matches:
                    is_video = '.mp4' in match
                    media_urls.append({
                        'url': match,
                        'is_video': is_video
                    })
            
            # If we found media URLs, return them
            if media_urls:
                media_list = []
                seen_urls = set()  # To avoid duplicates
                
                for i, media in enumerate(media_urls[:5]):  # Limit to 5
                    if media['url'] in seen_urls:
                        continue
                        
                    seen_urls.add(media['url'])
                    
                    media_list.append({
                        'url': media['url'],
                        'preview': media['url'] if not media['is_video'] else '',
                        'ext': 'mp4' if media['is_video'] else 'jpg',
                        'is_image': not media['is_video'],
                        'title': f"story_{username}_{i}"
                    })
                
                logger.info(f"Found {len(media_list)} media items via direct scraping")
                return media_list, None
        
        except requests.exceptions.TooManyRedirects:
            logger.warning("Too many redirects in fallback scraping method")
        except Exception as e:
            logger.error(f"Fallback method error: {str(e)}")
        
        return None, "Could not access or find story content - Instagram may require authentication for this content"
    
    except Exception as e:
        logger.error(f"Story download error: {str(e)}")
        return None, f"Story download error: {str(e)}"
    
@app.route('/download_story', methods=['POST'])
def download_story():
    """Special route for downloading stories with optional authentication"""
    url = request.form.get('url')
    username = request.form.get('username')  # Optional Instagram username
    password = request.form.get('password')  # Optional Instagram password
    
    if not url or not is_instagram_url(url) or '/stories/' not in url:
        return jsonify({'error': 'Invalid story URL'}), 400
    
    # First try without authentication
    media_info, error = download_story_updated(url)
    
    # If failed and credentials were provided, try with authentication
    if (not media_info or error) and username and password:
        try:
            # Create a temporary instaloader instance with credentials
            L_temp = instaloader.Instaloader(
                quiet=True,
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False
            )
            
            # Try to login
            logger.info(f"Attempting to login as {username}")
            L_temp.login(username, password)
            
            # Extract username and story ID from URL
            match = re.search(r'instagram\.com/stories/([^/]+)/([^/]+)', url)
            if not match:
                return jsonify({'error': 'Invalid story URL format'}), 400
                
            profile_username = match.group(1)
            story_id = match.group(2)
            
            # Get the profile
            profile = instaloader.Profile.from_username(L_temp.context, profile_username)
            
            # Try to download stories
            media_list = []
            
            # Fetch stories of this profile
            for story in L_temp.get_stories([profile.userid]):
                for item in story.get_items():
                    # Check if this is the story we want
                    if str(item.mediaid) == story_id or str(story_id) in str(item.url):
                        if item.is_video:
                            media_list.append({
                                'url': item.video_url,
                                'preview': item.url,
                                'ext': 'mp4',
                                'is_image': False,
                                'title': f"story_{profile_username}_{item.mediaid}"
                            })
                        else:
                            media_list.append({
                                'url': item.url,
                                'preview': item.url,
                                'ext': 'jpg',
                                'is_image': True,
                                'title': f"story_{profile_username}_{item.mediaid}"
                            })
            
            if media_list:
                # Add story-specific format information
                for media in media_list:
                    media['format'] = 'reel'  # Stories use the 9:16 format like reels
                
                return jsonify({
                    'success': True,
                    'media_info': media_list,
                    'content_type': 'story'
                })
                
        except instaloader.exceptions.LoginRequiredException:
            logger.error("Login failed - invalid credentials")
            return jsonify({'error': 'Invalid Instagram credentials'}), 401
        except Exception as e:
            logger.error(f"Authenticated story download error: {str(e)}")
            return jsonify({'error': f"Authentication error: {str(e)}"}), 500
    
    # If we got here, either we succeeded without auth or failed with auth
    if media_info:
        # Add story-specific format information
        for media in media_info:
            media['format'] = 'reel'  # Stories use the 9:16 format like reels
        
        return jsonify({
            'success': True,
            'media_info': media_info,
            'content_type': 'story'
        })
    else:
        return jsonify({'error': error or 'Unknown error downloading story'}), 500

def download_media(url):
    try:
        # Extract shortcode from URL
        shortcode = re.search(r'(?:reel|reels|p|stories)/([a-zA-Z0-9-_]+)', url)
        if not shortcode:
            return None, "Invalid URL format - couldn't extract content ID"
        
        shortcode = shortcode.group(1)
        logger.info(f"Downloading media with shortcode {shortcode}")
        
        # Check if it's a story
        is_story = '/stories/' in url
        
        if is_story:
            # Use updated story downloader
            return download_story_updated(url)
        
        try:
            # For regular posts/reels, try the standard instaloader method first
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            
            media_list = []
            
            if post.typename == 'GraphImage':
                media_list.append({
                    'url': post.url,
                    'preview': post.url,
                    'ext': 'jpg',
                    'is_image': True,
                    'title': f"insta_{shortcode}"
                })
            elif post.typename == 'GraphVideo':
                media_list.append({
                    'url': post.video_url,
                    'preview': post.url,  # Thumbnail URL
                    'ext': 'mp4',
                    'is_image': False,
                    'title': f"insta_{shortcode}"
                })
            elif post.typename == 'GraphSidecar':
                for node in post.get_sidecar_nodes():
                    if node.is_video:
                        media_list.append({
                            'url': node.video_url,
                            'preview': node.display_url,
                            'ext': 'mp4',
                            'is_image': False,
                            'title': f"insta_{shortcode}_{len(media_list)}"
                        })
                    else:
                        media_list.append({
                            'url': node.display_url,
                            'preview': node.display_url,
                            'ext': 'jpg',
                            'is_image': True,
                            'title': f"insta_{shortcode}_{len(media_list)}"
                        })
            
            # Verify and fix preview URLs
            for media in media_list:
                if not media['preview'].startswith(('http:', 'https:')):
                    media['preview'] = f"https://instagram.com{media['preview']}"
            
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

@app.route('/preview')
def serve_preview():
    """Proxy route to serve preview images"""
    url = request.args.get('url')
    if not url:
        return "", 404
    
    img = get_media_with_proxy(url)
    if img:
        return send_file(img, mimetype='image/jpeg')
    return "", 404

def fallback_download(url):
    """Alternative method using requests"""
    try:
        logger.info(f"Using fallback download method for {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Referer': 'https://www.instagram.com/',
            'Cookie': ''  # Empty cookie - we're not logged in
        }
        
        session = requests.Session()
        session.max_redirects = 5  # Limit redirects to 5
        response = session.get(
            url, 
            headers=headers, 
            timeout=10,
            allow_redirects=True
        )
        
        # Check if we were redirected to login page
        if "login" in response.url.lower():
            logger.warning("Redirected to login page")
            return None, "Instagram requires login to view this content"
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find media in meta tags
        media_url = None
        preview_url = None
        is_video = False
        
        for meta in soup.find_all('meta'):
            if meta.get('property') == 'og:video':
                media_url = meta.get('content')
                is_video = True
                break
            elif meta.get('property') == 'og:image' and not media_url:
                media_url = meta.get('content')
        
        if not media_url:
            # Try to find in JSON data
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if 'video' in data:
                        media_url = data['video']['contentUrl']
                        preview_url = data['video']['thumbnailUrl']
                        is_video = True
                        break
                    elif 'image' in data:
                        if isinstance(data['image'], list) and len(data['image']) > 0:
                            media_url = data['image'][0].get('url', '')
                        else:
                            media_url = data['image'].get('url', '')
                        break
                except (json.JSONDecodeError, AttributeError):
                    pass
        
        if not media_url:
            # Last attempt - look for embeded videos or images
            for script in soup.find_all('script'):
                if script.string and 'window.__additionalDataLoaded' in script.string:
                    # Try to extract JSON
                    json_match = re.search(r'window\.__additionalDataLoaded\([^,]+,\s*({.+})\);', script.string)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            
                            # Navigate the complex Instagram JSON structure
                            if 'graphql' in data:
                                post_data = data['graphql'].get('shortcode_media', {})
                                
                                if post_data.get('is_video'):
                                    media_url = post_data.get('video_url')
                                    preview_url = post_data.get('display_url')
                                    is_video = True
                                else:
                                    media_url = post_data.get('display_url')
                                    
                                # Handle carousel/multiple images
                                if not media_url and 'edge_sidecar_to_children' in post_data:
                                    edges = post_data['edge_sidecar_to_children'].get('edges', [])
                                    if edges and len(edges) > 0:
                                        first_node = edges[0]['node']
                                        if first_node.get('is_video'):
                                            media_url = first_node.get('video_url')
                                            preview_url = first_node.get('display_url')
                                            is_video = True
                                        else:
                                            media_url = first_node.get('display_url')
                            
                            # Also check items array used in some responses
                            elif 'items' in data and len(data['items']) > 0:
                                item = data['items'][0]
                                if item.get('video_versions') and len(item['video_versions']) > 0:
                                    media_url = item['video_versions'][0]['url']
                                    is_video = True
                                    if 'image_versions2' in item and 'candidates' in item['image_versions2']:
                                        preview_url = item['image_versions2']['candidates'][0]['url']
                                elif 'image_versions2' in item and 'candidates' in item['image_versions2']:
                                    media_url = item['image_versions2']['candidates'][0]['url']
                            
                        except json.JSONDecodeError:
                            pass
        
        if not media_url:
            # Try searching for any video or image URLs in the HTML
            video_urls = re.findall(r'https://[^"\'\s]+\.cdninstagram\.com/[^"\'\s]+\.mp4[^"\'\s]*', response.text)
            if video_urls:
                media_url = video_urls[0]
                is_video = True
            else:
                image_urls = re.findall(r'https://[^"\'\s]+\.cdninstagram\.com/[^"\'\s]+\.jpg[^"\'\s]*', response.text)
                if image_urls:
                    media_url = image_urls[0]
        
        if not media_url:
            return None, "No media found in this post"
        
        if not preview_url:
            preview_url = media_url if not is_video else None
        
        return [{
            'url': media_url,
            'preview': preview_url or media_url,
            'title': 'instagram_media',
            'ext': 'mp4' if is_video else 'jpg',
            'is_image': not is_video
        }], None
        
    except requests.exceptions.TooManyRedirects as e:
        logger.error(f"Too many redirects: {str(e)}")
        return None, "Error: Too many redirects. Instagram may be blocking access."
    except Exception as e:
        logger.error(f"Fallback download error: {str(e)}")
        return None, f"Error: {str(e)}"

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        if not url or not is_instagram_url(url):
            return render_template('index.html', error="Please enter a valid Instagram URL")
        
        content_type = get_content_type(url)
        if not content_type:
            return render_template('index.html', error="Unsupported Instagram URL type")
        
        # Special handling for stories
        if content_type == 'story':
            media_info, error = download_story_updated(url)
            if error:
                # If story-specific methods fail, try the general method
                media_info, error = download_media(url)
                if error:
                    return render_template('index.html', error=f"Failed to download story: {error}")
        else:
            # Regular posts and reels
            media_info, error = download_media(url)
            if error:
                return render_template('index.html', error=error)
        
        # Add format information for proper display
        for media in media_info:
            if content_type == 'story':
                media['format'] = 'reel'  # Stories use the 9:16 format like reels
            elif content_type == 'reel':
                media['format'] = 'reel'
            elif content_type == 'post':
                # Default to square, template will adjust based on actual dimensions
                media['format'] = 'square'
        
        return render_template('index.html', 
                            success=True,
                            url=url,
                            media_info=media_info,
                            content_type=content_type)
    
    return render_template('index.html')

# Modified download route to better handle story content
@app.route('/download', methods=['POST'])
def download():
    url = request.form.get('url')
    media_url = request.form.get('media_url')
    ext = request.form.get('ext', 'mp4')
    title = request.form.get('title', 'instagram_media')
    is_image = request.form.get('is_image', 'false').lower() == 'true'
    
    if not url or not is_instagram_url(url) or not media_url:
        return jsonify({'error': 'Invalid request'}), 400
    
    try:
        # For stories, we might need to add additional headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Referer': 'https://www.instagram.com/'
        }
        
        # Special handling for Instagram story URLs, which might have short expiration times
        if '/stories/' in url:
            # Try to refresh the media URL if it's from a story
            content_type = get_content_type(url)
            if content_type == 'story':
                media_info, error = download_story_updated(url)
                if media_info and not error:
                    # Use the freshest URL
                    for item in media_info:
                        if (is_image and item['is_image']) or (not is_image and not item['is_image']):
                            media_url = item['url']
                            break
        
        # Set max redirects to avoid redirection loops
        session = requests.Session()
        session.max_redirects = 5  # Limit to 5 redirects
        response = session.get(
            media_url, 
            stream=True, 
            timeout=15, 
            headers=headers,
            allow_redirects=True
        )
        response.raise_for_status()
        
        mem_file = BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            mem_file.write(chunk)
        mem_file.seek(0)
        
        if is_image or ext in ('jpg', 'jpeg', 'png', 'webp'):
            try:
                img = Image.open(mem_file)
                output = BytesIO()
                img.convert('RGB').save(output, 'JPEG', quality=95)
                output.seek(0)
                ext = 'jpg'
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
            mimetype='video/mp4' if ext == 'mp4' else 'image/jpeg'
        )
        
    except requests.exceptions.TooManyRedirects:
        logger.error(f"Too many redirects when downloading {media_url}")
        return jsonify({'error': "Download failed: Too many redirects. Try refreshing the page."}), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Download request error: {str(e)}")
        return jsonify({'error': f"Download failed: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Processing error: {str(e)}")
        return jsonify({'error': f"Processing error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=5000)