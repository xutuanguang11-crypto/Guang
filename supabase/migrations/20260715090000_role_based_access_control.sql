-- Four-role access model for 工装管家.
-- Existing text owner/manager columns remain for display and backwards compatibility.

create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  display_name text not null,
  active boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.user_roles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null check (role in ('owner', 'project_manager', 'finance', 'business')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.project_members (
  project_id bigint not null references public.projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (project_id, user_id)
);

alter table public.leads add column if not exists owner_user_id uuid references auth.users(id) on delete set null;
alter table public.projects add column if not exists sales_owner_user_id uuid references auth.users(id) on delete set null;
create index if not exists idx_leads_owner_user_id on public.leads(owner_user_id);
create index if not exists idx_projects_sales_owner_user_id on public.projects(sales_owner_user_id);
create index if not exists idx_project_members_user_id on public.project_members(user_id);

create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.profiles(user_id, email, display_name, active)
  values (
    new.id,
    coalesce(new.email, ''),
    coalesce(nullif(new.raw_user_meta_data ->> 'display_name', ''), split_part(coalesce(new.email, ''), '@', 1)),
    false
  )
  on conflict (user_id) do update
    set email = excluded.email, updated_at = now();
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_create_profile on auth.users;
create trigger on_auth_user_created_create_profile
  after insert or update of email on auth.users
  for each row execute function public.handle_new_auth_user();

insert into public.profiles(user_id, email, display_name, active)
select id, email, coalesce(nullif(raw_user_meta_data ->> 'display_name', ''), '团光'), true
from auth.users
where lower(email) = 'xutuanguang11@gmail.com'
on conflict (user_id) do update
set email = excluded.email, active = true, updated_at = now();

insert into public.user_roles(user_id, role)
select id, 'owner' from auth.users where lower(email) = 'xutuanguang11@gmail.com'
on conflict (user_id) do update set role = 'owner', updated_at = now();

update public.leads
set owner_user_id = (select id from auth.users where lower(email) = 'xutuanguang11@gmail.com' limit 1)
where owner_user_id is null;

update public.projects
set sales_owner_user_id = (select id from auth.users where lower(email) = 'xutuanguang11@gmail.com' limit 1)
where sales_owner_user_id is null;

create or replace function public.current_app_role()
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select ur.role
  from public.user_roles ur
  join public.profiles p on p.user_id = ur.user_id
  where ur.user_id = auth.uid() and p.active
$$;

create or replace function public.is_active_app_user()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists(select 1 from public.profiles where user_id = auth.uid() and active)
$$;

create or replace function public.has_app_role(allowed text[])
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select coalesce(public.current_app_role() = any(allowed), false)
$$;

create or replace function public.is_project_member(target_project_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists(
    select 1 from public.project_members
    where project_id = target_project_id and user_id = auth.uid()
  )
$$;

create or replace function public.can_read_project(target_project_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.is_active_app_user() and (
    public.has_app_role(array['owner','finance'])
    or public.is_project_member(target_project_id)
    or exists(
      select 1 from public.projects
      where id = target_project_id and sales_owner_user_id = auth.uid()
    )
  )
$$;

create or replace function public.can_read_project_operations(target_project_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.is_active_app_user() and (
    public.has_app_role(array['owner','finance'])
    or public.is_project_member(target_project_id)
  )
$$;

create or replace function public.can_manage_project(target_project_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.is_active_app_user() and (
    public.has_app_role(array['owner'])
    or (public.has_app_role(array['project_manager']) and public.is_project_member(target_project_id))
  )
$$;

create or replace function public.can_manage_project_cost(target_project_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.is_active_app_user() and (
    public.has_app_role(array['owner','finance'])
    or (public.has_app_role(array['project_manager']) and public.is_project_member(target_project_id))
  )
$$;

create or replace function public.can_manage_lead(target_lead_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.is_active_app_user() and (
    public.has_app_role(array['owner'])
    or (public.has_app_role(array['business']) and exists(
      select 1 from public.leads where id = target_lead_id and owner_user_id = auth.uid()
    ))
  )
$$;

create or replace function public.project_id_for_contract(target_contract_id bigint)
returns bigint language sql stable security definer set search_path = public, pg_temp
as $$ select project_id from public.contracts where id = target_contract_id $$;

create or replace function public.project_id_for_payment_node(target_node_id bigint)
returns bigint language sql stable security definer set search_path = public, pg_temp
as $$
  select c.project_id from public.payment_nodes n join public.contracts c on c.id = n.contract_id where n.id = target_node_id
$$;

create or replace function public.project_id_for_subcontract(target_subcontract_id bigint)
returns bigint language sql stable security definer set search_path = public, pg_temp
as $$ select project_id from public.subcontracts where id = target_subcontract_id $$;

create or replace function public.project_id_for_work_order(target_work_order_id bigint)
returns bigint language sql stable security definer set search_path = public, pg_temp
as $$ select project_id from public.work_orders where id = target_work_order_id $$;

do $$
declare p record;
begin
  for p in select schemaname, tablename, policyname from pg_policies where schemaname = 'public'
  loop
    execute format('drop policy if exists %I on %I.%I', p.policyname, p.schemaname, p.tablename);
  end loop;
end $$;

alter table public.profiles enable row level security;
alter table public.user_roles enable row level security;
alter table public.project_members enable row level security;

create policy profiles_read on public.profiles for select to authenticated
using (user_id = auth.uid() or public.has_app_role(array['owner']));
create policy profiles_owner_manage on public.profiles for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));
create policy roles_read on public.user_roles for select to authenticated
using (user_id = auth.uid() or public.has_app_role(array['owner']));
create policy roles_owner_manage on public.user_roles for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));
create policy project_members_read on public.project_members for select to authenticated
using (user_id = auth.uid() or public.has_app_role(array['owner']));
create policy project_members_owner_manage on public.project_members for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));

create policy projects_read on public.projects for select to authenticated using (public.can_read_project(id));
create policy projects_insert on public.projects for insert to authenticated with check (
  public.has_app_role(array['owner']) or (public.has_app_role(array['business']) and sales_owner_user_id = auth.uid())
);
create policy projects_update on public.projects for update to authenticated
using (public.can_manage_project(id)) with check (public.can_manage_project(id));
create policy projects_delete on public.projects for delete to authenticated using (public.has_app_role(array['owner']));

create policy leads_read on public.leads for select to authenticated
using (public.has_app_role(array['owner']) or (public.has_app_role(array['business']) and owner_user_id = auth.uid()));
create policy leads_insert on public.leads for insert to authenticated
with check (public.has_app_role(array['owner']) or (public.has_app_role(array['business']) and owner_user_id = auth.uid()));
create policy leads_update on public.leads for update to authenticated
using (public.can_manage_lead(id)) with check (public.can_manage_lead(id));
create policy leads_delete on public.leads for delete to authenticated using (public.can_manage_lead(id));
create policy lead_notes_read on public.lead_notes for select to authenticated using (public.can_manage_lead(lead_id));
create policy lead_notes_write on public.lead_notes for all to authenticated
using (public.can_manage_lead(lead_id)) with check (public.can_manage_lead(lead_id));

create policy milestones_read on public.milestones for select to authenticated using (public.can_read_project_operations(project_id));
create policy milestones_write on public.milestones for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy bom_items_read on public.bom_items for select to authenticated using (public.can_read_project_operations(project_id));
create policy bom_items_write on public.bom_items for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy bom_cost_items_read on public.bom_cost_items for select to authenticated using (public.can_read_project_operations(project_id));
create policy bom_cost_items_write on public.bom_cost_items for all to authenticated using (public.can_manage_project_cost(project_id)) with check (public.can_manage_project_cost(project_id));
create policy cost_invoices_read on public.cost_invoices for select to authenticated using (public.can_read_project_operations(project_id));
create policy cost_invoices_write on public.cost_invoices for all to authenticated using (public.can_manage_project_cost(project_id)) with check (public.can_manage_project_cost(project_id));
create policy equipment_costs_read on public.equipment_costs for select to authenticated using (public.can_read_project_operations(project_id));
create policy equipment_costs_write on public.equipment_costs for all to authenticated using (public.can_manage_project_cost(project_id)) with check (public.can_manage_project_cost(project_id));
create policy labor_reports_read on public.labor_reports for select to authenticated using (public.can_read_project_operations(project_id));
create policy labor_reports_write on public.labor_reports for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy project_alerts_read on public.project_alerts for select to authenticated using (public.can_read_project_operations(project_id));
create policy project_alerts_write on public.project_alerts for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy project_files_read on public.project_files for select to authenticated
using (public.has_app_role(array['owner']) or public.is_project_member(project_id));
create policy project_files_write on public.project_files for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy project_labor_rates_read on public.project_labor_rates for select to authenticated using (public.can_read_project_operations(project_id));
create policy project_labor_rates_write on public.project_labor_rates for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy work_orders_read on public.work_orders for select to authenticated
using (public.has_app_role(array['owner']) or public.is_project_member(project_id));
create policy work_orders_write on public.work_orders for all to authenticated using (public.can_manage_project(project_id)) with check (public.can_manage_project(project_id));
create policy work_order_photos_read on public.work_order_photos for select to authenticated
using (public.has_app_role(array['owner']) or public.is_project_member(public.project_id_for_work_order(work_order_id)));
create policy work_order_photos_write on public.work_order_photos for all to authenticated
using (public.can_manage_project(public.project_id_for_work_order(work_order_id)))
with check (public.can_manage_project(public.project_id_for_work_order(work_order_id)));

create policy contracts_read on public.contracts for select to authenticated
using (public.has_app_role(array['owner','finance']) or public.is_project_member(project_id));
create policy contracts_write on public.contracts for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));
create policy contract_changes_read on public.contract_changes for select to authenticated
using (public.has_app_role(array['owner','finance']) or public.is_project_member(public.project_id_for_contract(contract_id)));
create policy contract_changes_write on public.contract_changes for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));
create policy payment_nodes_read on public.payment_nodes for select to authenticated
using (public.has_app_role(array['owner','finance']) or public.is_project_member(public.project_id_for_contract(contract_id)));
create policy payment_nodes_write on public.payment_nodes for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));
create policy receipts_read on public.receipts for select to authenticated
using (public.has_app_role(array['owner','finance']) or public.is_project_member(public.project_id_for_payment_node(payment_node_id)));
create policy receipts_write on public.receipts for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));
create policy subcontracts_read on public.subcontracts for select to authenticated using (public.can_read_project_operations(project_id));
create policy subcontracts_write on public.subcontracts for all to authenticated using (public.can_manage_project_cost(project_id)) with check (public.can_manage_project_cost(project_id));
create policy sub_payment_nodes_read on public.sub_payment_nodes for select to authenticated
using (public.can_read_project_operations(public.project_id_for_subcontract(subcontract_id)));
create policy sub_payment_nodes_write on public.sub_payment_nodes for all to authenticated
using (public.can_manage_project_cost(public.project_id_for_subcontract(subcontract_id)))
with check (public.can_manage_project_cost(public.project_id_for_subcontract(subcontract_id)));
create policy invoice_ledger_finance on public.invoice_ledger for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));
create policy expense_payments_finance on public.expense_payments for all to authenticated
using (public.has_app_role(array['owner','finance'])) with check (public.has_app_role(array['owner','finance']));

