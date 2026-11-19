"""Sending research notifications to Telegram.

`telegram.py`   - the Bot API client: retries, rate limits, delivery logging
`formatters.py` - turning a research report into a readable message

Nothing here decides WHAT to send; that is the detectors' job. This package
only decides how it reads and makes sure it arrives.
"""
