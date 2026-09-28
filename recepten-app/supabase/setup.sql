-- Aanbiedingskeuken: tijdlijn met gedeelde recepten
-- Plak dit in Supabase → SQL Editor → New query → Run. Je kunt het veilig opnieuw draaien.

-- 1. Gedeelde recepten
create table if not exists public.recepten (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  auteur text not null check (char_length(auteur) between 1 and 40),
  titel text not null check (char_length(titel) between 2 and 80),
  tijd int check (tijd between 1 and 600),
  soort text check (soort in ('Vega', 'Vis', 'Kip', 'Vlees', 'Ei', 'Anders')),
  ingredienten text[] not null check (array_length(ingredienten, 1) between 1 and 40),
  bereiding text[] not null check (array_length(bereiding, 1) between 1 and 30),
  foto_pad text,
  created_at timestamptz not null default now()
);
create index if not exists recepten_created_at on public.recepten (created_at desc);
alter table public.recepten enable row level security;

drop policy if exists "iedereen leest recepten" on public.recepten;
create policy "iedereen leest recepten" on public.recepten for select using (true);
drop policy if exists "eigen recept plaatsen" on public.recepten;
create policy "eigen recept plaatsen" on public.recepten for insert to authenticated with check (auth.uid() = user_id);
drop policy if exists "eigen recept verwijderen" on public.recepten;
create policy "eigen recept verwijderen" on public.recepten for delete to authenticated using (auth.uid() = user_id);

-- 2. Hartjes
create table if not exists public.likes (
  recept_id uuid not null references public.recepten on delete cascade,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  created_at timestamptz not null default now(),
  primary key (recept_id, user_id)
);
alter table public.likes enable row level security;

drop policy if exists "iedereen leest likes" on public.likes;
create policy "iedereen leest likes" on public.likes for select using (true);
drop policy if exists "eigen like geven" on public.likes;
create policy "eigen like geven" on public.likes for insert to authenticated with check (auth.uid() = user_id);
drop policy if exists "eigen like weghalen" on public.likes;
create policy "eigen like weghalen" on public.likes for delete to authenticated using (auth.uid() = user_id);

-- 3. Foto's: openbare map 'recepten', iedereen uploadt alleen in zijn eigen map
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('recepten', 'recepten', true, 1048576, array['image/jpeg', 'image/webp'])
on conflict (id) do update set public = true, file_size_limit = 1048576, allowed_mime_types = array['image/jpeg', 'image/webp'];

drop policy if exists "eigen foto uploaden" on storage.objects;
create policy "eigen foto uploaden" on storage.objects for insert to authenticated
  with check (bucket_id = 'recepten' and (storage.foldername(name))[1] = auth.uid()::text);
drop policy if exists "eigen foto verwijderen" on storage.objects;
create policy "eigen foto verwijderen" on storage.objects for delete to authenticated
  using (bucket_id = 'recepten' and (storage.foldername(name))[1] = auth.uid()::text);
