# Blender Render

Render Blender scenes on a free Google Colab GPU from a simple web page.
Open the notebook, run it, and you get a link to a website where you upload a
`.blend` file and download the rendered images (or an MP4).

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/DCLEGEND2026/BlenderRender/blob/main/BlenderRender.ipynb)

## How to use

1. Open `BlenderRender.ipynb` in Colab (badge above).
2. **Runtime → Change runtime type → T4 GPU**.
3. `REPO_URL` in step 1 already points at this repo. Change it only if you use your own fork.
4. **Runtime → Run all**. Step 4 prints a link like
   `https://something.trycloudflare.com/login?key=...`.
5. Open the link, drop in your file and press **Upload & render**.

## What you can upload

- A `.blend` file. Pack textures into it first (*File → External Data → Pack Resources*).
- Or a `.zip` that holds the `.blend` plus its texture folders. Keep the same layout
  as on your computer so relative paths still work. If the zip holds more than one
  `.blend`, the one closest to the top of the zip is used.

Big files are fine: the page uploads in 32 MB pieces, so the tunnel's 100 MB
request limit doesn't apply.

## Render options

| Option | Notes |
| --- | --- |
| Still image / Animation | Still renders one frame (the file's current frame by default). Animation renders the file's frame range, or the start/end you enter. |
| Engine | *As saved in the file*, Cycles, Eevee or Workbench. Use **Cycles**: Eevee needs a display and may fail on Colab. |
| Cycles device | GPU uses OptiX, falling back to CUDA, on the Colab GPU. |
| Samples, Resolution % | Leave empty to keep the values from the file. |
| Image format | PNG, JPEG or OpenEXR. |
| MP4 | Animations can also be encoded to an H.264 MP4 at the scene's frame rate. |

Each render shows live progress (frame, samples, time left), a preview of every
frame, the Blender log, and buttons to download a zip of all the frames or the MP4.
Renders run one at a time, in the order they were uploaded.

## Good to know

- Colab deletes everything when the runtime stops. Download your renders, or run
  step 5 to copy them to Google Drive.
- Free Colab stops runtimes that sit idle and limits GPU hours, so keep the tab open
  during long renders.
- Anyone with the link and key can use your GPU, so don't share it.
- Blender 5.2.2 is the default. Pick 4.5.14 LTS in step 1 if you need it. A file saved
  in a newer Blender than the one rendering it may not open correctly.

## Project layout

```
BlenderRender.ipynb    Colab notebook: installs Blender, starts the site and the tunnel
app/server.py          Flask website + JSON API (login, chunked upload, jobs, files)
app/jobs.py            Render queue: runs Blender, reads progress, makes zip / MP4
app/blender_setup.py   Runs inside Blender: applies options, turns on the GPU
app/templates/         index.html (upload + renders) and login.html
app/static/            app.js and style.css
```

### Run it on your own machine

```
pip install -r requirements.txt
cd app
BLENDER_BIN=/path/to/blender ACCESS_KEY=choose-a-key python server.py
```

Then open http://localhost:5000/login?key=choose-a-key.
