-- =========================================================
-- CS2 Trade-Up Lab — schema Supabase (Postgres)
-- Idempotente: pode ser executado mais de uma vez.
-- Escrita somente via service_role; leitura pública apenas de catálogo/resultados.
-- =========================================================
create extension if not exists pg_trgm;

do $$ begin
  create type public.item_quality as enum ('normal','souvenir','stattrak');
exception when duplicate_object then null; end $$;
do $$ begin
  create type public.price_source as enum ('steam_priceoverview','steam_search','skinport','csfloat','manual');
exception when duplicate_object then null; end $$;
do $$ begin
  create type public.scan_mode as enum ('guaranteed','ranked');
exception when duplicate_object then null; end $$;

-- ---------- catálogo ----------
create table if not exists public.collections (
  id          smallint generated always as identity primary key,
  name        text not null unique,
  updated_at  timestamptz not null default now()
);

create table if not exists public.skins (
  id                text primary key,
  name              text not null,
  collection_id     smallint not null references public.collections(id) on delete cascade,
  rarity            smallint not null check (rarity between 0 and 6),
  min_float         double precision not null check (min_float >= 0 and min_float <= 1),
  max_float         double precision not null check (max_float >= min_float and max_float <= 1),
  stattrak_capable  boolean not null default true,
  souvenir_capable  boolean not null default false,
  image_url         text,
  updated_at        timestamptz not null default now()
);
create index if not exists skins_collection_rarity_idx on public.skins (collection_id, rarity);
create index if not exists skins_name_idx on public.skins (name);
create index if not exists skins_name_trgm_idx on public.skins using gin (name gin_trgm_ops);

create table if not exists public.market_items (
  market_hash_name  text primary key,
  skin_name         text not null,
  wear              text not null check (wear in ('Factory New','Minimal Wear','Field-Tested','Well-Worn','Battle-Scarred')),
  quality           public.item_quality not null default 'normal'
);
create index if not exists market_items_skin_idx on public.market_items (skin_name);

-- ---------- preços: histórico append-only ----------
create table if not exists public.price_quotes (
  id                bigint generated always as identity primary key,
  market_hash_name  text not null,
  currency          smallint not null,
  source            public.price_source not null,
  lowest            numeric(12,2) check (lowest is null or lowest >= 0),
  median            numeric(12,2) check (median is null or median >= 0),
  volume_24h        integer check (volume_24h is null or volume_24h >= 0),
  sell_listings     integer check (sell_listings is null or sell_listings >= 0),
  fetched_at        timestamptz not null default now()
);
create index if not exists price_quotes_item_time_idx
  on public.price_quotes (market_hash_name, currency, fetched_at desc);
create index if not exists price_quotes_time_brin
  on public.price_quotes using brin (fetched_at);

create table if not exists public.price_latest (
  market_hash_name  text not null,
  currency          smallint not null,
  source            public.price_source not null,
  lowest            numeric(12,2),
  median            numeric(12,2),
  volume_24h        integer,
  sell_listings     integer,
  fetched_at        timestamptz not null,
  primary key (market_hash_name, currency, source)
);
create index if not exists price_latest_currency_idx on public.price_latest (currency, fetched_at desc);

create or replace function public.trg_price_latest()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.price_latest as pl
    (market_hash_name, currency, source, lowest, median, volume_24h, sell_listings, fetched_at)
  values (new.market_hash_name, new.currency, new.source, new.lowest, new.median,
          new.volume_24h, new.sell_listings, new.fetched_at)
  on conflict (market_hash_name, currency, source) do update
    set lowest = excluded.lowest, median = excluded.median,
        volume_24h = excluded.volume_24h, sell_listings = excluded.sell_listings,
        fetched_at = excluded.fetched_at
    where pl.fetched_at <= excluded.fetched_at;
  return null;
end $$;
revoke all on function public.trg_price_latest() from public, anon, authenticated;

drop trigger if exists price_quotes_latest on public.price_quotes;
create trigger price_quotes_latest after insert on public.price_quotes
  for each row execute function public.trg_price_latest();

create or replace view public.price_best with (security_invoker = true) as
select
  m.market_hash_name, m.currency,
  coalesce(o.lowest, s.lowest)                  as lowest,
  o.median                                      as median,
  o.volume_24h                                  as volume_24h,
  coalesce(s.sell_listings, o.sell_listings)    as sell_listings,
  greatest(o.fetched_at, s.fetched_at)          as fetched_at
from (select distinct market_hash_name, currency from public.price_latest
      where source in ('steam_priceoverview','steam_search')) m
left join public.price_latest o on o.market_hash_name = m.market_hash_name
  and o.currency = m.currency and o.source = 'steam_priceoverview'
left join public.price_latest s on s.market_hash_name = m.market_hash_name
  and s.currency = m.currency and s.source = 'steam_search';

create table if not exists public.price_misses (
  market_hash_name  text not null,
  currency          smallint not null,
  source            public.price_source not null,
  missed_at         timestamptz not null default now(),
  primary key (market_hash_name, currency, source)
);

