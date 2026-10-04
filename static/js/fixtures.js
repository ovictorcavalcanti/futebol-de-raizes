/**
 * Dados de exemplo no formato exato do docs/CONTRACT.md — usados SÓ pelo guia de
 * estilo (/styleguide.html) para QA visual. Os horários são relativos ao momento
 * em que o módulo carrega, para o card ao vivo mostrar um minuto plausível.
 * Clubes reais de Pernambuco com suas cores; jogadores, árbitros e
 * transmissões são fictícios.
 */

const NOW = Date.now();
const MIN = 60_000;
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
const at = (minutesFromNow) => iso(NOW + minutesFromNow * MIN);

export const SERVER_TIME = iso(NOW);
export const TIMEZONE = 'America/Sao_Paulo';

/* --- Times (TeamOut) ----------------------------------------------------------- */
const team = (id, name, short_name, city, color_primary, color_secondary) => ({ id, name, short_name, city, color_primary, color_secondary, crest_url: '' });

export const TEAMS = {
  sport: team(1, 'Sport Club do Recife', 'SPT', 'Recife', '#D7141A', '#000000'),
  nautico: team(2, 'Clube Náutico Capibaribe', 'NAU', 'Recife', '#E30613', '#FFFFFF'),
  santa: team(3, 'Santa Cruz Futebol Clube', 'SAN', 'Recife', '#111111', '#E2001A'),
  retro: team(4, 'Retrô FC Brasil', 'RET', 'Camaragibe', '#F7C600', '#1F3C88'),
  central: team(5, 'Central Sport Club', 'CEN', 'Caruaru', '#000000', '#FFFFFF'),
  salgueiro: team(6, 'Salgueiro Atlético Clube', 'SAL', 'Salgueiro', '#D11A2A', '#0E7A3A'),
  ibis: team(7, 'Íbis Sport Club', 'ÍBI', 'Paulista', '#B5121B', '#000000'),
  afogados: team(8, 'Afogados da Ingazeira FC', 'AFO', 'Afogados da Ingazeira', '#1C3F94', '#D21F2B'),
};
const T = TEAMS;

/* --- Competições, fases, rodadas ------------------------------------------------ */
export const COMPETITIONS = [
  { id: 1, name: 'Pernambucano Raiz', slug: 'pernambucano', short_name: 'PE Raiz', position: 1 },
  { id: 2, name: 'Copa Pernambuco', slug: 'copa-pernambuco', short_name: 'Copa PE', position: 2 },
  { id: 3, name: 'Série A2 Pernambucana', slug: 'serie-a2', short_name: 'A2 PE', position: 3 },
  { id: 4, name: 'Copa do Interior', slug: 'copa-do-interior', short_name: 'Interior', position: 4 },
];
const PE = { id: 1, name: 'Pernambucano Raiz', slug: 'pernambucano', short_name: 'PE Raiz' };
const COPA = { id: 2, name: 'Copa Pernambuco', slug: 'copa-pernambuco', short_name: 'Copa PE' };
const LEAGUE_STAGE = { id: 3, name: '1ª fase', format: 'league' };
const KO_STAGE = { id: 7, name: 'Mata-mata', format: 'knockout' };
const TABLE_GROUP = { id: 4, name: 'Tabela' };
const ROUND5 = { id: 9, number: 5, name: 'Rodada 5' };
const ROUND6 = { id: 10, number: 6, name: 'Rodada 6' };
const SEMI = { id: 30, number: 1, name: 'Semifinal' };

/* --- Lances (EventOut) -------------------------------------------------------------- */
const PERIOD = {
  first_half: ['1º tempo', '1T'],
  half_time: ['Intervalo', 'INT'],
  second_half: ['2º tempo', '2T'],
  extra_time: ['Prorrogação', 'PRO'],
  penalties: ['Pênaltis', 'PÊN'],
};
const TYPE_LABEL = {
  match_start: 'Início de jogo', half_time: 'Fim do 1º tempo', second_half_start: 'Início do 2º tempo',
  extra_time_start: 'Início da prorrogação', penalties_start: 'Início dos pênaltis', match_end: 'Fim de jogo',
  goal: 'Gol', goal_annulled: 'Gol anulado', penalty_awarded: 'Pênalti marcado', penalty_missed: 'Pênalti perdido',
  var_review: 'Revisão do VAR', substitution: 'Substituição', yellow_card: 'Cartão amarelo', red_card: 'Cartão vermelho',
  stoppage_time: 'Acréscimos', shootout_kick: 'Cobrança de pênalti', suspended: 'Suspenso', postponed: 'Adiado',
};
const KIND = (type) => (['match_start', 'half_time', 'second_half_start', 'extra_time_start', 'penalties_start', 'match_end'].includes(type) ? 'structural' : ['suspended', 'postponed', 'resumed', 'rescheduled', 'cancelled'].includes(type) ? 'status' : 'game');

