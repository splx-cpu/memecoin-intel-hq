"""The two monitoring loops, and the policy that decides what gets stored.

`policy.py`    - when an observation is worth writing to the database
`discovery.py` - finds newly created pools (~every 5 minutes)
`watchlist.py` - re-checks tokens we already track (~every minute)
"""
