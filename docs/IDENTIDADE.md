# Identidade visual — Futebol de Raízes

**Conceito:** o placar com sotaque pernambucano. A cultura popular do estado —
xilogravura e cordel, a sombrinha do frevo, a bandeira de Pernambuco, os azulejos
do Recife Antigo e de Olinda — vestindo uma interface minimalista, rápida e clara.
Pernambucano na alma, sem virar fantasia: a identidade está em cor, tipografia,
ícones e microtexto; o conteúdo (placar, minuto, time) é sempre o protagonista.

## 1. Cores

Tokens em `:root` (tema claro, padrão) e `[data-theme="dark"]`. Nada de cor solta
no CSS fora dos tokens.

| Token | Claro | Escuro ("noite de maracatu") | Uso |
| --- | --- | --- | --- |
| `--paper` | `#FBF6EC` papel de cordel | `#0C1322` | fundo da página |
| `--surface` | `#FFFFFF` | `#141D31` | cards |
| `--surface-2` | `#F3EBDB` | `#1B263E` | áreas rebaixadas, cabeçalho do card |
| `--ink` | `#1B1712` tinta de xilo | `#F2EDE2` | texto principal |
| `--ink-2` | `#4B443A` | `#C7C0B2` | texto secundário |
| `--ink-3` | `#7B7365` | `#8E8778` | texto terciário, rótulos |
| `--line` | `#E5DAC5` | `#28344F` | bordas e divisórias |
| `--azul` | `#12306B` azul da bandeira | `#8FB0FF` | marca, links, foco |
| `--vermelho` | `#C8102E` frevo | `#FF5C6C` | ao vivo, gol, alertas |
| `--amarelo` | `#F2B705` sol | `#FFCB3D` | destaque, intervalo, foco secundário |
| `--verde` | `#0E8A4A` | `#3DD68C` | sucesso, classificado |

**Faixa do frevo** (assinatura da marca): quatro segmentos iguais
vermelho · amarelo · verde · azul. Aparece fina (3–4 px) no topo do cabeçalho,
sob títulos de seção e no aviso de gol. É o elemento que amarra todas as páginas.

Contraste mínimo AA (4.5:1 texto, 3:1 elementos gráficos) nos dois temas.

## 2. Tipografia (fontes locais em `static/fonts/`, woff2, `font-display: swap`)

* **Alfa Slab One** — letra de tipo de madeira, como capa de cordel e cartaz
  lambe-lambe. Só para a marca e títulos (h1/h2). Nunca em texto corrido.
* **Barlow Condensed** (600/700) — placar, minutos, rótulos em caixa alta,
  status, números de tabela. Sempre `font-variant-numeric: tabular-nums`.
* **Barlow** (400/500/600) — texto de interface.

## 3. Elementos gráficos

* **Ícones**: sprite SVG próprio (`templates/partials/icons.svg`, `<symbol>` +
  `<use>`), traço grosso de 2 px e cantos arredondados, inspirado em xilogravura.
  Conjunto: bola, apito, estádio, pino de mapa, TV, público, cartão, substituição,
  VAR, relógio, sino, alto-falante, sol, lua, seta/chevron, link externo, estrela,
  troféu, escudo, calendário, sair, cadeado.
* **Azulejo**: padrão SVG de azulejo português (geométrico, 1 cor) em opacidade
  muito baixa no cabeçalho e nos estados vazios.
* **Escudos**: quando o time não tem `crest_url`, o front desenha um escudo SVG
  com as cores do time (`color_primary`/`color_secondary`) e a sigla.
* **Ilustração de estado vazio**: sol da bandeira com mandacaru, em traço de xilo.

## 4. Logo (configurável)

* Arquivos: `static/img/logo.svg` (horizontal, tema claro), `logo-dark.svg`
  (para fundo escuro), `logo-mark.svg` (só o símbolo), `favicon.svg`.
* Símbolo: uma bola cuja metade de cima é a **sombrinha do frevo** nos quatro
  gomos de cor, com a **estrela** da bandeira de Pernambuco.