function makeEvents(match, specs, startMs) {
  let id = match.id * 100;
  return specs.map((sp, i) => {
    const [period_label, period_short] = PERIOD[sp.period] || [null, null];
    const side = sp.side || null;
    const teamObj = side ? match[side] : null;
    const player = sp.payload?.player ?? null;
    const eventId = sp.id ?? ++id;
    if (sp.id) id = sp.id;
    return {
      id: eventId,
      sequence: i + 1,
      type: sp.type,
      type_label: TYPE_LABEL[sp.type] || sp.type,
      kind: KIND(sp.type),
      period: sp.period || null,
      period_label,
      period_short,
      minute: sp.minute ?? null,
      stoppage: sp.stoppage ?? null,
      minute_label: sp.minute == null ? '' : sp.stoppage ? `${sp.minute}+${sp.stoppage}'` : `${sp.minute}'`,
      team_id: teamObj ? teamObj.id : null,
      team_side: side,
      player: { id: null, name: player },
      payload: sp.payload || {},
      annuls_event_id: sp.annuls ?? null,
      annulled: !!sp.annulled,
      derived: !!sp.derived,
      score_after: sp.score || null,
      created_at: iso(startMs + (sp.at ?? i) * MIN),
    };
  });
}

function goalsOf(match) {
  return match.events.filter((e) => e.type === 'goal' && !e.annulled).map((e) => ({
    event_id: e.id, match_id: match.id, team_id: e.team_id, team_side: e.team_side, player: e.payload.player,
    origin: e.payload.origin || 'open_play', period: e.period, minute: e.minute, stoppage: e.stoppage,
    minute_label: e.minute_label, score_after: e.score_after, created_at: e.created_at,
  }));
}

function cardsOf(match) {
  const count = { home: { yellow: 0, red: 0 }, away: { yellow: 0, red: 0 } };
  const reds = [];
  for (const e of match.events || []) {
    if (e.type === 'yellow_card') count[e.team_side].yellow++;
    if (e.type === 'red_card') {
      count[e.team_side].red++;
      reds.push({ team_side: e.team_side, player: e.payload.player, minute_label: e.minute_label });
    }
  }
  return { cards: count, red_cards: reds };
}

const NO_CLOCK = null;
function baseMatch(o) {
  const statusLabel = { scheduled: 'Agendado', live: 'Ao vivo', finished: 'Encerrado', postponed: 'Adiado', suspended: 'Suspenso', cancelled: 'Cancelado' }[o.status];
  const [period_label, period_short] = PERIOD[o.period] || [null, null];
  return {
    id: o.id,
    competition: o.competition || PE,
    stage: o.stage || LEAGUE_STAGE,
    group: o.group === undefined ? TABLE_GROUP : o.group,
    round: o.round || ROUND5,
    kickoff_at: o.kickoff_at,
    finished_at: o.finished_at || null,
    venue: o.venue,
    city: o.city,
    status: o.status,
    status_label: statusLabel,
    period: o.period || null,
    period_label,
    period_short,
    period_started_at: o.period_started_at || null,
    clock: o.clock ?? NO_CLOCK,
    home: o.home,
    away: o.away,
    home_score: o.home_score ?? 0,
    away_score: o.away_score ?? 0,
    home_penalties: o.home_penalties ?? null,
    away_penalties: o.away_penalties ?? null,
    winner: o.winner ?? null,
    version: o.version ?? 1,
    tie: o.tie ?? null,
    goals: [],
    cards: { home: { yellow: 0, red: 0 }, away: { yellow: 0, red: 0 } },
    red_cards: [],
  };
}

function withEvents(match, specs, startMs, detail = {}) {
  match.events = makeEvents(match, specs, startMs);
  match.goals = goalsOf(match);
  Object.assign(match, cardsOf(match));
  return Object.assign(match, {
    lineups: { home: null, away: null },
    officials: [],
    broadcasts: [],
    stats: [],
    attendance: null,
    revenue_cents: null,
    ...detail,
  });
}

/** MatchOut resumido (sem os campos de detalhe), como vem em GET /api/home. */
export function summaryOf(match) {
  const { events, lineups, officials, broadcasts, stats, attendance, revenue_cents, ...rest } = match;
  return rest;
}

