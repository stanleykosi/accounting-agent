-- Purpose: Initialize the canonical PostgreSQL database defaults for the local demo stack.
-- Scope: Enable pgvector and a small set of foundational extensions, then align the active database timezone with the product default.
-- Dependencies: Executed automatically by the pgvector PostgreSQL container on first initialization.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET timezone TO %L',
        current_database(),
        'Africa/Lagos'
    );
END
$$;
