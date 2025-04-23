import os
from flask import Flask, render_template, request, jsonify, send_from_directory, abort, Response, stream_with_context
import yt_dlp
import uuid
import re
import json
import time
import threading
from collections import defaultdict

app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 * 1024  # 50GB limit

# Ensure download folder exists
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

# Global dictionary to track download progress
download_progress = defaultdict(lambda: {"status": "waiting", "progress": 0, "speed": "0 KiB/s", "eta": "00:00", "filename": ""})
download_lock = threading.Lock()

def sanitize_filename(filename):
    """Sanitize the filename to remove invalid characters."""
    return re.sub(r'[\\/*?:"<>|]', "", filename)

def progress_hook(d):
    """Progress hook for yt-dlp to track download progress."""
    download_id = d.get('info_dict', {}).get('_download_id', 'unknown')
    
    with download_lock:
        if d['status'] == 'downloading':
            # Convert percent string to numeric value
            percent_str = d.get('_percent_str', '0%').strip('%')
            try:
                percent = float(percent_str)
            except ValueError:
                percent = 0
            
            # Calculate file size in MiB
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            total_bytes_mib = total_bytes / (1024 * 1024)
            filesize = f"{total_bytes_mib:.2f} MiB"
            
            download_progress[download_id] = {
                'status': 'downloading',
                'progress': percent,  # Always store as a number
                'speed': d.get('_speed_str', '0 KiB/s'),
                'eta': d.get('_eta_str', '00:00'),
                'filesize': filesize,
                'filename': os.path.basename(d.get('filename', '')) if 'filename' in d else ''
            }
            
            # Terminal output for debugging
            print(f"[download] {percent:.1f}% of {filesize} at {d.get('_speed_str', '0 KiB/s')} ETA {d.get('_eta_str', '00:00')}")
            