const p = (number, name, position) => ({ name, number, position });

/* 1. AO VIVO — 2º tempo, 72', com gol anulado, vermelho por 2º amarelo, escalações e ficha */
const live2T = NOW - 26.5 * MIN;
const liveKickoff = live2T - 64 * MIN;
const LIVE = baseMatch({
  id: 12, status: 'live', period: 'second_half', period_started_at: iso(live2T), kickoff_at: iso(liveKickoff),
  clock: { running: true, offset: 45, regular_end: 90, stoppage_announced: null },
  home: T.sport, away: T.nautico, home_score: 2, away_score: 1, venue: 'Ilha do Retiro', city: 'Recife, PE', version: 17,
});
withEvents(LIVE, [
  { type: 'match_start', period: 'first_half', minute: 0, at: 0 },
  { type: 'goal', period: 'first_half', minute: 18, side: 'home', payload: { player: 'Zé Roberto', origin: 'open_play', assist: 'Biel Ventura' }, score: { home: 1, away: 0 }, at: 18 },
  { type: 'yellow_card', period: 'first_half', minute: 23, side: 'away', payload: { player: 'Thiago Freitas' }, at: 23 },
  { type: 'var_review', period: 'first_half', minute: 31, payload: { incident: 'Possível pênalti em Biel Ventura', decision: 'Lance normal, segue o jogo' }, at: 31 },
  { id: 1205, type: 'goal', period: 'first_half', minute: 34, side: 'away', payload: { player: 'Kayo Lima', origin: 'open_play' }, annulled: true, at: 34 },
  { type: 'goal_annulled', period: 'first_half', minute: 36, side: 'away', annuls: 1205, payload: { reason: 'Impedimento marcado pelo VAR' }, at: 36 },
  { type: 'stoppage_time', period: 'first_half', minute: 45, payload: { minutes: 2 }, at: 45 },
  { type: 'half_time', period: 'first_half', minute: 45, stoppage: 2, at: 47 },
  { type: 'second_half_start', period: 'second_half', minute: 45, at: 64 },
  { type: 'substitution', period: 'second_half', minute: 55, side: 'home', payload: { player_out: 'Biel Ventura', player_in: 'Lucas Arcanjo' }, at: 74 },
  { type: 'goal', period: 'second_half', minute: 56, side: 'home', payload: { player: 'Lucas Arcanjo', origin: 'open_play' }, score: { home: 2, away: 0 }, at: 75 },
  { type: 'penalty_awarded', period: 'second_half', minute: 62, side: 'away', at: 81 },
  { type: 'goal', period: 'second_half', minute: 63, side: 'away', payload: { player: 'Paulo Sérgio', origin: 'penalty' }, score: { home: 2, away: 1 }, at: 82 },
  { type: 'yellow_card', period: 'second_half', minute: 66, side: 'home', payload: { player: 'Zé Roberto' }, at: 85 },
  { type: 'yellow_card', period: 'second_half', minute: 68, side: 'away', payload: { player: 'Thiago Freitas' }, at: 87 },
  { type: 'red_card', period: 'second_half', minute: 68, side: 'away', payload: { player: 'Thiago Freitas', reason: 'second_yellow', derived_from_sequence: 15 }, derived: true, at: 87 },
  { type: 'substitution', period: 'second_half', minute: 70, side: 'away', payload: { player_out: 'Kayo Lima', player_in: 'Dudu Capibaribe' }, at: 89 },
], liveKickoff, {
  lineups: {
    home: {
      formation: '4-3-3', coach: 'Mano Azevedo',
      starters: [p(1, 'Caíque França', 'GK'), p(2, 'Pedro Lima', 'DF'), p(4, 'Rafael Thyere', 'DF'), p(3, 'Chico Mendes', 'DF'), p(6, 'Felipinho', 'DF'), p(5, 'Fabinho Recife', 'MF'), p(8, 'Biel Ventura', 'MF'), p(10, 'Zé Roberto', 'MF'), p(7, 'Romarinho', 'FW'), p(9, 'Gustavo Leão', 'FW'), p(11, 'Barletta', 'FW')],
      substitutes: [p(12, 'Thiago Couto', 'GK'), p(14, 'Lucas Arcanjo', 'MF'), p(15, 'Igor Cariús', 'DF'), p(17, 'Pablo Dantas', 'FW'), p(19, 'Juninho Moura', 'MF')],
    },
    away: {
      formation: '4-2-3-1', coach: 'Hélio dos Anjos Neto',
      starters: [p(1, 'Vagner Alves', 'GK'), p(2, 'Sousa Pinheiro', 'DF'), p(3, 'Bruno Bispo', 'DF'), p(4, 'Mateus Silva', 'DF'), p(6, 'Igor Fernandes', 'DF'), p(5, 'Thiago Freitas', 'MF'), p(8, 'Wenderson', 'MF'), p(7, 'Paulo Sérgio', 'MF'), p(10, 'Patrick Allan', 'MF'), p(11, 'Marco Antônio', 'MF'), p(9, 'Kayo Lima', 'FW')],
      substitutes: [p(12, 'Lucas Perri', 'GK'), p(16, 'Dudu Capibaribe', 'FW'), p(18, 'Rhaldney', 'MF'), p(20, 'Victor Ferraz', 'DF')],
    },
  },
  officials: [
    { role: 'referee', role_label: 'Árbitro', name: 'Anderson Bezerra', state: 'PE' },
    { role: 'assistant', role_label: 'Assistentes', name: 'Clóvis Amaral', state: 'PE' },
    { role: 'assistant', role_label: 'Assistentes', name: 'Bruna Alves', state: 'PE' },
    { role: 'fourth', role_label: 'Quarto árbitro', name: 'José Woshington', state: 'PE' },
    { role: 'var', role_label: 'VAR', name: 'Daiane Muniz', state: 'SP' },
  ],
  broadcasts: [
    { name: 'TV Capibaribe', url: 'https://example.com/tv', kind: 'open_tv', kind_label: 'TV aberta' },
    { name: 'Raízes Play', url: 'https://example.com/play', kind: 'streaming', kind_label: 'Streaming' },
    { name: 'Rádio Frevo AM', url: '', kind: 'radio', kind_label: 'Rádio' },
  ],
  stats: [
    { key: 'possession', label: 'Posse de bola (%)', home: 58, away: 42 },
    { key: 'shots', label: 'Finalizações', home: 14, away: 9 },
    { key: 'shots_on_target', label: 'Finalizações no gol', home: 6, away: 3 },
    { key: 'corners', label: 'Escanteios', home: 7, away: 4 },
    { key: 'fouls', label: 'Faltas', home: 11, away: 15 },
    { key: 'offsides', label: 'Impedimentos', home: 2, away: 3 },
  ],
  attendance: 28417,
  revenue_cents: 98765400,
});

