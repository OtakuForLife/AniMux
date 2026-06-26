# AniMux

A web-based MKV remuxing tool. Select a source and destination file, pick which audio tracks, subtitles, chapters, attachments, and tags to transfer, and AniMux calls `mkvmerge` to update the destination in-place — no re-encoding, ever.

---

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
2. Paste the raw URL of `unraid-template.xml` from this repo into the **Template URL** field and click **Apply**:
   ```
   https://raw.githubusercontent.com/OtakuForLife/AniMux/main/unraid-template.xml
   ```
3. Set **Source Directory** to the Unraid share that holds your source MKV files (e.g. `/mnt/user/downloads`).
4. Set **Destination Directory** to your media library share (e.g. `/mnt/user/media`).
5. Click **Apply**. The WebUI will be available at `http://[unraid-ip]:8000`.

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
