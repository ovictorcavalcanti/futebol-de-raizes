"""Temporada de ano único (2026) ou que cruza o ano (2026/2027, como as europeias)."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from competitions.models import Competition, Season
from tests.factories import make_competition
from tests.test_admin import change_url, post_data

pytestmark = pytest.mark.django_db


def test_label_and_str():
    comp = Competition.objects.create(name="Premier League", slug="premier")
    single = Season.objects.create(competition=comp, year=2026)
    split = Season.objects.create(competition=comp, year=2027, end_year=2028)
    assert (single.label, split.label) == ("2026", "2027/2028")
    assert str(split) == "Premier League 2027/2028"


def test_end_year_must_come_after_start():
    comp = Competition.objects.create(name="La Liga", slug="laliga")
    season = Season(competition=comp, year=2026, end_year=2026)
    with pytest.raises(ValidationError, match="O ano final vem depois do ano de início"):
        season.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        season.save()


def test_competition_api_returns_the_label(client):
    comp, season = make_competition(slug="premier")
    Season.objects.filter(pk=season.pk).update(year=2026, end_year=2027)
    data = client.get("/api/competitions/premier").json()
    assert data["season"] == {"id": season.id, "year": 2026, "end_year": 2027, "label": "2026/2027"}


def test_admin_creates_split_season_inline(admin_client_fdr):
    comp = Competition.objects.create(name="Serie A", slug="serie-a")
    data = post_data(admin_client_fdr.get(change_url(comp)))
    data.update({"seasons-TOTAL_FORMS": "1", "seasons-0-year": "2026", "seasons-0-end_year": "2027"})
    response = admin_client_fdr.post(change_url(comp), data)
    assert response.status_code == 302, response.content.decode()[:2000]
    assert Season.objects.get(competition=comp).label == "2026/2027"
    data = post_data(admin_client_fdr.get(change_url(comp)))
    data.update({"seasons-TOTAL_FORMS": "2", "seasons-1-year": "2028", "seasons-1-end_year": "2027"})
    response = admin_client_fdr.post(change_url(comp), data)
    assert response.status_code == 200 and "O ano final vem depois do ano de início" in response.content.decode()