/* 2. INTERVALO */
const HALF_TIME = baseMatch({
  id: 13, status: 'live', period: 'half_time', period_started_at: at(-6), kickoff_at: at(-53),
  home: T.santa, away: T.retro, home_score: 0, away_score: 0, venue: 'Estádio do Arruda', city: 'Recife, PE', version: 6,
});
withEvents(HALF_TIME, [
  { type: 'match_start', period: 'first_half', minute: 0, at: 0 },
  { type: 'yellow_card', period: 'first_half', minute: 29, side: 'away', payload: { player: 'Mascote' }, at: 29 },
  { type: 'stoppage_time', period: 'first_half', minute: 45, payload: { minutes: 3 }, at: 45 },
  { type: 'half_time', period: 'first_half', minute: 45, stoppage: 3, at: 48 },
], NOW - 53 * MIN);

/* 3. AGENDADO hoje / 4. AGENDADO amanhã */
const later = Math.ceil((NOW + 150 * MIN) / (30 * MIN)) * 30 * MIN;
const SCHEDULED_TODAY = baseMatch({ id: 14, status: 'scheduled', kickoff_at: iso(later), home: T.central, away: T.salgueiro, venue: 'Lacerdão', city: 'Caruaru, PE' });
const SCHEDULED_TOMORROW = baseMatch({ id: 18, status: 'scheduled', kickoff_at: iso(later + 24 * 60 * MIN), home: T.afogados, away: T.santa, venue: 'Vianão', city: 'Afogados da Ingazeira, PE', round: ROUND6 });

