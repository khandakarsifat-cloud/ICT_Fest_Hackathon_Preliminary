"""Room statistics are derived directly from the database.

The `/rooms/{id}/stats` endpoint aggregates confirmed bookings in
`app/routers/rooms.py` so counts and revenue always reflect current booking
state. This module is kept only to preserve the existing package layout.
"""
