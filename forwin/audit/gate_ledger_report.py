from __future__ import annotations

from .gate_ledger import GateLedgerReport


def render_gate_ledger_markdown(report: GateLedgerReport) -> str:
    lines = [
        "# Gate Ledger",
        "",
        f"- Scope: `{report.scope}`",
        f"- Project: `{report.project_id or 'all'}`",
        f"- Band: `{report.band_id or 'all'}`",
        f"- Projects: {report.project_count}",
        f"- Events: {report.event_count}",
        f"- Checkpoints: {report.checkpoint_count}",
        "",
        "| gate | opportunities | evaluations | fires | blocks | pauses | approvals | overrides | unknown_legacy_count |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in report.metrics:
        lines.append(
            "| "
            + " | ".join(
                [
                    metric.gate_id,
                    str(metric.opportunities),
                    str(metric.evaluations),
                    str(metric.fires),
                    str(metric.blocks),
                    str(metric.pauses),
                    str(metric.approvals),
                    str(metric.overrides),
                    str(metric.unknown_legacy_count),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| gate | post_override_incident_proxy | post_pass_incident_proxy |",
            "|---|---:|---:|",
        ]
    )
    for metric in report.metrics:
        lines.append(
            f"| {metric.gate_id} | {metric.post_override_incident_proxy} | "
            f"{metric.post_pass_incident_proxy} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["render_gate_ledger_markdown"]
