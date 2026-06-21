"""Memory recall must flow into handoff briefings."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.briefing import BriefingBuilder
from centri.schemas import ContextPacket


def test_briefing_includes_relevant_memory():
    packet = ContextPacket(relevant_recall=["[letta] LiveKit owns realtime voice end-to-end"])
    briefing = BriefingBuilder().build(packet, "fix voice")

    assert "Relevant memory" in briefing
    assert "LiveKit owns realtime voice end-to-end" in briefing


def test_briefing_preserves_standing_self_continuity_preamble():
    standing_self = (
        "Standing self (continuity):\n"
        "Current work: Centri continuity capsule is the active shared work.\n"
        "Continuity: time=earlier today; last decision=standing self: make continuity explicit; "
        "next=wire this preamble into spawned handoffs so parallel work does not feel fresh.\n"
        "Memory (assembled from the event ledger):\n"
        "Decisions already made (do not relitigate):\n"
        "  - use one global standing self, with sessions as views"
    )
    packet = ContextPacket(relevant_recall=[standing_self])

    briefing = BriefingBuilder().build(packet, "continue the Jarvis build")

    assert "Relevant memory" in briefing
    assert "Standing self (continuity):" in briefing
    assert "Current work: Centri continuity capsule is the active shared work." in briefing
    assert "next=wire this preamble into spawned handoffs" in briefing