/* 5. ENCERRADO — vitória do visitante */
const FINISHED = baseMatch({
  id: 15, status: 'finished', kickoff_at: at(-200), finished_at: at(-95), home: T.ibis, away: T.afogados,
  home_score: 1, away_score: 2, winner: 'away', venue: 'Ademir Cunha', city: 'Paulista, PE', version: 22,
});
withEvents(FINISHED, [
  { type: 'match_start', period: 'first_half', minute: 0, at: 0 },
  { type: 'goal', period: 'first_half', minute: 9, side: 'away', payload: { player: 'Netinho do Pajeú', origin: 'open_play' }, score: { home: 0, away: 1 }, at: 9 },
  { type: 'goal', period: 'first_half', minute: 40, side: 'home', payload: { player: 'Mauro Shampoo Jr.', origin: 'open_play' }, score: { home: 1, away: 1 }, at: 40 },
  { type: 'half_time', period: 'first_half', minute: 45, at: 46 },
  { type: 'second_half_start', period: 'second_half', minute: 45, at: 62 },
  { type: 'goal', period: 'second_half', minute: 90, stoppage: 3, side: 'away', payload: { player: 'Ronaldo Sertanejo', origin: 'own_goal' }, score: { home: 1, away: 2 }, at: 110 },
  { type: 'match_end', period: 'second_half', minute: 90, stoppage: 5, at: 112 },
], NOW - 200 * MIN, { attendance: 1203, revenue_cents: 1804500 });

/* 6. ADIADO, 7. SUSPENSO, 8. CANCELADO */
const POSTPONED = baseMatch({ id: 16, status: 'postponed', kickoff_at: at(60 * 24 * 3), home: T.retro, away: T.sport, venue: 'Arena de Pernambuco', city: 'São Lourenço da Mata, PE', round: ROUND6 });
const SUSPENDED = baseMatch({
  id: 17, status: 'suspended', period: 'second_half', period_started_at: at(-30), kickoff_at: at(-95),
  clock: { running: false, offset: 45, regular_end: 90, stoppage_announced: null },
  home: T.nautico, away: T.central, home_score: 1, away_score: 1, venue: 'Aflitos', city: 'Recife, PE', version: 9,
});
withEvents(SUSPENDED, [
  { type: 'match_start', period: 'first_half', minute: 0, at: 0 },
  { type: 'goal', period: 'first_half', minute: 21, side: 'away', payload: { player: 'Tiago Agreste', origin: 'open_play' }, score: { home: 0, away: 1 }, at: 21 },
  { type: 'half_time', period: 'first_half', minute: 45, at: 46 },
  { type: 'second_half_start', period: 'second_half', minute: 45, at: 62 },
  { type: 'goal', period: 'second_half', minute: 52, side: 'home', payload: { player: 'Jean Carlos Recife', origin: 'penalty' }, score: { home: 1, away: 1 }, at: 69 },
  { type: 'suspended', period: 'second_half', payload: { reason: 'Chuva forte e campo alagado' }, at: 75 },
], NOW - 95 * MIN);
const CANCELLED = baseMatch({ id: 19, status: 'cancelled', kickoff_at: at(-60 * 24), home: T.salgueiro, away: T.ibis, venue: 'Cornélio de Barros', city: 'Salgueiro, PE', round: ROUND6 });

