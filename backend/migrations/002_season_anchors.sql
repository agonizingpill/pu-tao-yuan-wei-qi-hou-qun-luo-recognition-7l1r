-- 换季定名：主管指定的田间标准地按批留痕，标签候选可因换地撤回并重判。
CREATE TABLE IF NOT EXISTS field_standard (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role text NOT NULL CHECK (role IN ('cool', 'warm')),
    block_id integer NOT NULL REFERENCES vine_block(id),
    selected_by text NOT NULL REFERENCES account(username),
    selected_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    retired_by text REFERENCES account(username),
    CHECK ((retired_at IS NULL) = (retired_by IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS current_field_standard
    ON field_standard (role) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS field_standard_history ON field_standard (id DESC);

ALTER TABLE climate_run ADD COLUMN IF NOT EXISTS naming jsonb;

ALTER TABLE climate_run DROP CONSTRAINT IF EXISTS climate_run_status_check;
ALTER TABLE climate_run ADD CONSTRAINT climate_run_status_check
    CHECK (status IN ('blocked', 'queued', 'naming', 'effective', 'review', 'rejected', 'failed'));

ALTER TABLE climate_label DROP CONSTRAINT IF EXISTS climate_label_state_check;
ALTER TABLE climate_label ADD CONSTRAINT climate_label_state_check
    CHECK (state IN ('proposed', 'active', 'superseded', 'rejected', 'withdrawn'));

DROP INDEX IF EXISTS single_open_climate_run;
CREATE UNIQUE INDEX IF NOT EXISTS single_open_climate_run
    ON climate_run ((1)) WHERE status IN ('queued', 'naming', 'review');
