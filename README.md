# AniMux

A web-based MKV remuxing tool. Select a source and destination file, pick which audio tracks, subtitles, chapters, attachments, and tags to transfer, and AniMux calls `mkvmerge` to update the destination in-place — no re-encoding, ever.

---

<img src="screenshot.png" height="450">

## Quick Start (docker-compose)

```bash
git clone https://github.com/OtakuForLife/AniMux.git
cd AniMux
```

Edit `docker-compose.yml` and replace the volume paths:

```yaml
volumes:
  - /path/to/your/source:/source:ro        # folder with source MKV files
  - /path/to/your/destination:/destination:rw  # folder with destination MKV files
```

Then start the container:

```bash
docker compose up -d
```

Open **http://localhost:8000** in your browser.

---

## Unraid

1. In the Unraid Docker UI, click **Add Container**.
2. Set **Repository** to `ghcr.io/otakuforlife/animux:latest`
3. **Network Type:** Bridge
4. **Add Port** — container port `8000`, host port whatever you prefer (e.g. `8000`)
5. **Add Path** (read-only, source MKV files):
   - Container: `/source`
   - Host: e.g. `/mnt/user/downloads`
6. **Add Path** (read/write, destination MKV files):
   - Container: `/destination`
   - Host: e.g. `/mnt/user/media`
7. Click **Apply**.

Open **http://[unraid-ip]:[host-port]** (the host port from step 4). The app always listens on port `8000` inside the container — only the host-side mapping changes.

**Optional:** Install the template for one-click setup next time:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/animux.xml \
  https://raw.githubusercontent.com/OtakuForLife/AniMux/main/unraid-template.xml
```

After that, **AniMux** appears in the template dropdown when adding a container.

---

## How It Works

1. **Source** — browse and select the MKV you want to copy tracks *from*.
2. **Destination** — browse and select the MKV you want to update.
3. **Pick tracks** — check the audio tracks, subtitle tracks, chapters, attachments (fonts), and tags you want to transfer.
4. **Start** — AniMux runs `mkvmerge` in the background; a progress bar and live log show the status.
5. On success the destination file is atomically replaced (written to a temp file first, then swapped in — no half-written files).

---

## Supported Transfers

| Type | Notes |
|---|---|
| Audio tracks | Any codec; preserves language and title metadata |
| Subtitle tracks | SRT, ASS, PGS, and any other mkvmerge-supported format |
| Chapters | Named and ordered chapter entries |
| Attachments | Fonts and any other embedded files |
| Tags | Global and track-level tag blocks |

Video is **never** touched or re-encoded. The source video stream always stays in the destination file unchanged.
