
{% macro redshift__get_base_catalog() -%}
  {%- call statement('base_catalog', fetch_result=True) -%}
    {% if (databases | length) != 1 %}
        exceptions.raise_compiler_error('redshift get_catalog requires exactly one database')
    {% endif %}
    {% set database = databases[0] %}
    {{ adapter.verify_database(database) }}

    with late_binding as (
      select
        '{{ database }}'::varchar as table_database,
        table_schema,
        table_name,
        'LATE BINDING VIEW'::varchar as table_type,
        null::text as table_comment,

        column_name,
        column_index,
        column_type,
        null::text as column_comment
      from pg_get_late_binding_view_cols()
        cols(table_schema name, table_name name, column_name name,
             column_type varchar,
             column_index int)
        order by "column_index"
    ),

    table_owners as (

        select
            '{{ database }}'::varchar as table_database,
            schemaname as table_schema,
            tablename as table_name,
            tableowner as table_owner

        from pg_tables

        union all

        select
            '{{ database }}'::varchar as table_database,
            schemaname as table_schema,
            viewname as table_name,
            viewowner as table_owner

        from pg_views

    ),

    tables as (

      select
        table_catalog as table_database,
        table_schema,
        table_name,
        table_type

      from information_schema.tables

    ),

    columns as (

        select
            '{{ database }}'::varchar as table_database,
            table_schema,
            table_name,
            null::varchar as table_comment,

            column_name,
            ordinal_position as column_index,
            data_type as column_type,
            null::varchar as column_comment


        from information_schema.columns

    ),

    unioned as (

        select *
        from tables
        join columns using (table_database, table_schema, table_name)

        union all

        select *
        from late_binding

    )

    select *,
        table_database || '.' || table_schema || '.' || table_name as table_id

    from unioned
    join table_owners using (table_database, table_schema, table_name)

    where table_schema != 'information_schema'
      and table_schema not like 'pg_%'

    order by "column_index"
  {%- endcall -%}

  {{ return(load_result('base_catalog').table) }}
{%- endmacro %}

{% macro redshift__get_extended_catalog() %}
  {%- call statement('extended_catalog', fetch_result=True) -%}

    select
        "database" || '.' || "schema" || '.' || "table" as table_id,

        'Encoded'::text as "stats:encoded:label",
        encoded as "stats:encoded:value",
        'Indicates whether any column in the table has compression encoding defined.'::text as "stats:encoded:description",
        true as "stats:encoded:include",

        'Dist Style' as "stats:diststyle:label",
        diststyle as "stats:diststyle:value",
        'Distribution style or distribution key column, if key distribution is defined.'::text as "stats:diststyle:description",
        true as "stats:diststyle:include",

        'Sort Key 1' as "stats:sortkey1:label",
        -- handle 0xFF byte in response for interleaved sort styles
        case
            when sortkey1 like 'INTERLEAVED%' then 'INTERLEAVED'::text
            else sortkey1
        end as "stats:sortkey1:value",
        'First column in the sort key.'::text as "stats:sortkey1:description",
        (sortkey1 is not null) as "stats:sortkey1:include",

        'Max Varchar' as "stats:max_varchar:label",
        max_varchar as "stats:max_varchar:value",
        'Size of the largest column that uses a VARCHAR data type.'::text as "stats:max_varchar:description",
        true as "stats:max_varchar:include",

        -- exclude this, as the data is strangely returned with null-byte characters
        'Sort Key 1 Encoding' as "stats:sortkey1_enc:label",
        sortkey1_enc as "stats:sortkey1_enc:value",
        'Compression encoding of the first column in the sort key.' as "stats:sortkey1_enc:description",
        false as "stats:sortkey1_enc:include",

        '# Sort Keys' as "stats:sortkey_num:label",
        sortkey_num as "stats:sortkey_num:value",
        'Number of columns defined as sort keys.' as "stats:sortkey_num:description",
        (sortkey_num > 0) as "stats:sortkey_num:include",

        'Approximate Size' as "stats:size:label",
        size / 1000000.0 as "stats:size:value",
        'Approximate size of the table, calculated from a count of 1MB blocks'::text as "stats:size:description",
        true as "stats:size:include",

        'Disk Utilization' as "stats:pct_used:label",
        pct_used / 100.0 as "stats:pct_used:value",
        'Percent of available space that is used by the table.'::text as "stats:pct_used:description",
        true as "stats:pct_used:include",

        'Unsorted %' as "stats:unsorted:label",
        unsorted / 100.0 as "stats:unsorted:value",
        'Percent of unsorted rows in the table.'::text as "stats:unsorted:description",
        (unsorted is not null) as "stats:unsorted:include",

        'Stats Off' as "stats:stats_off:label",
        stats_off as "stats:stats_off:value",
        'Number that indicates how stale the table statistics are; 0 is current, 100 is out of date.'::text as "stats:stats_off:description",
        true as "stats:stats_off:include",

        'Approximate Row Count' as "stats:rows:label",
        tbl_rows as "stats:rows:value",
        'Approximate number of rows in the table. This value includes rows marked for deletion, but not yet vacuumed.'::text as "stats:rows:description",
        true as "stats:rows:include",

        'Sort Key Skew' as "stats:skew_sortkey1:label",
        skew_sortkey1 as "stats:skew_sortkey1:value",
        'Ratio of the size of the largest non-sort key column to the size of the first column of the sort key.'::text as "stats:skew_sortkey1:description",
        (skew_sortkey1 is not null) as "stats:skew_sortkey1:include",

        'Skew Rows' as "stats:skew_rows:label",
        skew_rows as "stats:skew_rows:value",
        'Ratio of the number of rows in the slice with the most rows to the number of rows in the slice with the fewest rows.'::text as "stats:skew_rows:description",
        (skew_rows is not null) as "stats:skew_rows:include"

    from svv_table_info

  {%- endcall -%}

  {{ return(load_result('extended_catalog').table) }}

{% endmacro %}

{% macro redshift__can_select_from(table_name) %}

  {%- call statement('has_table_privilege', fetch_result=True) -%}

    select has_table_privilege(current_user, '{{ table_name }}', 'SELECT') as can_select

  {%- endcall -%}

  {% set can_select = load_result('has_table_privilege').table[0]['can_select'] %}
  {{ return(can_select) }}

{% endmacro %}

{% macro redshift__no_svv_table_info_warning() %}

    {% set msg %}

    Warning: The database user "{{ target.user }}" has insufficient permissions to
    query the "svv_table_info" table. Please grant SELECT permissions on this table
    to the "{{ target.user }}" user to fetch extended table details from Redshift.

    {% endset %}

    {{ log(msg, info=True) }}

{% endmacro %}


{% macro redshift__get_catalog() %}

    {#-- Compute a left-outer join in memory. Some Redshift queries are
      -- leader-only, and cannot be joined to other compute-based queries #}

    {% set catalog = redshift__get_base_catalog() %}

    {% set select_extended =  redshift__can_select_from('svv_table_info') %}
    {% if select_extended %}
        {% set extended_catalog = redshift__get_extended_catalog() %}
        {% set catalog = catalog.join(extended_catalog, 'table_id') %}
    {% else %}
        {{ redshift__no_svv_table_info_warning() }}
    {% endif %}

    {{ return(catalog.exclude(['table_id'])) }}

{% endmacro %}