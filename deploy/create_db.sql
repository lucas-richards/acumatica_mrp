-- Create the MRP planner's own PostgreSQL role and database.
-- Does not touch westridgeApp's database (mydb) or its role.
--
-- Run as the postgres superuser, passing the password as a psql variable:
--   sudo -u postgres psql -v mrp_password='choose-a-strong-password' -f deploy/create_db.sql

\set ON_ERROR_STOP on

CREATE ROLE mrp_user WITH LOGIN PASSWORD :'mrp_password';
CREATE DATABASE mrp OWNER mrp_user ENCODING 'UTF8' TEMPLATE template0;

-- Only mrp_user (and superusers) may connect
REVOKE CONNECT ON DATABASE mrp FROM PUBLIC;
GRANT CONNECT ON DATABASE mrp TO mrp_user;

\connect mrp
ALTER SCHEMA public OWNER TO mrp_user;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
