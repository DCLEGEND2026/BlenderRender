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
| Render engine | Cycles (default), Eevee, Workbench, or *As in file*. Use **Cycles**: Eevee needs a display and may fail on Colab. |
| Resolution %, Image format | Resolution scale; PNG, JPEG or OpenEXR. |
| Cycles: Device | GPU uses OptiX, falling back to CUDA, on the Colab GPU. |
| Cycles: Max samples | Upper limit of samples per pixel. |
| Cycles: Noise threshold | Adaptive sampling stops a pixel once it is this clean. Lower = cleaner but slower (0.01 is Blender's default, 0.1 is a quick preview). 0 turns adaptive sampling off. |
| Cycles: Min samples | Samples every pixel gets before adaptive sampling may stop it (0 = automatic). |
| Cycles: Time limit | Stop each frame after this many seconds (0 = no limit). |
| Cycles: Max light bounces | Lower is faster; less light bouncing around the scene. |
| Cycles: Denoise, Denoiser | Turn denoising on/off; OptiX (fast, on the GPU) or OpenImageDenoise. |
| Eevee: Render samples, Ray tracing | Eevee's own quality settings. |
| MP4 | Animations can also be encoded to an H.264 MP4 at the scene's frame rate. |
| Compositing | *As in file*, *Off*, or *Custom nodes* (see below). |

Any box left empty (or set to *From file*) keeps the value saved in the `.blend`.

## Compositing

Pick **Custom nodes** to build your own chain of real Blender compositor nodes on
the page: *Render result → your nodes → Output*. Add nodes from the list, set their
values, reorder them with the arrows, or untick a node to skip it.

| Group | Nodes |
| --- | --- |
| Color | Exposure, Brightness/Contrast, Hue/Saturation/Value, Color Balance, Color Correction, Mix (color), Posterize, Invert Color |
| Filter | Glare, Blur, Denoise, Filter (sharpen, soften, edges…), Kuwahara, Pixelate |
| Lens & transform | Lens Distortion, Vignette, Flip, Rotate |

With custom nodes the frames are first rendered as linear EXR, and then the nodes run
over them. So once a render is done you can press **Edit compositing** on it, change
the nodes and press **Apply to render**: only the compositor runs again (usually a
second or two per frame), not the render. The MP4 is remade too. Custom nodes replace
the compositing saved in the `.blend`; pick *As in file* to use the file's own nodes.
Glare's *Sun Beams* type needs Blender 5.

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
app/compositing.py     The compositing nodes the page offers (shared by server and Blender)
app/blender_composite.py  Runs inside Blender: adds the compositing nodes to the rendered frames
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