* Wordmark "FUTEBOL DE RAÍZES" em Alfa Slab One (convertido em curvas no SVG).
* Configuração: `BRAND_LOGO_URL`, `BRAND_LOGO_DARK_URL`, `BRAND_NAME`,
  `BRAND_TAGLINE`, `BRAND_FAVICON_URL` (ver `config/settings.py` → `BRAND`).
  Os templates usam `{{ brand.logo_src }}`/`{{ brand.logo_dark_src }}`.

## 5. Tom de voz (microtexto)

Português do Brasil com sotaque pernambucano na medida — em momentos de emoção e
nos estados vazios, nunca em dados ou em mensagens de erro técnicas.

| Situação | Texto |
| --- | --- |
| Aviso de gol | **"É gol!"** + time, placar, jogador e minuto |
| Gol anulado / correção | **"Oxe! Gol anulado."** / "Lance corrigido pelo operador." |
| Home sem jogo | "Hoje não tem jogo, visse? A bola volta a rolar logo, logo." |
| Sem gol ainda | "Nenhum gol hoje ainda. Paciência, que ele vem." |
| Reconectando | "Reconectando ao vivo…" |
| Relógio | "Horário de Brasília" |
| Rodapé | "Do Recife ao Sertão, futebol de raiz." |
| Erro genérico | "Não deu certo agora. Tente de novo em instantes." |

## 6. Componente de jogo (inspirado no wireframe `docs/wireframe-jogo.webp`)

Mantém a leitura do wireframe — faixa de meta, placar central com escudos,
linha do tempo de dois lados, escalações, ficha — com ganhos de UX:

1. **Faixa de meta**: pílula de status com texto e cor (AO VIVO pulsando em
   vermelho; INTERVALO em amarelo; ENCERRADO em tinta; horário para agendado;
   ADIADO/SUSPENSO/CANCELADO com hachura), minuto ao vivo calculado no cliente
   ("2T · 72'", "45+2'"), data relativa ("Hoje · 16:30", "Amanhã"), estádio e
   cidade com ícones. Em telas pequenas quebra em duas linhas sem perder o status.
2. **Placar**: faixas laterais com a cor de cada time; nome completo no desktop e
   sigla no celular; placar grande tabular; vencedor em destaque e perdedor
   esmaecido ao fim; pênaltis "(4) × (3) pên."; no mata-mata, linha de agregado e
   quem avançou.
3. **Resumo sem abrir**: autores dos gols sob cada time ("Pavón 18' · Reinaldo 56'")
   e cartões vermelhos — a informação principal sem expandir.
4. **Detalhe em acordeão** (`<details>`, fechado por padrão) com abas só quando há
   dado: **Lances** (linha do tempo de dois lados com trilho central, todos os
   tipos com ícone, separadores de período, gol anulado riscado com motivo),
   **Escalações** (esquema, titulares, reservas, técnico) e **Ficha** (arbitragem,
   público e renda, transmissões com link externo, estatísticas em barras).
5. **Ao vivo**: quando chega gol pelo stream, o placar pisca e o lance novo
   ganha destaque por alguns segundos (respeita `prefers-reduced-motion`).
6. **Acessibilidade**: `aria-label` no placar ("Sport 2 a 1 Náutico"), status em
   texto, foco visível, cor nunca é o único sinal.

## 7. Layout

* Mobile first; quebras em 640, 960 e 1200 px; conteúdo até 1200 px; margem
  lateral de 16 px no celular; sem rolagem horizontal da página.
* Home: cabeçalho (logo, menu de competições, relógio de Brasília, seletor de
  tema) → faixa "Últimos gols" com aviso de gol e botões de som/notificação →
  uma seção por competição com **jogos à esquerda e classificação à direita,
  topo alinhado ao primeiro jogo** (empilha no celular).
* Competição: título + temporada, `<select>` de fase, navegação de rodada
  (‹ Rodada 5 ›), jogos e classificação lado a lado; mata-mata vira lista de
  confrontos por rodada com agregado e vencedor.
* Operador: funcional e denso, mesma identidade: login → escolha da partida →
  painel com placar, botões das ações disponíveis, formulário do lance, linha do
  tempo com "cancelar lançamento" e ações de status com `<dialog>` de confirmação.
