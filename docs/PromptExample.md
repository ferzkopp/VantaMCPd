# Prompt Example

> **How to run:** Attach this file to an agent-enabled chat connected to VantaMCPd and ask it to carry out the prompt below. Use an already configured cluster and writable share. Available capabilities are discovered automatically; no node names need to be edited.

---

# The Lensing Garden

Use vanta-mcp tools to create **The Lensing Garden**: a beautiful, futuristic image of an orbital observatory built around a garden of sculpted light.

## Art Direction

Imagine an immense, elegant observatory suspended above a pale planet's curved horizon. In its central atrium, a glass sphere hangs inside a luminous Einstein ring. Delicate arcs curl around it like ribbons caught between gravity and motion. Two smaller light sculptures echo the centerpiece deeper in the atrium, and a tiny maintenance rover gives the architecture a sense of scale.

Use pearl-white ceramic, brushed titanium, dark glass, warm amber architectural lighting, and electric-cyan light sculptures. Look upward from a low three-quarter viewpoint, with strong depth and one unmistakable focal point.

Aim for premium science-fiction cinematography: convincing materials, beautiful reflections and refraction, soft shadows, and luminous detail without blown-out highlights. Keep the scene elegant and spacious, without clutter, random neon, or text overlays.

## Bring It to Life

Discover what the available cluster can do and use it to develop the image, using available resources. Ignore nodes that are unreachable. Work with existing capabilities without installing software or changing configuration; no GPU or OCR is needed.

1. **Find a scientific spark.** Search arXiv for papers on the exact phrase "gravitational lensing" in the applicable categories, falling back to a broader topic search (for example "strong lensing" or "Einstein ring") if nothing is found. Then explore a reliable web reference that helps explain the phenomenon. Let one real idea visibly influence the sculpture rather than merely decorating the story.

2. **Develop the visual language.** Find freely usable lighting and material references that lend the observatory realism. Distill the research, palette, and references into a short, coherent design brief to guide the work.

3. **Grow a garden of possibilities using a Python script.** Explore how different arrangements of gravitational lenses and background light produce rings, arcs, and intricate luminous patterns. Run different explorations in parallel across the available workers, including smaller machines where practical, so each contributes distinct possibilities. Bring their results together into a visual shortlist. Choose the most beautiful pattern for the centerpiece and two complementary patterns for the smaller sculptures. Let the computed patterns genuinely shape the artwork, while treating their transformation into a sculpture as artistic interpretation.

4. **Build the observatory around the light.** Create a ray-traced scene that gives the chosen patterns a physical presence through glass, metal, and architectural scale. Carry the generated materials between stages using shared cluster storage. Explore a few small previews, then choose the camera and lighting that best reveal the sculpture.

5. **Finish it as a cinematic still.** Render a crisp, high-resolution image and refine its color, contrast, and detail with restraint. Preserve the subtle material textures and the shape of the luminous arcs. Prioritize a compelling image over additional objects or effects.

Show me the finished image and save it to the configured share with a descriptive, unique filename, without overwriting existing files. Confirm where it was saved and briefly explain the scientific inspiration, with source links and any required asset credits. If an essential capability or share access is unavailable, explain the blocker rather than claiming completion.

---

## Example Run Summary

<img src="lensing-garden.jpg" alt="The Lensing Garden: glass spheres and cyan lensing sculptures inside an orbital observatory" width="640">

### Runtime and Usage

| Metric | Recorded result |
| --- | --- |
| Agent and model | GitHub Copilot using GPT-6.1 Sol (`copilot/gpt-6.1-sol`), medium reasoning effort |
| Run window | October 24, 2026, 00:23:44-00:49:31 UTC |
| End-to-end runtime | 25 minutes 47 seconds |
| Tool-call rounds | 92 |
| Individual tool calls | 155: 114 Vanta MCP calls and 41 editor/local calls |
| Module operation calls | 55 |
| Module operation/schema discovery calls | 19 |
| Background-job status polls | 26 |
| Durable background jobs | 10 |
| Completion-token counter | 49,013 |
| Final prompt-token count | 168,374 |

Counts cover the artwork request only, excluding this documentation follow-up. Individual tool calls are counted without parallel-batch wrappers. Token figures are the final counters stored in the chat journal: the prompt count is not a sum of input tokens across all model requests, and these figures should not be interpreted as an aggregate billed-token total or monetary cost.

### Work Performed

- **Cluster:** Five reachable nodes participated. Three 1 GB ARM workers computed aligned, binary-lens, and external-shear studies in parallel; the four-core x86 worker explored triple lenses and performed the renders. The storage node hosted the scientific corpus, shared artifacts, and supporting work. The unreachable sixth node was skipped.
- **Research:** Exact-phrase arXiv searches in `astro-ph.CO` and `astro-ph.GA` found relevant papers. [ESA/Hubble's gravitational-lensing reference](https://esahubble.org/wordbank/gravitational-lensing/) informed the visual distinction between near-aligned rings and broken arcs. Research included [joint source-and-lens inference](https://arxiv.org/abs/2511.04792) and [strong-lensing sensitivity to mass structure](https://arxiv.org/abs/2208.13796); their machine-learning methods were not reproduced.
- **Numerical exploration:** Python generated 24 candidates across four study families, using 320 x 320 grids on the ARM workers and a 440 x 440 grid on x86. A combined contact sheet guided selection of Aligned 1, Binary 2, and Shear 4.
- **Scene construction:** Computed brightness contours became translucent ribbon meshes and luminous edges around glass spheres. The centerpiece also reused the shear pattern at a steep tilt. This added depth was artistic interpretation, not a simulation of literal light trajectories.
- **Preview development:** Four ray-traced previews tested two camera angles, filled ribbons, softer materials, a continuous foreground deck, and smoked-glass balustrades. Scene syntax checks passed before rendering.
- **Final render:** POV-Ray produced a 3200 x 1800, 16-bit PNG using four CPU threads, quality 9, and antialiasing. The master took **476.120 seconds** including **469.243 seconds of tracing**, with no renderer diagnostics.
- **Finishing and validation:** A restrained contrast, saturation, brightness, and sharpening pass preserved the material detail. The finished image was inspected and displayed. The bundled Python explorer regenerated all 18 candidate contours from the three selected families exactly.
- **Constraints:** No software was installed, no configuration was changed, and neither GPU processing nor OCR was used.

### Saved Outputs

All three files were published without overwriting existing files to the share:

- `The-Lensing-Garden-20261024T003445Z-f8c4b57d-3200x1800.png` - finished image, 17,825,210 bytes; export checksum and destination size verified.
- `The-Lensing-Garden-20261024T003445Z-f8c4b57d-credits.txt` - scientific references, method notes, and asset credits.
- `The-Lensing-Garden-20261024T003445Z-f8c4b57d-sources.zip` - final scene, ribbon geometry, selected contour datasets, visual shortlist, design brief, and reusable Python explorer.

**Assets:** Powered by Poly Haven. The [Studio Small 09 HDRI](https://polyhaven.com/a/studio_small_09) and [Marble 01 material](https://polyhaven.com/a/marble_01) are CC0. Scientific explanatory reference: ESA/Hubble; no ESA/Hubble image was used.

**Recovered issues:** An initial NASA reference URL returned 404 and was replaced with ESA/Hubble. One concurrent export was refused until the share's write probe was refreshed; only the failed export was retried, sequentially. No unresolved blockers remained.