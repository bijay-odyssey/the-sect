-- 0001_init: disciples and missions.
--
-- Applied by sect.core.db.run_migrations, which owns the schema_migrations table and
-- takes a pg_advisory_lock first, so two instances booting during a deploy cannot race.
--
-- gen_random_uuid() is built into PostgreSQL 13+, so no extension is required.

CREATE TABLE disciples (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Public identifier. Keep in step with models.DiscipleName.
    name          text NOT NULL UNIQUE
                    CHECK (name ~ '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'),
    display_name  text,
    arts          text[] NOT NULL CHECK (cardinality(arts) > 0),

    -- Keep in step with sect.realms.Realm; test_realms_match_database enforces it.
    realm         text NOT NULL DEFAULT 'qi-condensation'
                    CHECK (realm IN ('qi-condensation',
                                     'foundation-establishment',
                                     'core-formation')),

    repo_url      text,
    description   text,
    agent_version text,                     -- self-reported build id, for debugging

    token_hash    text NOT NULL UNIQUE,     -- sha256 hex of the bearer token
    active        boolean NOT NULL DEFAULT true,

    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX disciples_arts_gin ON disciples USING gin (arts);


CREATE TABLE missions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    title             text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description       text,
    required_art      text NOT NULL CHECK (length(required_art) BETWEEN 1 AND 64),
    payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority          smallint NOT NULL DEFAULT 0,          -- higher runs sooner

    status            text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','claimed','completed','failed','cancelled')),

    -- Retry and lease machinery.
    attempts          smallint NOT NULL DEFAULT 0,
    max_attempts      smallint NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 100),
    lease_seconds     integer  NOT NULL DEFAULT 900 CHECK (lease_seconds BETWEEN 30 AND 86400),
    not_before        timestamptz NOT NULL DEFAULT now(),

    -- Holder. Authoritative only while status = 'claimed'; deliberately left in place
    -- after completion so a retried complete() reads as a replay, not a conflict.
    claimed_by        uuid REFERENCES disciples(id) ON DELETE SET NULL,
    claim_token       uuid,                 -- server secret; never serialized to the wire
    claimed_at        timestamptz,
    lease_expires_at  timestamptz,

    -- Outcome.
    result            jsonb,
    error             text,

    idempotency_key   text UNIQUE,
    posted_by         text NOT NULL DEFAULT 'master',   -- 'master' or a disciple name

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,

    CONSTRAINT claimed_implies_holder CHECK (
        status <> 'claimed'
        OR (claimed_by IS NOT NULL AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT terminal_implies_finished CHECK (
        status NOT IN ('completed','failed','cancelled') OR finished_at IS NOT NULL
    )
);

-- The poll/claim hot path. Partial, so the ever-growing tail of finished missions is
-- excluded from the index that claiming actually walks.
CREATE INDEX missions_claimable_idx
    ON missions (required_art, priority DESC, created_at)
    WHERE status IN ('open','claimed');

CREATE INDEX missions_browse_idx     ON missions (created_at DESC, id DESC);
CREATE INDEX missions_status_idx     ON missions (status);
CREATE INDEX missions_claimed_by_idx ON missions (claimed_by) WHERE claimed_by IS NOT NULL;
CREATE INDEX missions_posted_by_idx  ON missions (posted_by);


CREATE FUNCTION sect_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER disciples_touch BEFORE UPDATE ON disciples
    FOR EACH ROW EXECUTE FUNCTION sect_touch_updated_at();

CREATE TRIGGER missions_touch BEFORE UPDATE ON missions
    FOR EACH ROW EXECUTE FUNCTION sect_touch_updated_at();
