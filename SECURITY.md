# Security policy

## Reporting a vulnerability

Use GitHub's private security-advisory reporting for vulnerabilities that could expose files,
execute unintended processes, escape configured roots, or overwrite user data. Do not include
sensitive reproduction data in a public issue. Use ordinary GitHub issues for non-sensitive bugs.

## Assets and trust boundary

Shotcut MCP protects saved `.mlt`/XML projects, source-media paths, preview/render outputs,
project backups, and durable render-job metadata. It is a local stdio server: the connected MCP
client can request reads, writes, previews, process launches, and renders with the permissions of
the operating-system user running the server.

The MCP client and administrator-provided executable paths are trusted to express user intent.
Project/media contents, embedded resource paths, tool arguments, process output, and concurrent
filesystem changes are untrusted. Shotcut GUI state is a separate writer and is not coordinated by
the MCP lock.

## Threats and mitigations

| Threat | Mitigation |
| --- | --- |
| Path traversal or access outside the intended workspace | Canonical resolution plus optional `SHOTCUT_MCP_ALLOWED_ROOTS` and absolute-path enforcement for every user-controlled data path |
| Network resources embedded in MLT XML | Denied by default and checked across recognized resource properties |
| Shell or argument injection | Shotcut, Melt, FFmpeg, and FFprobe run from argument lists without a shell |
| Unsafe render sidecars or multi-file consumers | Consumer properties use a single-file-safe allowlist unless an administrator opts in |
| Lost updates from concurrent writers | Revision checks, project locks, post-validation revision recheck, and output identity recheck |
| Partial or invalid project replacement | Sibling temporary file, complete MLT validation, `fsync` where supported, isolated backup, and atomic replace |
| Overwriting existing output | Explicit overwrite authorization plus protected temporary output and atomic promotion |
| Shared-producer mutation affecting unrelated clips | Clone a shared producer before a clip-local edit |
| Resource exhaustion | Bounded messages, projects, operation batches, workers, pending work, process output, logs, caches, searches, diffs, and inline images |
| Corrupt or forged backup/job metadata | Canonical project identity, owned backup filenames, validated job shapes, and bounded logs/history |

MCP responses and requests share the configured newline-delimited message budget. Structured
results are not duplicated into the text fallback on modern protocol versions. A project candidate
that exceeds the project-size limit is rejected before backup or replacement, so the server never
writes a project it would refuse to reopen. The default is 128 MiB;
`SHOTCUT_MCP_MAX_PROJECT_BYTES` can select a limit between 1 MiB and 512 MiB.

## Administrator hardening

- Set `SHOTCUT_MCP_ALLOWED_ROOTS` to the smallest practical set of project, media, and output roots.
- Set `SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS=1` when relative paths are unnecessary.
- Keep `SHOTCUT_MCP_MAX_PROJECT_BYTES` only as high as required for the expected project scale.
- Keep `SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES` and
  `SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES` disabled unless the added behavior was reviewed.
- Configure explicit trusted executable paths in controlled environments and restrict who can
  change the server process environment.
- Run the MCP as a non-administrator user with only the filesystem permissions it requires.
- Keep projects out of simultaneous Shotcut GUI and MCP editing; save/close GUI edits first.
- Review backups and durable render state using the public tools rather than editing their private
  stores manually.

## Residual risk

An authorized local client can intentionally modify or render any file allowed by the operating
system and configured root policy. External media decoders and the installed Shotcut/MLT/FFmpeg
stack process untrusted media outside this Python package. Atomic writes protect saved filesystem
state, but they cannot preserve unsaved Shotcut GUI edits or prevent another unrelated application
from changing source media during a long-running render.
