-- Aurora PostgreSQL: operational side of c360 (customer, address, credit_card, rewards_account)
CREATE SCHEMA IF NOT EXISTS c360;
SET search_path TO c360;

DROP TABLE IF EXISTS rewards_account, credit_card, customer, address CASCADE;

CREATE TABLE address (
  id            varchar(36) PRIMARY KEY,
  street_number varchar(10),
  street_name   varchar(64),
  street_suffix varchar(32),
  state         varchar(32),
  zip           varchar(10),
  city          varchar(64)
);

CREATE TABLE customer (
  cid        integer PRIMARY KEY,
  first_name varchar(64),
  last_name  varchar(64),
  email      varchar(128),
  ssn        varchar(16),
  picture    varchar(256),
  phone      varchar(20),
  location   varchar(36) REFERENCES address(id),
  full_name  varchar(128)
);

CREATE TABLE credit_card (
  id        varchar(36) PRIMARY KEY,
  cid       integer REFERENCES customer(cid),
  card_num  varchar(20),
  card_type varchar(32)
);

CREATE TABLE rewards_account (
  id          varchar(36) PRIMARY KEY,
  cid         integer REFERENCES customer(cid),
  account_id  varchar(16),
  create_date date
);
