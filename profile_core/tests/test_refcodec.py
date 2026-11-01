"""人物引用：token 存储、同名渲染、反解。"""

from __future__ import annotations

from profile_core.refcodec import RefCatalog, encode_profile, make_token, render_profile


def test_same_name_gets_stable_llm_suffix_but_clean_ui() -> None:
    catalog = RefCatalog([("2", "小明"), ("1", "小明"), ("3", "小红")])
    assert catalog.label("1") == "小明#1"
    assert catalog.label("2") == "小明#2"
    assert catalog.render(make_token("1"), for_llm=True) == "「小明#1」"
    assert catalog.render(make_token("1"), for_llm=False) == "小明"
    assert catalog.resolve("小明#2") == "2"
    assert catalog.resolve("小明") is None
    assert catalog.resolve("小红") == "3"


def test_encode_labels_and_p_refs_to_tokens() -> None:
    catalog = RefCatalog([("1", "小明"), ("2", "小红")])
    profile = {
        "long": {"relations": "与「小红」关系好"},
        "persons": [
            {
                "speaker": "P1",
                "sections": {"relations": "与 P2 关系好"},
            }
        ],
    }
    encoded = encode_profile(
        profile,
        catalog,
        speaker_map={"P1": "1", "P2": "2"},
    )
    assert encoded["long"]["relations"] == f"与{make_token('2')}关系好"
    assert (
        encoded["persons"][0]["sections"]["relations"]
        == f"与 {make_token('2')} 关系好"
    )
    assert encoded["persons"][0]["speaker"] == "P1"


def test_render_profile_hides_tokens_from_llm_and_ui() -> None:
    catalog = RefCatalog([("1", "小明")])
    profile = {"mid": {"relations": f"与{make_token('1')}关系好"}}
    assert render_profile(profile, catalog, for_llm=True)["mid"][
        "relations"
    ] == "与「小明」关系好"
    assert render_profile(profile, catalog, for_llm=False)["mid"][
        "relations"
    ] == "与小明关系好"
