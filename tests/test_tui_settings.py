from __future__ import annotations

import dataclasses

import pytest

from buckshotcalc.tui import settings, theme


def test_roundtrip(tmp_path):
    p = tmp_path / "sub" / "tui.toml"
    s = settings.Settings(
        palette="custom", custom=(("fg", "#112233"),), background="solid",
        number_anim="N5", d2=False, motion="reduced", pulse=False,
    )
    settings.save(s, p)
    assert settings.load(p) == s
    hash(s)


def test_custom_key_needing_quotes_roundtrips(tmp_path):
    p = tmp_path / "t.toml"
    s = settings.Settings(palette="custom", custom=(("a b", "#112233"),))
    settings.save(s, p)
    assert settings.load(p) == s


def test_missing_and_garbage_give_defaults(tmp_path):
    assert settings.load(tmp_path / "none.toml") == settings.Settings()
    bad = tmp_path / "bad.toml"
    bad.write_text("this is [not toml")
    assert settings.load(bad) == settings.Settings()


def test_invalid_fields_fall_back_per_field(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(
        'palette = "pink"\nnumber_anim = "N5"\nd1 = "yes"\nd2 = false\nmotion = 3\n'
        'bogus = 1\n[custom]\nfg = "#abcdef"\nacc = "red"\n'
    )
    s = settings.load(p)
    assert s.palette == "amber" and s.number_anim == "N5"
    assert s.d1 is True and s.d2 is False and s.motion == "full"
    assert s.custom == (("fg", "#abcdef"),)


def test_default_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings.default_path() == tmp_path / "buckshotcalc" / "tui.toml"
    settings.save(settings.Settings(palette="red"))
    assert settings.load().palette == "red"


@pytest.mark.parametrize("name", ["amber", "green", "red"])
def test_palette_tokens_valid_hex(name):
    p = theme.palette_for(settings.Settings(palette=name))
    for tok in theme.TOKENS:
        v = getattr(p, tok)
        assert settings._is_hex(v), (name, tok, v)
    assert {f.name for f in dataclasses.fields(p)} == set(theme.TOKENS)


def test_red_live_differs_from_blank():
    assert theme.RED.live != theme.RED.blank


def test_custom_palette_overrides_and_ignores_unknown():
    s = settings.Settings(palette="custom", custom=(("fg", "#010203"), ("nope", "#000000")))
    p = theme.palette_for(s)
    assert p.fg == "#010203" and p.acc == theme.AMBER.acc


def test_transparent_styles_have_no_background():
    for name in ("amber", "green", "red"):
        for tok in theme.TOKENS:
            assert theme.style(settings.Settings(palette=name), tok).bgcolor is None


def test_solid_style_has_background():
    st = theme.style(settings.Settings(background="solid"), "fg")
    assert st.bgcolor is not None


def test_reveal_scale_roundtrips_and_rejects_unknown_values(tmp_path):
    p = tmp_path / "t.toml"
    settings.save(settings.Settings(reveal_scale=0.5), p)
    assert settings.load(p).reveal_scale == 0.5
    p.write_text("reveal_scale = 7\n")
    assert settings.load(p).reveal_scale == 1.0
    p.write_text("reveal_scale = 0\n")
    assert settings.load(p).reveal_scale == 0.0