/* 9. MATA-MATA — volta decidida nos pênaltis, com agregado */
const tieA = {
  id: 4, legs: 2, extra_time: false, position: 1, round: SEMI, team_a: T.santa, team_b: T.nautico,
  aggregate: { team_a: 3, team_b: 3 }, winner_team_id: T.nautico.id, decided_by: 'penalties', decided_by_label: 'nos pênaltis', complete: true,
};
const KO_LEG1 = baseMatch({
  id: 20, competition: COPA, stage: KO_STAGE, group: null, round: SEMI, status: 'finished', kickoff_at: at(-60 * 24 * 7), finished_at: at(-60 * 24 * 7 + 110),
  home: T.santa, away: T.nautico, home_score: 2, away_score: 1, winner: 'home', venue: 'Estádio do Arruda', city: 'Recife, PE', tie: { ...tieA, leg: 1 },
});
const koStart = NOW - 170 * MIN;
const KO_FINAL = baseMatch({
  id: 21, competition: COPA, stage: KO_STAGE, group: null, round: SEMI, status: 'finished', period: 'penalties', kickoff_at: iso(koStart), finished_at: at(-40),
  home: T.nautico, away: T.santa, home_score: 2, away_score: 1, home_penalties: 4, away_penalties: 3, winner: 'home',
  venue: 'Aflitos', city: 'Recife, PE', tie: { ...tieA, leg: 2 }, version: 31,
});
withEvents(KO_FINAL, [
  { type: 'match_start', period: 'first_half', minute: 0, at: 0 },
  { type: 'goal', period: 'first_half', minute: 12, side: 'away', payload: { player: 'Caio Arruda', origin: 'open_play' }, score: { home: 0, away: 1 }, at: 12 },
  { type: 'goal', period: 'first_half', minute: 44, side: 'home', payload: { player: 'Paulo Sérgio', origin: 'open_play' }, score: { home: 1, away: 1 }, at: 44 },
  { type: 'half_time', period: 'first_half', minute: 45, stoppage: 1, at: 46 },
  { type: 'second_half_start', period: 'second_half', minute: 45, at: 62 },
  { type: 'goal', period: 'second_half', minute: 81, side: 'home', payload: { player: 'Kayo Lima', origin: 'open_play' }, score: { home: 2, away: 1 }, at: 98 },
  { type: 'penalties_start', period: 'penalties', minute: 90, at: 112 },
  { type: 'shootout_kick', period: 'penalties', side: 'home', payload: { player: 'Paulo Sérgio', scored: true }, at: 114 },
  { type: 'shootout_kick', period: 'penalties', side: 'away', payload: { player: 'Caio Arruda', scored: true }, at: 115 },
  { type: 'shootout_kick', period: 'penalties', side: 'home', payload: { player: 'Wenderson', scored: true }, at: 116 },
  { type: 'shootout_kick', period: 'penalties', side: 'away', payload: { player: 'Thiaguinho', scored: false }, at: 117 },
  { type: 'shootout_kick', period: 'penalties', side: 'home', payload: { player: 'Kayo Lima', scored: false }, at: 118 },
  { type: 'shootout_kick', period: 'penalties', side: 'away', payload: { player: 'Pipico Neto', scored: true }, at: 119 },
  { type: 'shootout_kick', period: 'penalties', side: 'home', payload: { player: 'Patrick Allan', scored: true }, at: 120 },
  { type: 'shootout_kick', period: 'penalties', side: 'away', payload: { player: 'Danny Morais', scored: true }, at: 121 },
  { type: 'shootout_kick', period: 'penalties', side: 'home', payload: { player: 'Bruno Bispo', scored: true }, at: 122 },
  { type: 'shootout_kick', period: 'penalties', side: 'away', payload: { player: 'Lucas Tricolor', scored: false }, at: 123 },
  { type: 'match_end', period: 'penalties', minute: 90, at: 124 },
], koStart, { attendance: 17950, revenue_cents: 61450000 });

const tieB = {
  id: 5, legs: 1, extra_time: true, position: 2, round: SEMI, team_a: T.sport, team_b: T.retro,
  aggregate: { team_a: 0, team_b: 0 }, winner_team_id: null, decided_by: '', decided_by_label: '', complete: false,
};
const KO_SINGLE = baseMatch({
  id: 22, competition: COPA, stage: KO_STAGE, group: null, round: SEMI, status: 'scheduled', kickoff_at: iso(later + 24 * 60 * MIN + 60 * MIN),
  home: T.sport, away: T.retro, venue: 'Ilha do Retiro', city: 'Recife, PE', tie: { ...tieB, leg: 1 },
});

export const MATCHES = {
  live: LIVE,
  halfTime: HALF_TIME,
  scheduledToday: SCHEDULED_TODAY,
  scheduledTomorrow: SCHEDULED_TOMORROW,
  finished: FINISHED,
  postponed: POSTPONED,
  suspended: SUSPENDED,
  cancelled: CANCELLED,
  knockoutLeg1: KO_LEG1,
  knockoutPenalties: KO_FINAL,
  knockoutSingle: KO_SINGLE,
};

/* --- Confrontos (TieDetailOut) -------------------------------------------------------- */
export const TIES = [
  { ...tieA, matches: [summaryOf(KO_LEG1), summaryOf(KO_FINAL)] },
  { ...tieB, matches: [summaryOf(KO_SINGLE)] },
];

/* --- Classificação (StageStandingsOut) ---------------------------------------------------- */
const ZONE_SEMI = { name: 'Semifinal', color: '#1B7F3B' };
const ZONE_DOWN = { name: 'Rebaixamento', color: '#C8102E' };
const row = (position, t, played, won, drawn, lost, gf, ga, extra = {}) => ({
  position, team: t, played, won, drawn, lost, goals_for: gf, goals_against: ga, goal_difference: gf - ga,
  points: won * 3 + drawn, yellow_cards: (t.id * 3) % 9, red_cards: t.id % 3 === 0 ? 1 : 0, tied: false,
  zone: position <= 4 ? ZONE_SEMI : position >= 7 ? ZONE_DOWN : null, playing: false, ...extra,
});

