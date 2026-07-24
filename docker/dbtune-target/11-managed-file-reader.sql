\set ON_ERROR_STOP on
\getenv target_agent_user POSTGRES_AGENT_USER

CREATE OR REPLACE FUNCTION public.dbtune_file_settings()
RETURNS TABLE (
    seqno INTEGER,
    sourcefile TEXT,
    sourceline INTEGER,
    name TEXT,
    applied BOOLEAN,
    error TEXT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        setting.seqno,
        setting.sourcefile,
        setting.sourceline,
        setting.name,
        setting.applied,
        CASE
            WHEN setting.error IS NULL THEN NULL
            ELSE 'configuration parse error'
        END
    FROM pg_catalog.pg_show_all_file_settings() AS setting
    WHERE setting.name = ANY (
        ARRAY[
            'work_mem',
            'random_page_cost',
            'seq_page_cost',
            'checkpoint_completion_target',
            'effective_io_concurrency',
            'max_parallel_workers_per_gather',
            'max_parallel_workers',
            'max_wal_size',
            'min_wal_size',
            'bgwriter_lru_maxpages',
            'bgwriter_delay',
            'effective_cache_size',
            'maintenance_work_mem',
            'default_statistics_target',
            'max_parallel_maintenance_workers',
            'shared_buffers',
            'max_worker_processes',
            'wal_buffers',
            'huge_pages'
        ]::TEXT[]
    )
    OR setting.error IS NOT NULL
$function$;

REVOKE ALL ON FUNCTION public.dbtune_file_settings() FROM PUBLIC;
GRANT pg_monitor TO :"target_agent_user";
GRANT EXECUTE ON FUNCTION public.dbtune_file_settings() TO :"target_agent_user";
GRANT EXECUTE ON FUNCTION pg_catalog.pg_reload_conf() TO :"target_agent_user";
