from bs4 import BeautifulSoup
from flask import Flask, json, render_template, request, send_file, jsonify
import requests
import tempfile
import os
import yt_dlp
from urllib.parse import urlparse
import re
from io import BytesIO
from PIL import Image
import time

app = Flask(__name__)

def is_instagram_url(url):
    parsed = urlparse(url)
    return parsed.netloc in ('www.instagram.com', 'instagram.com')

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

def download_media(url):
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        'outtmpl': '%(title)s.%(ext)s',
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                return None, "Failed to extract media info"
            
            # Handle case when video isn't available but images are
            if not info.get('url') and not info.get('entries'):
                # Try alternative method for image posts
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                response = requests.get(url, headers=headers)
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Find image in meta tags
                image_url = None
                for meta in soup.find_all('meta'):
                    if meta.get('property') == 'og:image':
                        image_url = meta.get('content')
                        break
                
                if image_url:
                    return [{
                        'url': image_url,
                        'title': 'instagram_image',
                        'ext': 'jpg',
                        'is_image': True
                    }], None
                else:
                    return None, "No video or image found in this post"
            
            # Handle carousel posts
            if 'entries' in info:
                media_urls = []
                for entry in info['entries']:
                    if entry.get('url'):
                        media_urls.append({
                            'url': entry['url'],
                            'title': entry.get('title', 'instagram_media'),
                            'ext': entry.get('ext', 'jpg' if entry.get('is_image') else 'mp4'),
                            'is_image': entry.get('is_image', False)
                        })
                return media_urls, None
            
            # Handle single media
            return [{
                'url': info['url'],
                'title': info.get('title', 'instagram_media'),
                'ext': info.get('ext', 'jpg' if info.get('is_image') else 'mp4'),
                'is_image': info.get('is_image', False)
            }], None
            
    except yt_dlp.utils.DownloadError as e:
        if "There is no video in this post" in str(e):
            # Fallback to image extraction
            return download_media_images(url)
        return None, str(e)
    except Exception as e:
        return None, str(e)

def download_media_images(url):
    """Alternative method to extract images when video isn't available"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all image URLs in the page
        image_urls = []
        for meta in soup.find_all('meta'):
            if meta.get('property') == 'og:image':
                image_urls.append(meta.get('content'))
        
        # Also check for carousel images
        for script in soup.find_all('script', type='text/javascript'):
            if 'window.__additionalDataLoaded' in script.text:
                data = re.search(r'({.*})', script.text)
                if data:
                    try:
                        json_data = json.loads(data.group(1))
                        if 'items' in json_data:
                            for item in json_data['items']:
                                if 'image_versions2' in item:
                                    image_urls.append(item['image_versions2']['candidates'][0]['url'])
                    except json.JSONDecodeError:
                        pass
        
        if not image_urls:
            return None, "No images found in this post"
        
        return [{
            'url': url,
            'title': 'instagram_image',
            'ext': 'jpg',
            'is_image': True
        } for url in image_urls], None
    except Exception as e:
        return None, str(e)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        if not url or not is_instagram_url(url):
            return render_template('index.html', error="Please enter a valid Instagram URL")
        
        content_type = get_content_type(url)
        if not content_type:
            return render_template('index.html', error="Unsupported Instagram URL type")
        
        media_info, error = download_media(url)
        if error:
            return render_template('index.html', error=error)
        
        return render_template('index.html', 
                            success=True,
                            url=url,
                            media_info=media_info,
                            content_type=content_type)
    
    return render_template('index.html')

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
        response = requests.get(media_url, stream=True, timeout=10)
        response.raise_for_status()
        
        mem_file = BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            mem_file.write(chunk)
        mem_file.seek(0)
        
        if is_image or ext in ('jpg', 'jpeg', 'png', 'webp'):
            img = Image.open(mem_file)
            output = BytesIO()
            img.convert('RGB').save(output, 'JPEG', quality=95)
            output.seek(0)
            ext = 'jpg'
            mem_file = output
        
        filename = f"{clean_filename(title)}.{ext}"
        
        return send_file(
            mem_file,
            as_attachment=True,
            download_name=filename,
            mimetype='video/mp4' if ext == 'mp4' else 'image/jpeg'
        )
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f"Download failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({'error': f"Processing error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=5000)