-- Keep RLS helpers outside the PostgREST-exposed public schema.
create schema if not exists private;
revoke all on schema private from public, anon;
grant usage on schema private to authenticated;

alter function public.handle_new_auth_user() set schema private;
alter function public.current_app_role() set schema private;
alter function public.is_active_app_user() set schema private;
alter function public.has_app_role(text[]) set schema private;
alter function public.is_project_member(bigint) set schema private;
alter function public.can_read_project(bigint) set schema private;
alter function public.can_read_project_operations(bigint) set schema private;
alter function public.can_manage_project(bigint) set schema private;
alter function public.can_manage_project_cost(bigint) set schema private;
alter function public.can_manage_lead(bigint) set schema private;
alter function public.project_id_for_contract(bigint) set schema private;
alter function public.project_id_for_payment_node(bigint) set schema private;
alter function public.project_id_for_subcontract(bigint) set schema private;
alter function public.project_id_for_work_order(bigint) set schema private;

create or replace function private.has_app_role(allowed text[])
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$ select coalesce(private.current_app_role() = any(allowed), false) $$;

create or replace function private.can_read_project(target_project_id bigint)
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$
  select private.is_active_app_user() and (
    private.has_app_role(array['owner','finance'])
    or private.is_project_member(target_project_id)
    or exists(select 1 from public.projects where id = target_project_id and sales_owner_user_id = auth.uid())
  )
$$;

create or replace function private.can_read_project_operations(target_project_id bigint)
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$
  select private.is_active_app_user() and (
    private.has_app_role(array['owner','finance']) or private.is_project_member(target_project_id)
  )
$$;

create or replace function private.can_manage_project(target_project_id bigint)
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$
  select private.is_active_app_user() and (
    private.has_app_role(array['owner'])
    or (private.has_app_role(array['project_manager']) and private.is_project_member(target_project_id))
  )
$$;

create or replace function private.can_manage_project_cost(target_project_id bigint)
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$
  select private.is_active_app_user() and (
    private.has_app_role(array['owner','finance'])
    or (private.has_app_role(array['project_manager']) and private.is_project_member(target_project_id))
  )
$$;

create or replace function private.can_manage_lead(target_lead_id bigint)
returns boolean language sql stable security definer set search_path = public, private, pg_temp
as $$
  select private.is_active_app_user() and (
    private.has_app_role(array['owner'])
    or (private.has_app_role(array['business']) and exists(
      select 1 from public.leads where id = target_lead_id and owner_user_id = auth.uid()
    ))
  )
$$;

revoke execute on all functions in schema private from public, anon, authenticated;
grant execute on function private.current_app_role() to authenticated;
grant execute on function private.is_active_app_user() to authenticated;
grant execute on function private.has_app_role(text[]) to authenticated;
grant execute on function private.is_project_member(bigint) to authenticated;
grant execute on function private.can_read_project(bigint) to authenticated;
grant execute on function private.can_read_project_operations(bigint) to authenticated;
grant execute on function private.can_manage_project(bigint) to authenticated;
grant execute on function private.can_manage_project_cost(bigint) to authenticated;
grant execute on function private.can_manage_lead(bigint) to authenticated;
grant execute on function private.project_id_for_contract(bigint) to authenticated;
grant execute on function private.project_id_for_payment_node(bigint) to authenticated;
grant execute on function private.project_id_for_subcontract(bigint) to authenticated;
grant execute on function private.project_id_for_work_order(bigint) to authenticated;
