-- One name, not a given/family pair (package migration 0013). Name structure
-- and order vary too much across cultures for a split to be anything but
-- lossy, so a person is `name` everywhere: the `profiles` column, and `name`
-- inside `grants.subject_json` / `access_requests.subject_json`. Existing
-- pairs are joined "first last" (gcs's admin console only ever wrote `first`).

ALTER TABLE profiles ADD COLUMN name TEXT;
UPDATE profiles SET name = NULLIF(TRIM(COALESCE(first, '') || ' ' || COALESCE(last, '')), '');
ALTER TABLE profiles DROP COLUMN first;
ALTER TABLE profiles DROP COLUMN last;

-- Only rows that carry a first/last are rewritten. A pair that joins to nothing
-- drops `name` too; a subject left empty becomes NULL, as `cleanSubject` would
-- have stored it.
UPDATE grants SET subject_json = json_remove(
  CASE
    WHEN TRIM(COALESCE(json_extract(subject_json, '$.first'), '') || ' ' || COALESCE(json_extract(subject_json, '$.last'), '')) = ''
    THEN subject_json
    ELSE json_set(subject_json, '$.name', TRIM(COALESCE(json_extract(subject_json, '$.first'), '') || ' ' || COALESCE(json_extract(subject_json, '$.last'), '')))
  END,
  '$.first', '$.last'
)
WHERE json_type(subject_json, '$.first') IS NOT NULL OR json_type(subject_json, '$.last') IS NOT NULL;
UPDATE grants SET subject_json = NULL WHERE subject_json = '{}';

UPDATE access_requests SET subject_json = json_remove(
  CASE
    WHEN TRIM(COALESCE(json_extract(subject_json, '$.first'), '') || ' ' || COALESCE(json_extract(subject_json, '$.last'), '')) = ''
    THEN subject_json
    ELSE json_set(subject_json, '$.name', TRIM(COALESCE(json_extract(subject_json, '$.first'), '') || ' ' || COALESCE(json_extract(subject_json, '$.last'), '')))
  END,
  '$.first', '$.last'
)
WHERE json_type(subject_json, '$.first') IS NOT NULL OR json_type(subject_json, '$.last') IS NOT NULL;
UPDATE access_requests SET subject_json = NULL WHERE subject_json = '{}';
