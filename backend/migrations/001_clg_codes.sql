-- Lista codici del brand + registro verifiche.
create table if not exists public.clg_codes (
  code text primary key check (code ~ '^\d{12}$'),
  status text not null default 'valid' check (status in ('valid','suspicious','revoked')),
  note text,
  created_at timestamptz not null default now()
);

create table if not exists public.clg_checks (
  id bigint generated always as identity primary key,
  code text not null,
  outcome text not null,
  context jsonb,
  checked_at timestamptz not null default now()
);
create index if not exists clg_checks_code_time on public.clg_checks (code, checked_at desc);

-- Nessun accesso diretto dal client: passa tutto dalla edge function.
alter table public.clg_codes enable row level security;
alter table public.clg_checks enable row level security;
