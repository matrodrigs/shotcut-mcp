# Workflow and examples

[Shotcut MCP](../README.md) · [Installation](installation.md) · [Technical reference](reference.md)

## Recommended workflow

Describe the result you want in ordinary language. The MCP client receives tool-specific guidance
at runtime, so you do not need to know individual tool names or request schemas.

Give the assistant your goal, available media, destination folder or saved project, and any
constraints that matter: audience, approximate length, aspect ratio, tone, or moments to retain.
These are useful creative inputs, not a required questionnaire. Within your brief, the assistant
can choose pacing, cuts, framing, titles, and transitions, then show a preview for iteration.
It can discover installed effects and combine operations beyond the examples below.

For a new project, the assistant can choose a descriptive `.mlt` filename in your destination
folder. Routine editing choices do not require separate approval. Replacing an existing file,
restoring a backup, and exporting still follow their explicit authorization rules; an action
you already requested does not need another confirmation.

1. Begin with a readiness check for Shotcut MCP and the local media tools.
2. Provide the saved `.mlt` project and source media, then describe the result you want.
3. For cleanup work, review measurements such as silence, black frames, freezes, interlacing, or
   loudness before choosing which changes to apply.
4. For a large or sensitive edit, review the proposed changes before applying them.
5. After each visual edit batch, you receive a concise change summary and representative frames or
   a contact sheet, making it easy to request another adjustment.
6. When the edit is ready, request an export. If export was not already explicit, confirm the
   project, output, preset, range or duration, and overwrite behavior first. During rendering, you
   receive meaningful progress updates; on completion, you receive both the video and its exact
   editable project.

Let an MCP edit finish before saving the same project from the Shotcut GUI. After manual adjustments,
save in Shotcut and ask the client to inspect the project again before continuing.

## Example prompts

```text
Turn the travel clips in this folder into a lively 45-second vertical edit for friends.
Choose the strongest moments, vary the pacing, keep transitions restrained, and use only
the audio I provided. Save a new project in this destination folder and show me a preview.
```

```text
Create a 1920×1080, 30 fps Shotcut project from every video in this folder.
Put narration on A1, add 12-frame crossfades, and save it as documentary.mlt.
```

```text
Inspect documentary.mlt, remove the pauses between clips on V1, add title cards,
add a slow push-in with smooth audio and video fades, generate preview frames at each
section boundary, and keep the project editable.
```

```text
Add these Portuguese subtitles, burn them in using a readable bottom-center style,
then render an H.264 web export. Monitor the job until it completes.
```

```text
Analyze interview.mov for silence, frozen or black video, interlacing, and EBU R128 loudness.
Return the measurements before proposing any cleanup edits.
```

```text
Inspect documentary.mlt, export its point markers as chapters.txt, then render only the range
marker named "Trailer" with the H.264 web preset.
```

For export presets and delivery behavior, see [rendering](reference.md#rendering).
