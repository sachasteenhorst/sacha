-- Aanbiedingskeuken: gedeelde boodschappenlijst voor het hele gezin
-- Plak dit in Supabase → SQL Editor → New query → Run. Je kunt het veilig opnieuw draaien.
-- Iedereen die de code van een lijst heeft, kan meedoen; zonder code zie je niets.

-- 1. Lijsten, leden en producten
create table if not exists public.lijsten (
  id uuid primary key default gen_random_uuid(),
  code text not null unique check (code ~ '^[A-HJ-NP-Z2-9]{8}$'),
  eigenaar uuid not null default auth.uid() references auth.users on delete cascade,
  created_at timestamptz not null default now()
);
create table if not exists public.lijst_leden (
  lijst_id uuid not null references public.lijsten on delete cascade,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  created_at timestamptz not null default now(),
  primary key (lijst_id, user_id)
);
create table if not exists public.lijst_items (
  lijst_id uuid not null references public.lijsten on delete cascade,
  id text not null check (char_length(id) between 1 and 40),
  data jsonb not null check (pg_column_size(data) < 4000),
  updated_at timestamptz not null default now(),
  primary key (lijst_id, id)
);
create index if not exists lijst_items_lijst on public.lijst_items (lijst_id);

-- 2. Hulpfunctie: is de ingelogde gebruiker lid van deze lijst?
create or replace function public.is_lid(l uuid) returns boolean
  language sql stable security definer set search_path = public
  as $$ select exists (select 1 from lijst_leden where lijst_id = l and user_id = auth.uid()) $$;

-- 3. Regels: alleen leden zien en veranderen een lijst
alter table public.lijsten enable row level security;
alter table public.lijst_leden enable row level security;
alter table public.lijst_items enable row level security;

drop policy if exists "leden zien lijst" on public.lijsten;
create policy "leden zien lijst" on public.lijsten for select to authenticated using (public.is_lid(id));
drop policy if exists "eigenaar verwijdert lijst" on public.lijsten;
create policy "eigenaar verwijdert lijst" on public.lijsten for delete to authenticated using (eigenaar = auth.uid());

drop policy if exists "leden zien leden" on public.lijst_leden;
create policy "leden zien leden" on public.lijst_leden for select to authenticated using (public.is_lid(lijst_id));
drop policy if exists "zelf uit lijst stappen" on public.lijst_leden;
create policy "zelf uit lijst stappen" on public.lijst_leden for delete to authenticated using (user_id = auth.uid());

drop policy if exists "leden lezen producten" on public.lijst_items;
create policy "leden lezen producten" on public.lijst_items for select to authenticated using (public.is_lid(lijst_id));
drop policy if exists "leden voegen producten toe" on public.lijst_items;
create policy "leden voegen producten toe" on public.lijst_items for insert to authenticated with check (public.is_lid(lijst_id));
drop policy if exists "leden wijzigen producten" on public.lijst_items;
create policy "leden wijzigen producten" on public.lijst_items for update to authenticated using (public.is_lid(lijst_id)) with check (public.is_lid(lijst_id));
drop policy if exists "leden verwijderen producten" on public.lijst_items;
create policy "leden verwijderen producten" on public.lijst_items for delete to authenticated using (public.is_lid(lijst_id));

-- 4. Een lijst maken en meedoen met een code (maken en lid worden gebeurt in één keer)
create or replace function public.lijst_maken() returns table (lijst_id uuid, lijst_code text)
  language plpgsql security definer set search_path = public as $$
declare c text; l uuid; tekens text := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
begin
  if auth.uid() is null then raise exception 'niet ingelogd'; end if;
  loop
    c := (select string_agg(substr(tekens, 1 + floor(random() * 32)::int, 1), '') from generate_series(1, 8));
    exit when not exists (select 1 from lijsten where code = c);
  end loop;
  insert into lijsten (code, eigenaar) values (c, auth.uid()) returning id into l;
  insert into lijst_leden (lijst_id, user_id) values (l, auth.uid());
  return query select l, c;
end $$;

create or replace function public.lijst_deelnemen(p_code text) returns uuid
  language plpgsql security definer set search_path = public as $$
declare l uuid;
begin
  if auth.uid() is null then raise exception 'niet ingelogd'; end if;
  select id into l from lijsten where code = upper(regexp_replace(p_code, '[^A-Za-z0-9]', '', 'g'));
  if l is null then raise exception 'onbekende code'; end if;
  insert into lijst_leden (lijst_id, user_id) values (l, auth.uid()) on conflict do nothing;
  return l;
end $$;

revoke all on function public.lijst_maken() from public, anon;
revoke all on function public.lijst_deelnemen(text) from public, anon;
grant execute on function public.lijst_maken() to authenticated;
grant execute on function public.lijst_deelnemen(text) to authenticated;

-- 5. Live bijwerken: wijzigingen direct doorsturen naar de telefoons van de andere leden
do $$ begin
  alter publication supabase_realtime add table public.lijst_items;
exception when duplicate_object then null; end $$;
