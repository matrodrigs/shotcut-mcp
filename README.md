# Shotcut MCP

Plugin MCP local para integrar o Codex ao [Shotcut](https://www.shotcut.org/) e ao
[MLT](https://www.mltframework.org/).

## Ferramentas

- `shotcut_status`: localiza Shotcut, Melt e ffprobe e mostra suas versões.
- `probe_media`: lê metadados técnicos de áudio, vídeo ou imagem.
- `inspect_project`: resume perfil, timeline, recursos e arquivos ausentes de um `.mlt`.
- `create_project`: cria uma timeline Shotcut editável a partir de uma sequência de clipes.
- `validate_project`: abre o projeto no MLT e valida o primeiro quadro.
- `open_in_shotcut`: abre projeto ou mídia na interface do Shotcut.
- `start_render`: inicia uma exportação em segundo plano usando presets seguros.
- `render_status`: consulta andamento, log e arquivo gerado.
- `cancel_render`: interrompe uma exportação iniciada nesta sessão MCP.

O servidor usa transporte MCP `stdio`, não envia dados para a internet e não sobrescreve
projetos nem exports existentes sem `overwrite: true`.

## Configuração opcional

O servidor encontra instalações comuns automaticamente. Para caminhos personalizados, defina:

- `SHOTCUT_PATH`
- `SHOTCUT_MELT_PATH`
- `SHOTCUT_FFPROBE_PATH`

## Limites

O Shotcut não expõe uma API remota pública. Este plugin trabalha com o formato MLT XML e os
executáveis oficiais distribuídos com o editor. Projetos criados pelo MCP usam uma timeline V1
sequencial simples; edições visuais avançadas continuam no Shotcut.
