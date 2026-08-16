"""
PostgreSQL PL/pgSQL function definitions for COW (Copy-On-Write).

All functions are deployed to the database via ``deploy_cow_functions()``.
"""

COW_ORDER_COLUMN = "_cow_order"
COW_ORDER_SEQUENCE_NAME = "_cow_operation_order_seq"
COW_BASE_EXISTS_COLUMN = "_cow_base_exists"
COW_BASE_ROW_COLUMN = "_cow_base_row"
COW_BASE_SCHEMA_COLUMN = "_cow_base_schema"
COW_INTERNAL_SCHEMA = "agentcow"

CREATE_HARDENED_ROLES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agentcow._cow_hardened_roles (
    schema_name text NOT NULL,
    role_oid oid NOT NULL,
    role_name name NOT NULL,
    role_kind text NOT NULL CHECK (role_kind IN ('owner', 'runtime', 'reviewer')),
    PRIMARY KEY (schema_name, role_oid, role_kind)
);
"""

CREATE_TABLE_SECURITY_MODES_SQL = """
CREATE TABLE IF NOT EXISTS agentcow._cow_table_security_modes (
    schema_name text NOT NULL,
    view_name text NOT NULL,
    fail_closed_writes boolean NOT NULL,
    security_definer_triggers boolean NOT NULL,
    PRIMARY KEY (schema_name, view_name)
);
"""

REVOKE_PUBLIC_CONTROL_SCHEMA_SQL = "REVOKE ALL ON SCHEMA agentcow FROM PUBLIC"
REVOKE_PUBLIC_CONTROL_TABLES_SQL = (
    "REVOKE ALL ON ALL TABLES IN SCHEMA agentcow FROM PUBLIC"
)
REVOKE_PUBLIC_CONTROL_FUNCTIONS_SQL = (
    "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agentcow FROM PUBLIC"
)

COW_CHANGES_TABLE_NAME_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_changes_table_name(p_base_table text)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT CASE
        WHEN p_base_table LIKE '%_base'
        THEN regexp_replace(p_base_table, '_base$', '') || '_changes'
        ELSE p_base_table || '_changes'
    END;
$$;
"""

CREATE_INTERNAL_SCHEMA_SQL = "CREATE SCHEMA IF NOT EXISTS agentcow"
DROP_LEGACY_SETUP_FUNCTION_SQL = (
    "DROP FUNCTION IF EXISTS agentcow.setup_cow(text, text, text, text[])"
)

DROP_LEGACY_COMMIT_FUNCTIONS_SQL = """
DROP FUNCTION IF EXISTS
    agentcow.commit_cow(text, text, text[], uuid, uuid[]),
    agentcow.commit_cow_upsert(text, text, text[], uuid, uuid[]),
    agentcow.commit_cow_delete(text, text, text[], uuid, uuid[])
"""

COW_SCHEMA_SIGNATURE_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_schema_signature(
    p_schema text,
    p_table text
)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog
AS $$
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_array(
                attr.attname,
                format_type(attr.atttypid, attr.atttypmod),
                attr.attnotnull,
                attr.attidentity,
                attr.attgenerated,
                attr.attcollation::oid,
                pg_get_expr(default_.adbin, default_.adrelid)
            ) ORDER BY attr.attnum
        ),
        '[]'::jsonb
    )
    FROM pg_class table_
    JOIN pg_namespace namespace_ ON namespace_.oid = table_.relnamespace
    JOIN pg_attribute attr ON attr.attrelid = table_.oid
    LEFT JOIN pg_attrdef default_
      ON default_.adrelid = table_.oid
     AND default_.adnum = attr.attnum
    WHERE namespace_.nspname = p_schema
      AND table_.relname = p_table
      AND table_.relkind IN ('r', 'p')
      AND attr.attnum > 0
      AND NOT attr.attisdropped;
$$;
"""

REQUIRE_REVIEWER_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_require_reviewer(p_schema text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    schema_is_hardened boolean;
    caller_is_authorized boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM agentcow._cow_hardened_roles roles
        WHERE roles.schema_name = p_schema
    ) INTO schema_is_hardened;

    -- Before hardening, only the function owner (or a role able to SET ROLE
    -- to it) retains the legacy administrative path. A reviewer granted a
    -- controlled function for one schema cannot use it against another,
    -- unhardened schema.
    IF NOT schema_is_hardened THEN
        IF pg_has_role(session_user, current_user, 'MEMBER') THEN
            RETURN;
        END IF;
        RAISE EXCEPTION 'schema % is not configured for reviewer access',
            p_schema
            USING ERRCODE = '42501';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM agentcow._cow_hardened_roles roles
        JOIN pg_roles live_role
          ON live_role.oid = roles.role_oid
         AND live_role.rolname = roles.role_name
        WHERE roles.schema_name = p_schema
          AND roles.role_kind IN ('owner', 'reviewer')
          AND pg_has_role(session_user, live_role.oid, 'MEMBER')
    ) INTO caller_is_authorized;

    IF NOT caller_is_authorized THEN
        RAISE EXCEPTION 'reviewer authority is required for hardened COW schema %',
            p_schema
            USING ERRCODE = '42501';
    END IF;
END;
$$;
"""

