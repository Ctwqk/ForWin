from __future__ import annotations


PROPOSAL_TYPES = {
    "NoteOnlyProposal",
    "CanonCorrectionProposal",
    "AliasRenameProposal",
    "RelationshipCorrectionProposal",
    "MapCorrectionProposal",
    "KnowledgeGapProposal",
    "ContradictionClaimProposal",
    "ExpansionRequestProposal",
}


def classify_proposal(*, page_type: str, target_field: str, proposed_text: str) -> str:
    field = target_field.lower()
    page_type = page_type.lower()
    text = proposed_text.lower()
    if field == "manual notes" or field == "human questions":
        return "NoteOnlyProposal"
    if "alias" in text or "rename" in text or "改名" in text or "别名" in text:
        return "AliasRenameProposal"
    if page_type in {"map", "map_node", "map_edge", "location", "region", "subworld"}:
        return "MapCorrectionProposal"
    if page_type in {"relationship", "character", "faction", "organization", "family"} and any(
        marker in text for marker in ["relationship", "关系", "edge", "allied", "opposes"]
    ):
        return "RelationshipCorrectionProposal"
    if page_type in {"secret", "knowledge_gap"} or any(
        marker in text for marker in ["knowledge gap", "秘密", "悬念", "reveal"]
    ):
        return "KnowledgeGapProposal"
    if any(marker in text for marker in ["contradiction", "conflict", "矛盾", "冲突"]):
        return "ContradictionClaimProposal"
    if any(marker in text for marker in ["expand", "补充", "扩展", "new node"]):
        return "ExpansionRequestProposal"
    return "CanonCorrectionProposal"
