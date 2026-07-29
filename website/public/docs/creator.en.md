# QwenPaw Creator

QwenPaw Creator is an **agentic video creation platform**: hand your goals, sources, and constraints to the Agent, and it handles planning, generation, editing, and composition like a full production team. The Agent runs through the entire creation process — playing the roles of screenwriter, director, visual development artist, motion designer, and editor — breaking down your goal, producing content step by step, and pausing at every key point for your review and confirmation. You can step in with a single sentence at any time to steer the direction.

Creator can both **generate from scratch** (e.g. short dramas: script → assets → storyboard → video) and **edit existing footage into a finished film** (source understanding → timeline arrangement → subtitles/motion/transitions → composition). Whether you start from a single idea or a batch of raw footage, you can hand it all to Creator.

![QwenPaw Creator home page](/docs/images/creator/home.png)

---

## Quick start

### Open Creator

Creator is an app inside QwenPaw. Start QwenPaw, open the console (default `http://127.0.0.1:8088/`), go to **Apps** in the left navigation, and click QwenPaw Creator to enter the platform.

![Open Creator from the QwenPaw Apps page](/docs/images/creator/app-entry.png)

### Configure models

Before first use, click **Model Configuration** next to the home-page input (or follow the onboarding guide) to connect your models. Different scenarios require different models:

| Scenario                                | Required models                                                      |
| --------------------------------------- | -------------------------------------------------------------------- |
| All scenarios                           | Large language model (LLM)                                           |
| Generative creation (short drama, etc.) | Image generation, video generation, and vision-language (VLM) models |
| Editing uploaded sources                | VLM (for source understanding)                                       |
| Sources with voice tracks               | Speech recognition (ASR) model                                       |

Currently supported providers:

| Capability       | Supported providers                                                                 |
| ---------------- | ----------------------------------------------------------------------------------- |
| LLM / VLM        | OpenAI protocol, Bailian, Claude, DeepSeek, Gemini, Qianfan, Volcano Engine, custom |
| Image generation | OpenAI protocol, Bailian                                                            |
| Video generation | Bailian (wan2.x r2v, happyhorse-1.x-r2v), Volcano Engine (doubao-seedance-2.x)      |
| ASR              | Fun-ASR, Whisper                                                                    |

### Create your first project

On the home page, hand your creative intent to the Agent:

1. **Describe the goal**: e.g. "A CEO-romance short drama — fast-paced, strong dramatic conflict, with a happy ending", or "Edit a one-minute highlight reel of my cat videos";
2. **Provide sources** (optional): import videos, images, and documents via "Add file / Add folder / Add link". Once inside the project they become manageable, referenceable, and traceable project assets;
3. **Pick the creation type and specs**: Short Drama / Editing / General (talking-head, text-to-video, and other formats work well with the General type plus a goal description), then choose resolution and aspect ratio (e.g. 720P, 16:9);
4. Hit send — the Agent starts planning and takes you into the creation workbench.

---

## The creation workbench

Inside a project you'll work in the **Video Plan** workbench, where everything the Agent produces is presented and arranged in a structured way:

![Video Plan workbench](/docs/images/creator/workbench.png)

- **Creation outline**: the overall plan and film info (total duration, resolution, aspect ratio, content count);
- **Timeline**: a multi-track view of the film — clips, subtitles, motion effects, and transitions each on their own track. Drag the playhead to jump to any moment;
- **Time-point contents**: the lower-left list shows every item active at the current moment (type, time range, and summary), making it easy to confirm that footage, subtitles, and motion effects line up;
- **Detail panel**: click any segment (on the timeline or in the list) to open its details, where you can inspect and edit start time, duration, stacking order, on-screen position, opacity, and more — then click "Apply changes";
- **Asset library**: switch at the top to browse all sources and generated outputs in the project (character images, scene images, storyboard frames, video clips, etc.);
- **Creation assistant**: the right panel is your conversation with the Agent, streaming every production step in real time (ideation, delegating Specialists, generation, plan updates). Type a revision request at any moment.

### Collaborating with the Agent

