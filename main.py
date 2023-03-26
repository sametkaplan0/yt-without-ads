from flask import Flask, request, render_template
from pytube import YouTube, Search

from lxml import html
import requests
 
# filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["GET", "POST"])
def search():
    q = request.args.get("q")
    s = Search(q)
    
    return render_template("search.html", r=s,yt=YouTube, html=html, requests=requests)

@app.route("/v")
def vid():
    id = request.args.get("id")
    video = YouTube(f"https://www.youtube.com/watch?v={id}")
    watchvideo = video.streams.get_highest_resolution()
    return render_template("video.html", video=watchvideo)
if __name__ == '__main__':
    app.run(host="0.0.0.0", port="80",debug=True)