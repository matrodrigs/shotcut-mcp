"""MCP tool catalog and handlers."""

from __future__ import annotations

from typing import Any, Callable

from .errors import ToolError
from .platform import (
    describe_service,
    expand_path,
    list_services,
    open_in_shotcut,
    render_preview,
    status,
    summarize_media,
    validate_project_file,
)
from .project import (
    ProjectDocument,
    create_project,
    edit_project,
    list_backups,
    restore_backup,
)
from .render import RENDER_PRESETS, cancel_render, render_status, start_render


OPERATION_CATALOG: dict[str, dict[str, Any]] = {
    "add_track": {
        "required": ["kind"],
        "optional": ["name"],
        "notes": "kind: video|audio",
    },
    "remove_track": {"required": ["track"]},
    "update_track": {
        "required": ["track"],
        "optional": ["name", "locked", "hidden", "muted", "composite"],
    },
    "move_track": {"required": ["track", "before"]},
    "add_clip": {
        "required": ["track", "path"],
        "optional": [
            "position_frame",
            "mode",
            "in_frame",
            "out_frame",
            "in_seconds",
            "out_seconds",
            "caption",
            "image_duration_seconds",
        ],
        "notes": "mode: insert|overwrite; omitir position_frame adiciona no fim",
    },
    "add_generator": {
        "required": ["track", "generator", "duration_frames"],
        "optional": [
            "position_frame",
            "mode",
            "color",
            "text",
            "frequency",
            "level",
            "properties",
        ],
        "notes": "generator: color|text|tone|noise",
    },
    "remove_item": {"required": ["track", "item_index"], "optional": ["ripple"]},
    "trim_item": {
        "required": ["track", "item_index"],
        "optional": ["in_frame", "out_frame"],
    },
    "split_item": {"required": ["track", "item_index", "offset_frame"]},
    "move_item": {
        "required": ["track", "item_index", "position_frame"],
        "optional": ["target_track", "mode", "ripple_source"],
    },
    "insert_gap": {
        "required": ["position_frame", "duration_frames"],
        "optional": ["tracks"],
        "notes": "tracks: lista de nomes/ids ou 'all'",
    },
    "remove_range": {
        "required": ["position_frame", "duration_frames"],
        "optional": ["tracks", "ripple"],
    },
    "add_transition": {
        "required": ["track", "left_item_index", "duration_frames"],
        "optional": ["service", "properties", "audio_crossfade", "name"],
        "notes": "Cria um tractor Shotcut aninhado entre dois clipes adjacentes.",
    },
    "remove_transition": {"required": ["track", "item_index"]},
    "add_filter": {
        "required": ["target", "service"],
        "optional": [
            "track",
            "item_index",
            "shotcut_filter",
            "in_frame",
            "out_frame",
            "properties",
        ],
        "notes": "target: project|track|clip; animações/keyframes são strings de propriedade MLT.",
    },
    "update_filter": {
        "required": ["filter_id"],
        "optional": ["enabled", "in_frame", "out_frame", "properties"],
        "notes": "Use null numa propriedade para removê-la.",
    },
    "remove_filter": {"required": ["filter_id"]},
    "set_notes": {"required": ["notes"]},
    "add_marker": {
        "required": ["start_frame"],
        "optional": ["end_frame", "text", "color"],
    },
    "remove_marker": {"required": ["marker_id"]},
    "set_subtitle_track": {
        "required": ["name", "items"],
        "optional": ["language", "burn_in", "style"],
        "notes": "Cada item exige start_ms, end_ms e text.",
    },
    "remove_subtitle_track": {"required": ["name"]},
    "relink_media": {
        "required": ["from", "to"],
        "optional": ["match_basename"],
    },
    "set_profile": {
        "required": ["preserve_frame_numbers"],
        "optional": [
            "width",
            "height",
            "frame_rate_num",
            "frame_rate_den",
            "progressive",
            "sample_aspect_num",
            "sample_aspect_den",
            "display_aspect_num",
            "display_aspect_den",
            "colorspace",
        ],
        "notes": "Exige preserve_frame_numbers=true; não reamostra posições existentes.",
    },
}


def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "compatibility": {
            "shotcut": "26.2.26",
            "mlt": "7.37.x",
            "project_format": "MLT XML",
        },
        "transaction_guarantees": [
            "optimistic concurrency using SHA-256 revision",
            "single parse/write for up to 500 operations",
            "MCP lock file",
            "temporary-file MLT validation before replace",
            "atomic replace",
            "timestamped backup retention (20)",
            "unknown XML elements and properties preserved",
        ],
        "operations": OPERATION_CATALOG,
        "render_presets": RENDER_PRESETS,
        "workflow": [
            "inspect_project to obtain revision and current item indexes",
            "optionally list_mlt_services/describe_mlt_service",
            "edit_project with expected_revision and one batch of operations",
            "render_preview or validate_project",
            "start_render and poll render_status",
        ],
    }


def inspect_project(arguments: dict[str, Any]) -> dict[str, Any]:
    return ProjectDocument.load(expand_path(arguments.get("path", ""))).snapshot()


def validate_project(arguments: dict[str, Any]) -> dict[str, Any]:
    path = expand_path(arguments.get("path", ""))
    timeout = arguments.get("timeout_seconds", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 300
    ):
        raise ToolError("timeout_seconds deve ser um inteiro entre 1 e 300.")
    return {
        "project": ProjectDocument.load(path).snapshot(),
        **validate_project_file(path, timeout),
    }


def render_preview_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    frame = arguments.get("frame", 0)
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise ToolError("frame deve ser um inteiro.")
    return render_preview(
        expand_path(arguments.get("project_path", "")),
        expand_path(arguments.get("output_path", "")),
        frame,
        arguments.get("overwrite", False),
    )


