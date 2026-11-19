"""Turning observations into notification-worthy events.

`keys.py`      - the deterministic event_key that prevents duplicate alerts
`detectors.py` - rules that spot a meaningful change between two snapshots
`risk.py`      - heuristics that record risk indicators in risk_flags

Nothing here decides whether to BUY anything. Detectors describe what changed
and how much; the research report explains it; the reader decides.
"""
