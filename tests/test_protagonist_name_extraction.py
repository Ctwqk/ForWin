import pytest

from forwin.canon_quality.placeholder import extract_expected_protagonist_names


def test_fixed_protagonist_takes_priority_over_relationship_description():
    setting = (
        "固定主角周砚；账房兼谈判手唐穗。"
        "城邦稽核员宁檀，原则明确，利益与主角时有冲突，关系渐进。"
    )
    assert extract_expected_protagonist_names(setting) == {"周砚"}


@pytest.mark.parametrize("connector", ["与", "和", "同"])
def test_relationship_clause_does_not_introduce_a_protagonist_name(connector):
    assert extract_expected_protagonist_names(
        f"稽核员的利益{connector}主角时有冲突，关系渐进。"
    ) == set()


@pytest.mark.parametrize("setting", [
    "主角：周砚，维修学徒。宁檀与主角时有冲突，关系渐进。",
    "固定主人公周砚；他从维修学徒起步。",
    "固定主角是周砚；维修学徒。",
    "固定主人公为周砚；维修学徒。",
    "固定主角是周砚，维修学徒。",
    "固定主人公为周砚，维修学徒。",
    "主角陆明是维修学徒。",
    "他与主角陆明在维修站相遇。",
    "他和主角陆明在维修站相遇。",
    "他同主角陆明在维修站相遇。",
])
def test_explicit_names_and_existing_bare_name_form_remain_supported(setting):
    expected = {"陆明"} if "陆明" in setting else {"周砚"}
    assert extract_expected_protagonist_names(setting) == expected