def list_backups_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    return list_backups(expand_path(arguments.get("project_path", "")))


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


PATH = {"type": "string", "description": "Caminho local absoluto ou relativo."}
TRACK = {
    "type": "string",
    "description": "Nome ou id de faixa retornado por inspect_project.",
}
OP_NAMES = list(OPERATION_CATALOG)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "shotcut_status",
        "title": "Verificar Shotcut",
        "description": "Localiza Shotcut, Melt, ffprobe e ffmpeg e informa suas versões.",
        "inputSchema": _object_schema({}),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "shotcut_capabilities",
        "title": "Consultar operações de edição",
        "description": "Retorna o catálogo completo de operações, parâmetros e garantias transacionais.",
        "inputSchema": _object_schema({}),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "probe_media",
        "title": "Analisar mídia",
        "description": "Lê duração, codecs, resolução, frame rate e áudio com cache por arquivo.",
        "inputSchema": _object_schema({"path": PATH}, ["path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "inspect_project",
        "title": "Inspecionar projeto completo",
        "description": "Retorna revision SHA-256, perfil, faixas, itens, filtros, marcadores, legendas e recursos.",
        "inputSchema": _object_schema({"path": PATH}, ["path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "create_project",
        "title": "Criar projeto Shotcut multifaixa",
        "description": "Cria MLT XML Shotcut 26.2 com background, V1, faixas adicionais e clipes opcionais.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "width": {"type": "integer", "minimum": 16, "default": 1920},
                "height": {"type": "integer", "minimum": 16, "default": 1080},
                "fps_num": {"type": "integer", "minimum": 1, "default": 30},
                "fps_den": {"type": "integer", "minimum": 1, "default": 1},
                "notes": {"type": "string"},
                "tracks": {"type": "array", "items": {"type": "object"}},
                "clips": {"type": "array", "items": {"type": "object"}},
                "overwrite": {"type": "boolean", "default": False},
                "validate": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            ["project_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "edit_project",
        "title": "Editar projeto em transação",
        "description": (
            "Aplica até 500 operações em uma gravação atômica. Obtenha revision em inspect_project e "
            "consulte shotcut_capabilities para os parâmetros de cada op."
        ),
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "properties": {"op": {"type": "string", "enum": OP_NAMES}},
                        "required": ["op"],
                        "additionalProperties": True,
                    },
                },
                "force": {"type": "boolean", "default": False},
                "validate": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            ["project_path", "operations"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_mlt_services",
        "title": "Listar serviços MLT",
        "description": "Lista filtros, transições, producers ou consumers realmente instalados no Shotcut.",
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["filter", "transition", "producer", "consumer"],
                }
            },
            ["kind"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "describe_mlt_service",
        "title": "Descrever serviço MLT",
        "description": "Consulta propriedades e metadados oficiais expostos pela instalação local do MLT.",
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["filter", "transition", "producer", "consumer"],
                },
                "name": {"type": "string"},
            },
            ["kind", "name"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "validate_project",
        "title": "Validar projeto no MLT",
        "description": "Analisa o XML e processa o primeiro quadro com a instalação local do Melt.",
        "inputSchema": _object_schema(
            {
                "path": PATH,
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 30,
                },
            },
            ["path"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "render_preview",
        "title": "Renderizar quadro de preview",
        "description": "Renderiza um único frame PNG para verificar visualmente uma edição.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "frame": {"type": "integer", "minimum": 0, "default": 0},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "open_in_shotcut",
        "title": "Abrir no Shotcut",
        "description": "Abre projeto, mídia ou pasta na interface do Shotcut.",
        "inputSchema": _object_schema(
            {"path": PATH, "fullscreen": {"type": "boolean", "default": False}},
            ["path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "start_render",
        "title": "Iniciar render",
        "description": "Exporta em segundo plano e retorna job_id monitorável.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "preset": {
                    "type": "string",
                    "enum": list(RENDER_PRESETS),
                    "default": "h264-high",
                },
                "consumer_properties": {"type": "object", "additionalProperties": True},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "render_status",
        "title": "Consultar render",
        "description": "Retorna estado, progresso, log e tamanho da saída.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "cancel_render",
        "title": "Cancelar render",
        "description": "Interrompe um render ativo iniciado nesta sessão MCP.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_project_backups",
        "title": "Listar backups do projeto",
        "description": "Lista revisões automáticas disponíveis para recuperação.",
        "inputSchema": _object_schema({"project_path": PATH}, ["project_path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "restore_project_backup",
        "title": "Restaurar backup do projeto",
        "description": "Valida e restaura um backup, salvando antes uma cópia da versão atual.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "backup_path": PATH,
                "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "force": {"type": "boolean", "default": False},
            },
            ["project_path", "backup_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "shotcut_status": lambda _: status(),
    "shotcut_capabilities": capabilities,
    "probe_media": lambda arguments: summarize_media(
        expand_path(arguments.get("path", ""))
    ),
    "inspect_project": inspect_project,
    "create_project": create_project,
    "edit_project": edit_project,
    "list_mlt_services": lambda arguments: list_services(arguments.get("kind", "")),
    "describe_mlt_service": lambda arguments: describe_service(
        arguments.get("kind", ""), arguments.get("name", "")
    ),
    "validate_project": validate_project,
    "render_preview": render_preview_tool,
    "open_in_shotcut": lambda arguments: open_in_shotcut(
        expand_path(arguments.get("path", "")), arguments.get("fullscreen", False)
    ),
    "start_render": start_render,
    "render_status": lambda arguments: render_status(arguments.get("job_id", "")),
    "cancel_render": lambda arguments: cancel_render(arguments.get("job_id", "")),
    "list_project_backups": list_backups_tool,
    "restore_project_backup": restore_backup,
}
