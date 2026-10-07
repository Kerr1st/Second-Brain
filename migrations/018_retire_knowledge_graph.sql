-- 018: retire the knowledge graph (D4; audit F-111, review B §1).
--
-- Drops entities, entity_edges and memory_entities (migration 008; 77 k rows on
-- the installed system, zero readers, written hourly by the Slack graph stage
-- that R7 removes) together with every idx_entities_* index. Live data is never
-- dropped unseen: unless all three tables are empty, this file requires the
-- marker that scripts/maintenance/export_graph_tables.py writes AFTER it has
-- produced and verified the encrypted archive -- a corpus_maintenance row with
-- operation_key = 'graph-export' whose details->'rows' records the row count of
-- each table at export time. The current counts must equal the recorded ones,
-- so an archive taken before further writes (the hourly job still running) is
-- not accepted as cover for them. The marker row itself stays: it is the pointer
-- from the ledger to the archive and its receipt.
--
-- Re-runnable: when the tables are already gone the file only reports it.
-- Runs as the owner role (migrate.sh); DROP needs ownership, nothing more.
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    present text[] := ARRAY(SELECT t FROM unnest(ARRAY['memory_entities', 'entity_edges', 'entities']) AS t
                            WHERE to_regclass('public.' || t) IS NOT NULL);
    live    jsonb := '{}'::jsonb;
    marker  record;
    t       text;
    n       bigint;
BEGIN
    IF cardinality(present) = 0 THEN
        RAISE NOTICE '018: knowledge-graph tables already dropped';
    ELSE
        FOREACH t IN ARRAY present LOOP
            EXECUTE format('SELECT count(*) FROM public.%I', t) INTO n;
            live := live || jsonb_build_object(t, n);
        END LOOP;
        IF (SELECT bool_and((v)::bigint = 0) FROM jsonb_each_text(live) AS e(k, v)) THEN
            RAISE NOTICE '018: knowledge-graph tables are empty; dropping without an export (%)', live;
        ELSE
            SELECT * INTO marker FROM corpus_maintenance
             WHERE operation_key = 'graph-export' ORDER BY applied_at DESC LIMIT 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION '018: refusing to drop % rows: no graph-export marker in corpus_maintenance; run scripts/maintenance/export_graph_tables.py first', live;
            END IF;
            FOREACH t IN ARRAY present LOOP
                IF (marker.details->'rows'->>t) IS NULL OR (marker.details->'rows'->>t)::bigint <> (live->>t)::bigint THEN
                    RAISE EXCEPTION '018: graph-export marker (% at %, rows %) does not match the current row counts %; re-run scripts/maintenance/export_graph_tables.py',
                        marker.manifest_sha256, marker.applied_at, marker.details->'rows', live;
                END IF;
            END LOOP;
            RAISE NOTICE '018: dropping % behind graph-export % (%)', live, marker.manifest_sha256, marker.details->>'archive';
        END IF;
    END IF;
END $$;

-- Dependents first (FKs to entities), then the indexes 016 may already have
-- removed, then the tables. CASCADE is not used: the only dependents are listed.
DROP TABLE IF EXISTS memory_entities;
DROP TABLE IF EXISTS entity_edges;
DROP INDEX IF EXISTS idx_entities_name;
DROP INDEX IF EXISTS idx_entities_embedding;
DROP INDEX IF EXISTS idx_entities_category;
DROP TABLE IF EXISTS entities;