REQUIRE_COW_TABLE_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_require_cow_table(
    p_schema text,
    p_base_table text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    view_name text := regexp_replace(p_base_table, '_base$', '');
    changes_table text := agentcow._cow_changes_table_name(p_base_table);
BEGIN
    IF right(p_base_table, 5) <> '_base'
       OR NOT EXISTS (
           SELECT 1
           FROM pg_class base
           JOIN pg_namespace base_ns ON base_ns.oid = base.relnamespace
           WHERE base_ns.nspname = p_schema
             AND base.relname = p_base_table
             AND base.relkind IN ('r', 'p')
       )
       OR NOT EXISTS (
           SELECT 1
           FROM pg_class changes
           JOIN pg_namespace changes_ns ON changes_ns.oid = changes.relnamespace
           WHERE changes_ns.nspname = p_schema
             AND changes.relname = changes_table
             AND changes.relkind IN ('r', 'p')
       )
       OR NOT EXISTS (
           SELECT 1
           FROM pg_class view_
           JOIN pg_namespace view_ns ON view_ns.oid = view_.relnamespace
           WHERE view_ns.nspname = p_schema
             AND view_.relname = view_name
             AND view_.relkind = 'v'
       ) THEN
        RAISE EXCEPTION '%.% is not an enabled COW base table',
            p_schema, p_base_table
            USING ERRCODE = '42501';
    END IF;
END;
$$;
"""

REQUIRE_PRIMARY_KEY_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_require_primary_key(
    p_schema text,
    p_base_table text,
    p_pk_cols text[]
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    actual_pk text[];
BEGIN
    SELECT array_agg(attr.attname::text ORDER BY key_.ordinal)
    INTO actual_pk
    FROM pg_constraint constraint_
    JOIN pg_class table_ ON table_.oid = constraint_.conrelid
    JOIN pg_namespace namespace_ ON namespace_.oid = table_.relnamespace
    CROSS JOIN LATERAL unnest(constraint_.conkey) WITH ORDINALITY key_(attnum, ordinal)
    JOIN pg_attribute attr
      ON attr.attrelid = table_.oid
     AND attr.attnum = key_.attnum
    WHERE constraint_.contype = 'p'
      AND namespace_.nspname = p_schema
      AND table_.relname = p_base_table;

    IF actual_pk IS NULL OR actual_pk IS DISTINCT FROM p_pk_cols THEN
        RAISE EXCEPTION 'primary-key columns do not match %.%',
            p_schema, p_base_table
            USING ERRCODE = '22023';
    END IF;
END;
$$;
"""

SETUP_COW_SQL = """
CREATE OR REPLACE FUNCTION agentcow.setup_cow(
    p_schema     text,
    p_base_table text,
    p_view_name  text,
    p_pk_cols    text[],
    p_fail_closed_writes boolean DEFAULT NULL,
    p_security_definer_triggers boolean DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    changes_table_name   text := agentcow._cow_changes_table_name(p_base_table);

    qual_base            text := format('%I.%I', p_schema, p_base_table);
    qual_changes         text := format('%I.%I', p_schema, changes_table_name);
    qual_view            text := format('%I.%I', p_schema, p_view_name);
    qual_dirty_tables    text := format('%I.%I', p_schema, 'cow_dirty_tables');
    order_sequence_name  text := '_cow_operation_order_seq';
    qual_order_sequence  text := format('%I.%I', p_schema, order_sequence_name);
    order_sequence_comment text := 'agent-cow deterministic operation order';

    col_list             text;
    col_list_prefixed_b  text;
    coalesce_select_list text;
    changes_select_list  text;
    excluded_set_list    text;
    new_values_list      text;
    old_values_list      text;
    base_update_set      text;

    pk_cols_quoted       text;
    pk_join_condition    text;
    pk_distinct_on       text;
    pk_order_by          text;
    pk_base_join         text;
    pk_null_check        text;
    pk_delete_condition  text;
    pk_old_values        text;
    pk_prior_new_condition text;
    pk_prior_old_condition text;
    pk_base_new_condition text;
    pk_base_old_condition text;
    pk_new_json          text;
    pk_old_json          text;

    upsert_fn_name       text := p_view_name || '_cow_upsert';
    delete_fn_name       text := p_view_name || '_cow_delete';
    base_table_owner     text;
    schema_owner         text;
    base_on_conflict     text;
    changes_on_conflict  text;
    order_sequence_exists boolean;
    existing_sequence_comment text;
    has_order_column     boolean;
    has_conflict_baseline boolean;
    has_pending_changes  boolean;
    legacy_registry_exists boolean;
    order_table          RECORD;
    max_order            bigint := 0;
    table_max_order      bigint;
    sequence_last_value  bigint;
    fail_closed_writes   boolean;
    security_definer_triggers boolean;
    trigger_security_clause text;
    missing_upsert_context_sql text;
    missing_delete_context_sql text;
    capture_new_baseline_sql text;
    capture_old_baseline_sql text;
BEGIN
    SELECT
        COALESCE(
            p_fail_closed_writes,
            modes.fail_closed_writes,
            true
        ),
        COALESCE(
            p_security_definer_triggers,
            modes.security_definer_triggers,
            false
        )
    INTO fail_closed_writes, security_definer_triggers
    FROM (SELECT 1) singleton
    LEFT JOIN agentcow._cow_table_security_modes modes
      ON modes.schema_name = p_schema
     AND modes.view_name = p_view_name;

    INSERT INTO agentcow._cow_table_security_modes (
        schema_name,
        view_name,
        fail_closed_writes,
        security_definer_triggers
    ) VALUES (
        p_schema,
        p_view_name,
        fail_closed_writes,
        security_definer_triggers
    )
    ON CONFLICT (schema_name, view_name) DO UPDATE SET
        fail_closed_writes = EXCLUDED.fail_closed_writes,
        security_definer_triggers = EXCLUDED.security_definer_triggers;

    trigger_security_clause := CASE
        WHEN security_definer_triggers THEN 'SECURITY DEFINER'
        ELSE 'SECURITY INVOKER'
    END;

    pk_cols_quoted := (SELECT string_agg(quote_ident(col), ', ') FROM unnest(p_pk_cols) col);
    pk_join_condition := (SELECT string_agg(format('c2.%I = b.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_distinct_on := (SELECT string_agg(format('c3.%I', col), ', ') FROM unnest(p_pk_cols) col);
    pk_order_by := pk_distinct_on;
    pk_base_join := (SELECT string_agg(format('b.%I = c.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_null_check := format('b.%I IS NULL', p_pk_cols[1]);
    pk_delete_condition := (SELECT string_agg(format('%I = OLD.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_old_values := (SELECT string_agg(format('OLD.%I', col), ', ') FROM unnest(p_pk_cols) col);
    pk_prior_new_condition := (SELECT string_agg(format('prior.%I = NEW.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_prior_old_condition := (SELECT string_agg(format('prior.%I = OLD.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_base_new_condition := (SELECT string_agg(format('base.%I = NEW.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_base_old_condition := (SELECT string_agg(format('base.%I = OLD.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);
    pk_new_json := format(
        'jsonb_build_object(%s)',
        (SELECT string_agg(format('%L, NEW.%I', col, col), ', ') FROM unnest(p_pk_cols) col)
    );
    pk_old_json := format(
        'jsonb_build_object(%s)',
        (SELECT string_agg(format('%L, OLD.%I', col, col), ', ') FROM unnest(p_pk_cols) col)
    );
    capture_new_baseline_sql := format($capture$
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME || ':' ||
                sess::text || ':' || (%s)::text,
                0
            )
        );
        SELECT prior._cow_base_exists,
               prior._cow_base_row,
               prior._cow_base_schema
        INTO baseline_exists, baseline_row, baseline_schema
        FROM %s prior
        WHERE prior.session_id = sess AND %s
        ORDER BY prior._cow_order
        LIMIT 1;
        IF NOT FOUND THEN
            baseline_schema := agentcow._cow_schema_signature(%L, %L);
            SELECT true, to_jsonb(base)
            INTO baseline_exists, baseline_row
            FROM %s base
            WHERE %s
            LIMIT 1;
            IF NOT FOUND THEN
                baseline_exists := false;
                baseline_row := NULL;
            END IF;
        END IF;
    $capture$,
        pk_new_json,
        qual_changes,
        pk_prior_new_condition,
        p_schema,
        p_base_table,
        qual_base,
        pk_base_new_condition
    );
    capture_old_baseline_sql := format($capture$
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME || ':' ||
                sess::text || ':' || (%s)::text,
                0
            )
        );
        SELECT prior._cow_base_exists,
               prior._cow_base_row,
               prior._cow_base_schema
        INTO baseline_exists, baseline_row, baseline_schema
        FROM %s prior
        WHERE prior.session_id = sess AND %s
        ORDER BY prior._cow_order
        LIMIT 1;
        IF NOT FOUND THEN
            baseline_schema := agentcow._cow_schema_signature(%L, %L);
            SELECT true, to_jsonb(base)
            INTO baseline_exists, baseline_row
            FROM %s base
            WHERE %s
            LIMIT 1;
            IF NOT FOUND THEN
                baseline_exists := false;
                baseline_row := NULL;
            END IF;
        END IF;
    $capture$,
        pk_old_json,
        qual_changes,
        pk_prior_old_condition,
        p_schema,
        p_base_table,
        qual_base,
        pk_base_old_condition
    );

    -- Registry state belongs to the application schema, not to the caller's
    -- search path or to a global public-schema assumption.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %s (
           schema_name text NOT NULL,
           session_id uuid NOT NULL,
           table_name text NOT NULL,
           PRIMARY KEY (schema_name, session_id, table_name)
         )',
        qual_dirty_tables
    );

    -- H01 used one public registry for every application schema. Move any
    -- non-public entries into their owning schema without touching change
    -- rows, so an enabled table can be redeployed with pending work intact.
    IF p_schema <> 'public' THEN
        SELECT EXISTS (
            SELECT 1
            FROM pg_class cls
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            WHERE ns.nspname = 'public'
              AND cls.relname = 'cow_dirty_tables'
              AND cls.relkind IN ('r', 'p')
        ) INTO legacy_registry_exists;

        IF legacy_registry_exists THEN
            EXECUTE format(
                'INSERT INTO %s (schema_name, session_id, table_name)
                 SELECT schema_name, session_id, table_name
                 FROM public.cow_dirty_tables
                 WHERE schema_name = $1
                 ON CONFLICT DO NOTHING',
                qual_dirty_tables
            ) USING p_schema;
            DELETE FROM public.cow_dirty_tables WHERE schema_name = p_schema;
        END IF;
    END IF;

    -- One sequence per schema provides a shared causal order across every COW
    -- table in that schema. Sequence gaps after rollback are intentional.
    SELECT EXISTS (
        SELECT 1
        FROM pg_class cls
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = p_schema
          AND cls.relname = order_sequence_name
          AND cls.relkind = 'S'
    ) INTO order_sequence_exists;

    IF NOT order_sequence_exists THEN
        EXECUTE format('CREATE SEQUENCE %s AS bigint', qual_order_sequence);
        EXECUTE format(
            'COMMENT ON SEQUENCE %s IS %L',
            qual_order_sequence, order_sequence_comment
        );

        SELECT pg_get_userbyid(nspowner) INTO schema_owner
        FROM pg_namespace
        WHERE nspname = p_schema;

        IF schema_owner IS NOT NULL THEN
            EXECUTE format(
                'ALTER SEQUENCE %s OWNER TO %I',
                qual_order_sequence, schema_owner
            );
        END IF;
    ELSE
        SELECT obj_description(cls.oid, 'pg_class')
        INTO existing_sequence_comment
        FROM pg_class cls
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = p_schema
          AND cls.relname = order_sequence_name
          AND cls.relkind = 'S';

        IF existing_sequence_comment IS DISTINCT FROM order_sequence_comment THEN
            RAISE EXCEPTION
                'Sequence %.% already exists but is not managed by agent-cow',
                p_schema, order_sequence_name
                USING ERRCODE = '42710';
        END IF;
    END IF;

    -- 1. Create the changes table. _cow_updated_at remains timestamp metadata;
    -- _cow_order is the authoritative operation order.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %s (
           session_id uuid NOT NULL,
           operation_id uuid NOT NULL,
           LIKE %s INCLUDING DEFAULTS INCLUDING GENERATED,
           _cow_deleted boolean NOT NULL DEFAULT false,
           _cow_updated_at timestamptz NOT NULL DEFAULT now(),
           _cow_order bigint NOT NULL DEFAULT nextval(%L::regclass),
           _cow_base_exists boolean NOT NULL,
           _cow_base_row jsonb,
           _cow_base_schema jsonb NOT NULL,
           PRIMARY KEY (session_id, operation_id, %s)
         );',
        qual_changes, qual_base, qual_order_sequence, pk_cols_quoted
    );

    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = p_schema
          AND table_name = changes_table_name
          AND column_name = '_cow_order'
    ) INTO has_order_column;

    IF NOT has_order_column THEN
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s LIMIT 1)',
            qual_changes
        ) INTO has_pending_changes;

        IF has_pending_changes THEN
            RAISE EXCEPTION
                'Cannot add deterministic ordering to %.% while pending legacy COW changes exist; commit or discard them with the previous version first',
                p_schema, changes_table_name
                USING ERRCODE = '55000';
        END IF;

        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN _cow_order bigint NOT NULL DEFAULT nextval(%L::regclass)',
            qual_changes, qual_order_sequence
        );
    ELSE
        EXECUTE format(
            'ALTER TABLE %s ALTER COLUMN _cow_order SET DEFAULT nextval(%L::regclass)',
            qual_changes, qual_order_sequence
        );
    END IF;

    SELECT COUNT(*) = 3 AND bool_and(
        CASE column_name
            WHEN '_cow_base_exists' THEN
                data_type = 'boolean' AND is_nullable = 'NO'
            WHEN '_cow_base_row' THEN
                data_type = 'jsonb' AND is_nullable = 'YES'
            WHEN '_cow_base_schema' THEN
                data_type = 'jsonb' AND is_nullable = 'NO'
            ELSE false
        END
    )
    INTO has_conflict_baseline
    FROM information_schema.columns
    WHERE table_schema = p_schema
      AND table_name = changes_table_name
      AND column_name IN (
          '_cow_base_exists',
          '_cow_base_row',
          '_cow_base_schema'
      );

    IF NOT has_conflict_baseline THEN
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s LIMIT 1)',
            qual_changes
        ) INTO has_pending_changes;

        IF has_pending_changes THEN
            RAISE EXCEPTION
                'Cannot add first-touch conflict baselines to %.% while pending pre-H06 COW changes exist; commit or discard them with the previous version first',
                p_schema, changes_table_name
                USING ERRCODE = '55000';
        END IF;

        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN IF NOT EXISTS _cow_base_exists boolean NOT NULL',
            qual_changes
        );
        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN IF NOT EXISTS _cow_base_row jsonb',
            qual_changes
        );
        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN IF NOT EXISTS _cow_base_schema jsonb NOT NULL',
            qual_changes
        );
    END IF;

    -- If the sequence was restored or recreated, advance it beyond every
    -- existing value in this schema without inventing order for legacy rows.
    FOR order_table IN
        SELECT c.table_name
        FROM information_schema.columns c
        WHERE c.table_schema = p_schema
          AND c.column_name = '_cow_order'
          AND right(c.table_name, 8) = '_changes'
    LOOP
        EXECUTE format(
            'SELECT COALESCE(MAX(_cow_order), 0) FROM %I.%I',
            p_schema, order_table.table_name
        ) INTO table_max_order;
        max_order := GREATEST(max_order, table_max_order);
    END LOOP;

    EXECUTE format('SELECT last_value FROM %s', qual_order_sequence)
        INTO sequence_last_value;
    IF max_order > sequence_last_value THEN
        PERFORM setval(qual_order_sequence::regclass, max_order, true);
    END IF;

    SELECT tableowner INTO base_table_owner
    FROM pg_tables
    WHERE schemaname = p_schema AND tablename = p_base_table;

    IF base_table_owner IS NOT NULL THEN
        EXECUTE format('ALTER TABLE %s OWNER TO %I', qual_changes, base_table_owner);
    END IF;

    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS %I ON %s (session_id, %s, _cow_order DESC)',
        changes_table_name || '_session_pk_order_idx',
        qual_changes, pk_cols_quoted
    );

    -- 2. Build column lists from the base table
    SELECT
        string_agg(quote_ident(c.column_name), ', ' ORDER BY c.ordinal_position),
        string_agg(format('b.%I', c.column_name), ', ' ORDER BY c.ordinal_position),
        string_agg(format('COALESCE(c.%1$I, b.%1$I) AS %1$I', c.column_name), ', ' ORDER BY c.ordinal_position),
        string_agg(format('c.%I', c.column_name), ', ' ORDER BY c.ordinal_position),
        string_agg(
            CASE WHEN NOT (c.column_name = ANY(p_pk_cols)) THEN
                format('%1$I = COALESCE(EXCLUDED.%1$I, %2$s)', c.column_name, COALESCE(c.column_default, 'NULL'))
            END,
            ', ' ORDER BY c.ordinal_position
        ) FILTER (WHERE NOT (c.column_name = ANY(p_pk_cols))),
        string_agg(
            format('COALESCE(NEW.%I, %s)', c.column_name, COALESCE(c.column_default, 'NULL')),
            ', ' ORDER BY c.ordinal_position
        ),
        string_agg(format('OLD.%I', c.column_name), ', ' ORDER BY c.ordinal_position),
        string_agg(
            CASE WHEN NOT (c.column_name = ANY(p_pk_cols)) THEN
                format('%1$I = COALESCE(NEW.%1$I, %2$s)', c.column_name, COALESCE(c.column_default, 'NULL'))
            END,
            ', ' ORDER BY c.ordinal_position
        ) FILTER (WHERE NOT (c.column_name = ANY(p_pk_cols)))
    INTO
        col_list, col_list_prefixed_b, coalesce_select_list, changes_select_list,
        excluded_set_list, new_values_list, old_values_list, base_update_set
    FROM information_schema.columns c
    WHERE c.table_schema = p_schema AND c.table_name = p_base_table;

    -- 3. Create the COW overlay view
    EXECUTE format($v$
        CREATE OR REPLACE VIEW %s AS
        SELECT %s
        FROM %s b
        WHERE NULLIF(current_setting('app.session_id', true), '') IS NULL

        UNION ALL

        SELECT %s
        FROM %s b
        LEFT JOIN LATERAL (
            SELECT * FROM %s c2
            WHERE c2.session_id = NULLIF(current_setting('app.session_id', true), '')::uuid
              AND %s
              AND (
                    NULLIF(current_setting('app.visible_operations', true), '') IS NULL
                    OR c2.operation_id = ANY(
                         string_to_array(current_setting('app.visible_operations', true), ',')::uuid[]
                       )
                  )
            ORDER BY c2._cow_order DESC
            LIMIT 1
        ) c ON true
        WHERE NULLIF(current_setting('app.session_id', true), '') IS NOT NULL
          AND COALESCE(c._cow_deleted, false) = false

        UNION ALL

        SELECT %s
        FROM (
            SELECT DISTINCT ON (%s) c3.*
            FROM %s c3
            WHERE c3.session_id = NULLIF(current_setting('app.session_id', true), '')::uuid
              AND (
                    NULLIF(current_setting('app.visible_operations', true), '') IS NULL
                    OR c3.operation_id = ANY(
                         string_to_array(current_setting('app.visible_operations', true), ',')::uuid[]
                       )
                  )
            ORDER BY %s, c3._cow_order DESC
        ) c
        LEFT JOIN %s b ON %s
        WHERE NULLIF(current_setting('app.session_id', true), '') IS NOT NULL
          AND %s
          AND c._cow_deleted = false;
    $v$,
        qual_view,
        col_list_prefixed_b, qual_base,
        coalesce_select_list, qual_base, qual_changes, pk_join_condition,
        changes_select_list, pk_distinct_on, qual_changes, pk_order_by,
        qual_base, pk_base_join, pk_null_check
    );

    IF base_table_owner IS NOT NULL THEN
        EXECUTE format('ALTER VIEW %s OWNER TO %I', qual_view, base_table_owner);
    END IF;

    -- 4. Upsert trigger function
    IF base_update_set IS NULL OR base_update_set = '' THEN
        base_on_conflict := 'DO NOTHING';
        changes_on_conflict := 'DO UPDATE SET _cow_deleted = false, _cow_updated_at = now(), _cow_order = EXCLUDED._cow_order';
    ELSE
        base_on_conflict := format('DO UPDATE SET %s', base_update_set);
        changes_on_conflict := format('DO UPDATE SET %s, _cow_deleted = false, _cow_updated_at = now(), _cow_order = EXCLUDED._cow_order', excluded_set_list);
    END IF;

    IF fail_closed_writes THEN
        missing_upsert_context_sql :=
            'RAISE EXCEPTION ''app.session_id and app.operation_id must be set for COW writes'' USING ERRCODE = ''22023'';';
        missing_delete_context_sql := missing_upsert_context_sql;
    ELSE
        missing_upsert_context_sql := format(
            'INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) %s;',
            qual_base, col_list, new_values_list, pk_cols_quoted, base_on_conflict
        );
        missing_delete_context_sql := format(
            'DELETE FROM %s WHERE %s;',
            qual_base, pk_delete_condition
        );
    END IF;

    EXECUTE format($f$
        CREATE OR REPLACE FUNCTION %I.%I()
        RETURNS trigger
        LANGUAGE plpgsql
        %s
        SET search_path = pg_catalog
        AS $trigger$
        DECLARE
            sess uuid;
            sess_str text;
            op_id uuid;
            op_str text;
            baseline_exists boolean;
            baseline_row jsonb;
            baseline_schema jsonb;
        BEGIN
            sess_str := NULLIF(current_setting('app.session_id', true), '');
            IF sess_str IS NOT NULL THEN
                sess := sess_str::uuid;
            END IF;

            IF sess IS NULL THEN
                %s
            ELSE
                op_str := NULLIF(current_setting('app.operation_id', true), '');
                IF op_str IS NULL THEN
                    RAISE EXCEPTION 'app.operation_id must be set when app.session_id is set'
                        USING ERRCODE = '22023';
                END IF;
                op_id := op_str::uuid;

                %s

                INSERT INTO %s (
                    session_id, operation_id, %s,
                    _cow_deleted, _cow_updated_at,
                    _cow_base_exists, _cow_base_row, _cow_base_schema
                )
                VALUES (
                    sess, op_id, %s, false, now(),
                    baseline_exists, baseline_row, baseline_schema
                )
                ON CONFLICT (session_id, operation_id, %s) %s;

                INSERT INTO %s (schema_name, session_id, table_name)
                VALUES (TG_TABLE_SCHEMA, sess, TG_TABLE_NAME)
                ON CONFLICT DO NOTHING;
            END IF;

            RETURN NEW;
        END;
        $trigger$;
    $f$,
        p_schema, upsert_fn_name, trigger_security_clause,
        missing_upsert_context_sql,
        capture_new_baseline_sql,
        qual_changes, col_list, new_values_list, pk_cols_quoted, changes_on_conflict,
        qual_dirty_tables
    );

    EXECUTE format(
        'REVOKE ALL ON FUNCTION %I.%I() FROM PUBLIC',
        p_schema, upsert_fn_name
    );

    -- 5. Delete trigger function
    EXECUTE format($f$
        CREATE OR REPLACE FUNCTION %I.%I()
        RETURNS trigger
        LANGUAGE plpgsql
        %s
        SET search_path = pg_catalog
        AS $trigger$
        DECLARE
            sess uuid;
            sess_str text;
            op_id uuid;
            op_str text;
            baseline_exists boolean;
            baseline_row jsonb;
            baseline_schema jsonb;
        BEGIN
            sess_str := NULLIF(current_setting('app.session_id', true), '');
            IF sess_str IS NOT NULL THEN
                sess := sess_str::uuid;
            END IF;

            IF sess IS NULL THEN
                %s
            ELSE
                op_str := NULLIF(current_setting('app.operation_id', true), '');
                IF op_str IS NULL THEN
                    RAISE EXCEPTION 'app.operation_id must be set when app.session_id is set'
                        USING ERRCODE = '22023';
                END IF;
                op_id := op_str::uuid;

                %s

                INSERT INTO %s (
                    session_id, operation_id, %s,
                    _cow_deleted, _cow_updated_at,
                    _cow_base_exists, _cow_base_row, _cow_base_schema
                )
                VALUES (
                    sess, op_id, %s, true, now(),
                    baseline_exists, baseline_row, baseline_schema
                )
                ON CONFLICT (session_id, operation_id, %s) DO UPDATE
                    SET _cow_deleted = true,
                        _cow_updated_at = now(),
                        _cow_order = EXCLUDED._cow_order;

                INSERT INTO %s (schema_name, session_id, table_name)
                VALUES (TG_TABLE_SCHEMA, sess, TG_TABLE_NAME)
                ON CONFLICT DO NOTHING;
            END IF;

            RETURN OLD;
        END;
        $trigger$;
    $f$,
        p_schema, delete_fn_name, trigger_security_clause,
        missing_delete_context_sql,
        capture_old_baseline_sql,
        qual_changes, col_list, old_values_list, pk_cols_quoted,
        qual_dirty_tables
    );

    EXECUTE format(
        'REVOKE ALL ON FUNCTION %I.%I() FROM PUBLIC',
        p_schema, delete_fn_name
    );

    -- 6. Attach triggers to the COW view
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s;', upsert_fn_name || '_trigger', qual_view);
    EXECUTE format(
        'CREATE TRIGGER %I INSTEAD OF INSERT OR UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION %I.%I();',
        upsert_fn_name || '_trigger', qual_view, p_schema, upsert_fn_name
    );

    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s;', delete_fn_name || '_trigger', qual_view);
    EXECUTE format(
        'CREATE TRIGGER %I INSTEAD OF DELETE ON %s FOR EACH ROW EXECUTE FUNCTION %I.%I();',
        delete_fn_name || '_trigger', qual_view, p_schema, delete_fn_name
    );
END;
$$;
"""

GET_COW_CONFLICTS_SQL = """
CREATE OR REPLACE FUNCTION agentcow.get_cow_conflicts(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL,
    p_deleted         boolean DEFAULT NULL
)
RETURNS TABLE (
    table_name text,
    primary_key jsonb,
    conflict_kind text,
    operation_id uuid,
    cow_order bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_base          text := format('%I.%I', p_schema, p_base_table);
    qual_changes       text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    view_name          text := regexp_replace(p_base_table, '_base$', '');
    pk_cols_quoted     text;
    pk_join_condition  text;
    pk_json            text;
    base_present       text;
    schema_signature   jsonb;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);
    PERFORM agentcow._cow_require_primary_key(p_schema, p_base_table, p_pk_cols);

    pk_cols_quoted := (
        SELECT string_agg(quote_ident(col), ', ')
        FROM unnest(p_pk_cols) col
    );
    pk_join_condition := (
        SELECT string_agg(format('c.%I = b.%I', col, col), ' AND ')
        FROM unnest(p_pk_cols) col
    );
    pk_json := format(
        'jsonb_build_object(%s)',
        (
            SELECT string_agg(format('%L, c.%I', col, col), ', ')
            FROM unnest(p_pk_cols) col
        )
    );
    base_present := format('b.%I IS NOT NULL', p_pk_cols[1]);
    schema_signature := agentcow._cow_schema_signature(p_schema, p_base_table);

    RETURN QUERY EXECUTE format($sql$
        WITH latest AS (
            SELECT DISTINCT ON (%s) *
            FROM %s
            WHERE session_id = $1
              AND ($2::uuid[] IS NULL OR operation_id = ANY($2))
            ORDER BY %s, _cow_order DESC
        )
        SELECT %L::text,
               (%s),
               CASE
                   WHEN c._cow_base_schema IS DISTINCT FROM $4
                       THEN 'BASE_SCHEMA_CHANGED'
                   WHEN c._cow_base_exists AND NOT (%s)
                       THEN 'BASE_ROW_DELETED'
                   WHEN NOT c._cow_base_exists AND (%s)
                       THEN 'BASE_ROW_CREATED'
                   ELSE 'BASE_ROW_CHANGED'
               END,
               c.operation_id,
               c._cow_order
        FROM latest c
        LEFT JOIN %s b ON %s
        WHERE ($3::boolean IS NULL OR c._cow_deleted = $3)
          AND (
              c._cow_base_schema IS DISTINCT FROM $4
              OR (c._cow_base_exists AND NOT (%s))
              OR (NOT c._cow_base_exists AND (%s))
              OR (
                  c._cow_base_exists
                  AND (%s)
                  AND to_jsonb(b) IS DISTINCT FROM c._cow_base_row
              )
          )
        ORDER BY c._cow_order
    $sql$,
        pk_cols_quoted,
        qual_changes,
        pk_cols_quoted,
        view_name,
        pk_json,
        base_present,
        base_present,
        qual_base,
        pk_join_condition,
        base_present,
        base_present,
        base_present
    ) USING p_session, p_operation_ids, p_deleted, schema_signature;
END;
$$;
"""

REQUIRE_SELECTIVE_PREFIX_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_require_selective_prefix(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[]
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_changes       text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    pk_same_row        text;
    pk_json            text;
    invalid_operation  uuid;
    invalid_key        text;
BEGIN
    IF p_operation_ids IS NULL THEN
        RETURN;
    END IF;

    pk_same_row := (
        SELECT string_agg(format('prior.%I = selected.%I', col, col), ' AND ')
        FROM unnest(p_pk_cols) col
    );
    pk_json := format(
        'jsonb_build_object(%s)',
        (
            SELECT string_agg(format('%L, selected.%I', col, col), ', ')
            FROM unnest(p_pk_cols) col
        )
    );

    EXECUTE format($sql$
        SELECT selected.operation_id, (%s)::text
        FROM %s selected
        WHERE selected.session_id = $1
          AND selected.operation_id = ANY($2)
          AND EXISTS (
              SELECT 1
              FROM %s prior
              WHERE prior.session_id = selected.session_id
                AND %s
                AND prior._cow_order < selected._cow_order
                AND NOT (prior.operation_id = ANY($2))
          )
        ORDER BY selected._cow_order
        LIMIT 1
    $sql$, pk_json, qual_changes, qual_changes, pk_same_row)
    INTO invalid_operation, invalid_key
    USING p_session, p_operation_ids;

    IF invalid_operation IS NOT NULL THEN
        RAISE EXCEPTION
            'Selective COW commit is not a causal prefix for key %; operation % has an unselected predecessor',
            invalid_key, invalid_operation
            USING ERRCODE = '22023';
    END IF;
END;
$$;
"""

REBASE_COW_AFTER_COMMIT_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_rebase_after_commit(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[],
    p_deleted         boolean
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_base          text := format('%I.%I', p_schema, p_base_table);
    qual_changes       text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    pk_cols_quoted     text;
    pk_latest_base     text;
    pk_remaining       text;
    base_present       text;
    schema_signature   jsonb;
BEGIN
    IF p_operation_ids IS NULL THEN
        RETURN;
    END IF;

    pk_cols_quoted := (
        SELECT string_agg(quote_ident(col), ', ')
        FROM unnest(p_pk_cols) col
    );
    pk_latest_base := (
        SELECT string_agg(format('latest.%I = b.%I', col, col), ' AND ')
        FROM unnest(p_pk_cols) col
    );
    pk_remaining := (
        SELECT string_agg(format('remaining.%I = rebased.%I', col, col), ' AND ')
        FROM unnest(p_pk_cols) col
    );
    base_present := format('b.%I IS NOT NULL', p_pk_cols[1]);
    schema_signature := agentcow._cow_schema_signature(p_schema, p_base_table);

    EXECUTE format($sql$
        WITH latest AS (
            SELECT DISTINCT ON (%s) *
            FROM %s
            WHERE session_id = $1
              AND operation_id = ANY($2)
            ORDER BY %s, _cow_order DESC
        ),
        rebased AS (
            SELECT latest.*,
                   (%s) AS base_exists,
                   CASE WHEN (%s) THEN to_jsonb(b) ELSE NULL END AS base_row
            FROM latest
            LEFT JOIN %s b ON %s
            WHERE latest._cow_deleted = $3
        )
        UPDATE %s remaining
        SET _cow_base_exists = rebased.base_exists,
            _cow_base_row = rebased.base_row,
            _cow_base_schema = $4
        FROM rebased
        WHERE remaining.session_id = $1
          AND remaining._cow_order > rebased._cow_order
          AND %s
    $sql$,
        pk_cols_quoted,
        qual_changes,
        pk_cols_quoted,
        base_present,
        base_present,
        qual_base,
        pk_latest_base,
        qual_changes,
        pk_remaining
    ) USING p_session, p_operation_ids, p_deleted, schema_signature;
END;
$$;
"""

COMMIT_COW_UPSERT_SQL = """
CREATE OR REPLACE FUNCTION agentcow.commit_cow_upsert(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL,
    p_conflict_policy text DEFAULT 'error'
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_base          text := format('%I.%I', p_schema, p_base_table);
    qual_changes       text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    pk_cols_quoted     text;
    update_set_clause  text;
    col_list           text;
    detected_conflict  RECORD;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);
    PERFORM agentcow._cow_require_primary_key(p_schema, p_base_table, p_pk_cols);

    IF p_conflict_policy NOT IN ('error', 'overwrite') THEN
        RAISE EXCEPTION 'Unsupported COW conflict policy: %', p_conflict_policy
            USING ERRCODE = '22023';
    END IF;

    EXECUTE format(
        'LOCK TABLE %s, %s IN SHARE ROW EXCLUSIVE MODE',
        qual_base, qual_changes
    );
    PERFORM agentcow._cow_require_selective_prefix(
        p_schema, p_base_table, p_pk_cols, p_session, p_operation_ids
    );

    IF p_conflict_policy = 'error' THEN
        SELECT * INTO detected_conflict
        FROM agentcow.get_cow_conflicts(
            p_schema,
            p_base_table,
            p_pk_cols,
            p_session,
            p_operation_ids,
            false
        )
        LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'COW conflict on %.% key %: %',
                p_schema,
                regexp_replace(p_base_table, '_base$', ''),
                detected_conflict.primary_key,
                detected_conflict.conflict_kind
                USING ERRCODE = '40001';
        END IF;
    END IF;

    pk_cols_quoted := (SELECT string_agg(quote_ident(col), ', ') FROM unnest(p_pk_cols) col);

    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
    INTO col_list
    FROM information_schema.columns
    WHERE table_schema = p_schema AND table_name = p_base_table;

    SELECT string_agg(
        format('%1$I = EXCLUDED.%1$I', column_name),
        ', ' ORDER BY ordinal_position
    )
    INTO update_set_clause
    FROM information_schema.columns
    WHERE table_schema = p_schema
      AND table_name = p_base_table
      AND NOT (column_name = ANY(p_pk_cols));

    IF update_set_clause IS NULL OR update_set_clause = '' THEN
        EXECUTE format($sql$
            INSERT INTO %s
            SELECT %s FROM (
                SELECT DISTINCT ON (%s) *
                FROM %s
                WHERE session_id = $1
                  AND ($2::uuid[] IS NULL OR operation_id = ANY($2))
                ORDER BY %s, _cow_order DESC
            ) latest
            WHERE latest._cow_deleted = FALSE
            ON CONFLICT (%s) DO NOTHING
        $sql$, qual_base, col_list, pk_cols_quoted, qual_changes, pk_cols_quoted, pk_cols_quoted)
        USING p_session, p_operation_ids;
    ELSE
        EXECUTE format($sql$
            INSERT INTO %s
            SELECT %s FROM (
                SELECT DISTINCT ON (%s) *
                FROM %s
                WHERE session_id = $1
                  AND ($2::uuid[] IS NULL OR operation_id = ANY($2))
                ORDER BY %s, _cow_order DESC
            ) latest
            WHERE latest._cow_deleted = FALSE
            ON CONFLICT (%s) DO UPDATE SET %s
        $sql$, qual_base, col_list, pk_cols_quoted, qual_changes, pk_cols_quoted, pk_cols_quoted, update_set_clause)
        USING p_session, p_operation_ids;
    END IF;

    PERFORM agentcow._cow_rebase_after_commit(
        p_schema,
        p_base_table,
        p_pk_cols,
        p_session,
        p_operation_ids,
        false
    );
END;
$$;
"""

COMMIT_COW_DELETE_SQL = """
CREATE OR REPLACE FUNCTION agentcow.commit_cow_delete(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL,
    p_conflict_policy text DEFAULT 'error'
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_base          text := format('%I.%I', p_schema, p_base_table);
    qual_changes       text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    pk_cols_quoted     text;
    pk_join_condition  text;
    detected_conflict  RECORD;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);
    PERFORM agentcow._cow_require_primary_key(p_schema, p_base_table, p_pk_cols);

    IF p_conflict_policy NOT IN ('error', 'overwrite') THEN
        RAISE EXCEPTION 'Unsupported COW conflict policy: %', p_conflict_policy
            USING ERRCODE = '22023';
    END IF;

    EXECUTE format(
        'LOCK TABLE %s, %s IN SHARE ROW EXCLUSIVE MODE',
        qual_base, qual_changes
    );
    PERFORM agentcow._cow_require_selective_prefix(
        p_schema, p_base_table, p_pk_cols, p_session, p_operation_ids
    );

    IF p_conflict_policy = 'error' THEN
        SELECT * INTO detected_conflict
        FROM agentcow.get_cow_conflicts(
            p_schema,
            p_base_table,
            p_pk_cols,
            p_session,
            p_operation_ids,
            true
        )
        LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'COW conflict on %.% key %: %',
                p_schema,
                regexp_replace(p_base_table, '_base$', ''),
                detected_conflict.primary_key,
                detected_conflict.conflict_kind
                USING ERRCODE = '40001';
        END IF;
    END IF;

    pk_cols_quoted := (SELECT string_agg(quote_ident(col), ', ') FROM unnest(p_pk_cols) col);
    pk_join_condition := (SELECT string_agg(format('c.%I = b.%I', col, col), ' AND ') FROM unnest(p_pk_cols) col);

    EXECUTE format($sql$
        DELETE FROM %s b
        USING (
            SELECT DISTINCT ON (%s) *
            FROM %s
            WHERE session_id = $1
              AND ($2::uuid[] IS NULL OR operation_id = ANY($2))
            ORDER BY %s, _cow_order DESC
        ) c
        WHERE c._cow_deleted = TRUE AND %s
    $sql$, qual_base, pk_cols_quoted, qual_changes, pk_cols_quoted, pk_join_condition)
    USING p_session, p_operation_ids;

    PERFORM agentcow._cow_rebase_after_commit(
        p_schema,
        p_base_table,
        p_pk_cols,
        p_session,
        p_operation_ids,
        true
    );
END;
$$;
"""

COMMIT_COW_CLEANUP_SQL = """
CREATE OR REPLACE FUNCTION agentcow.commit_cow_cleanup(
    p_schema          text,
    p_base_table      text,
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_changes   text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    qual_dirty_tables text := format('%I.%I', p_schema, 'cow_dirty_tables');
    p_view_name    text := regexp_replace(p_base_table, '_base$', '');
    has_remaining  boolean;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);

    EXECUTE format(
        'DELETE FROM %s WHERE session_id = $1 AND ($2::uuid[] IS NULL OR operation_id = ANY($2))',
        qual_changes
    )
    USING p_session, p_operation_ids;

    EXECUTE format(
        'SELECT EXISTS(SELECT 1 FROM %s WHERE session_id = $1 LIMIT 1)',
        qual_changes
    ) INTO has_remaining USING p_session;

    IF NOT has_remaining THEN
        EXECUTE format(
            'DELETE FROM %s WHERE schema_name = $1 AND session_id = $2 AND table_name = $3',
            qual_dirty_tables
        ) USING p_schema, p_session, p_view_name;
    END IF;
END;
$$;
"""

COMMIT_COW_SQL = """
CREATE OR REPLACE FUNCTION agentcow.commit_cow(
    p_schema          text,
    p_base_table      text,
    p_pk_cols         text[],
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL,
    p_conflict_policy text DEFAULT 'error'
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);
    PERFORM agentcow._cow_require_primary_key(p_schema, p_base_table, p_pk_cols);

    PERFORM agentcow.commit_cow_upsert(
        p_schema,
        p_base_table,
        p_pk_cols,
        p_session,
        p_operation_ids,
        p_conflict_policy
    );
    PERFORM agentcow.commit_cow_delete(
        p_schema,
        p_base_table,
        p_pk_cols,
        p_session,
        p_operation_ids,
        p_conflict_policy
    );
    PERFORM agentcow.commit_cow_cleanup(p_schema, p_base_table, p_session, p_operation_ids);
END;
$$;
"""

DISCARD_COW_SQL = """
CREATE OR REPLACE FUNCTION agentcow.discard_cow(
    p_schema          text,
    p_base_table      text,
    p_session         uuid,
    p_operation_ids   uuid[] DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    qual_changes  text := format('%I.%I', p_schema, agentcow._cow_changes_table_name(p_base_table));
    qual_dirty_tables text := format('%I.%I', p_schema, 'cow_dirty_tables');
    p_view_name   text := regexp_replace(p_base_table, '_base$', '');
    has_remaining  boolean;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);

    EXECUTE format(
        'DELETE FROM %s WHERE session_id = $1 AND ($2::uuid[] IS NULL OR operation_id = ANY($2))',
        qual_changes
    )
    USING p_session, p_operation_ids;

    -- Clean up dirty table entry if no changes remain for this session+table
    EXECUTE format(
        'SELECT EXISTS(SELECT 1 FROM %s WHERE session_id = $1 LIMIT 1)',
        qual_changes
    ) INTO has_remaining USING p_session;

    IF NOT has_remaining THEN
        EXECUTE format(
            'DELETE FROM %s WHERE schema_name = $1 AND session_id = $2 AND table_name = $3',
            qual_dirty_tables
        ) USING p_schema, p_session, p_view_name;
    END IF;
END;
$$;
"""

TEARDOWN_COW_SQL = """
CREATE OR REPLACE FUNCTION agentcow.teardown_cow(
    p_schema    text,
    p_view_name text
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    base_table_name    text := p_view_name || '_base';
    qual_base          text := format('%I.%I', p_schema, base_table_name);
    changes_table_name text := p_view_name || '_changes';
    upsert_fn_name     text := p_view_name || '_cow_upsert';
    delete_fn_name     text := p_view_name || '_cow_delete';
    qual_dirty_tables  text := format('%I.%I', p_schema, 'cow_dirty_tables');
    order_sequence_is_managed boolean;
    r                  RECORD;
BEGIN
    -- Revert FK constraints on the base table to NOT DEFERRABLE, undoing
    -- what setup_cow did. Only touch constraints we know we flipped
    -- (DEFERRABLE INITIALLY IMMEDIATE); leave any that were already
    -- DEFERRABLE INITIALLY DEFERRED alone.
    FOR r IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class cls ON con.conrelid = cls.oid
        JOIN pg_namespace ns ON cls.relnamespace = ns.oid
        WHERE con.contype = 'f'
          AND ns.nspname = p_schema
          AND cls.relname = base_table_name
          AND con.condeferrable
          AND NOT con.condeferred
    LOOP
        EXECUTE format(
            'ALTER TABLE %s ALTER CONSTRAINT %I NOT DEFERRABLE',
            qual_base, r.conname
        );
    END LOOP;

    EXECUTE format('DROP VIEW IF EXISTS %I.%I CASCADE', p_schema, p_view_name);
    EXECUTE format('DROP TABLE IF EXISTS %I.%I CASCADE', p_schema, changes_table_name);
    EXECUTE format('DROP FUNCTION IF EXISTS %I.%I()', p_schema, upsert_fn_name);
    EXECUTE format('DROP FUNCTION IF EXISTS %I.%I()', p_schema, delete_fn_name);

    EXECUTE format(
        'DELETE FROM %s WHERE schema_name = $1 AND table_name = $2',
        qual_dirty_tables
    ) USING p_schema, p_view_name;

    DELETE FROM agentcow._cow_table_security_modes
    WHERE schema_name = p_schema AND view_name = p_view_name;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = p_schema
          AND column_name = '_cow_order'
          AND right(table_name, 8) = '_changes'
    ) THEN
        SELECT obj_description(cls.oid, 'pg_class') =
               'agent-cow deterministic operation order'
        INTO order_sequence_is_managed
        FROM pg_class cls
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = p_schema
          AND cls.relname = '_cow_operation_order_seq'
          AND cls.relkind = 'S';

        IF COALESCE(order_sequence_is_managed, false) THEN
            EXECUTE format(
                'DROP SEQUENCE %I.%I',
                p_schema, '_cow_operation_order_seq'
            );
        END IF;
    END IF;
END;
$$;
"""

GET_DIRTY_CHANGES_TABLES_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_dirty_changes_tables(
    p_schema     text,
    p_session_id uuid
)
RETURNS TABLE(table_name text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    RETURN QUERY EXECUTE format(
        'SELECT d.table_name || ''_changes'' FROM %I.cow_dirty_tables d WHERE d.schema_name = $1 AND d.session_id = $2',
        p_schema
    ) USING p_schema, p_session_id;
END;
$$;
"""

GET_COW_DEPENDENCIES_SQL = """
CREATE OR REPLACE FUNCTION agentcow.get_cow_dependencies(
    p_schema     text,
    p_session_id uuid
)
RETURNS TABLE(depends_on uuid, operation_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    tbl RECORD;
    fk RECORD;
    query text := '';
    fk_query text := '';
    pk_cols text[];
    pk_join_condition text;
    base_table_name text;
    referenced_table_name text;
    referenced_changes_table text;
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);

    FOR tbl IN
        SELECT t.table_name FROM agentcow._cow_dirty_changes_tables(p_schema, p_session_id) t
    LOOP
        SELECT array_agg(kcu.column_name ORDER BY kcu.ordinal_position) INTO pk_cols
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = p_schema
          AND tc.table_name = tbl.table_name
          AND kcu.ordinal_position >= 3;

        IF pk_cols IS NULL OR array_length(pk_cols, 1) IS NULL THEN
            CONTINUE;
        END IF;

        pk_join_condition := (SELECT string_agg(format('a.%I = b.%I', col, col), ' AND ') FROM unnest(pk_cols) col);

        IF query != '' THEN
            query := query || ' UNION ';
        END IF;

        query := query || format($q$
            SELECT DISTINCT a.operation_id as dep_on, b.operation_id as op_id
            FROM %I.%I a
            JOIN %I.%I b
              ON a.session_id = b.session_id
             AND %s
             AND a.operation_id != b.operation_id
            WHERE a.session_id = $1
              AND a._cow_order < b._cow_order
        $q$, p_schema, tbl.table_name, p_schema, tbl.table_name, pk_join_condition);
    END LOOP;

    FOR tbl IN
        SELECT t.table_name FROM agentcow._cow_dirty_changes_tables(p_schema, p_session_id) t
    LOOP
        base_table_name := regexp_replace(tbl.table_name, '_changes$', '');

        FOR fk IN
            SELECT
                kcu.column_name AS fk_column,
                ccu.table_name AS referenced_table,
                ccu.column_name AS referenced_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = p_schema
              AND (tc.table_name = base_table_name || '_base' OR tc.table_name = base_table_name)
            GROUP BY kcu.column_name, ccu.table_name, ccu.column_name
        LOOP
            referenced_table_name := regexp_replace(fk.referenced_table, '_base$', '');
            referenced_changes_table := referenced_table_name || '_changes';

            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = p_schema
                  AND table_name = referenced_changes_table
            ) THEN
                IF fk_query != '' THEN
                    fk_query := fk_query || ' UNION ';
                END IF;

                fk_query := fk_query || format($q$
                    SELECT a.operation_id as dep_on, b.operation_id as op_id
                    FROM (
                        SELECT operation_id, %I, MIN(_cow_order) as earliest_order
                        FROM %I.%I
                        WHERE session_id = $1 AND _cow_deleted = false
                        GROUP BY operation_id, %I
                    ) a
                    JOIN (
                        SELECT operation_id, %I, MIN(_cow_order) as earliest_order
                        FROM %I.%I
                        WHERE session_id = $1 AND _cow_deleted = false
                        GROUP BY operation_id, %I
                    ) b
                      ON a.%I = b.%I
                     AND a.operation_id != b.operation_id
                     AND a.earliest_order < b.earliest_order
                $q$,
                    fk.referenced_column,
                    p_schema, referenced_changes_table,
                    fk.referenced_column,
                    fk.fk_column,
                    p_schema, tbl.table_name,
                    fk.fk_column,
                    fk.referenced_column, fk.fk_column
                );
            END IF;
        END LOOP;
    END LOOP;

    IF query = '' AND fk_query = '' THEN
        RETURN;
    END IF;

    IF query != '' AND fk_query != '' THEN
        query := query || ' UNION ' || fk_query;
    ELSIF fk_query != '' THEN
        query := fk_query;
    END IF;

    RETURN QUERY EXECUTE query USING p_session_id;
END;
$$;
"""

GET_SESSION_OPERATIONS_SQL = """
CREATE OR REPLACE FUNCTION agentcow.get_cow_session_operations(
    p_schema     text,
    p_session_id uuid
)
RETURNS TABLE(operation_id uuid, earliest_change timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    tbl RECORD;
    query text := '';
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);

    FOR tbl IN
        SELECT t.table_name FROM agentcow._cow_dirty_changes_tables(p_schema, p_session_id) t
    LOOP
        IF query != '' THEN
            query := query || ' UNION ALL ';
        END IF;

        query := query || format($q$
            SELECT
                operation_id,
                MIN(_cow_order) as earliest_order,
                MIN(_cow_updated_at) as earliest_change
            FROM %I.%I
            WHERE session_id = $1
            GROUP BY operation_id
        $q$, p_schema, tbl.table_name);
    END LOOP;

    IF query = '' THEN
        RETURN;
    END IF;

    RETURN QUERY EXECUTE format($q$
        SELECT operation_id, MIN(earliest_change) as earliest_change
        FROM (%s) combined
        GROUP BY operation_id
        ORDER BY MIN(earliest_order)
    $q$, query) USING p_session_id;
END;
$$;
"""

GET_COW_DIRTY_TABLES_SQL = """
CREATE OR REPLACE FUNCTION agentcow.get_cow_dirty_tables(
    p_schema text,
    p_session_id uuid
)
RETURNS TABLE(table_name text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);

    RETURN QUERY
    SELECT regexp_replace(dirty.table_name, '_changes$', '')
    FROM agentcow._cow_dirty_changes_tables(p_schema, p_session_id) dirty
    ORDER BY dirty.table_name;
END;
$$;
"""

GET_COW_PRIMARY_KEY_COLUMNS_SQL = """
CREATE OR REPLACE FUNCTION agentcow.get_cow_primary_key_columns(
    p_schema text,
    p_base_table text
)
RETURNS TABLE(column_name text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);
    PERFORM agentcow._cow_require_cow_table(p_schema, p_base_table);

    RETURN QUERY
    SELECT attr.attname::text
    FROM pg_constraint constraint_
    JOIN pg_class table_ ON table_.oid = constraint_.conrelid
    JOIN pg_namespace namespace_ ON namespace_.oid = table_.relnamespace
    CROSS JOIN LATERAL unnest(constraint_.conkey) WITH ORDINALITY key_(attnum, ordinal)
    JOIN pg_attribute attr
      ON attr.attrelid = table_.oid
     AND attr.attnum = key_.attnum
    WHERE constraint_.contype = 'p'
      AND namespace_.nspname = p_schema
      AND table_.relname = p_base_table
    ORDER BY key_.ordinal;
END;
$$;
"""

GET_COW_FK_EDGES_SQL = """
CREATE OR REPLACE FUNCTION agentcow._cow_fk_edges(
    p_schema      text,
    p_base_tables text[]
)
RETURNS TABLE(parent_base_table text, child_base_table text, is_self_ref boolean)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM agentcow._cow_require_reviewer(p_schema);

    RETURN QUERY
    SELECT DISTINCT
        parent_cls.relname::text AS parent_base_table,
        child_cls.relname::text  AS child_base_table,
        (parent_cls.oid = child_cls.oid) AS is_self_ref
    FROM pg_constraint con
    JOIN pg_class child_cls ON con.conrelid = child_cls.oid
    JOIN pg_namespace child_ns ON child_cls.relnamespace = child_ns.oid
    JOIN pg_class parent_cls ON con.confrelid = parent_cls.oid
    JOIN pg_namespace parent_ns ON parent_cls.relnamespace = parent_ns.oid
    WHERE con.contype = 'f'
      AND child_ns.nspname = p_schema
      AND parent_ns.nspname = p_schema
      AND child_cls.relname = ANY(p_base_tables)
      AND parent_cls.relname = ANY(p_base_tables);
END;
$$;
"""
