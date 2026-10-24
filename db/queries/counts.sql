-- Row counts and applied migrations (read only).
SELECT (SELECT count(*) FROM questions) AS questions,
       (SELECT count(*) FROM responses) AS responses,
       (SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations) AS migrations;
