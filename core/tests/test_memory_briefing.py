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
