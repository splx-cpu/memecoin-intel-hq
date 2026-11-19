"""Database access layer.

`connection.py` owns how we connect to Supabase Postgres.
`migrate.py` applies the SQL files in the top-level `migrations/` folder.

Query functions ("repositories") are added in Stage D, once the scanner has
real data to store. Keeping them out of Stage A means the schema can still
change cheaply.
"""
