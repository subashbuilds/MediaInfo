

<h2 align="center">Telegram MediaInfoBot</h2>
</div>

<p align="center">
<img src="https://img.shields.io/github/stars/subashbuilds/MediaInfo">
<img src="https://img.shields.io/github/forks/subashbuilds/MediaInfo">
<img src="https://img.shields.io/badge/License-MIT-green.svg">
<img src="https://www.repostatus.org/badges/latest/active.svg">
</p>

----

<b> This Telegram Bot can perform various tasks with media files, such as :- </b>

- **Automatic action menu** — just send any media file (video / audio / document / voice note) or a direct download / Google Drive link, and the bot instantly replies with an inline menu: 🎬 Mediainfo · 📄 Raw info · 📸 Screenshots · 🎵 Spectrogram · ✂️ Sample. **No slash commands needed.**
- Generating **detailed Mediainfo** from Google Drive links, direct download links, and Telegram files — full `mediainfo -f` output (all fields: colour primaries, HDR metadata, stream sizes, languages, tags) posted to a Telegraph page, plus a rich summary (container, duration, resolution, HDR, codecs, audio tracks, subtitles) directly in the chat.
- Creating screenshots from Google Drive links, direct download links, and Telegram files, and posting them on slow.pics
- Generating custom-duration video samples (1 / 3 / 5 min) cut from a **random position** (~15–40% into the video) instead of the beginning.
- Creating **spectrograms in the classic SoX style** (wide 2400px image, Kaiser window, title + track info) — for **video files the bot generates one spectrogram per audio track**, each captioned with codec, bitrate (192 kbps / 640 kbps …), channels, sample rate and language. Each track is analysed in mono so the image is a **single graph** (no double pane). Voice notes and nameless audio files are supported too.
- **Everything works via partial downloads** — the bot streams only the head of the file (25 → 75 → 200 MB stages until the media parses) or uses HTTP Range requests for links, so it **never downloads the whole file**.
- Pasting text to the katb.in website.


____

<h2 align="center"> Screenshots </h2> 

