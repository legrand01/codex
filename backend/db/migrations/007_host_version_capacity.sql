-- PostgreSQL version() includes build, architecture, compiler, and libc details.
ALTER TABLE hosts
    ALTER COLUMN pg_version TYPE VARCHAR(255);
