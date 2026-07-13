#!/usr/bin/env python3
"""Dependency-free local MCP server for Shotcut and the MLT framework."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "shotcut-mcp"
SERVER_VERSION = "0.1.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
JOB_DIR = Path(tempfile.gettempdir()) / "shotcut-mcp" / "jobs"
JOB_DIR.mkdir(parents=True, exist_ok=True)
RUNNING_JOBS: dict[str, subprocess.Popen[Any]] = {}


class ToolError(Exception):
    """A recoverable error that should be returned to the model as tool output."""


@dataclass(frozen=True)
class Executables:
    shotcut: Path | None
    melt: Path | None
    ffprobe: Path | None


def _windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def _expand_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("O caminho deve ser uma string não vazia.")
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def _which(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value).resolve() if value else None


def discover_executables() -> Executables:
    env_shotcut = os.environ.get("SHOTCUT_PATH")
    env_melt = os.environ.get("SHOTCUT_MELT_PATH")
    env_ffprobe = os.environ.get("SHOTCUT_FFPROBE_PATH")

    shotcut_candidates: list[Path | None] = [
        _expand_path(env_shotcut) if env_shotcut else None,
        _which("shotcut"),
        _which("shotcut.exe"),
    ]
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        shotcut_candidates.extend(
            [
                program_files / "Shotcut" / "shotcut.exe",
                local_appdata / "Programs" / "Shotcut" / "shotcut.exe",
            ]
        )
    elif sys.platform == "darwin":
        shotcut_candidates.append(Path("/Applications/Shotcut.app/Contents/MacOS/shotcut"))

    shotcut = _first_existing(shotcut_candidates)
    sibling_dir = shotcut.parent if shotcut else None
    melt_names = ["melt.exe", "melt"] if os.name == "nt" else ["melt"]
    ffprobe_names = ["ffprobe.exe", "ffprobe"] if os.name == "nt" else ["ffprobe"]

    melt_candidates: list[Path | None] = [
        _expand_path(env_melt) if env_melt else None,
        *[(sibling_dir / name) if sibling_dir else None for name in melt_names],
        _which("melt"),
        _which("shotcut.melt"),
    ]
    ffprobe_candidates: list[Path | None] = [
        _expand_path(env_ffprobe) if env_ffprobe else None,
        *[(sibling_dir / name) if sibling_dir else None for name in ffprobe_names],
        _which("ffprobe"),
    ]
    if sys.platform == "darwin":
        melt_candidates.append(Path("/Applications/Shotcut.app/Contents/MacOS/melt"))

    return Executables(
        shotcut=shotcut,
        melt=_first_existing(melt_candidates),
        ffprobe=_first_existing(ffprobe_candidates),
    )


def _run_capture(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_windows_creation_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"O comando excedeu o limite de {timeout} segundos.") from exc
    except OSError as exc:
        raise ToolError(f"Não foi possível executar {command[0]}: {exc}") from exc


def _version_line(executable: Path | None, args: list[str]) -> str | None:
    if executable is None:
        return None
    result = _run_capture([str(executable), *args], timeout=10)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return output.splitlines()[0] if output else None


def _require_executable(path: Path | None, label: str, env_name: str) -> Path:
    if path is None:
        raise ToolError(
            f"{label} não foi encontrado. Instale o Shotcut ou defina a variável {env_name}."
        )
    return path


def _probe_media_raw(media_path: Path) -> dict[str, Any]:
    if not media_path.is_file():
        raise ToolError(f"Arquivo de mídia não encontrado: {media_path}")
    ffprobe = _require_executable(
        discover_executables().ffprobe, "ffprobe", "SHOTCUT_FFPROBE_PATH"
    )
    result = _run_capture(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe retornou erro sem detalhes."
        raise ToolError(f"Falha ao analisar {media_path}: {detail[-1200:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("ffprobe retornou JSON inválido.") from exc
    if not isinstance(payload, dict):
        raise ToolError("ffprobe retornou um resultado inesperado.")
    return payload


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fraction(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _as_float(value)
    numerator, denominator = value.split("/", 1)
    num = _as_float(numerator)
    den = _as_float(denominator)
    if num is None or den in (None, 0):
        return None
    return num / den


def _media_duration(probe: dict[str, Any]) -> float | None:
    durations: list[float] = []
    format_duration = _as_float(probe.get("format", {}).get("duration"))
    if format_duration is not None and format_duration > 0:
        durations.append(format_duration)
    for stream in probe.get("streams", []):
        duration = _as_float(stream.get("duration"))
        if duration is not None and duration > 0:
            durations.append(duration)
    return max(durations) if durations else None


def _summarize_probe(media_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    streams: list[dict[str, Any]] = []
    for stream in payload.get("streams", []):
        item: dict[str, Any] = {
            "index": stream.get("index"),
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
            "duration_seconds": _as_float(stream.get("duration")),
        }
        if stream.get("codec_type") == "video":
            item.update(
                {
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "pixel_format": stream.get("pix_fmt"),
                    "frame_rate": _fraction(
                        stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                    ),
                }
            )
        elif stream.get("codec_type") == "audio":
            item.update(
                {
                    "sample_rate": _as_float(stream.get("sample_rate")),
                    "channels": stream.get("channels"),
                    "channel_layout": stream.get("channel_layout"),
                }
            )
        streams.append(item)
    format_info = payload.get("format", {})
    return {
        "path": str(media_path),
        "size_bytes": media_path.stat().st_size,
        "duration_seconds": _media_duration(payload),
        "format": format_info.get("format_long_name") or format_info.get("format_name"),
        "bit_rate": _as_float(format_info.get("bit_rate")),
        "streams": streams,
    }


def tool_shotcut_status(_: dict[str, Any]) -> dict[str, Any]:
    executables = discover_executables()
    return {
        "ready": all((executables.shotcut, executables.melt, executables.ffprobe)),
        "shotcut": {
            "path": str(executables.shotcut) if executables.shotcut else None,
            "found": executables.shotcut is not None,
        },
        "melt": {
            "path": str(executables.melt) if executables.melt else None,
            "found": executables.melt is not None,
            "version": _version_line(executables.melt, ["--version"]),
        },
        "ffprobe": {
            "path": str(executables.ffprobe) if executables.ffprobe else None,
            "found": executables.ffprobe is not None,
            "version": _version_line(executables.ffprobe, ["-version"]),
        },
        "configuration": {
            "SHOTCUT_PATH": os.environ.get("SHOTCUT_PATH"),
            "SHOTCUT_MELT_PATH": os.environ.get("SHOTCUT_MELT_PATH"),
            "SHOTCUT_FFPROBE_PATH": os.environ.get("SHOTCUT_FFPROBE_PATH"),
        },
    }


def tool_probe_media(arguments: dict[str, Any]) -> dict[str, Any]:
    path = _expand_path(arguments.get("path", ""))
    return _summarize_probe(path, _probe_media_raw(path))


def _properties(element: ET.Element) -> dict[str, str]:
    return {
        prop.get("name", ""): prop.text or ""
        for prop in element.findall("property")
        if prop.get("name")
    }


def _xml_duration_frames(element: ET.Element, fps: float) -> int | None:
    def parse_position(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            pass
        match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", value)
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return round((hours * 3600 + minutes * 60 + seconds) * fps)

    in_frame = parse_position(element.get("in")) or 0
    out_frame = parse_position(element.get("out"))
    if out_frame is None:
        length = parse_position(element.get("length"))
        return length
    return max(0, out_frame - in_frame + 1)


def _looks_like_file_resource(resource: str) -> bool:
    if not resource or resource.startswith(("color:", "colour:", "noise:", "tone:")):
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", resource):
        return resource.startswith("file://")
    return True


def _resource_path(resource: str, project_path: Path, xml_root: str | None) -> Path | None:
    if not _looks_like_file_resource(resource):
        return None
    cleaned = resource[7:] if resource.startswith("file://") else resource
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate.resolve()
    base = Path(xml_root) if xml_root else project_path.parent
    if not base.is_absolute():
        base = project_path.parent / base
    return (base / candidate).resolve()


def inspect_project_file(project_path: Path) -> dict[str, Any]:
    if not project_path.is_file():
        raise ToolError(f"Projeto não encontrado: {project_path}")
    try:
        tree = ET.parse(project_path)
    except ET.ParseError as exc:
        raise ToolError(f"MLT XML inválido: {exc}") from exc
    root = tree.getroot()
    if root.tag != "mlt":
        raise ToolError(f"Raiz XML inesperada: <{root.tag}>; era esperado <mlt>.")

    profile = root.find("profile")
    frame_rate_num = int(profile.get("frame_rate_num", "25")) if profile is not None else 25
    frame_rate_den = int(profile.get("frame_rate_den", "1")) if profile is not None else 1
    fps = frame_rate_num / frame_rate_den if frame_rate_den else 25.0
    elements_by_id = {
        element.get("id"): element
        for element in root
        if element.get("id") is not None
    }

    tracks: list[dict[str, Any]] = []
    tractors = root.findall("tractor")
    main_tractor = next(
        (item for item in tractors if item.get("id") == root.get("producer")),
        tractors[-1] if tractors else None,
    )
    if main_tractor is not None:
        for index, track in enumerate(main_tractor.findall("track"), start=1):
            producer_id = track.get("producer")
            source = elements_by_id.get(producer_id)
            props = _properties(source) if source is not None else {}
            entries = source.findall("entry") if source is not None else []
            blanks = source.findall("blank") if source is not None else []
            duration_frames = sum(
                duration or 0
                for duration in (
                    _xml_duration_frames(item, fps) for item in [*entries, *blanks]
                )
            )
            tracks.append(
                {
                    "index": index,
                    "producer_id": producer_id,
                    "name": props.get("shotcut:name") or f"Track {index}",
                    "entries": len(entries),
                    "blanks": len(blanks),
                    "duration_frames": duration_frames or None,
                    "duration_seconds": (duration_frames / fps) if duration_frames else None,
                    "hidden": track.get("hide"),
                }
            )

    resources: list[dict[str, Any]] = []
    seen_resources: set[str] = set()
    for element in [*root.findall("producer"), *root.findall("chain")]:
        props = _properties(element)
        resource = props.get("resource")
        if not resource or resource in seen_resources:
            continue
        seen_resources.add(resource)
        resolved = _resource_path(resource, project_path, root.get("root"))
        resources.append(
            {
                "resource": resource,
                "resolved_path": str(resolved) if resolved else None,
                "exists": resolved.exists() if resolved else None,
            }
        )

    profile_summary = None
    if profile is not None:
        profile_summary = {
            "description": profile.get("description"),
            "width": int(profile.get("width", "0")),
            "height": int(profile.get("height", "0")),
            "progressive": profile.get("progressive") == "1",
            "frame_rate_num": frame_rate_num,
            "frame_rate_den": frame_rate_den,
            "fps": fps,
            "display_aspect": f"{profile.get('display_aspect_num', '?')}:{profile.get('display_aspect_den', '?')}",
        }
    return {
        "path": str(project_path),
        "title": root.get("title"),
        "mlt_version": root.get("version"),
        "profile": profile_summary,
        "shotcut_editable": any(_properties(item).get("shotcut") == "1" for item in tractors),
        "counts": {
            "producers": len(root.findall("producer")),
            "chains": len(root.findall("chain")),
            "playlists": len(root.findall("playlist")),
            "tractors": len(tractors),
            "filters": len(root.findall(".//filter")),
            "transitions": len(root.findall(".//transition")),
        },
        "tracks": tracks,
        "resources": resources,
        "missing_resources": [
            item["resolved_path"] for item in resources if item["exists"] is False
        ],
    }


def tool_inspect_project(arguments: dict[str, Any]) -> dict[str, Any]:
    return inspect_project_file(_expand_path(arguments.get("path", "")))


def _add_property(element: ET.Element, name: str, value: str | int | float) -> None:
    prop = ET.SubElement(element, "property", {"name": name})
    prop.text = str(value)


def _validated_number(
    arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} deve ser um número inteiro.")
    if not minimum <= value <= maximum:
        raise ToolError(f"{name} deve ficar entre {minimum} e {maximum}.")
    return value


def tool_create_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project_path = _expand_path(arguments.get("project_path", ""))
    if project_path.suffix.lower() not in {".mlt", ".xml"}:
        raise ToolError("O projeto deve usar a extensão .mlt ou .xml.")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite deve ser booleano.")
    if project_path.exists() and not overwrite:
        raise ToolError(
            f"O projeto já existe: {project_path}. Use overwrite=true para substituí-lo."
        )

    clips = arguments.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ToolError("clips deve ser uma lista não vazia.")
    if len(clips) > 500:
        raise ToolError("Um projeto pode receber no máximo 500 clipes por chamada.")

    width = _validated_number(arguments, "width", 1920, 16, 16384)
    height = _validated_number(arguments, "height", 1080, 16, 16384)
    fps_num = _validated_number(arguments, "fps_num", 30, 1, 100000)
    fps_den = _validated_number(arguments, "fps_den", 1, 1, 100000)
    fps = fps_num / fps_den
    image_duration = _as_float(arguments.get("image_duration_seconds", 5.0))
    if image_duration is None or not 0.04 <= image_duration <= 86400:
        raise ToolError("image_duration_seconds deve ficar entre 0.04 e 86400.")

    normalized_clips: list[dict[str, Any]] = []
    for index, clip in enumerate(clips, start=1):
        if not isinstance(clip, dict):
            raise ToolError(f"clips[{index - 1}] deve ser um objeto.")
        media_path = _expand_path(clip.get("path", ""))
        probe = _probe_media_raw(media_path)
        media_duration = _media_duration(probe) or image_duration
        in_seconds = _as_float(clip.get("in_seconds", 0.0))
        out_seconds = _as_float(clip.get("out_seconds", media_duration))
        if in_seconds is None or in_seconds < 0:
            raise ToolError(f"clips[{index - 1}].in_seconds deve ser zero ou positivo.")
        if out_seconds is None or out_seconds <= in_seconds:
            raise ToolError(f"clips[{index - 1}].out_seconds deve ser maior que in_seconds.")
        if _media_duration(probe) is not None and out_seconds > media_duration + 0.05:
            raise ToolError(
                f"clips[{index - 1}].out_seconds ({out_seconds}) excede a duração "
                f"da mídia ({media_duration:.3f}s)."
            )
        full_frames = max(1, math.ceil(media_duration * fps))
        in_frame = max(0, round(in_seconds * fps))
        out_frame = min(full_frames - 1, max(in_frame, math.ceil(out_seconds * fps) - 1))
        normalized_clips.append(
            {
                "path": media_path,
                "full_frames": full_frames,
                "in_frame": in_frame,
                "out_frame": out_frame,
                "duration_seconds": (out_frame - in_frame + 1) / fps,
            }
        )

    aspect_divisor = math.gcd(width, height)
    root = ET.Element(
        "mlt",
        {
            "LC_NUMERIC": "C",
            "version": "7.0.0",
            "title": arguments.get("title") or project_path.stem,
            "producer": "tractor0",
            "root": str(project_path.parent),
        },
    )
    ET.SubElement(
        root,
        "profile",
        {
            "description": f"{width}x{height} {fps:.3f} fps",
            "width": str(width),
            "height": str(height),
            "progressive": "1",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "display_aspect_num": str(width // aspect_divisor),
            "display_aspect_den": str(height // aspect_divisor),
            "frame_rate_num": str(fps_num),
            "frame_rate_den": str(fps_den),
            "colorspace": "709" if height >= 720 else "601",
        },
    )

    for index, clip in enumerate(normalized_clips):
        producer = ET.SubElement(
            root,
            "producer",
            {
                "id": f"producer{index}",
                "in": "0",
                "out": str(clip["full_frames"] - 1),
            },
        )
        _add_property(producer, "length", clip["full_frames"])
        _add_property(producer, "eof", "pause")
        _add_property(producer, "resource", str(clip["path"]))
        _add_property(producer, "mlt_service", "avformat-novalidate")
        _add_property(producer, "seekable", 1)
        _add_property(producer, "shotcut:skipConvert", 1)
        _add_property(producer, "shotcut:caption", clip["path"].name)

    playlist = ET.SubElement(root, "playlist", {"id": "playlist0"})
    _add_property(playlist, "shotcut:video", 1)
    _add_property(playlist, "shotcut:name", "V1")
    for index, clip in enumerate(normalized_clips):
        ET.SubElement(
            playlist,
            "entry",
            {
                "producer": f"producer{index}",
                "in": str(clip["in_frame"]),
                "out": str(clip["out_frame"]),
            },
        )

    total_frames = sum(
        clip["out_frame"] - clip["in_frame"] + 1 for clip in normalized_clips
    )
    tractor = ET.SubElement(
        root,
        "tractor",
        {"id": "tractor0", "in": "0", "out": str(max(0, total_frames - 1))},
    )
    _add_property(tractor, "shotcut", 1)
    _add_property(tractor, "shotcut:projectAudioChannels", 2)
    _add_property(tractor, "shotcut:projectNotes", arguments.get("notes", ""))
    ET.SubElement(tractor, "track", {"producer": "playlist0"})

    ET.indent(root, space="  ")
    project_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = project_path.with_name(f".{project_path.name}.{uuid.uuid4().hex}.tmp")
    ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
    os.replace(temporary, project_path)
    return {
        "created": True,
        "path": str(project_path),
        "clip_count": len(normalized_clips),
        "duration_frames": total_frames,
        "duration_seconds": total_frames / fps,
        "profile": {
            "width": width,
            "height": height,
            "frame_rate_num": fps_num,
            "frame_rate_den": fps_den,
            "fps": fps,
        },
    }


def tool_validate_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project_path = _expand_path(arguments.get("path", ""))
    summary = inspect_project_file(project_path)
    melt = _require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    timeout = arguments.get("timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ToolError("timeout_seconds deve ser um inteiro entre 1 e 300.")
    result = _run_capture(
        [
            str(melt),
            str(project_path),
            "in=0",
            "out=0",
            "-consumer",
            "null",
            "real_time=-1",
            "terminate_on_pause=1",
        ],
        timeout=timeout,
    )
    diagnostic = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return {
        "valid": result.returncode == 0,
        "return_code": result.returncode,
        "diagnostic": diagnostic[-4000:] or None,
        "project": summary,
    }


def tool_open_in_shotcut(arguments: dict[str, Any]) -> dict[str, Any]:
    path = _expand_path(arguments.get("path", ""))
    if not path.exists():
        raise ToolError(f"Arquivo ou diretório não encontrado: {path}")
    shotcut = _require_executable(
        discover_executables().shotcut, "Shotcut", "SHOTCUT_PATH"
    )
    command = [str(shotcut)]
    if arguments.get("fullscreen", False):
        command.append("--fullscreen")
    command.append(str(path))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_windows_creation_flags(),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ToolError(f"Não foi possível abrir o Shotcut: {exc}") from exc
    return {"opened": True, "path": str(path), "pid": process.pid}


RENDER_PRESETS: dict[str, dict[str, str]] = {
    "h264-high": {
        "f": "mp4",
        "vcodec": "libx264",
        "crf": "18",
        "preset": "medium",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "h264-web": {
        "f": "mp4",
        "vcodec": "libx264",
        "crf": "23",
        "preset": "medium",
        "acodec": "aac",
        "ab": "160k",
        "movflags": "+faststart",
    },
    "hevc": {
        "f": "mp4",
        "vcodec": "libx265",
        "crf": "22",
        "preset": "medium",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "prores": {
        "f": "mov",
        "vcodec": "prores_ks",
        "profile": "3",
        "acodec": "pcm_s24le",
    },
    "audio-mp3": {"f": "mp3", "vn": "1", "acodec": "libmp3lame", "ab": "192k"},
}


def _job_metadata_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ToolError("job_id inválido.")
    return JOB_DIR / f"{job_id}.json"


def _write_job(metadata: dict[str, Any]) -> None:
    path = _job_metadata_path(metadata["job_id"])
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_job(job_id: str) -> dict[str, Any]:
    path = _job_metadata_path(job_id)
    if not path.is_file():
        raise ToolError(f"Render não encontrado: {job_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Metadados de render inválidos: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolError("Metadados de render inválidos.")
    return payload


def _validate_consumer_properties(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolError("consumer_properties deve ser um objeto.")
    if len(value) > 50:
        raise ToolError("consumer_properties aceita no máximo 50 opções.")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", key):
            raise ToolError(f"Nome de propriedade MLT inválido: {key!r}")
        if key in {"target", "resource"}:
            raise ToolError(f"A propriedade {key} é controlada pelo servidor.")
        if isinstance(raw, bool):
            text = "1" if raw else "0"
        elif isinstance(raw, (str, int, float)):
            text = str(raw)
        else:
            raise ToolError(f"Valor inválido para consumer_properties.{key}.")
        if len(text) > 500:
            raise ToolError(f"consumer_properties.{key} excede 500 caracteres.")
        result[key] = text
    return result


def tool_start_render(arguments: dict[str, Any]) -> dict[str, Any]:
    project_path = _expand_path(arguments.get("project_path", ""))
    output_path = _expand_path(arguments.get("output_path", ""))
    if not project_path.is_file():
        raise ToolError(f"Projeto não encontrado: {project_path}")
    if project_path == output_path:
        raise ToolError("Projeto e arquivo de saída não podem ser o mesmo caminho.")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite deve ser booleano.")
    if output_path.exists() and not overwrite:
        raise ToolError(
            f"A saída já existe: {output_path}. Use overwrite=true para substituí-la."
        )
    if output_path.exists() and overwrite:
        if not output_path.is_file():
            raise ToolError(f"A saída existente não é um arquivo: {output_path}")
        output_path.unlink()

    preset_name = arguments.get("preset", "h264-high")
    if preset_name not in RENDER_PRESETS:
        raise ToolError(f"Preset inválido. Opções: {', '.join(RENDER_PRESETS)}")
    properties = dict(RENDER_PRESETS[preset_name])
    properties.update(_validate_consumer_properties(arguments.get("consumer_properties")))

    melt = _require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    log_path = JOB_DIR / f"{job_id}.log"
    command = [
        str(melt),
        str(project_path),
        "-progress",
        "-consumer",
        f"avformat:{output_path}",
        "real_time=-1",
        "terminate_on_pause=1",
        *[f"{key}={value}" for key, value in properties.items()],
    ]
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=_windows_creation_flags(),
                start_new_session=os.name != "nt",
            )
    except OSError as exc:
        raise ToolError(f"Não foi possível iniciar o render: {exc}") from exc

    RUNNING_JOBS[job_id] = process
    metadata = {
        "job_id": job_id,
        "pid": process.pid,
        "status": "running",
        "return_code": None,
        "project_path": str(project_path),
        "output_path": str(output_path),
        "preset": preset_name,
        "consumer_properties": properties,
        "log_path": str(log_path),
        "started_at": time.time(),
        "finished_at": None,
    }
    _write_job(metadata)
    return metadata


def _render_progress(log_path: Path) -> tuple[int | None, str | None]:
    if not log_path.is_file():
        return None, None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    matches = re.findall(r"(?:percentage|percent|progress)\s*[:=]\s*(\d{1,3})", text, re.I)
    progress = min(100, int(matches[-1])) if matches else None
    tail = text[-4000:].strip() or None
    return progress, tail


def tool_render_status(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = arguments.get("job_id", "")
    if not isinstance(job_id, str):
        raise ToolError("job_id deve ser uma string.")
    metadata = _read_job(job_id)
    process = RUNNING_JOBS.get(job_id)
    if process is not None:
        return_code = process.poll()
        if return_code is not None and metadata.get("status") == "running":
            metadata["return_code"] = return_code
            metadata["status"] = "completed" if return_code == 0 else "failed"
            metadata["finished_at"] = time.time()
            _write_job(metadata)
            RUNNING_JOBS.pop(job_id, None)
    output_path = Path(metadata["output_path"])
    progress, log_tail = _render_progress(Path(metadata["log_path"]))
    metadata["progress_percent"] = progress
    if metadata.get("status") == "completed":
        metadata["progress_percent"] = 100
    metadata["output_exists"] = output_path.is_file()
    metadata["output_size_bytes"] = output_path.stat().st_size if output_path.is_file() else None
    metadata["log_tail"] = log_tail
    return metadata


def tool_cancel_render(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = arguments.get("job_id", "")
    if not isinstance(job_id, str):
        raise ToolError("job_id deve ser uma string.")
    metadata = _read_job(job_id)
    process = RUNNING_JOBS.get(job_id)
    if process is None:
        raise ToolError(
            "Este render não está ativo nesta sessão MCP; ele pode ter terminado ou o servidor foi reiniciado."
        )
    if process.poll() is not None:
        return tool_render_status({"job_id": job_id})
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    metadata["status"] = "cancelled"
    metadata["return_code"] = process.returncode
    metadata["finished_at"] = time.time()
    _write_job(metadata)
    RUNNING_JOBS.pop(job_id, None)
    return metadata


TOOLS: list[dict[str, Any]] = [
    {
        "name": "shotcut_status",
        "title": "Verificar instalação do Shotcut",
        "description": "Localiza Shotcut, Melt e ffprobe e informa se a integração está pronta.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "probe_media",
        "title": "Analisar mídia",
        "description": "Lê duração, codecs, resolução, frame rate e áudio de um arquivo local.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Caminho local da mídia."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "inspect_project",
        "title": "Inspecionar projeto Shotcut",
        "description": "Resume perfil, tracks, recursos, filtros e arquivos ausentes de um projeto MLT XML.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Caminho do projeto .mlt."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "create_project",
        "title": "Criar projeto Shotcut",
        "description": "Cria uma timeline Shotcut editável com clipes sequenciais na track V1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "Destino .mlt ou .xml."},
                "clips": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "in_seconds": {"type": "number", "minimum": 0},
                            "out_seconds": {"type": "number", "exclusiveMinimum": 0},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "width": {"type": "integer", "default": 1920},
                "height": {"type": "integer", "default": 1080},
                "fps_num": {"type": "integer", "default": 30},
                "fps_den": {"type": "integer", "default": 1},
                "image_duration_seconds": {"type": "number", "default": 5.0},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["project_path", "clips"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "validate_project",
        "title": "Validar projeto no MLT",
        "description": "Analisa o XML e tenta processar o primeiro quadro com Melt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "open_in_shotcut",
        "title": "Abrir no Shotcut",
        "description": "Inicia a interface do Shotcut e abre um projeto, mídia ou pasta local.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "fullscreen": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "start_render",
        "title": "Iniciar render",
        "description": "Exporta um projeto em segundo plano e retorna um job_id monitorável.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "output_path": {"type": "string"},
                "preset": {"type": "string", "enum": list(RENDER_PRESETS), "default": "h264-high"},
                "consumer_properties": {
                    "type": "object",
                    "description": "Opções MLT avformat adicionais, por exemplo crf, vb ou threads.",
                    "additionalProperties": {"type": ["string", "number", "boolean"]},
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["project_path", "output_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "render_status",
        "title": "Consultar render",
        "description": "Retorna estado, progresso estimado, log e tamanho da saída de um render.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cancel_render",
        "title": "Cancelar render",
        "description": "Interrompe um render ativo iniciado pela sessão MCP atual.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
]

TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "shotcut_status": tool_shotcut_status,
    "probe_media": tool_probe_media,
    "inspect_project": tool_inspect_project,
    "create_project": tool_create_project,
    "validate_project": tool_validate_project,
    "open_in_shotcut": tool_open_in_shotcut,
    "start_render": tool_start_render,
    "render_status": tool_render_status,
    "cancel_render": tool_cancel_render,
}


def _tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message
    if is_notification:
        return None

    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Integração local com Shotcut. Não sobrescreva projetos ou exports sem "
                "autorização explícita do usuário. Consulte shotcut_status antes do primeiro uso."
            ),
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Parâmetros inválidos."},
            }
        name = params.get("name")
        handler = TOOL_HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Ferramenta desconhecida: {name}"},
            }
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            payload = handler(arguments)
            result = _tool_result(payload)
        except ToolError as exc:
            result = _tool_result({"error": str(exc)}, is_error=True)
        except Exception as exc:  # Keep the stdio server alive on unexpected tool failures.
            print(f"Unexpected error in {name}: {exc!r}", file=sys.stderr, flush=True)
            result = _tool_result(
                {"error": f"Falha interna inesperada: {type(exc).__name__}: {exc}"},
                is_error=True,
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Método não encontrado: {method}"},
    }


def _write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    # MCP stdio is always UTF-8, even when the Windows process code page is not.
    sys.stdout.buffer.write(encoded.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("A mensagem não é um objeto JSON.")
            response = _handle_request(message)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"JSON inválido: {exc}"},
            }
        if response is not None:
            _write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