![](https://graph.org/file/3f9346b6c369f4222cadb.jpg) |
|----------------------------------------------------------|

![](https://graph.org//file/499ec614796ce14e4cd3c.png)
|----------------------------------------------------------|

| ![](https://graph.org/file/8bcc663207262b45b0e8a.jpg) | ![](https://graph.org/file/9a0d26cb23963d96d28d6.jpg)|
|--------------------------------------------------------|--------------------------------------------------------|

<p align="center"><b>Exmaple Links<br>( <a href="https://mediainfo.deta.dev/ohROIsS5">Mediainfo</a> • <a href="https://slow.pics/c/PiZz4LuS">Screenshot</a> • <a href="https://graph.org//file/499ec614796ce14e4cd3c.png">Audiograph</a> )</b></p>

------

<h2 align="center"> Automatic Menu (no commands required) </h2>

Simply send / forward any media file or paste a link in the chat — the bot replies with an inline menu:

| Button | What it does |
|--------|--------------|
| 🎬 **Mediainfo** | Detailed mediainfo → Telegraph page + summary in chat |
| 📄 **Raw info** | Raw mediainfo text sent as a document |
| 📸 **Screenshots** | 10 screenshots uploaded to slow.pics |
| 🎵 **Spectrogram** | One single-graph spectrogram per audio track, with bitrate |
| ✂️ **Sample** | Pick 1 / 3 / 5 minutes → sample video from a random position |

Only the person who sent the media (or a sudo user) can press the buttons.

<h2 align="center"> Bot Commands and Usage</h2>

- `/m or /mediainfo  ` - Type link after command or Reply to telegram file to generate Mediainfo.
- `/ss or /screenshot` - Type link after command or Reply to telegram video file to generate screenshots.
- `/sample`  or `/trim` - Type link after command or Reply to telegram video file to generate sample video.
- `/spek` or `/sox`  - Reply to Telegram file (audio **or video**) to generate a spectrogram for every audio track.
- `/paste` - paste your text in katb.in website. 

 <b> Additional Flags for screenshot and mediainfo command :- </b>

**--count=10** *[ Number of screenshots. Default 10, Max 20 ]*, 
**--fps=10**  *[ Difference between two consecutive screenshots in seconds. Default 5, Max 15 ]*.
**--time=01:20:10**  *[ Time from where the screenshots should be taken in HH:MM:SS format ]*,
**--hdr**  *[ For HDR Videos.]*

**--r** *[ For raw Mediainfo in document format. ]*

 (Few more commands `/start` `/help` `/ping` `/update` `/logs`)

> The bot also registers its command list with Telegram on startup, so the `/` command menu appears automatically in your chat.

<h2 align="center"> How it works </h2>

- **Telegram files** are downloaded progressively: the bot fetches the first 25 MB, tries to parse them with ffprobe/MediaInfo, and only downloads more (75 MB → 200 MB) if needed. Small audio files are fetched fully so spectrograms cover the whole track.
- **Links / Google Drive** are read with HTTP Range requests (`bytes=0-N`) using the same progressive stages — authenticated with your Google token for Drive links.
- The raw text output of `mediainfo -f` (all available fields) is cleaned up and posted to Telegraph; the chat reply shows a compact summary built from the MediaInfo JSON (duration, resolution, fps, HDR, per-track codecs and languages).
- Spectrograms are rendered with SoX (`-x 2400 -y 900 -z 120 -w Kaiser`), one per audio track, extracted as **mono** PCM with `ffmpeg -map 0:a:N -ac 1` (mono keeps SoX to a single graph instead of one pane per channel).

 _____
 
<h2 align="center"> Bot Deployment </h2>
 
<b> Install and update Dependencies 

```sh
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3-pip
sudo pip3 install -U pip
sudo apt-get install -y --no-install-recommends mediainfo ffmpeg
sudo apt-get install libsox-fmt-mp3
sudo apt-get install sox 
```
clone the repository and install the requirements

```sh
git clone https://github.com/sanjit-sinha/Tg-MediaInfoBot
cd Tg-MediaInfoBot
pip3 install -U -r requirements.txt
```

Now edit the config vars by typing `nano config.env` and save it by pressing <kbd>ctrl</kbd>+<kbd>x</kbd>.
<br>

<details>
<summary><strong>Getting Google OAuth API credential.json file and token.json (important)</strong></summary>
<br>
<ul>
<li>

**NOTES**
- You need credentials.json and token.json in root folder for bot to work.
- Old authentication changed, now we can't use bot or replit to generate token.json. You need OS with a local browser. For example `Termux`.
- You can ONLY open the generated link from `token_generator.py` in local browser.

**STEPS**
- Visit the [Google Cloud Console](https://console.developers.google.com/apis/credentials)
- Go to the OAuth Consent tab, fill it, and save.
- Go to the Credentials tab and click Create Credentials -> OAuth Client ID
- Choose Desktop and Create.
- Publish your OAuth consent screen App to prevent **token.json** from expire
- Use the download button to download your credentials.
- Rename that file into credentials.json and move that file to the root of repo.
- Visit [Google API page](https://console.developers.google.com/apis/library)
- Search for Google Drive Api and enable it in Google Cloud Console 
- Finally, run the script from inside of repository 
<br>
 
```sh
pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib
python3 token_generator.py
```
 </ul>
</details>

Now you can start the bot by simply typing `bash start` or `python3 -m TelegramBot`

> **The bot will stop working once you logout from the server. You can run the bot 24*7 in the server by using tmux.**
> ```sh
> sudo apt install tmux -y
> tmux
> bash start
> ```

Now the bot will run 24*7 even if you logout from the server.
<br >

> **Note** You can also Deploy Bot using Docker
> ```sh
> docker build . -t mediainfobot 
> docker run mediainfobot
>```

_____

  
<h2 align="center">Copyright and License</h1>
<br>
<img src="https://telegra.ph/file/b5850b957f081cfe5f0a6.png" align="right" width="110">

* Licensed under the terms of the [The MIT License](https://github.com/subashbuilds/MediaInfo/blob/main/LICENSE)


-------

  

