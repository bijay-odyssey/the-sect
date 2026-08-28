-- 0002_peaks: the Peak System (v0.2.0).
--
-- A peak is a named specialty group. A disciple may belong to one (disciples.peak_id)
-- or wander unaffiliated (peak_id IS NULL). A mission may carry a peak_id as a routing
-- HINT: it never gates a claim -- a peak is not a wall -- it only makes a peak's own
-- work sort first for that peak's disciples in claim-next and on the open board.
--
-- This migration also adds a per-DISCIPLE contribution ledger. Points and reputation
-- follow the disciple, not the peak, so a disciple's standing survives a transfer.
--
-- Applied by sect.core.db.run_migrations inside a transaction, behind the same
-- pg_advisory_lock as 0001. gen_random_uuid() is built into PostgreSQL 13+.

CREATE TABLE peaks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Public identifier. Same slug shape as disciples.name; keep in step with
    -- models.DiscipleName.
    name          text NOT NULL UNIQUE
                    CHECK (name ~ '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'),
    display_name  text NOT NULL,
    description   text NOT NULL DEFAULT '',

    -- Declared specialties. May be empty: a peak can be registered before it decides
    -- exactly which arts it covers.
    arts          text[] NOT NULL DEFAULT '{}',

    -- Push-based dispatch (a peak endpoint / webhook the core POSTs to) is a v0.3+ idea.
    -- The columns for it land in the migration that wires it up, not before -- an unused
    -- column is dead weight.

    status        text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'inactive', 'suspended')),

    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX peaks_arts_gin   ON peaks USING gin (arts);
CREATE INDEX peaks_active_idx ON peaks (name) WHERE status = 'active';

CREATE TRIGGER peaks_touch BEFORE UPDATE ON peaks
    FOR EACH ROW EXECUTE FUNCTION sect_touch_updated_at();


-- --- affiliation ---------------------------------------------------------------

ALTER TABLE disciples
    ADD COLUMN peak_id uuid REFERENCES peaks(id) ON DELETE SET NULL;

CREATE INDEX disciples_peak_idx ON disciples (peak_id) WHERE peak_id IS NOT NULL;

ALTER TABLE missions
    ADD COLUMN peak_id uuid REFERENCES peaks(id) ON DELETE SET NULL;

CREATE INDEX missions_peak_idx ON missions (peak_id) WHERE peak_id IS NOT NULL;


-- --- contribution ledger (per disciple) --------------------------------------

ALTER TABLE disciples
    ADD COLUMN contribution_points integer          NOT NULL DEFAULT 0,
    ADD COLUMN completed_missions  integer          NOT NULL DEFAULT 0
        CHECK (completed_missions >= 0),
    ADD COLUMN failed_missions     integer          NOT NULL DEFAULT 0
        CHECK (failed_missions >= 0),
    ADD COLUMN success_rate        double precision NOT NULL DEFAULT 0.0,
    ADD COLUMN reputation          integer          NOT NULL DEFAULT 0;

-- Backfill from history so the columns are the single source of truth from the first
-- boot on this migration. Terminal outcomes only: a retryable failure clears its
-- holder columns and is attributed to nobody, which matches the live counts 0.1
-- exposed via a LATERAL join. At backfill, contribution_points == completed_missions
-- (one point per completed mission).
UPDATE disciples AS d
SET completed_missions  = c.completed,
    failed_missions     = c.failed,
    contribution_points = c.completed,
    success_rate        = CASE WHEN c.completed + c.failed = 0 THEN 0.0
                               ELSE c.completed::double precision / (c.completed + c.failed) END,
    reputation          = CASE WHEN c.completed + c.failed = 0 THEN 0
                               ELSE floor(c.completed
                                          * (c.completed::double precision
                                             / (c.completed + c.failed)))::int END
FROM (
    SELECT dd.id,
           count(*) FILTER (WHERE m.status = 'completed') AS completed,
           count(*) FILTER (WHERE m.status = 'failed')    AS failed
    FROM disciples AS dd
    LEFT JOIN missions AS m ON m.claimed_by = dd.id
    GROUP BY dd.id
) AS c
WHERE c.id = d.id;