-- ---------- preços por float (CSFloat) ----------
-- k anúncios mais baratos com float em [min_float, max_float]; base da
-- "float check" que re-precifica as entradas no float que o contrato exige.
create table if not exists public.float_listings (
  market_hash_name  text not null,
  min_float         numeric(6,3) not null,
  max_float         numeric(6,3) not null check (max_float > min_float),
  source            public.price_source not null default 'csfloat',
  listings          jsonb not null,          -- [[price_usd, float_value, listing_id], ...]
  fetched_at        timestamptz not null default now(),
  primary key (market_hash_name, min_float, max_float, source)
);
create index if not exists float_listings_time_idx on public.float_listings (fetched_at desc);

-- ---------- liquidez ----------
create table if not exists public.item_liquidity (
  market_hash_name  text not null,
  currency          smallint not null,
  tier              smallint not null check (tier between 0 and 3),
  realization       numeric(4,3) not null check (realization > 0 and realization <= 1),
  can_buy_ten       boolean not null,
  policy_version    text not null default 'v1',
  computed_at       timestamptz not null default now(),
  primary key (market_hash_name, currency, policy_version)
);

-- ---------- resultados de scans ----------
create table if not exists public.scan_runs (
  id            uuid primary key default gen_random_uuid(),
  mode          public.scan_mode not null,
  rarity        smallint not null,
  currency      smallint not null,
  params        jsonb not null,
  params_hash   text not null,
  prices_asof   timestamptz,
  result_count  integer not null default 0,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null default now() + interval '10 minutes'
);
create index if not exists scan_runs_hash_idx on public.scan_runs (params_hash, created_at desc);
create index if not exists scan_runs_expires_idx on public.scan_runs (expires_at);

create table if not exists public.tradeup_results (
  run_id             uuid not null references public.scan_runs(id) on delete cascade,
  rank               integer not null,
  target_collection  text not null,
  filler_collection  text,
  grade              text not null,
  cost               numeric(12,2) not null,
  ev_net             numeric(12,2) not null,
  profit             numeric(12,2) not null,
  roi_pct            numeric(8,2) not null,
  win_chance_pct     numeric(5,2) not null,
  worst_net          numeric(12,2) not null,
  guaranteed_profit  numeric(12,2) not null,
  payload            jsonb not null,
  primary key (run_id, rank)
);
create index if not exists tradeup_results_grade_idx
  on public.tradeup_results (grade, win_chance_pct desc, profit desc);

-- ---------- infraestrutura do ingest ----------
create table if not exists public.http_cache (
  url            text primary key,
  etag           text,
  last_modified  text,
  status         smallint,
  body           jsonb,
  fetched_at     timestamptz not null default now(),
  expires_at     timestamptz not null
);

create table if not exists public.sweep_checkpoints (
  source       public.price_source not null,
  query_key    text not null,
  currency     smallint not null,
  next_start   integer not null default 0,
  total_count  integer,
  page_size    integer,
  updated_at   timestamptz not null default now(),
  primary key (source, query_key, currency)
);

-- ---------- RLS ----------
alter table public.collections        enable row level security;
alter table public.skins              enable row level security;
alter table public.market_items       enable row level security;
alter table public.price_quotes       enable row level security;
alter table public.price_latest       enable row level security;
alter table public.price_misses       enable row level security;
alter table public.item_liquidity     enable row level security;
alter table public.scan_runs          enable row level security;
alter table public.tradeup_results    enable row level security;
alter table public.http_cache         enable row level security;
alter table public.sweep_checkpoints  enable row level security;
alter table public.float_listings     enable row level security;

drop policy if exists "public read collections"     on public.collections;
drop policy if exists "public read skins"           on public.skins;
drop policy if exists "public read market_items"    on public.market_items;
drop policy if exists "public read price_latest"    on public.price_latest;
drop policy if exists "public read item_liquidity"  on public.item_liquidity;
drop policy if exists "public read scan_runs"       on public.scan_runs;
drop policy if exists "public read tradeup_results" on public.tradeup_results;

create policy "public read collections"     on public.collections     for select to anon, authenticated using (true);
create policy "public read skins"           on public.skins           for select to anon, authenticated using (true);
create policy "public read market_items"    on public.market_items    for select to anon, authenticated using (true);
create policy "public read price_latest"    on public.price_latest    for select to anon, authenticated using (true);
create policy "public read item_liquidity"  on public.item_liquidity  for select to anon, authenticated using (true);
create policy "public read scan_runs"       on public.scan_runs       for select to anon, authenticated using (expires_at > now() - interval '1 day');
create policy "public read tradeup_results" on public.tradeup_results for select to anon, authenticated using (true);
-- price_quotes, price_misses, http_cache, sweep_checkpoints: sem policy => apenas service_role

revoke insert, update, delete, truncate on all tables in schema public from anon, authenticated;
revoke all on public.price_quotes, public.price_misses, public.http_cache, public.sweep_checkpoints,
  public.float_listings from anon, authenticated;
grant select on public.price_best to anon, authenticated;

-- ---------- retenção (agendar via pg_cron se desejado) ----------
create or replace function public.purge_old_data() returns void
language sql security definer set search_path = public as $$
  delete from public.price_quotes where fetched_at < now() - interval '180 days';
  delete from public.scan_runs    where expires_at < now() - interval '7 days';
  delete from public.http_cache   where expires_at < now() - interval '30 days';
$$;
revoke all on function public.purge_old_data() from public, anon, authenticated;
-- select cron.schedule('purge-tradeup', '0 4 * * *', 'select public.purge_old_data()');
