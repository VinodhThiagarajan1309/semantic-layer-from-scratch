-- Amazon Redshift: analytics side of c360 (purchase, product, category, vendor)
-- cid is a plain column here: the cross-engine "join" is the shared IRI template, not an FK.
CREATE SCHEMA IF NOT EXISTS c360;

DROP TABLE IF EXISTS c360.purchase;
DROP TABLE IF EXISTS c360.product;
DROP TABLE IF EXISTS c360.category;
DROP TABLE IF EXISTS c360.vendor;

CREATE TABLE c360.vendor (
  id          integer PRIMARY KEY,
  vendor_name varchar(128),
  industry    varchar(64),
  homepage    varchar(256)
);

CREATE TABLE c360.category (
  id          integer PRIMARY KEY,
  dept_name   varchar(64),
  parent      integer,
  description varchar(1024)
);

CREATE TABLE c360.product (
  id          integer PRIMARY KEY,
  name        varchar(128),
  brand       varchar(128),
  price       decimal(10,2),
  dept        integer,
  description varchar(2048)
);

CREATE TABLE c360.purchase (
  id             varchar(36) PRIMARY KEY,
  pid            integer,
  date           date,
  time           varchar(16),
  quantity       integer,
  price          decimal(12,2),
  cid            integer,
  card           varchar(36),
  reward_account varchar(36)
) DISTKEY(cid) SORTKEY(date);
