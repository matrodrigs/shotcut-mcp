# Shotcut MCP

Servidor MCP local e transacional para criar, inspecionar, editar, validar e renderizar
projetos [Shotcut](https://www.shotcut.org/) no formato MLT XML.

Esta versão foi implementada e testada contra Shotcut 26.2.26 e MLT 7.37.0. O runtime usa
somente a biblioteca padrão do Python e os executáveis distribuídos com o Shotcut.

## O que ele edita

- Faixas de vídeo e áudio: adicionar, remover, renomear, bloquear, ocultar, silenciar,
  reordenar e habilitar/desabilitar composição.
- Timeline: inserir mídia ou generators, inserir gaps, overwrite, ripple delete, remover regiões,
  mover, recortar e dividir clipes.
- Transições: crossfade de vídeo com qualquer transition service MLT e crossfade de áudio.
- Efeitos: filtros MLT em clipe, faixa ou saída, incluindo propriedades animadas/keyframes.
- Generators: cor, texto, tom e ruído.
- Projeto: notas, marcadores, perfil e relink de mídia.
- Legendas: faixas `subtitle_feed`, itens SRT e burn-in estilizado.
- Saída: preview PNG, validação MLT e render assíncrono em H.264, HEVC, AV1, ProRes,
  DNxHD, FLAC ou MP3, além de propriedades `avformat` personalizadas.

## Ferramentas MCP

- `shotcut_status`
- `shotcut_capabilities`
- `probe_media`
- `inspect_project`
- `create_project`
- `edit_project`
- `list_mlt_services`
- `describe_mlt_service`
- `validate_project`
- `render_preview`
- `open_in_shotcut`
- `start_render`
- `render_status`
- `cancel_render`
- `list_project_backups`
- `restore_project_backup`

## Fluxo seguro de edição

1. Chame `inspect_project` e guarde o campo `revision`.
2. Consulte `shotcut_capabilities` para os parâmetros das operações necessárias.
3. Envie todas as mudanças em uma chamada `edit_project`, usando `revision` como
   `expected_revision`.
4. O servidor adquire um lock, aplica as operações em memória, grava um arquivo temporário,
   valida esse arquivo com Melt, cria um backup e só então substitui o projeto atomicamente.
5. Use `render_preview` ou abra o projeto no Shotcut para revisão visual.

Até 500 operações podem ser agrupadas por transação. Se outro programa salvar o projeto depois
da inspeção, a revisão muda e a escrita é recusada em vez de sobrescrever trabalho recente.

## Operações de `edit_project`

`add_track`, `remove_track`, `update_track`, `move_track`, `add_clip`, `add_generator`,
`remove_item`, `trim_item`, `split_item`, `move_item`, `insert_gap`, `remove_range`,
`add_transition`, `remove_transition`, `add_filter`, `update_filter`, `remove_filter`,
`set_notes`, `add_marker`, `remove_marker`, `set_subtitle_track`, `remove_subtitle_track`,
`relink_media` e `set_profile`.

`shotcut_capabilities` é a referência em tempo de execução para os argumentos de cada operação.

## Preservação e recuperação

- Elementos, atributos e propriedades XML desconhecidos são preservados.
- Cada edição bem-sucedida mantém um backup em `.shotcut-mcp/backups` ao lado do projeto.
- São mantidos os 20 backups mais recentes por projeto.
- `restore_project_backup` também valida o backup e salva a versão atual antes da restauração.
- Arquivos existentes não são sobrescritos sem `overwrite`, `expected_revision` ou `force`,
  conforme a operação.

## Serviços MLT e efeitos

O catálogo de filtros varia conforme a compilação do Shotcut. Use `list_mlt_services` e
`describe_mlt_service`; depois aplique o serviço e suas propriedades com `add_filter`.
Animações são aceitas na sintaxe nativa de propriedades do MLT, por exemplo valores como
`0=0;30=1` quando o filtro correspondente suporta animação.

## Configuração opcional

Instalações comuns são detectadas automaticamente. Caminhos personalizados podem ser definidos
com `SHOTCUT_PATH`, `SHOTCUT_MELT_PATH`, `SHOTCUT_FFPROBE_PATH` e `SHOTCUT_FFMPEG_PATH`.

## Limites honestos

- O MCP edita o último estado salvo em disco; ele não vê mudanças ainda não salvas na GUI.
- Estruturas desconhecidas são preservadas, mas uma operação é recusada quando o alvo é ambíguo
  ou exigiria adivinhar o formato de uma transição de terceiros.
- A disponibilidade e o comportamento de filtros de terceiros, GPU/OpenGL e codecs dependem da
  instalação local.
- Alterar FPS de um projeto existente preserva números de frame e exige confirmação explícita;
  não há reamostragem automática da montagem.
