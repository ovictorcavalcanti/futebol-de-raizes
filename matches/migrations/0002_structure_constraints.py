"""Estrutura da partida e do confronto garantida pelo banco (docs/PLANO.md, "Restrições
que sustentam as regras"), não só pelo `clean()` do admin:

* `uniq_match_tie_leg`: um jogo por (confronto, ida/volta) — também contra dois "Salvar"
  simultâneos no admin;
* gatilhos (envolvem outras tabelas, fora do alcance de um CHECK), com SQLSTATE
  `check_violation` (o Django levanta `IntegrityError`):
  - `matches`: grupo, rodada e confronto da mesma fase da partida; fase de mata-mata ⇔
    partida com confronto; fase com tabela ⇒ partida com grupo; `leg` ≤ `ties.legs`;
  - `ties`: só em fase de mata-mata, rodada da mesma fase; com jogos, `legs` não fica
    menor que o jogo de volta e a fase não muda (espelha o `TieForm`);
  - `groups`/`rounds`: com partidas (ou confrontos) de uma fase, não mudam de fase;
  - `stages`: com partidas ou confrontos, o formato não muda para um que os deixe inválidos.
"""

from django.db import migrations, models

FORWARD = r"""
CREATE OR REPLACE FUNCTION fdr_matches_check_structure() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_format text;
    v_stage bigint;
    v_legs integer;
BEGIN
    SELECT format INTO v_format FROM stages WHERE id = NEW.stage_id;
    IF FOUND THEN
        IF v_format = 'knockout' AND NEW.tie_id IS NULL THEN
            RAISE EXCEPTION 'Em fase de mata-mata, a partida precisa de um confronto.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_knockout_has_tie', TABLE = 'matches';
        END IF;
        IF v_format <> 'knockout' AND NEW.tie_id IS NOT NULL THEN
            RAISE EXCEPTION 'Partida de fase com tabela não tem confronto.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_table_without_tie', TABLE = 'matches';
        END IF;
        IF v_format <> 'knockout' AND NEW.group_id IS NULL THEN
            RAISE EXCEPTION 'Em fase com tabela, a partida precisa de um grupo.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_table_has_group', TABLE = 'matches';
        END IF;
    END IF;
    IF NEW.group_id IS NOT NULL THEN
        SELECT stage_id INTO v_stage FROM groups WHERE id = NEW.group_id;
        IF FOUND AND v_stage <> NEW.stage_id THEN
            RAISE EXCEPTION 'O grupo precisa ser da mesma fase da partida.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_group_same_stage', TABLE = 'matches';
        END IF;
    END IF;
    IF NEW.round_id IS NOT NULL THEN
        SELECT stage_id INTO v_stage FROM rounds WHERE id = NEW.round_id;
        IF FOUND AND v_stage <> NEW.stage_id THEN
            RAISE EXCEPTION 'A rodada precisa ser da mesma fase da partida.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_round_same_stage', TABLE = 'matches';
        END IF;
    END IF;
    IF NEW.tie_id IS NOT NULL THEN
        SELECT stage_id, legs INTO v_stage, v_legs FROM ties WHERE id = NEW.tie_id;
        IF FOUND THEN
            IF v_stage <> NEW.stage_id THEN
                RAISE EXCEPTION 'O confronto precisa ser da mesma fase da partida.'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'match_tie_same_stage', TABLE = 'matches';
            END IF;
            IF NEW.leg > v_legs THEN
                RAISE EXCEPTION 'O confronto não aceita mais partidas do que o número de jogos.'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'match_leg_within_tie_legs', TABLE = 'matches';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER matches_check_structure
    BEFORE INSERT OR UPDATE OF stage_id, group_id, round_id, tie_id, leg ON matches
    FOR EACH ROW EXECUTE FUNCTION fdr_matches_check_structure();

CREATE OR REPLACE FUNCTION fdr_ties_check_structure() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_format text;
    v_stage bigint;
BEGIN
    SELECT format INTO v_format FROM stages WHERE id = NEW.stage_id;
    IF FOUND AND v_format <> 'knockout' THEN
        RAISE EXCEPTION 'Confronto só existe em fase de mata-mata.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'tie_knockout_stage', TABLE = 'ties';
    END IF;
    SELECT stage_id INTO v_stage FROM rounds WHERE id = NEW.round_id;
    IF FOUND AND v_stage <> NEW.stage_id THEN
        RAISE EXCEPTION 'A rodada precisa ser da mesma fase do confronto.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'tie_round_same_stage', TABLE = 'ties';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF EXISTS (SELECT 1 FROM matches WHERE tie_id = NEW.id AND leg > NEW.legs) THEN
            RAISE EXCEPTION 'O confronto já tem o jogo de volta: não aceita menos jogos.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_leg_within_tie_legs', TABLE = 'ties';
        END IF;
        IF EXISTS (SELECT 1 FROM matches WHERE tie_id = NEW.id AND stage_id <> NEW.stage_id) THEN
            RAISE EXCEPTION 'O confronto já tem jogos: ele não muda de fase.'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'match_tie_same_stage', TABLE = 'ties';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER ties_check_structure
    BEFORE INSERT OR UPDATE OF stage_id, round_id, legs ON ties
    FOR EACH ROW EXECUTE FUNCTION fdr_ties_check_structure();

CREATE OR REPLACE FUNCTION fdr_groups_check_stage() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM matches WHERE group_id = NEW.id AND stage_id <> NEW.stage_id) THEN
        RAISE EXCEPTION 'O grupo já tem partidas: ele não muda de fase.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'match_group_same_stage', TABLE = 'groups';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER groups_check_stage
    BEFORE UPDATE OF stage_id ON groups
    FOR EACH ROW WHEN (OLD.stage_id IS DISTINCT FROM NEW.stage_id)
    EXECUTE FUNCTION fdr_groups_check_stage();

CREATE OR REPLACE FUNCTION fdr_rounds_check_stage() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM matches WHERE round_id = NEW.id AND stage_id <> NEW.stage_id) THEN
        RAISE EXCEPTION 'A rodada já tem partidas: ela não muda de fase.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'match_round_same_stage', TABLE = 'rounds';
    END IF;
    IF EXISTS (SELECT 1 FROM ties WHERE round_id = NEW.id AND stage_id <> NEW.stage_id) THEN
        RAISE EXCEPTION 'A rodada já tem confrontos: ela não muda de fase.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'tie_round_same_stage', TABLE = 'rounds';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rounds_check_stage
    BEFORE UPDATE OF stage_id ON rounds
    FOR EACH ROW WHEN (OLD.stage_id IS DISTINCT FROM NEW.stage_id)
    EXECUTE FUNCTION fdr_rounds_check_stage();

CREATE OR REPLACE FUNCTION fdr_stages_check_format() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.format = 'knockout' AND EXISTS (SELECT 1 FROM matches WHERE stage_id = NEW.id AND tie_id IS NULL) THEN
        RAISE EXCEPTION 'A fase tem partidas sem confronto: ela não vira mata-mata.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'match_knockout_has_tie', TABLE = 'stages';
    END IF;
    IF NEW.format <> 'knockout' AND EXISTS (SELECT 1 FROM ties WHERE stage_id = NEW.id) THEN
        RAISE EXCEPTION 'A fase tem confrontos: ela só pode ser de mata-mata.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'tie_knockout_stage', TABLE = 'stages';
    END IF;
    IF NEW.format <> 'knockout' AND EXISTS (SELECT 1 FROM matches WHERE stage_id = NEW.id AND group_id IS NULL) THEN
        RAISE EXCEPTION 'A fase tem partidas sem grupo: ela não vira fase com tabela.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'match_table_has_group', TABLE = 'stages';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stages_check_format
    BEFORE UPDATE OF format ON stages
    FOR EACH ROW WHEN (OLD.format IS DISTINCT FROM NEW.format)
    EXECUTE FUNCTION fdr_stages_check_format();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS stages_check_format ON stages;
DROP FUNCTION IF EXISTS fdr_stages_check_format();
DROP TRIGGER IF EXISTS rounds_check_stage ON rounds;
DROP FUNCTION IF EXISTS fdr_rounds_check_stage();
DROP TRIGGER IF EXISTS groups_check_stage ON groups;
DROP FUNCTION IF EXISTS fdr_groups_check_stage();
DROP TRIGGER IF EXISTS ties_check_structure ON ties;
DROP FUNCTION IF EXISTS fdr_ties_check_structure();
DROP TRIGGER IF EXISTS matches_check_structure ON matches;
DROP FUNCTION IF EXISTS fdr_matches_check_structure();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("competitions", "0002_zone_no_overlap"),
        ("matches", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="match",
            constraint=models.UniqueConstraint(
                fields=("tie", "leg"),
                name="uniq_match_tie_leg",
                violation_error_message="Já existe uma partida para este jogo do confronto.",
            ),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
