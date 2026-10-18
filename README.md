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

Pick **Custom nodes** under *Compositing* before you render. When frames are done,
press **Open compositor** on the render: it opens a node editor like Blender's, with
a live preview next to it that updates as you change anything.

- **Shift+A** (or *Add node*) adds a node; type to search. After Shift+A the node
  follows the mouse until you click, and dropping it on a wire puts it in between.
- Drag from an output socket to an input socket to connect them; drag a wire off an
  input to move or remove it. Drag the background to pan, scroll to zoom, **Home** to
  frame all nodes.
- **X**/Delete removes the selected nodes (the wire is kept going past them),
  **M** mutes, **Shift+D** duplicates, **Ctrl+Z**/**Ctrl+Shift+Z** undo and redo.
- **Ctrl+Shift+click** a node to see it through a Viewer node in the preview.
- Number fields work like Blender's: drag sideways to change (Shift for fine steps),
  click to type a value.
- **Apply to render** composites every frame at full size and remakes the MP4.

| Group | Nodes |
| --- | --- |
| Input / output | Render Result, RGB, Value, Composite, Viewer |
| Color | Exposure, Brightness/Contrast, Hue/Saturation/Value, Color Balance, Color Correction, Mix, Alpha Over, Posterize, Invert Color, RGB to BW |
| Filter | Glare, Blur, Denoise, Filter (sharpen, soften, edges…), Kuwahara, Pixelate, Vignette |
| Mask | Ellipse Mask, Box Mask, Dilate/Erode |
| Converter | Math |
| Distort | Lens Distortion, Transform, Rotate, Flip |

How it works: with custom nodes the frames are first rendered as linear EXR. The
preview keeps one Blender running that composites a smaller copy of the frame with
your nodes (a few hundredths of a second each), so nothing is ever rendered again.
Custom nodes replace the compositing saved in the `.blend`; pick *As in file* to use
the file's own nodes. Glare's *Sun Beams* type needs Blender 5.

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
app/compositing.py     The compositing nodes on offer, and the check for graphs from the page
app/preview.py         Keeps one Blender running for the compositor's live preview
app/blender_nodes.py   Runs inside Blender: builds a compositing graph as real nodes
app/blender_composite.py  Runs inside Blender: composites every rendered frame at full size
app/blender_preview.py    Runs inside Blender: answers preview requests from preview.py
app/templates/         index.html (upload + renders), compositor.html and login.html
app/static/            app.js, compositor.js and their CSS
```

### Run it on your own machine

```
pip install -r requirements.txt
cd app
BLENDER_BIN=/path/to/blender ACCESS_KEY=choose-a-key python server.py
```

Then open http://localhost:5000/login?key=choose-a-key.