def get_video_info(url):
    """Fetch available formats for a YouTube video."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Extract all available formats
            formats = info.get('formats', [])
            
            # Get best format for each resolution (video + audio)
            video_formats = []
            # Also include video-only formats that can be combined with audio
            video_only_formats = []
            audio_formats = []
            
            for f in formats:
                # Video with audio
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                    resolution = f.get('height', 0)
                    if resolution:
                        video_formats.append({
                            'format_id': f['format_id'],
                            'resolution': f'{resolution}p',
                            'ext': f.get('ext', 'mp4'),
                            'filesize': f.get('filesize_approx', f.get('filesize', 0)),
                            'note': f.get('format_note', ''),
                            'combined': True  # Video+audio in single stream
                        })
                
                # Video only (can be combined with audio)
                elif f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                    resolution = f.get('height', 0)
                    if resolution:
                        video_only_formats.append({
                            'format_id': f['format_id'],
                            'resolution': f'{resolution}p',
                            'ext': f.get('ext', 'mp4'),
                            'filesize': f.get('filesize_approx', f.get('filesize', 0)),
                            'note': f.get('format_note', ''),
                            'combined': False  # Needs separate audio
                        })
                
                # Audio only
                elif f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    audio_formats.append({
                        'format_id': f['format_id'],
                        'ext': f.get('ext', 'mp3'),
                        'filesize': f.get('filesize_approx', f.get('filesize', 0)),
                        'note': f.get('format_note', '')
                    })
            
            # Create combined format options for video-only formats
            for vf in video_only_formats:
                # Find best audio format to pair with
                best_audio = None
                for af in audio_formats:
                    if not best_audio or af.get('filesize', 0) > best_audio.get('filesize', 0):
                        best_audio = af
                
                if best_audio:
                    video_formats.append({
                        'format_id': f"{vf['format_id']}+{best_audio['format_id']}",
                        'resolution': vf['resolution'],
                        'ext': 'mp4',
                        'filesize': (vf.get('filesize', 0) + best_audio.get('filesize', 0)),
                        'note': vf['note'] + ' (with audio)',
                        'combined': False  # Combined format
                    })
            
            # Remove duplicate resolutions and sort
            unique_video_formats = {}
            for vf in video_formats:
                if vf['resolution'] not in unique_video_formats:
                    unique_video_formats[vf['resolution']] = vf
                elif vf['filesize'] > unique_video_formats[vf['resolution']]['filesize']:
                    unique_video_formats[vf['resolution']] = vf
            
            sorted_video_formats = sorted(
                unique_video_formats.values(),
                key=lambda x: int(x['resolution'].replace('p', '')),
                reverse=True
            )
            
            print(info.get('duration', 0))
            seconds = info.get('duration', 0)
            
            if seconds < 0:
                return "Invalid input: Seconds cannot be negative"

            hours = seconds // 3600
            remaining_seconds = seconds % 3600
            minutes = remaining_seconds // 60
            seconds = remaining_seconds % 60

            
            
            if hours == 0:
                time = f"{minutes:02d}:{seconds:02d}"
            elif minutes == 0:
                time = f"{seconds:02d}"
            else:
                time =  f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            # Prepare response
            result = {
                'title': info.get('title', 'Untitled'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': time,
                'video_formats': sorted_video_formats,
                'audio_formats': audio_formats,
                'error': None
            }
            
            return result
    
    except Exception as e:
        return {'error': str(e)}
    
# Add this to the download_progress_sse function before returning Response:
@app.route('/debug-progress/<download_id>')
def debug_progress(download_id):
    """Debug endpoint to view current progress data."""
    with download_lock:
        progress_data = download_progress.get(download_id, {})
    return jsonify(progress_data)

# Add this to your main app section:
@app.after_request
def after_request(response):
    """Add CORS headers."""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        if not url:
            return render_template('index.html', error='Please enter a YouTube URL')
        
        video_info = get_video_info(url)
        if video_info.get('error'):
            return render_template('index.html', error=video_info['error'])
        
        return render_template('index.html', 
                            video_info=video_info, 
                            url=url,
                            title=video_info['title'],
                            thumbnail=video_info['thumbnail'])
    
    return render_template('index.html')

@app.route('/download', methods=['GET', 'POST'])
def download():
    if request.method == 'POST':
        data = request.json
        url = data.get('url')
        format_id = data.get('format_id')
        download_type = data.get('type')
    else:  # GET method
        url = request.args.get('url')
        format_id = request.args.get('format')
        download_type = request.args.get('type')
    
    if not url or not format_id:
        return jsonify({'error': 'Missing required parameters'}), 400
    
    # Generate a unique ID for this download
    download_id = str(uuid.uuid4())
    
    # Initialize progress tracking for this download
    with download_lock:
        download_progress[download_id] = {
            "status": "starting", 
            "progress": 0, 
            "speed": "0 KiB/s", 
            "eta": "00:00",
            "filename": ""
        }
    
    # Generate a unique filename
    random_id = str(uuid.uuid4())[:8]
    output_template = f"{app.config['DOWNLOAD_FOLDER']}/%(title)s_{random_id}.%(ext)s"
    
    ydl_opts = {
        'format': format_id,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'info_dict': {'_download_id': download_id}  # Pass download ID to the hooks
    }
    
    if download_type == 'audio':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'extractaudio': True,  # Only keep the audio
        })
    else:
        # For video downloads, ensure we merge formats if needed
        if '+' in format_id:
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]
            
    # Start download in a separate thread
    def download_task():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                if download_type == 'audio':
                    # Ensure the filename ends with .mp3
                    filename = os.path.splitext(filename)[0] + '.mp3'
                elif download_type == 'video' and '+' in format_id:
                    filename = filename.replace('.webm', '.mp4').replace('.mkv', '.mp4')
                
                # Get the actual downloaded filename
                actual_filename = os.path.basename(filename)
                
                # Update progress to complete
                with download_lock:
                    download_progress[download_id]['status'] = 'complete'
                    download_progress[download_id]['filename'] = actual_filename
                    
        except Exception as e:
            with download_lock:
                download_progress[download_id]['status'] = 'error'
                download_progress[download_id]['error'] = str(e)
    
    # Start download thread
    thread = threading.Thread(target=download_task)
    thread.daemon = True
    thread.start()
    
    # Return the download ID immediately
    return jsonify({'download_id': download_id})

@app.route('/download-progress/<download_id>')
def download_progress_sse(download_id):
    """Server-Sent Events endpoint to stream download progress."""
    def generate():
        last_progress = None
        
        # Keep connection alive until download completes or fails
        while True:
            with download_lock:
                current_progress = download_progress.get(download_id, {}).copy()
            
            # Only send updates when the progress changes
            current_value = current_progress.get('progress')
            if current_value != last_progress:
                last_progress = current_value
                yield f"data: {json.dumps(current_progress)}\n\n"
            
            # If download is complete or errored, end the stream
            if current_progress.get('status') in ['complete', 'error']:
                # Wait a moment to ensure the client receives the final status
                time.sleep(0.5)
                yield f"data: {json.dumps(current_progress)}\n\n"
                break
                
            time.sleep(0.2)  # Check for updates more frequently (200ms instead of 500ms)
    
    return Response(stream_with_context(generate()), 
                  mimetype='text/event-stream',
                  headers={'Cache-Control': 'no-cache', 
                           'Connection': 'keep-alive'})
    
@app.route('/get-file/<download_id>')
def get_download_file(download_id):
    """Get download file by download ID."""
    with download_lock:
        if download_id not in download_progress:
            return jsonify({'error': 'Download not found'}), 404
            
        progress_data = download_progress[download_id]
        if progress_data['status'] != 'complete':
            return jsonify({'error': 'Download not complete'}), 400
            
        filename = progress_data['filename']
    
    return send_from_directory(
        app.config['DOWNLOAD_FOLDER'],
        filename,
        as_attachment=True
    )

@app.route('/downloads/<filename>')
def download_file(filename):
    try:
        return send_from_directory(
            app.config['DOWNLOAD_FOLDER'],
            filename,
            as_attachment=True
        )
    except FileNotFoundError:
        abort(404)

if __name__ == '__main__':
    # For production, use waitress:
    # from waitress import serve
    # serve(app, host="0.0.0.0", port=8080)
    
    app.run(debug=True)