export const STANDINGS = {
  stage_id: 3,
  stage_name: '1ª fase',
  kind: 'live',
  points: { win: 3, draw: 1, loss: 0 },
  criteria: [
    { key: 'points', label: 'Pontos' },
    { key: 'wins', label: 'Vitórias' },
    { key: 'goal_difference', label: 'Saldo de gols' },
    { key: 'goals_for', label: 'Gols pró' },
    { key: 'head_to_head', label: 'Confronto direto' },
  ],
  legend: [
    { name: 'Semifinal', color: '#1B7F3B', from: 1, to: 4 },
    { name: 'Rebaixamento', color: '#C8102E', from: 7, to: 8 },
  ],
  groups: [{
    id: 4,
    name: 'Tabela',
    rows: [
      row(1, T.sport, 5, 4, 1, 0, 11, 3, { playing: true }),
      row(2, T.afogados, 5, 3, 1, 1, 8, 5),
      row(3, T.santa, 5, 2, 3, 0, 6, 3, { playing: true }),
      row(4, T.retro, 5, 2, 2, 1, 7, 5, { playing: true }),
      row(5, T.central, 4, 2, 0, 2, 5, 6, { tied: true }),
      row(6, T.salgueiro, 4, 2, 0, 2, 5, 6, { tied: true }),
      row(7, T.nautico, 5, 1, 1, 3, 5, 9, { playing: true }),
      row(8, T.ibis, 5, 0, 0, 5, 2, 12),
    ],
  }],
};

export const STANDINGS_GROUPS = {
  stage_id: 8,
  stage_name: 'Fase de grupos',
  kind: 'official',
  points: { win: 3, draw: 1, loss: 0 },
  criteria: STANDINGS.criteria.slice(0, 4),
  legend: [{ name: 'Classificados', color: '#12306B', from: 1, to: 2 }],
  groups: [
    { id: 11, name: 'Grupo A', rows: [row(1, T.sport, 3, 2, 1, 0, 6, 2), row(2, T.central, 3, 1, 1, 1, 3, 3), row(3, T.ibis, 3, 1, 0, 2, 2, 4), row(4, T.retro, 3, 0, 2, 1, 2, 4)].map((r) => ({ ...r, zone: r.position <= 2 ? { name: 'Classificados', color: '#12306B' } : null })) },
    { id: 12, name: 'Grupo B', rows: [row(1, T.nautico, 3, 3, 0, 0, 7, 1), row(2, T.santa, 3, 1, 1, 1, 4, 4), row(3, T.salgueiro, 3, 1, 0, 2, 3, 5), row(4, T.afogados, 3, 0, 1, 2, 1, 5)].map((r) => ({ ...r, zone: r.position <= 2 ? { name: 'Classificados', color: '#12306B' } : null })) },
  ],
};

/* --- Últimos gols (LatestGoalOut) --------------------------------------------------------------- */
function latest(match, goal) {
  return {
    ...goal,
    match: { id: match.id, competition: { name: match.competition.name, slug: match.competition.slug }, home: match.home, away: match.away },
    team: match[goal.team_side],
  };
}
export const LATEST_GOALS = [...LIVE.goals.map((g) => latest(LIVE, g)), ...FINISHED.goals.map((g) => latest(FINISHED, g)), ...KO_FINAL.goals.map((g) => latest(KO_FINAL, g))]
  .sort((a, b) => b.created_at.localeCompare(a.created_at))
  .slice(0, 10);

/* --- HomeOut / CompetitionOut ---------------------------------------------------------------- */
export const HOME = {
  date: new Intl.DateTimeFormat('en-CA', { timeZone: TIMEZONE }).format(NOW),
  server_time: SERVER_TIME,
  timezone: TIMEZONE,
  cursor: 4182,
  competitions: [
    { ...COMPETITIONS[0], stages: [{ ...LEAGUE_STAGE, matches: [LIVE, HALF_TIME, SCHEDULED_TODAY, FINISHED].map(summaryOf), standings: STANDINGS }] },
    { ...COMPETITIONS[1], stages: [{ ...KO_STAGE, matches: [summaryOf(KO_FINAL)], standings: null }] },
  ],
  latest_goals: LATEST_GOALS,
};

export const COMPETITION = {
  server_time: SERVER_TIME,
  timezone: TIMEZONE,
  cursor: 4182,
  competition: COMPETITIONS[1],
  season: { id: 2, year: 2026 },
  stages: [
    { id: 6, name: 'Fase de grupos', format: 'groups', position: 1, rounds: [{ id: 25, number: 1, name: 'Rodada 1' }, { id: 26, number: 2, name: 'Rodada 2' }, { id: 27, number: 3, name: 'Rodada 3' }] },
    { id: 7, name: 'Mata-mata', format: 'knockout', position: 2, rounds: [SEMI, { id: 31, number: 2, name: 'Final' }] },
  ],
  current_stage_id: 7,
  current_round_id: 30,
  stage: { id: 7, name: 'Mata-mata', format: 'knockout', standings: null, matches: [KO_LEG1, KO_FINAL, KO_SINGLE].map(summaryOf), ties: TIES },
};