The creation assistant is your companion throughout. A few practical tips:

- **Reference with @**: type `@` to pull storyboard shots, sources, and other objects into the conversation as context; the currently selected object is attached automatically;
- **Intervene anytime**: whether the Agent is planning or generating, issue a new instruction directly (e.g. "change the caption of the second clip to…") — it reads the current project state and makes the related changes;
- **Stop instantly**: running tasks can be interrupted immediately with the stop button.

---

## Two typical ways to create

### Short drama generation: zero to film

For generative creation, the Agent moves through the following steps, where each step's output feeds the next:

```
Script → Storyboard text → Asset generation (character anchor + scene base images) → Storyboard frames → Video → Composition
```

1. **Script and storyboard**: the Agent (screenwriter/director) writes the script and breaks it into structured shots (scenes, characters, actions, dialogue);
2. **Asset generation**: the visual development Specialist first creates anchor images for each character and base images for each scene — the critical prerequisite that keeps characters and scenes consistent across the whole film;
3. **Storyboard frames**: each shot's frame is generated using the asset images as reference inputs;
4. **Video generation**: reference-to-video (r2v) models generate clips shot by shot from storyboard frames and asset images;
5. **Composition**: once all clips are ready, the final film is composed automatically.

Every step's output enters the review flow for your confirmation — for example, after a character image is generated:

![Character asset image review](/docs/images/creator/asset-review.png)

### Footage editing: sources to film

For editing, just hand your raw footage to the Agent:

1. **Import and understand sources**: the Agent uses the VLM (plus ASR for voice tracks) to understand each source segment and spot highlights;
2. **Editing plan**: the Agent (editor) selects clips, arranges the timeline, and adds subtitles, motion effects, and transitions as needed;
3. **Refine**: click any segment on the timeline to adjust it yourself, or ask the Agent (e.g. "add sunray decorations to the opening");
4. **Compose**: confirm the plan and render the final film.

---

## Review and cost control

Every change the Agent makes stays under your control.

### Content review

- **Images and videos** generated or modified by the Agent enter the review queue — "Keep" to adopt, "Undo" to revert, item by item or all at once;
- **Text changes** from Agent interventions are shown as red/green diffs; click "View" to jump to the original location for comparison;
- Review items jump precisely to their generation context (character image → asset detail, storyboard frame → shot detail, video → video generation detail);
- Content you **edit manually** is applied directly and skips review.

![Text change diff review](/docs/images/creator/text-review.png)

### Production confirmation (cost estimate)

Before calling a paid generation model (image/video), the Agent shows a **production confirmation card** listing the target, model, parameters, and prompt, along with a locally computed cost estimate. The billable task is only submitted after you click "Continue"; "Cancel" aborts the production. You can disable this confirmation in the model configuration.

![Production confirmation with cost estimate](/docs/images/creator/execution-auth.png)

> 💰 Estimates are computed locally from each model's published pricing and are for reference only; actual charges follow your provider's bill.

---

## Preview and export

- **Film preview**: click "Video Preview" in the workbench, or "Preview" on any project card in "My Projects", to play the current film directly;
- **Download**: use "Download Film" at the top right of the workbench to export the final video file.

![Film preview](/docs/images/creator/film-preview.png)

The "My Projects" page manages all your creations, sortable by update time, with creation type, aspect ratio, and resolution shown on each card:

![My Projects](/docs/images/creator/projects.png)

---

## Appendix: installation and runtime

Creator currently ships with QwenPaw as an app package. Install it while QwenPaw is offline; after a restart it appears in the Apps page:

```bash
qwenpaw plugin install /path/to/qwenpaw-creator
```

Creator relies on a few native tools (without touching your system installation): `ffmpeg` powers media processing and film composition (set `CREATOR_FFMPEG_PATH`, or it falls back to system `ffmpeg` / `imageio-ffmpeg`); `jq` powers the Agent's structured project file edits (`CREATOR_JQ_PATH` or `PATH`). With missing dependencies Creator starts in degraded mode; check `GET /api/qwenpaw-creator/health` for details.
