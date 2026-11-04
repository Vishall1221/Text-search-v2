import pytest

from app.fusion import Hit, dedupe, fuse

H = Hit


def test_rrf_is_rank_based_and_ignores_raw_scores():
    ch = {"a": [H("X", None, 99.0), H("Y", None, 1.0)], "b": [H("Y", None, 0.001), H("X", None, 0.0009)]}
    out = fuse(ch, {"a": 1, "b": 1}, set())
    assert [r.asset_key for r in out] == ["X", "Y"]                  # tie on score -> asset_key order
    assert out[0].score == pytest.approx(1 / 61 + 1 / 62)


def test_weights_change_the_order():
    ch = {"a": [H("X", None, 1)], "b": [H("Y", None, 1)]}
    assert fuse(ch, {"a": 1.0, "b": 0.5}, set())[0].asset_key == "X"
    assert fuse(ch, {"a": 0.5, "b": 1.0}, set())[0].asset_key == "Y"
    assert [r.asset_key for r in fuse(ch, {"a": 1.0, "b": 0.0}, set())] == ["X"]       # weight 0 = off


def test_maxp_one_vote_per_asset_per_channel():
    scenes = [H("X", f"s{i}", 1) for i in range(5)] + [H("Y", "s1", 1)]
    out = fuse({"scene": scenes}, {"scene": 1}, {"scene"})
    x, y = out[0], out[1]
    assert x.asset_key == "X" and x.ranks["scene"] == 1 and y.ranks["scene"] == 2         # not rank 6
    assert x.score == pytest.approx(1 / 61)                                                 # one vote
    assert [s.scene_id for s in x.scenes] == ["s0", "s1", "s2", "s3", "s4"]


def test_multi_scene_bonus_is_off_by_default_and_optional():
    scenes = [H("X", "s0", 1), H("X", "s1", 1), H("Y", "s9", 1)]
    base = fuse({"scene": scenes}, {"scene": 1}, {"scene"})
    assert [r.asset_key for r in base] == ["X", "Y"] and "multi_scene_bonus" not in base[0].contributions
    boosted = fuse({"scene": scenes}, {"scene": 1}, {"scene"}, bonus=1.0)
    assert boosted[0].contributions["multi_scene_bonus"] > 0


def test_scene_level_fusion_inside_an_asset():
    ch = {"lex": [H("X", "s2", 1), H("X", "s1", 1)], "dense": [H("X", "s1", 1), H("X", "s3", 1)]}
    x = fuse(ch, {"lex": 1, "dense": 1}, {"lex", "dense"})[0]
    assert x.scenes[0].scene_id == "s1" and set(x.scenes[0].ranks) == {"lex", "dense"}


def test_window_limits_assets_per_channel():
    hits = [H(f"A{i}", None, 1) for i in range(100)]
    assert len(fuse({"c": hits}, {"c": 1}, set(), window=50)) == 50


def test_dedupe():
    hits = [H("X", "s1", 3), H("X", "s2", 2), H("X", "s1", 1)]
    assert len(dedupe(hits, by_scene=True)) == 2 and len(dedupe(hits, by_scene=False)) == 1


def test_empty_channels_are_harmless():
    assert fuse({"a": [], "b": []}, {"a": 1, "b": 1}, set()) == []
