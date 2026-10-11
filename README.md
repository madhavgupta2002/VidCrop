# VidCrop

Local app to drop videos on a canvas, move/scale/crop them, and export one video with ffmpeg.

    python3 -I server.py [port]      # default http://localhost:8767

Needs `ffmpeg`/`ffprobe` and Python 3. Videos in `VIDCROP_VIDEOS` (default: the whiteboardf `videos` folder)
named `face.mp4`, `whiteboard.mp4`, `date.mp4` load automatically; drop or "Add videos…" for others.