create policy material_catalog_read on public.material_catalog for select to authenticated
using (public.has_app_role(array['owner','project_manager','finance']));
create policy material_catalog_owner_write on public.material_catalog for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));
create policy labor_catalog_read on public.labor_rate_catalog for select to authenticated
using (public.has_app_role(array['owner','project_manager','finance']));
create policy labor_catalog_owner_write on public.labor_rate_catalog for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));
create policy app_settings_owner on public.app_settings for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));
create policy activity_logs_owner on public.activity_logs for all to authenticated
using (public.has_app_role(array['owner'])) with check (public.has_app_role(array['owner']));

revoke all privileges on all tables in schema public from anon, authenticated;
revoke all privileges on all sequences in schema public from anon, authenticated;
grant select, insert, update, delete on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;
revoke execute on function public.handle_new_auth_user() from public, anon, authenticated;
revoke execute on function public.current_app_role() from public, anon, authenticated;
revoke execute on function public.is_active_app_user() from public, anon, authenticated;
revoke execute on function public.has_app_role(text[]) from public, anon, authenticated;
revoke execute on function public.is_project_member(bigint) from public, anon, authenticated;
revoke execute on function public.can_read_project(bigint) from public, anon, authenticated;
revoke execute on function public.can_read_project_operations(bigint) from public, anon, authenticated;
revoke execute on function public.can_manage_project(bigint) from public, anon, authenticated;
revoke execute on function public.can_manage_project_cost(bigint) from public, anon, authenticated;
revoke execute on function public.can_manage_lead(bigint) from public, anon, authenticated;
revoke execute on function public.project_id_for_contract(bigint) from public, anon, authenticated;
revoke execute on function public.project_id_for_payment_node(bigint) from public, anon, authenticated;
revoke execute on function public.project_id_for_subcontract(bigint) from public, anon, authenticated;
revoke execute on function public.project_id_for_work_order(bigint) from public, anon, authenticated;
grant execute on function public.current_app_role() to authenticated;
grant execute on function public.is_active_app_user() to authenticated;
grant execute on function public.has_app_role(text[]) to authenticated;
grant execute on function public.is_project_member(bigint) to authenticated;
grant execute on function public.can_read_project(bigint) to authenticated;
grant execute on function public.can_read_project_operations(bigint) to authenticated;
grant execute on function public.can_manage_project(bigint) to authenticated;
grant execute on function public.can_manage_project_cost(bigint) to authenticated;
grant execute on function public.can_manage_lead(bigint) to authenticated;
grant execute on function public.project_id_for_contract(bigint) to authenticated;
grant execute on function public.project_id_for_payment_node(bigint) to authenticated;
grant execute on function public.project_id_for_subcontract(bigint) to authenticated;
grant execute on function public.project_id_for_work_order(bigint) to authenticated;
alter default privileges in schema public revoke all on tables from anon;
alter default privileges in schema public revoke all on sequences from anon;
