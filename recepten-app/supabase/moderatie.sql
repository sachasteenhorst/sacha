-- Aanbiedingskeuken: berichten op de tijdlijn melden
-- Plak dit in Supabase → SQL Editor → New query → Run (na setup.sql). Je kunt het veilig opnieuw draaien.
-- Een bericht dat door 3 verschillende mensen is gemeld, wordt automatisch verborgen voor iedereen behalve de maker.
-- Terugzetten of echt verwijderen doe je als beheerder in Table Editor → recepten (kolom 'verborgen').

alter table public.recepten add column if not exists verborgen boolean not null default false;

create table if not exists public.meldingen (
  recept_id uuid not null references public.recepten on delete cascade,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  reden text not null check (reden in ('ongepast', 'reclame', 'geen recept', 'anders')),
  created_at timestamptz not null default now(),
  primary key (recept_id, user_id)
);
alter table public.meldingen enable row level security;

drop policy if exists "eigen melding plaatsen" on public.meldingen;
create policy "eigen melding plaatsen" on public.meldingen for insert to authenticated with check (auth.uid() = user_id);
drop policy if exists "eigen meldingen zien" on public.meldingen;
create policy "eigen meldingen zien" on public.meldingen for select to authenticated using (auth.uid() = user_id);

-- Verborgen berichten ziet alleen de maker nog
drop policy if exists "iedereen leest recepten" on public.recepten;
create policy "iedereen leest recepten" on public.recepten for select using (not verborgen or auth.uid() = user_id);

-- Na 3 meldingen van verschillende mensen: verbergen
create or replace function public.na_melding() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from meldingen where recept_id = new.recept_id) >= 3 then
    update recepten set verborgen = true where id = new.recept_id;
  end if;
  return new;
end $$;
drop trigger if exists na_melding on public.meldingen;
create trigger na_melding after insert on public.meldingen for each row execute function public.na_melding();