/* --- Operador: usuário e catálogo (EventSpecOut) ------------------------------------------------- */
export const ME = {
  authenticated: true,
  user: { id: 3, username: 'operador', name: 'Maria do Frevo', roles: ['Operador'], permissions: { post_event: true, void_event: true, change_status: true, manage_users: false, admin_site: false } },
  csrf_token: 'exemplo',
};

const field = (name, kind, label, required = true, choices = []) => ({ name, kind, label, required, choices });
export const CATALOG = {
  events: [
    { type: 'match_start', label: 'Início de jogo', kind: 'structural', icon: 'whistle', minute: 'optional', periods: [], fields: [] },
    { type: 'half_time', label: 'Fim do 1º tempo', kind: 'structural', icon: 'whistle', minute: 'optional', periods: [], fields: [] },
    { type: 'second_half_start', label: 'Início do 2º tempo', kind: 'structural', icon: 'whistle', minute: 'optional', periods: [], fields: [] },
    { type: 'match_end', label: 'Fim de jogo', kind: 'structural', icon: 'flag', minute: 'optional', periods: [], fields: [] },
    { type: 'goal', label: 'Gol', kind: 'game', icon: 'ball', minute: 'required', periods: ['first_half', 'second_half', 'extra_time'], fields: [field('team_id', 'team', 'Time beneficiado'), field('payload.player', 'player', 'Jogador'), field('payload.origin', 'choice', 'Origem', false, [['open_play', 'Jogada'], ['penalty', 'Pênalti'], ['own_goal', 'Contra']]), field('payload.assist', 'player', 'Assistência', false)] },
    { type: 'goal_annulled', label: 'Gol anulado', kind: 'game', icon: 'ball-x', minute: 'required', periods: [], fields: [field('annuls_event_id', 'event_ref', 'Gol anulado', false), field('payload.reason', 'text', 'Motivo')] },
    { type: 'yellow_card', label: 'Cartão amarelo', kind: 'game', icon: 'card-yellow', minute: 'required', periods: [], fields: [field('team_id', 'team', 'Time'), field('payload.player', 'player', 'Jogador')] },
    { type: 'red_card', label: 'Cartão vermelho', kind: 'game', icon: 'card-red', minute: 'required', periods: [], fields: [field('team_id', 'team', 'Time'), field('payload.player', 'player', 'Jogador')] },
    { type: 'substitution', label: 'Substituição', kind: 'game', icon: 'sub', minute: 'required', periods: [], fields: [field('team_id', 'team', 'Time'), field('payload.player_out', 'player', 'Sai'), field('payload.player_in', 'player', 'Entra')] },
    { type: 'penalty_awarded', label: 'Pênalti marcado', kind: 'game', icon: 'ball-penalty', minute: 'required', periods: [], fields: [field('team_id', 'team', 'Time a favor')] },
    { type: 'penalty_missed', label: 'Pênalti perdido', kind: 'game', icon: 'x-circle', minute: 'required', periods: [], fields: [field('team_id', 'team', 'Time'), field('payload.player', 'player', 'Cobrador')] },
    { type: 'var_review', label: 'Revisão do VAR', kind: 'game', icon: 'var', minute: 'required', periods: [], fields: [field('payload.incident', 'text', 'Lance revisado'), field('payload.decision', 'text', 'Decisão')] },
    { type: 'stoppage_time', label: 'Acréscimos', kind: 'game', icon: 'clock', minute: 'required', periods: [], fields: [field('payload.minutes', 'int', 'Minutos de acréscimo')] },
  ],
  status_actions: [
    { action: 'postpone', label: 'Adiar' },
    { action: 'suspend', label: 'Suspender' },
    { action: 'resume', label: 'Retomar' },
    { action: 'reschedule', label: 'Reagendar' },
    { action: 'cancel', label: 'Cancelar' },
  ],
};
export const AVAILABLE = { events: ['goal', 'goal_annulled', 'yellow_card', 'red_card', 'substitution', 'penalty_awarded', 'penalty_missed', 'var_review', 'stoppage_time', 'match_end'], status: ['suspend'] };
