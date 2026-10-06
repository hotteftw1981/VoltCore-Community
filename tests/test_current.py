#!/usr/bin/env python3
"""Current release QA: V0.9.7.74 restart-safe post-session occupancy."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os, re, sqlite3, subprocess, sys, tempfile, types, asyncio, importlib.util

root=Path(__file__).resolve().parents[1]
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='ocpp-v09755-')
os.environ['WEB_PORT']='18054'
os.environ['OCPP_PORT']='19054'
os.environ.pop('ENABLE_API_DOCS',None)
sys.path.insert(0,str(root))

stub=types.ModuleType('app.ocpp_server')
class DummyServer:
    def close(self): pass
    async def wait_closed(self): pass
async def serve_ocpp(port=9000): return DummyServer()
async def remote_command(cp_id,command,*a,**k):
    if command=='trigger_message' and str(k.get('requested_message') or '') not in {'BootNotification','DiagnosticsStatusNotification','FirmwareStatusNotification','Heartbeat','MeterValues','StatusNotification'}:
        raise ValueError('Nicht unterstützter TriggerMessage-Typ')
    return {'status':'Accepted','accepted':True,'elapsed_ms':12}
async def probe_capabilities(*a,**k): return {'status':'ok','profiles':[],'capabilities':{},'configuration':{},'unknown_keys':[]}
async def read_configuration(cp_id,keys=None):
    keys=keys or ['HeartbeatInterval']
    return {'configuration':{str(k):{'value':'60','readonly':False} for k in keys},'unknown_keys':[],'elapsed_ms':7}
def is_connected(cp_id): return False
def register_smart_charging_trigger(callback): return None
async def sync_local_list(*a,**k): return {'ok':True,'status':'Synchronisiert'}
async def sync_pending_local_lists(*a,**k): return []
async def verify_offline_authorization(*a,**k): return {'ok':True,'status':'Aktiv','detail':'QA'}
def connection_snapshot(): return {'connections':[],'diagnostics':{'healthy':0,'warning':0,'critical':0}}
for n in ['serve_ocpp','remote_command','probe_capabilities','read_configuration','is_connected','register_smart_charging_trigger','sync_local_list','sync_pending_local_lists','verify_offline_authorization','connection_snapshot']:
    setattr(stub,n,locals()[n])
sys.modules['app.ocpp_server']=stub

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from py_vapid import Vapid
from app import db, main, mailer, backup, totp, security, updates, ocpp_diagnostics, web_push

assert main.APP_VERSION=='0.9.7.74'
assert main.app.title=='VoltCore'
assert updates.REPOSITORY=='hotteftw1981/VoltCore'
assert updates.LEGACY_REPOSITORY=='hotteftw1981/drk-ocpp-backend'
assert main.PBKDF2_ITERATIONS>=600000
assert not main.ENABLE_API_DOCS
assert security.normalize_charge_point_id('CP-01')=='CP-01'
assert security.normalize_charge_point_id('../CP') is None
assert security.normalize_charge_point_id('A'*129) is None

# V0.9.7.51: fresh installations use neutral VoltCore defaults.
db.init_db()
push_cfg=web_push.ensure_vapid_keys()
assert push_cfg['public_key'] and '=' not in push_cfg['public_key']
assert web_push.public_config()['enabled']
Vapid.from_string(db.get_setting('web_push_vapid_private_key'))
assert web_push.validate_endpoint('https://push.example.com/device')=='https://push.example.com/device'
for unsafe_endpoint in ['http://push.example.com/device','https://127.0.0.1/push','https://localhost/push','https://10.0.0.8/push']:
    try:
        web_push.validate_endpoint(unsafe_endpoint)
        raise AssertionError('unsafe push endpoint accepted: '+unsafe_endpoint)
    except ValueError:
        pass
fresh_brand=db.branding_settings()
assert fresh_brand['product_name']=='VoltCore'
assert fresh_brand['display_name']=='VoltCore'
assert fresh_brand['organization_name']=='Ihre Organisation'
assert fresh_brand['product_subtitle']=='OCPP Charging Management'
assert fresh_brand['voucher_prefix']=='VOLT'
assert fresh_brand['primary_color']=='#2563eb'
assert db.get_setting('branding_identity_v51_migrated')=='1'

# Upgrades from <=V0.9.7.50 keep the prior DRK appearance without overwriting custom values.
legacy_conn=sqlite3.connect(':memory:')
legacy_conn.row_factory=sqlite3.Row
legacy_conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)')
legacy_conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('smart_charging_enabled','0','qa')")
legacy_conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('branding_display_name','Eigene Ladezentrale','qa')")
legacy_conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('branding_product_name','DRK OCPP Backend','qa')")
assert db._migrate_branding_identity_v51_conn(legacy_conn,3)
legacy_brand={row['key']:row['value'] for row in legacy_conn.execute("SELECT key,value FROM app_settings WHERE key LIKE 'branding_%'")}
assert legacy_brand['branding_product_name']=='VoltCore'
assert legacy_brand['branding_organization_name']=='DRK Ortsverein Schwelm e. V.'
assert legacy_brand['branding_display_name']=='Eigene Ladezentrale'
assert legacy_brand['branding_product_subtitle']=='OCPP Backend'
assert legacy_brand['branding_voucher_prefix']=='DRK'
assert legacy_brand['branding_primary_color']=='#e30613'
assert legacy_brand['branding_identity_v51_migrated']=='1'
assert not db._migrate_branding_identity_v51_conn(legacy_conn,3)
legacy_conn.close()

custom_product_conn=sqlite3.connect(':memory:')
custom_product_conn.row_factory=sqlite3.Row
custom_product_conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)')
custom_product_conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('branding_product_name','Eigenes CSMS','qa')")
assert db._migrate_branding_identity_v51_conn(custom_product_conn,1)
assert custom_product_conn.execute("SELECT value FROM app_settings WHERE key='branding_product_name'").fetchone()[0]=='Eigenes CSMS'
custom_product_conn.close()

fresh_conn=sqlite3.connect(':memory:')
fresh_conn.row_factory=sqlite3.Row
fresh_conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)')
assert not db._migrate_branding_identity_v51_conn(fresh_conn,0)
assert fresh_conn.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'branding_%' AND key<>'branding_identity_v51_migrated'").fetchone()[0]==0
fresh_conn.close()

mailer.ensure_defaults()
backup.ensure_defaults()
updates.ensure_defaults()
db.seed_default_achievements()

release_builder=(root/'scripts/build_release.py').read_text(encoding='utf-8')
assert 'VoltCore_V' in release_builder and 'DRK_OCPP_Backend_V' not in release_builder
container_workflow_src=(root/'.github/workflows/container.yml').read_text(encoding='utf-8')
assert 'Publishing VoltCore' in container_workflow_src and 'org.opencontainers.image.title=VoltCore' in container_workflow_src

# Charge-point permissions: one user may be allowed at selected stations only.
db.upsert_charge_point('QA-ACCESS-A',vendor='QA',model='Access A',status='Available',onboarded=1,source_type='ocpp')
db.upsert_charge_point('QA-ACCESS-B',vendor='QA',model='Access B',status='Available',onboarded=1,source_type='ocpp')
access_user=db.create_user('Charge Access QA',role='Fahrer',department='Test',charge_access_mode='selected',allowed_charge_point_ids=['QA-ACCESS-A'])
db.create_rfid_card('QA-ACCESS-TAG',label='Access QA',user_id=access_user,status='Aktiv')
access_cfg=db.user_charge_access(access_user)
assert access_cfg=={'mode':'selected','charge_point_ids':['QA-ACCESS-A']}
assert db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-A')['accepted']
denied=db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-B')
assert not denied['accepted'] and denied['ocpp_status']=='Blocked' and 'QA-ACCESS-B' in denied['reason']
assert any(x['idTag']=='QA-ACCESS-TAG' for x in db.rfid_local_list_full('QA-ACCESS-A'))
assert all(x['idTag']!='QA-ACCESS-TAG' for x in db.rfid_local_list_full('QA-ACCESS-B'))
old_access_version=db.rfid_local_list_version()
assert db.update_user(access_user,charge_access_mode='selected',allowed_charge_point_ids=['QA-ACCESS-B'])
new_access_version=db.rfid_local_list_version()
assert new_access_version>old_access_version
assert not db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-A')['accepted']
assert db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-B')['accepted']
diff_a=next(x for x in db.rfid_local_list_changes_for_version(new_access_version,'QA-ACCESS-A') if x['idTag']=='QA-ACCESS-TAG')
diff_b=next(x for x in db.rfid_local_list_changes_for_version(new_access_version,'QA-ACCESS-B') if x['idTag']=='QA-ACCESS-TAG')
assert 'idTagInfo' not in diff_a and diff_b['idTagInfo']['status']=='Accepted'
assert db.update_user(access_user,charge_access_mode='selected',allowed_charge_point_ids=[])
assert not db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-A')['accepted']
assert not db.authorization_decision('QA-ACCESS-TAG','QA-ACCESS-B')['accepted']
all_access_user=db.create_user('Charge Access All QA',role='Fahrer',department='Test')
db.create_rfid_card('QA-ACCESS-ALL',label='Access All QA',user_id=all_access_user,status='Aktiv')
assert db.authorization_decision('QA-ACCESS-ALL','QA-ACCESS-A')['accepted']
assert db.authorization_decision('QA-ACCESS-ALL','QA-ACCESS-B')['accepted']
for cp_id in ('QA-ACCESS-A','QA-ACCESS-B'):
    ok,reason=db.delete_ocpp_device(cp_id)
    assert ok and reason=='deleted'
ocpp_server_src=(root/'app/ocpp_server.py').read_text(encoding='utf-8')
assert ocpp_server_src.count('authorization_decision(id_tag,self.id)')>=2
assert 'authorization_decision(id_tag,cp_id)' in ocpp_server_src
assert 'rfid_local_list_full(cp_id)' in ocpp_server_src and 'rfid_local_list_changes_for_version(backend_version,cp_id)' in ocpp_server_src
assert 'sync_local_list(cp_id,force_full=True,reason="Ladepunkt verbunden · Vollabgleich")' in ocpp_server_src
db_src=(root/'app/db.py').read_text(encoding='utf-8')
ladecloud_src=(root/'app/ladecloud_import.py').read_text(encoding='utf-8')
vehicles_tpl=(root/'app/templates/vehicles.html').read_text(encoding='utf-8')
assert 'formatEnergyKwh(sr.analytics.month_energy_kwh||0,1,3)' in vehicles_tpl
assert 'cost_allowance_reset_v09762' in db_src
assert 'offline_auth_status' in db_src and 'fleet_integration_sessions' in db_src and '"description"' in db_src
assert 'post_session_occupied_started_at' in db_src and 'finalize_post_session_occupancy' in db_src and 'PROMPT_UNPLUG_SECONDS' in db_src
assert '"period_budget":period_budget' in db_src and 'period_ctx.get("selected_key")!="all"' in db_src
assert 'chargeable_energy=max(0.0,energy-base_remaining-float(bonus or 0))' in db_src
assert '"cost_cents":0' in ladecloud_src and 'cost=0' in db_src
assert '*0.30' not in vehicles_tpl
ocpp_server_src=(root/'app/ocpp_server.py').read_text(encoding='utf-8')
public_portal_tpl=(root/'app/templates/public_budgets.html').read_text(encoding='utf-8')
portal_reset_tpl=(root/'app/templates/portal_pin_reset.html').read_text(encoding='utf-8')
compose_src=(root/'docker-compose.portainer.yml').read_text(encoding='utf-8')
main_src=(root/'app/main.py').read_text(encoding='utf-8')
assert 'GetConfiguration' in ocpp_server_src and 'set_local_list_offline_auth_state' in ocpp_server_src
assert 'async def verify_offline_authorization' in ocpp_server_src
assert 'PostSessionOccupancyStarted' in ocpp_server_src and 'PostSessionOccupancyEnded' in ocpp_server_src and 'PostSessionOccupancyRecovered' in ocpp_server_src
assert 'learned_state.get("supported")==1' in ocpp_server_src
assert 'portal-period-toolbar' in public_portal_tpl and 'portal-ranking-explainer' in public_portal_tpl
assert 'portal-current-budget' in public_portal_tpl and 'portal-period-budget-card' in public_portal_tpl
assert 'portalPeriodSubmit' in public_portal_tpl and 'voltcore-portal-period-scroll' in public_portal_tpl
assert 'p.period_budget' in public_portal_tpl
assert 'Nachbelegung' in public_portal_tpl and 'Ø Abstecken' in public_portal_tpl and 'gemessener Connector-Freigabe' in public_portal_tpl
assert '← Zurück zur Anmeldung' in public_portal_tpl and 'portal-reset-back' in portal_reset_tpl
assert 'OCPP_TLS_CERTFILE' in compose_src and 'OCPP_TLS_KEYFILE' in compose_src and 'FLEET_INTEGRATION_TOKEN' in compose_src
assert '/api/integrations/fleet/v1/health' in main_src and '/api/integrations/fleet/v1/sessions' in main_src
assert '/api/rfid/local-list/offline-auth' in main_src and 'verify_offline_authorization' in main_src
assert '_ocpp_transport_status' in main_src and '_fleet_integration_status' in main_src
users_tpl=(root/'app/templates/users.html').read_text(encoding='utf-8')
assert 'formatEnergyKwh(p.energy_kwh||0,1,3)' in users_tpl
assert 'post_session_occupied_seconds' in users_tpl and 'Ø Abstecken' in users_tpl and 'Nachbelegung' in users_tpl
assert 'RFID Backend' in users_tpl and 'Säule IST' in users_tpl and 'Säule SOLL' in users_tpl and 'Vollabgleich' in users_tpl and 'syncLocalList(null,true)' in users_tpl
assert '*0.30' not in users_tpl and 't.cost_cents' in users_tpl
assert 'Offline-Autorisierung aktiv' in users_tpl
assert 'Offline-Modus prüfen' in users_tpl and 'offlineAuthState' in users_tpl and 'local-offline-check' in users_tpl
assert 'charge-access-option' in users_tpl and 'charge-access-check' in users_tpl and 'Klicke die Ladepunkte an' in users_tpl
assert 'user-profile-overlay-card' in users_tpl and 'localListDiagnosis' in users_tpl and 'LocalList nicht bestätigt' in users_tpl
assert "actionLabel=!x.online?'Wartet auf Verbindung'" in users_tpl and "probeState?'Erneut prüfen'" in users_tpl and "retryState?'Erneut aktualisieren'" in users_tpl and 'Offline-Autorisierung noch nicht geprüft' in users_tpl
style_src=(root/'app/static/style.css').read_text(encoding='utf-8')
for token in ['.charge-point-access-grid','.charge-access-option.selected','.user-profile-overlay-card','.local-list-diagnostic.unsupported']:
    assert token in style_src
assert 'GetLocalListVersion=-1 · OCPP 1.6: Säule meldet Local Authorization List ausdrücklich als nicht unterstützt' in ocpp_server_src
assert 'local_list_send_was_accepted' in ocpp_server_src and 'not accepted_before and not force_full' in ocpp_server_src and 'bereits gelernte SendLocalList-Unterstützung vorhanden; automatischer Vollabgleich wird fortgesetzt' in ocpp_server_src and 'manueller Admin-Test erzwingt einmalig' in ocpp_server_src and 'station_entry_count=len(entries)' in ocpp_server_src
assert 'payload.get("force_full",True)' in main_src and 'item["authorized_count"]=len(db.rfid_local_list_full(item.get("id")))' in main_src and 'backend_authorized_count' in main_src and 'station_entry_count' in main_src
assert '@app.delete("/api/rfid/requests/{request_id}")' in main_src
assert '@app.delete("/api/notifications/{notification_id}")' in main_src and '@app.delete("/api/notifications/read-all")' in main_src
assert 'data-request-delete' in users_tpl and 'Kartenstatus, Sperrstatus und Kartenhistorie bleiben unverändert' in users_tpl
base_tpl=(root/'app/templates/base.html').read_text(encoding='utf-8')
assert 'notificationClearRead' in base_tpl and 'data-notification-dismiss' in base_tpl and 'Gelesene leeren' in base_tpl

# Data maintenance: billing groups can be edited/deactivated without deleting historical configuration.
billing_user=db.create_user('Billing Group QA',role='Fahrer',department='Test')
billing_group=db.create_billing_group('QA Gruppe')
db.assign_user_billing_group(billing_user,billing_group)
assert db.update_billing_group(billing_group,'QA Gruppe Neu',None,False)
assert next(g for g in db.list_billing_groups() if int(g['id'])==billing_group)['active']==0
assert int(billing_group) not in {int(g['id']) for g in db.billing_groups_for_user(billing_user) if int(g.get('active') or 0)==1}
assert db.update_billing_group(billing_group,'QA Gruppe Neu',None,True)

# Data maintenance: unused vouchers are editable/deletable; redeemed vouchers are immutable and only deactivatable.
voucher_unused=db.create_bonus_voucher('QA-UNUSED-001',5,None,30,1,'unused',True)
assert db.update_bonus_voucher(voucher_unused,'QA-UNUSED-002',7,None,45,2,'edited',True)
voucher_row=next(v for v in db.list_bonus_vouchers() if int(v['id'])==voucher_unused)
assert voucher_row['code']=='QA-UNUSED-002' and float(voucher_row['amount_kwh'])==7
assert db.delete_bonus_voucher(voucher_unused)
voucher_user=db.create_user('Voucher QA',role='Fahrer',department='Test')
voucher_used=db.create_bonus_voucher('QA-USED-001',3,None,30,1,'used',True)
db.redeem_bonus_voucher(voucher_user,'QA-USED-001')
for protected_action in (
    lambda: db.update_bonus_voucher(voucher_used,'QA-USED-002',4,None,30,1,'changed',True),
    lambda: db.delete_bonus_voucher(voucher_used),
):
    try:
        protected_action()
        raise AssertionError('redeemed voucher mutation unexpectedly allowed')
    except ValueError:
        pass
assert db.set_bonus_voucher_active(voucher_used,False)

# V0.9.7.49: a managed offline OCPP device can be removed while transaction history survives.
db.upsert_charge_point('QA-OCPP-REMOVE',vendor='QA',model='Simulator',status='Available',onboarded=1,source_type='ocpp')
remove_tx=db.start_transaction('QA-OCPP-REMOVE',id_tag='QA-REMOVE',connector_id=1,meter_start_kwh=10.0)
db.stop_transaction(remove_tx,energy_kwh=4.2,meter_stop_kwh=14.2,status='Completed')
ok,reason=db.delete_ocpp_device('QA-OCPP-REMOVE')
assert ok and reason=='deleted' and db.get_charge_point('QA-OCPP-REMOVE') is None
preserved_tx=db.get_transaction(remove_tx)
assert preserved_tx and preserved_tx['charge_point_id']=='QA-OCPP-REMOVE' and abs(float(preserved_tx['energy_kwh'] or 0)-4.2)<0.001

# V0.9.7.49 gamification: rich editable catalogue, XP/levels, tiers, secrets and dynamic rankings.
achievement_catalog=db.list_achievements()
assert len(achievement_catalog)>=45
assert any(a.get('category')=='Energie' and a.get('tier_name')=='Bronze' for a in achievement_catalog)
assert any(bool(a.get('system_secret')) and a.get('rarity')=='secret' for a in achievement_catalog)
gamification_user_id=db.create_user('Gamification QA',role='Fahrer',department='Test')
profile=db.user_gamification_profile(gamification_user_id)
assert profile['level']==1 and profile['xp']==0 and profile['title']=='Stecker-Neuling'
xp_progress_qa=db._gamification_level_data(3519)
assert xp_progress_qa['level']==8 and xp_progress_qa['next_level_total_xp']==4500 and xp_progress_qa['xp_to_next']==981
assert abs(float(xp_progress_qa['progress_pct'])-1.9)<0.01 and abs(float(xp_progress_qa['total_progress_pct'])-78.2)<0.01
manual_badge=next(a for a in achievement_catalog if a.get('seed_key')=='manual-community-hero')
db.award_achievement(gamification_user_id,int(manual_badge['id']),'manual')
profile=db.user_gamification_profile(gamification_user_id)
assert profile['xp']>=400 and profile['achievement_count']>=1
secret_badge=next(a for a in achievement_catalog if a.get('seed_key')=='secret-42')
assert int(secret_badge['id']) not in {int(a['id']) for a in db.achievements_for_user(gamification_user_id)}
db.award_achievement(gamification_user_id,int(secret_badge['id']),'manual')
secret_visible=db.achievements_for_user(gamification_user_id)
assert int(secret_badge['id']) in {int(a['id']) for a in secret_visible}
custom_id=db.create_achievement('QA XP Badge','Test','🧪','sessions_total',9999,False,False,True,'QA','epic',321,'qa-tier','Platin',4)
custom=db.get_achievement(custom_id)
assert custom['category']=='QA' and custom['rarity']=='epic' and custom['xp']==321 and custom['tier_name']=='Platin'
assert db.update_achievement(custom_id,xp=654,rarity='legendary')
custom=db.get_achievement(custom_id)
assert custom['xp']==654 and custom['rarity']=='legendary'

# Dynamic leaderboard catalogue is driven by safe achievement metrics.
metric_catalog=db.leaderboard_metric_catalog()
metric_keys={x['key'] for x in metric_catalog}
assert {'energy_kwh','sessions','xp','achievement_count','early_sessions','evening_sessions','night_sessions','weekend_sessions','week_streak','prompt_unplug_sessions'} <= metric_keys
assert 'exact_42_sessions' not in metric_keys and 'midnight_sessions' not in metric_keys
early_seed=next(a for a in db.list_achievements() if a.get('seed_key')=='early-5')
assert bool(early_seed.get('leaderboard_enabled'))
assert db.update_achievement(int(secret_badge['id']),leaderboard_enabled=True)
assert not bool(db.get_achievement(int(secret_badge['id']))['leaderboard_enabled'])
overview=db.gamification_overview()
mine=next(x for x in overview if x['user_id']==gamification_user_id)
assert mine['xp']>=820 and mine['level']>=3 and mine['achievement_count']>=2 and mine['xp_to_next']>=0
xp_board=db.general_leaderboard('all','xp')
assert xp_board['metric']=='xp' and xp_board['unit']=='XP'
assert next(x for x in xp_board['leaderboard'] if x['user_id']==gamification_user_id)['value']>=820
early_board=db.general_leaderboard('all','early_sessions')
assert early_board['metric_label']=='Frühlader / Early Birds' and early_board['unit']=='Sessions'

# Personal charging-credit portal exposes all safe permanent rankings but keeps other names private.
ranking_rival_id=db.create_user('Ranking Rival QA',role='Fahrer',department='Test')
db.award_achievement(ranking_rival_id,int(manual_badge['id']),'manual')
db.set_setting('portal_leaderboard_show_names','0')
portal_ranking=db.portal_general_leaderboards(gamification_user_id,period='all',metric='xp')
assert portal_ranking['board']['metric']=='xp' and portal_ranking['selected_period']=='all'
assert {'xp','achievement_count','early_sessions'} <= {m['key'] for m in portal_ranking['metrics']}
own=next(x for x in portal_ranking['board']['leaderboard'] if x['user_id']==gamification_user_id)
other=next(x for x in portal_ranking['board']['leaderboard'] if x['user_id']==ranking_rival_id)
assert own['name']=='Gamification QA' and own['is_me']
assert other['name']=='********' and not other['is_me']
db.upsert_charge_point('QA-PROFILE-CP',vendor='QA',model='Profile Test',status='Available',onboarded=1,source_type='ocpp')
db.create_rfid_card('QA-PROFILE-RFID',label='QA Profile',user_id=gamification_user_id)
profile_tx=db.start_transaction('QA-PROFILE-CP',id_tag='QA-PROFILE-RFID',connector_id=1,meter_start_kwh=100.0)
db.update_transaction_from_meter(profile_tx,meter_kwh=112.5,power_kw=11.2)
profile_end=datetime.now(timezone.utc)
db.set_status_notification('QA-PROFILE-CP',1,'Finishing',error_code='NoError')
db.stop_transaction(profile_tx,energy_kwh=12.5,meter_stop_kwh=112.5,status='Completed',ended_at=profile_end.isoformat())
# Post-session occupancy is persisted from the original session end. Re-running
# the recovery path (as after a backend restart) must not reset the clock.
occ_live=db.transaction_post_session_occupancy(profile_tx,now=profile_end+timedelta(minutes=3))
assert occ_live['tracked'] and occ_live['active'] and 179<=occ_live['seconds']<=181
recovered=db.mark_post_session_occupied('QA-PROFILE-CP',1,observed_at=(profile_end+timedelta(minutes=4)).isoformat())
assert recovered and not recovered['new'] and recovered['started_at']==profile_end.isoformat()
finished=db.finalize_post_session_occupancy('QA-PROFILE-CP',1,observed_at=(profile_end+timedelta(minutes=5)).isoformat())
assert finished and 299<=finished['seconds']<=301
db.set_status_notification('QA-PROFILE-CP',1,'Available',error_code='NoError')
occ_done=db.transaction_post_session_occupancy(profile_tx,now=profile_end+timedelta(hours=3))
assert occ_done['tracked'] and not occ_done['active'] and 299<=occ_done['seconds']<=301
stored_profile_tx=db.get_transaction(profile_tx)
assert stored_profile_tx['unplugged_at'] and 299<=float(stored_profile_tx['post_session_occupied_seconds'])<=301
profile_analytics=db.user_analytics(gamification_user_id,now=profile_end+timedelta(minutes=6))['all_time']
assert profile_analytics['unplug_delay_sessions']>=1 and profile_analytics['prompt_unplug_sessions']>=1
assert profile_analytics['post_session_occupied_seconds']>=299 and profile_analytics['avg_unplug_delay_seconds']<=db.PROMPT_UNPLUG_SECONDS
assert any(a.get('seed_key')=='unplug-fast-1' and a.get('earned') for a in db.achievements_for_user(gamification_user_id))
unplug_board=db.general_leaderboard('all','prompt_unplug_sessions')
assert unplug_board['metric_label']=='Stecker-Sprinter' and next(x for x in unplug_board['leaderboard'] if x['user_id']==gamification_user_id)['value']>=1
portal_dash=db.portal_dashboard(gamification_user_id,ranking_metric='xp',ranking_period='all')
assert portal_dash['ranking']['board']['metric']=='xp'
assert portal_dash['gamification']['xp']>=820 and portal_dash['ranking']['board']['my_entry']['user_id']==gamification_user_id
assert portal_dash['profile']['all_time_sessions']>=1
assert portal_dash['profile']['all_time_energy_kwh']>=12.5
assert portal_dash['profile']['favorite_charge_point']['id']=='QA-PROFILE-CP'
assert portal_dash['profile']['record_energy']['value']>=12.5
assert portal_dash['profile']['record_power']['value']>=11.2
assert portal_dash['profile']['xp_rank'] is not None and portal_dash['profile']['participants']>=2
assert portal_dash['profile']['recent_achievements']
# First V0.9.7.50 portal visit establishes a baseline instead of replaying historic awards.
assert not portal_dash['gamification_reveals']['pending']
baseline_level=portal_dash['gamification']['level']
reveal_badge_id=db.create_achievement('QA Reveal Badge','Neu freigeschaltet','✨','manual',None,True,True,True,'QA','secret',5000,'qa-reveal','Geheim',1)
db.award_achievement(gamification_user_id,reveal_badge_id,'manual')
reveal_dash=db.portal_dashboard(gamification_user_id,ranking_metric='xp',ranking_period='all')
assert reveal_dash['gamification_reveals']['pending']
assert any(int(x['achievement_id'])==reveal_badge_id and x['display_name']=='QA Reveal Badge' for x in reveal_dash['gamification_reveals']['achievements'])
assert reveal_dash['gamification_reveals']['level_up'] and reveal_dash['gamification_reveals']['level_up']['level']>baseline_level
assert db.acknowledge_portal_gamification_reveals(gamification_user_id,reveal_dash['gamification_reveals']['ack_award_id'],reveal_dash['gamification_reveals']['ack_level'])
assert not db.portal_dashboard(gamification_user_id,ranking_metric='xp',ranking_period='all')['gamification_reveals']['pending']
ok,reason=db.delete_ocpp_device('QA-PROFILE-CP')
assert ok and reason=='deleted'
db.set_setting('portal_leaderboard_show_names','1')
portal_named=db.portal_general_leaderboards(gamification_user_id,period='all',metric='xp')
assert next(x for x in portal_named['board']['leaderboard'] if x['user_id']==ranking_rival_id)['name']=='Ranking Rival QA'
db.set_setting('portal_leaderboard_show_names','0')

# V0.9.7.49: test/dummy users without protected history can be deleted permanently.
safe_delete_user=db.create_user('Permanent Delete QA',role='Fahrer',department='Test')
db.create_rfid_card('QA-DELETE-RFID',label='Delete QA',user_id=safe_delete_user)
db.award_achievement(safe_delete_user,int(manual_badge['id']),'manual')
safe_check=db.user_delete_check(safe_delete_user)
assert safe_check and safe_check['can_delete'] and safe_check['removable']['rfid_cards']==1 and safe_check['removable']['achievements']>=1
ok,reason,removed=db.delete_user_permanently(safe_delete_user)
assert ok and reason=='deleted' and db.get_user(safe_delete_user) is None
assert not any(c.get('uid')=='QA-DELETE-RFID' for c in db.list_rfid_cards())

protected_user=db.create_user('Protected History QA',role='Fahrer',department='Test')
db.create_rfid_card('QA-PROTECTED-RFID',label='Protected QA',user_id=protected_user)
db.upsert_charge_point('QA-PROTECTED-CP',vendor='QA',model='History Test',status='Available',onboarded=1,source_type='ocpp')
protected_tx=db.start_transaction('QA-PROTECTED-CP',id_tag='QA-PROTECTED-RFID',connector_id=1,meter_start_kwh=20.0)
db.add_meter_sample('QA-PROTECTED-CP',connector_id=1,transaction_id=protected_tx,power_kw=7.2,energy_kwh=21.0)
db.add_event('QA-PROTECTED-CP','MeterValues','QA cleanup event',transaction_id=protected_tx)
db.stop_transaction(protected_tx,energy_kwh=3.0,meter_stop_kwh=23.0,status='Completed')
protected_check=db.user_delete_check(protected_user)
assert protected_check and not protected_check['can_delete'] and protected_check['can_purge'] and protected_check['blockers']['transactions']>=1
assert protected_check['removable']['meter_samples']>=1 and protected_check['removable']['transaction_events']>=1
ok,reason,blocked=db.delete_user_permanently(protected_user)
assert not ok and reason=='history' and db.get_user(protected_user) is not None
ok,reason,purged=db.purge_user_with_history(protected_user)
assert ok and reason=='purged' and db.get_user(protected_user) is None and db.get_transaction(protected_tx) is None
assert not any(int(x.get('transaction_id') or 0)==protected_tx for x in db.meter_samples_for_charge_point('QA-PROTECTED-CP',50))
ok,reason=db.delete_ocpp_device('QA-PROTECTED-CP')
assert ok and reason=='deleted'

# LiveView / Kiosk 2.0 settings are server-side, validated and persistent.
lv_default=db.liveview_settings()
assert lv_default['preset']=='standard' and lv_default['sort']=='auto' and lv_default['columns']=='auto'
lv_saved=db.save_liveview_settings({'preset':'people','sort':'active','columns':'3','refresh_seconds':5,'show_vehicle':True,'show_user':False,'show_soc':True,'show_energy':True,'show_diagnostics':True,'show_clock':False,'show_technical_id':True})
assert lv_saved['preset']=='people' and lv_saved['sort']=='active' and lv_saved['columns']=='3' and lv_saved['refresh_seconds']==5
assert not lv_saved['show_user'] and not lv_saved['show_clock']
try:
    db.save_liveview_settings({'preset':'neon-unknown'})
    raise AssertionError('invalid LiveView preset accepted')
except ValueError:
    pass
db.save_liveview_settings({'preset':'standard','sort':'auto','columns':'auto','refresh_seconds':2,'show_vehicle':True,'show_user':True,'show_soc':True,'show_energy':True,'show_diagnostics':True,'show_clock':True,'show_technical_id':True})

# Update-center core: private GitHub release detection, version comparison and secret handling.
assert updates.is_newer('0.9.7.75','0.9.7.74')
assert not updates.is_newer('0.9.7.55','0.9.7.55')
updates.save_settings({'check_enabled':True,'github_token':'qa-token','portainer_webhook':'https://portainer.invalid:9443/api/stacks/webhooks/qa-test','portainer_tls_verify':True})
original_latest=updates._github_latest_release
updates._github_latest_release=lambda token:{'tag_name':'v0.9.7.75','name':'V0.9.7.75 QA','html_url':'https://example.invalid/release','published_at':'2026-10-04T15:00:00Z','body':'QA release notes'}
try:
    us=updates.check_latest(main.APP_VERSION,True)
    assert us['configured'] and us['update_available'] and us['latest_version']=='0.9.7.75' and us['install_ready']
finally:
    updates._github_latest_release=original_latest
assert updates.cached_status(main.APP_VERSION)['available']
# Keep the application lifespan deterministic: no real network checks during TestClient startup.
updates.save_settings({'check_enabled':False,'portainer_tls_verify':True})
db.set_setting('update_last_seen_version',main.APP_VERSION)
db.set_setting('update_last_error','')

# OCPP connection diagnostics are deterministic and vendor-neutral.
fixed_now=datetime(2026,10,4,15,30,0,tzinfo=timezone.utc)
healthy=ocpp_diagnostics.evaluate_connection({
    'duration_seconds':300,'subprotocol':'ocpp1.6','boot_received':True,
    'last_heartbeat':'2026-10-04T15:29:40+00:00','last_status':'2026-10-04T15:29:20+00:00',
    'last_meter_values':None,'station_status':'Available','error_code':'NoError',
    'connector_statuses':['Available'],
},now=fixed_now)
assert healthy['level']=='healthy' and healthy['score']==100 and healthy['heartbeat_age_seconds']==20
warning=ocpp_diagnostics.evaluate_connection({
    'duration_seconds':200,'subprotocol':'ocpp1.6','boot_received':True,
    'last_heartbeat':'2026-10-04T15:28:35+00:00','last_status':'2026-10-04T15:29:20+00:00',
    'station_status':'Available','error_code':'NoError','connector_statuses':['Available'],
},now=fixed_now)
assert warning['level']=='warning' and any(x['code']=='heartbeat_delayed' for x in warning['issues'])
critical=ocpp_diagnostics.evaluate_connection({
    'duration_seconds':400,'subprotocol':'ocpp1.6','boot_received':True,
    'last_heartbeat':'2026-10-04T15:29:50+00:00','last_status':'2026-10-04T15:29:40+00:00',
    'last_meter_values':'2026-10-04T15:20:00+00:00','station_status':'Charging','error_code':'NoError',
    'connector_statuses':['Charging'],
},now=fixed_now)
assert critical['level']=='critical' and any(x['code']=='meter_stale' for x in critical['issues'])
faulted=ocpp_diagnostics.evaluate_connection({
    'duration_seconds':100,'subprotocol':'ocpp1.6','boot_received':True,
    'last_heartbeat':'2026-10-04T15:29:50+00:00','last_status':'2026-10-04T15:29:50+00:00',
    'station_status':'Faulted','error_code':'GroundFailure','connector_statuses':['Faulted'],
},now=fixed_now)
assert faulted['level']=='critical' and faulted['faulted']

# Capability snapshots persist and the real OCPP probe parses standard configuration.
spec=importlib.util.spec_from_file_location('app._qa_real_ocpp_server',root/'app/ocpp_server.py')
real_ocpp=importlib.util.module_from_spec(spec);sys.modules[spec.name]=real_ocpp;spec.loader.exec_module(real_ocpp)
class FakeCapabilityCP:
    async def call(self,request):
        name=type(request).__name__
        if name=='GetConfiguration':
            requested=list(getattr(request,'key',None) or [])
            values={
                'SupportedFeatureProfiles':'Core,SmartCharging,LocalAuthListManagement,RemoteTrigger',
                'HeartbeatInterval':'60',
                'MeterValueSampleInterval':'30',
                'ClockAlignedDataInterval':'900',
                'ChargingScheduleAllowedChargingRateUnit':'Current,Power',
                'MaxChargingProfilesInstalled':'10',
                'LocalAuthListMaxLength':'100',
                'LocalAuthListEnabled':'true',
                'LocalAuthorizeOffline':'true',
                'ChargeProfileMaxStackLevel':'8',
                'ChargingScheduleMaxPeriods':'24',
            }
            rows=[types.SimpleNamespace(key=k,readonly=True,value=values[k]) for k in requested if k in values]
            unknown=[k for k in requested if k not in values]
            return types.SimpleNamespace(configuration_key=rows,unknown_key=unknown)
        if name=='GetLocalListVersion':
            return types.SimpleNamespace(list_version=3)
        raise AssertionError('unexpected OCPP request '+name)
real_ocpp.ACTIVE_CONNECTIONS['QA-CAP']={'charge_point':FakeCapabilityCP()}
snapshot=asyncio.run(real_ocpp.probe_capabilities('QA-CAP'))
assert snapshot['capabilities']['smart_charging']['state']=='supported'
assert snapshot['capabilities']['local_auth_list']['state']=='supported'
# GetLocalListVersion=-1 is authoritative in OCPP 1.6 and must override advertised profiles/config keys.
class FakeNoLocalListCP(FakeCapabilityCP):
    async def call(self,request):
        if type(request).__name__=='GetLocalListVersion':
            return types.SimpleNamespace(list_version=-1)
        return await super().call(request)
real_ocpp.ACTIVE_CONNECTIONS['QA-CAP-NO-LOCAL']={'charge_point':FakeNoLocalListCP()}
no_local_snapshot=asyncio.run(real_ocpp.probe_capabilities('QA-CAP-NO-LOCAL'))
assert no_local_snapshot['capabilities']['local_auth_list']['state']=='not_advertised'
assert 'nicht unterstützt' in no_local_snapshot['capabilities']['local_auth_list']['evidence']
assert snapshot['capabilities']['remote_trigger']['state']=='supported'
assert snapshot['configuration']['HeartbeatInterval']['value']=='60'
assert db.get_ocpp_capability_snapshot('QA-CAP')['profiles'][0]=='Core'
assert any(x['event_type']=='CapabilityProbe' for x in db.events_for_charge_point('QA-CAP',10))
db.add_event('QA-LOCAL-HISTORY','SendLocalList','reason=QA; update_type=Full; list_version=4; entries=2; response=Accepted',direction='OUT')
assert db.local_list_send_was_accepted('QA-LOCAL-HISTORY')

# Remote capability learning distinguishes explicit NotSupported from normal rejection.
class FakeRemoteCP:
    def __init__(self,status): self.status=status
    async def call(self,request): return types.SimpleNamespace(status=self.status)

db.reset_remote_capability_profile('QA-REMOTE')
real_ocpp.ACTIVE_CONNECTIONS['QA-REMOTE']={'charge_point':FakeRemoteCP('NotSupported')}
not_supported=asyncio.run(real_ocpp.remote_command('QA-REMOTE','unlock',connector_id=1))
assert not not_supported['accepted'] and not_supported['status']=='NotSupported'
learned=db.remote_capability_profile('QA-REMOTE')
assert learned['unlock']['state']=='unsupported' and learned['unlock']['not_supported_count']==1
try:
    asyncio.run(real_ocpp.remote_command('QA-REMOTE','unlock',connector_id=1))
    raise AssertionError('learned unsupported command was not blocked')
except RuntimeError as exc:
    assert 'NotSupported' in str(exc)

db.reset_remote_capability_profile('QA-REMOTE')
real_ocpp.ACTIVE_CONNECTIONS['QA-REMOTE']={'charge_point':FakeRemoteCP('Rejected')}
rejected=asyncio.run(real_ocpp.remote_command('QA-REMOTE','availability',connector_id=0,availability_type='Operative'))
assert not rejected['accepted']
learned=db.remote_capability_profile('QA-REMOTE')
assert learned['availability']['state']=='available' and learned['availability']['not_supported_count']==0

db.reset_remote_capability_profile('QA-REMOTE')
real_ocpp.ACTIVE_CONNECTIONS['QA-REMOTE']={'charge_point':FakeRemoteCP('Accepted')}
accepted=asyncio.run(real_ocpp.remote_command('QA-REMOTE','reset',reset_type='Soft'))
assert accepted['accepted']
learned=db.remote_capability_profile('QA-REMOTE')
assert learned['reset']['state']=='supported' and learned['reset']['success_count']==1
assert db.reset_remote_capability_profile('QA-REMOTE')>=1
assert db.remote_capability_profile('QA-REMOTE')['reset']['state']=='untested'

# Security event log is deliberately compact: exactly 10 rows per page.
for i in range(23):
    db.add_security_event('Pagination QA '+str(i),category='pagination_qa',success=bool(i%2))
page1=db.security_events_page(1,10,'pagination_qa')
page2=db.security_events_page(2,10,'pagination_qa')
page3=db.security_events_page(3,10,'pagination_qa')
assert page1['page_size']==10 and page1['total']==23 and page1['pages']==3 and len(page1['items'])==10
assert page2['page']==2 and len(page2['items'])==10
assert page3['page']==3 and len(page3['items'])==3
assert db.security_events_page(99,10,'pagination_qa')['page']==3

# Production/source cleanup and container hardening invariants.
assert not (root/'app/templates/devices.html').exists()
assert not (root/'app/templates/device_detail.html').exists()
assert (root/'app/runtime_utils.py').exists()
assert (root/'app/totp.py').exists()
assert (root/'dev/simulator/simulator.py').exists()
assert not list(root.glob('QA_REPORT_V*.md'))
assert not (root/'tools').exists()
for compose_name in ('docker-compose.yml','docker-compose.portainer.yml'):
    compose=(root/compose_name).read_text(encoding='utf-8')
    assert 'read_only: true' in compose
    assert 'no-new-privileges:true' in compose
    assert 'cap_drop:' in compose and '- ALL' in compose
    assert 'pids_limit: 256' in compose
    assert 'ENABLE_API_DOCS: ${ENABLE_API_DOCS:-0}' in compose
assert 'pip check' in (root/'Dockerfile').read_text(encoding='utf-8')
assert 'qrcode' in (root/'requirements.txt').read_text(encoding='utf-8').lower()

# No product artifact may reveal the underlying assistant provider.
for p in root.rglob('*'):
    if p.is_file() and p.suffix.lower() in {'.py','.html','.md','.js','.css','.txt'}:
        text=p.read_text(encoding='utf-8',errors='ignore')
        assert ('Chat'+'GPT') not in text and ('Open'+'AI') not in text, p
info=(root/'app/templates/_info_modal.html').read_text(encoding='utf-8')
assert 'Technische Unterstützung: Buddy' in info

# TOTP implementation: deterministic generation/verification and recovery-code hashing.
secret=totp.generate_secret()
fixed_time=1700000000
fixed_code=totp.current_code(secret,fixed_time)
assert len(fixed_code)==6 and fixed_code.isdigit()
matched=totp.verify_code(secret,fixed_code,fixed_time,window=1)
assert matched==totp.counter_for_time(fixed_time)
recovery=totp.generate_recovery_codes(8)
assert len(recovery)==8 and len({totp.recovery_code_hash(x) for x in recovery})==8

# Central status and V0.9.7.27 hardening remain intact.
mailer.save_settings({'enabled':True,'host':'smtp.example.org','port':587,'security':'starttls','username':'','from_email':'laden@example.org','from_name':'Laden','admin_recipients':'','public_base_url':'','event_rfid_requests':True,'event_pin_reset_admin':True,'event_backup_failures':True,'event_security_warnings':False,'event_access_requests':True})
admin_password='abcdefghijkl'
admin_id=db.create_system_user('admin28','Admin QA',main._password_hash(admin_password),'admin',True)

# V0.9.7.54 Web-Push: subscription baseline prevents old-notification floods,
# each revision is delivered once, and the severity threshold is per device.
old_push_id=db.create_notification('qa:push:old','warning','Alter Push-Hinweis','Soll beim Aktivieren nicht sofort gepusht werden','/',audience='admin')
saved_push=db.save_web_push_subscription(admin_id,'https://push.example.invalid/sub','qa-p256dh','qa-auth','warning','QA Browser')
assert saved_push['min_severity']=='warning'
assert all(int(x['notification_id'])!=old_push_id for x in db.pending_web_push_deliveries(100))
new_push_id=db.create_notification('qa:push:new','warning','Neuer Push-Hinweis','Soll genau einmal zugestellt werden','/',audience='admin')
pending_push=db.pending_web_push_deliveries(100)
new_delivery=next(x for x in pending_push if int(x['notification_id'])==new_push_id)
db.mark_web_push_delivery(new_delivery['subscription_id'],new_delivery['notification_id'],new_delivery['revision'])
assert all(int(x['notification_id'])!=new_push_id for x in db.pending_web_push_deliveries(100))
db.deactivate_notification('qa:push:new')
db.create_notification('qa:push:new','warning','Neuer Push-Hinweis erneut','Reaktivierter Zustand','/',audience='admin')
reactivated=next(x for x in db.pending_web_push_deliveries(100) if int(x['notification_id'])==new_push_id)
assert int(reactivated['revision'])>int(new_delivery['revision'])
info_push_id=db.create_notification('qa:push:info','info','Info Push','Nur bei Stufe Alle','/',audience='admin')
assert all(int(x['notification_id'])!=info_push_id for x in db.pending_web_push_deliveries(100))
assert db.update_web_push_preference(admin_id,'https://push.example.invalid/sub','info')
assert any(int(x['notification_id'])==info_push_id for x in db.pending_web_push_deliveries(100))
assert db.remove_web_push_subscription(admin_id,'https://push.example.invalid/sub')

# Actual dispatcher path is exercised without network I/O.
dispatch_sub=db.save_web_push_subscription(admin_id,'https://push.example.com/qa-dispatch','qa-p256dh-dispatch','qa-auth-dispatch','warning','QA Dispatcher')
dispatch_notification=db.create_notification('qa:push:dispatch','critical','Dispatcher QA','Payload wird aufgebaut','/security',audience='admin')
captured=[]
original_webpush=web_push.webpush
web_push.webpush=lambda **kwargs: captured.append(kwargs) or types.SimpleNamespace(status_code=201)
try:
    dispatched=web_push.dispatch_pending(20)
    assert dispatched['sent']>=1 and captured
    assert captured[-1]['vapid_private_key']==db.get_setting('web_push_vapid_private_key')
    assert 'Dispatcher QA' in captured[-1]['data']
    assert all(int(x['notification_id'])!=dispatch_notification for x in db.pending_web_push_deliveries(100))
finally:
    web_push.webpush=original_webpush
    db.remove_web_push_subscription(admin_id,'https://push.example.com/qa-dispatch')

# Public kiosk must be able to load vehicle/profile media without a backend login.
public_media=main.MEDIA_DIR/'kiosk-media-qa.png'
public_media.write_bytes(b'kiosk-media')
with TestClient(main.app) as public_client:
    media_response=public_client.get('/media/kiosk-media-qa.png',follow_redirects=False)
    assert media_response.status_code==200 and media_response.content==b'kiosk-media'
public_media.unlink(missing_ok=True)

saved_recovery=[]
active_secret=None
with TestClient(main.app) as client:
    login=client.post('/login',data={'username':'admin28','password':admin_password,'next':'/'},follow_redirects=False)
    assert login.status_code==303 and login.headers['location']=='/'
    # Access-request maintenance: admins can delete requests; an approved linked user remains intact.
    access_sig_name='qa-access-delete.png'
    (main.ACCESS_SIGNATURE_DIR/access_sig_name).write_bytes(b'QA')
    raw_access_token='qa-access-delete-token'
    access_token_hash=main._session_hash(raw_access_token)
    access_email='qa-access-delete@example.org'
    access_email_hash=__import__('hashlib').sha256(access_email.encode('utf-8')).hexdigest()
    access_exp=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()
    db.create_access_request_verification(access_email,access_email_hash,'qa-ip-hash',access_token_hash,access_exp)
    access_id=db.create_access_request(
        access_token_hash,name='Access Delete QA',street='Teststr. 1',postal_code='58332',city='Schwelm',phone='0123',
        vehicle_make_model='QA EV',vehicle_plate='EN-QA 1E',weekly_hours=39,
        field_values_json='{}',field_schema_json='[]',terms_version='qa',terms_snapshot='[]',signature_path=access_sig_name,ip_hash='qa-ip-hash'
    )
    approved=db.approve_access_request(access_id,admin_id,main._portal_pin_hash('123456'),return_details=True)
    linked_user_id=int(approved['user_id'])
    access_delete=client.delete('/api/access-requests/'+str(access_id))
    assert access_delete.status_code==200 and access_delete.json()['linked_user_id']==linked_user_id
    assert db.get_access_request(access_id) is None and db.get_user(linked_user_id) is not None
    assert not (main.ACCESS_SIGNATURE_DIR/access_sig_name).exists()

    # RFID self-service request deletion removes only the request, never card state/history.
    rfid_cleanup_user=db.create_user('RFID Request Cleanup QA',role='Fahrer',department='Test')
    rfid_cleanup_card=db.create_rfid_card('QA-RFID-REQUEST-CLEANUP',label='Cleanup QA',user_id=rfid_cleanup_user)
    rfid_cleanup_request=db.create_rfid_replacement_request(
        rfid_cleanup_user,rfid_cleanup_card,reason='lost',note='QA delete request',block_card=True
    )
    assert db.get_rfid_card(rfid_cleanup_card)['status']=='Verloren'
    request_delete=client.delete('/api/rfid/requests/'+str(rfid_cleanup_request))
    assert request_delete.status_code==200 and request_delete.json()['card_id']==rfid_cleanup_card
    assert all(int(x['id'])!=rfid_cleanup_request for x in db.list_rfid_replacement_requests())
    assert db.get_rfid_card(rfid_cleanup_card)['status']=='Verloren'
    cleanup_detail=db.rfid_card_detail(rfid_cleanup_card)
    assert any(x['event_type']=='request_deleted' for x in cleanup_detail['history'])
    assert client.delete('/api/rfid/requests/'+str(rfid_cleanup_request)).status_code==404

    # Notification cleanup is per system user; same revision stays hidden, escalated revision reappears.
    cleanup_notification=db.create_notification('qa:notification-cleanup','info','Notification Cleanup QA','Nur für Cleanup-Test','/activity',audience='admin')
    visible=client.get('/api/notifications?limit=100').json()
    assert any(int(x['id'])==cleanup_notification for x in visible['items'])
    dismissed=client.delete('/api/notifications/'+str(cleanup_notification))
    assert dismissed.status_code==200
    assert all(int(x['id'])!=cleanup_notification for x in client.get('/api/notifications?limit=100').json()['items'])
    db.create_notification('qa:notification-cleanup','info','Notification Cleanup QA','gleiche Revision','/activity',audience='admin')
    assert all(int(x['id'])!=cleanup_notification for x in client.get('/api/notifications?limit=100').json()['items'])
    db.create_notification('qa:notification-cleanup','critical','Notification Cleanup QA eskaliert','muss wieder erscheinen','/activity',audience='admin')
    assert any(int(x['id'])==cleanup_notification for x in client.get('/api/notifications?limit=100').json()['items'])
    assert client.post('/api/notifications/'+str(cleanup_notification)+'/read').status_code==200
    cleanup_read_notification=db.create_notification('qa:notification-clear-read','info','Gelesen leeren QA','wird ausgeblendet','/activity',audience='admin')
    assert client.post('/api/notifications/'+str(cleanup_read_notification)+'/read').status_code==200
    clear_read=client.delete('/api/notifications/read-all')
    assert clear_read.status_code==200 and clear_read.json()['count']>=2
    remaining_ids={int(x['id']) for x in client.get('/api/notifications?limit=100').json()['items']}
    assert cleanup_notification not in remaining_ids and cleanup_read_notification not in remaining_ids

    status=client.get('/api/system-status')
    assert status.status_code==200
    push_config=client.get('/api/notifications/push/config')
    assert push_config.status_code==200 and push_config.json()['public_key']
    push_sub=client.post('/api/notifications/push/subscribe',json={
        'endpoint':'https://push.example.invalid/api-device',
        'keys':{'p256dh':'qa-p256dh-api','auth':'qa-auth-api'},
        'min_severity':'warning',
    })
    assert push_sub.status_code==200 and push_sub.json()['min_severity']=='warning'
    push_pref=client.post('/api/notifications/push/preferences',json={'endpoint':'https://push.example.invalid/api-device','min_severity':'critical'})
    assert push_pref.status_code==200 and push_pref.json()['min_severity']=='critical'
    original_push_test=main.web_push.send_test
    main.web_push.send_test=lambda row: True
    try:
        push_test=client.post('/api/notifications/push/test',json={'endpoint':'https://push.example.invalid/api-device'})
        assert push_test.status_code==200
    finally:
        main.web_push.send_test=original_push_test
    push_remove=client.request('DELETE','/api/notifications/push/subscribe',json={'endpoint':'https://push.example.invalid/api-device'})
    assert push_remove.status_code==200 and push_remove.json()['removed']
    data=status.json()
    assert data['version']=='0.9.7.74' and data['overall']['label']=='System OK'
    assert {'backend','ocpp','mail','backup','pwa','security','ocpp_transport','fleet_integration'} <= {x['key'] for x in data['services']}
    page=client.get('/')
    assert page.status_code==200 and 'System OK · V0.9.7.74' in page.text
    csp=page.headers.get('content-security-policy','')
    assert "object-src 'none'" in csp and "frame-src 'none'" in csp
    assert client.get('/docs').status_code==404
    update_page=client.get('/updates'); assert update_page.status_code==200 and 'Update-Center' in update_page.text and 'updateConfirmModal' in update_page.text and "if(!confirm('V'" not in update_page.text
    live_settings=client.get('/api/settings/liveview'); assert live_settings.status_code==200 and live_settings.json()['preset']=='standard'
    saved_live=client.put('/api/settings/liveview',json={'preset':'people','sort':'auto','columns':'2','refresh_seconds':3,'show_vehicle':True,'show_user':True,'show_soc':True,'show_energy':True,'show_diagnostics':True,'show_clock':True,'show_technical_id':True})
    assert saved_live.status_code==200 and saved_live.json()['preset']=='people' and saved_live.json()['columns']=='2'
    preview=client.get('/liveview?preview=people'); assert preview.status_code==200 and 'data-preset="people"' in preview.text and 'LiveView / Kiosk 2.0' in preview.text
    standard_preview=client.get('/liveview?preview=standard'); assert standard_preview.status_code==200 and 'standard-main' in standard_preview.text and 'standard-connector-id' in standard_preview.text
    pro_preview=client.get('/liveview?preview=pro'); assert pro_preview.status_code==200 and 'pro-main' in pro_preview.text and 'pro-health' in pro_preview.text and 'pro-operating' in pro_preview.text
    live_api=client.get('/api/liveview'); assert live_api.status_code==200
    live_data=live_api.json(); assert live_data['settings']['preset']=='people' and 'people_sessions' in live_data and {'offline','available','critical','warnings'} <= set(live_data['summary'])
    db.save_liveview_settings({'preset':'standard','sort':'auto','columns':'auto','refresh_seconds':2,'show_vehicle':True,'show_user':True,'show_soc':True,'show_energy':True,'show_diagnostics':True,'show_clock':True,'show_technical_id':True})

    # White-label assets extend beyond login branding.
    brand_before=client.get('/api/settings/branding'); assert brand_before.status_code==200
    bv=brand_before.json()
    brand_save=client.post('/api/settings/branding',data={
        'product_name':bv['product_name'],'organization_name':bv['organization_name'],'display_name':bv['display_name'],
        'product_subtitle':bv['product_subtitle'],'voucher_prefix':bv['voucher_prefix'],'primary_color':bv['primary_color'],
    },files={
        'app_background':('app-bg.png',b'\x89PNG\r\n\x1a\nQA-APP-BG','image/png'),
        'header_background':('header-bg.png',b'\x89PNG\r\n\x1a\nQA-HEADER-BG','image/png'),
    })
    assert brand_save.status_code==200
    saved_brand=brand_save.json()
    assert saved_brand['app_background_url'].startswith('/branding/app_background.') and saved_brand['header_background_url'].startswith('/branding/header_background.')
    branded_page=client.get('/')
    assert branded_page.status_code==200 and 'has-app-background' in branded_page.text and 'has-header-background' in branded_page.text
    profile_user_id=db.create_user('Profilbild QA',role='Fahrer',department='Test')
    assert db.earned_achievement_count(profile_user_id)==0
    profile_png=b'\x89PNG\r\n\x1a\nQA-PROFILE'
    image_upload=client.post('/api/users/'+str(profile_user_id)+'/image',files={'image':('profile.png',profile_png,'image/png')})
    assert image_upload.status_code==200 and image_upload.json()['image_path'].startswith('/media/user-')
    assert db.get_user(profile_user_id)['image_path']==image_upload.json()['image_path']
    image_delete=client.delete('/api/users/'+str(profile_user_id)+'/image')
    assert image_delete.status_code==200 and db.get_user(profile_user_id)['image_path'] is None
    delete_check=client.get('/api/users/'+str(profile_user_id)+'/delete-check')
    assert delete_check.status_code==200 and delete_check.json()['can_delete']
    permanent_delete=client.delete('/api/users/'+str(profile_user_id)+'/permanent')
    assert permanent_delete.status_code==200 and permanent_delete.json()['mode']=='deleted' and db.get_user(profile_user_id) is None

    purge_api_user=db.create_user('API Testdaten Löschen',role='Fahrer',department='Test')
    db.create_rfid_card('QA-PURGE-API-RFID',label='Purge API',user_id=purge_api_user)
    db.upsert_charge_point('QA-PURGE-API-CP',vendor='QA',model='Purge API',status='Available',onboarded=1,source_type='ocpp')
    purge_api_tx=db.start_transaction('QA-PURGE-API-CP',id_tag='QA-PURGE-API-RFID',connector_id=1,meter_start_kwh=30.0)
    db.stop_transaction(purge_api_tx,energy_kwh=2.0,meter_stop_kwh=32.0,status='Completed')
    purge_check=client.get('/api/users/'+str(purge_api_user)+'/delete-check')
    assert purge_check.status_code==200 and not purge_check.json()['can_delete'] and purge_check.json()['can_purge']
    purge_wrong=client.request('DELETE','/api/users/'+str(purge_api_user)+'/purge',json={'confirmation':'FALSCH'})
    assert purge_wrong.status_code==400 and db.get_user(purge_api_user) is not None
    purge_ok=client.request('DELETE','/api/users/'+str(purge_api_user)+'/purge',json={'confirmation':'API Testdaten Löschen'})
    assert purge_ok.status_code==200 and purge_ok.json()['mode']=='purged' and db.get_user(purge_api_user) is None and db.get_transaction(purge_api_tx) is None
    ok,reason=db.delete_ocpp_device('QA-PURGE-API-CP')
    assert ok and reason=='deleted'

    # Real portal sessions can acknowledge a reveal; admin auth is not required.
    reveal_api_user=db.create_user('Portal Reveal API QA',role='Fahrer',department='Test')
    db.set_user_portal_enabled(reveal_api_user,True)
    reveal_api_baseline=db.portal_dashboard(reveal_api_user)
    assert not reveal_api_baseline['gamification_reveals']['pending']
    reveal_api_badge=db.create_achievement('Portal Reveal API Badge','API reveal','🎉','manual',None,False,False,True,'QA','epic',900)
    db.award_achievement(reveal_api_user,reveal_api_badge,'manual')
    reveal_api_pending=db.portal_dashboard(reveal_api_user)
    assert reveal_api_pending['gamification_reveals']['pending']
    portal_token='qa-portal-reveal-token'
    db.create_portal_session(main._session_hash(portal_token),reveal_api_user,'2030-01-01T00:00:00+00:00')
    client.cookies.set(main.PORTAL_COOKIE,portal_token,path='/')
    portal_api=client.get('/api/public/portal')
    assert portal_api.status_code==200 and portal_api.json()['gamification_reveals']['pending']
    reveal_ack=client.post('/api/public/portal/gamification-reveals/ack',json={
        'award_id':portal_api.json()['gamification_reveals']['ack_award_id'],
        'level':portal_api.json()['gamification_reveals']['ack_level'],
    })
    assert reveal_ack.status_code==200 and reveal_ack.json()['ok']
    assert not client.get('/api/public/portal').json()['gamification_reveals']['pending']
    client.cookies.pop(main.PORTAL_COOKIE,None)

    db.upsert_charge_point('CAP-API',status='Available',onboarded=1,ignored=0)
    async def fake_probe(cp_id):
        return db.save_ocpp_capability_snapshot(cp_id,profiles=['Core','SmartCharging'],capabilities={'smart_charging':{'state':'supported','label':'Unterstützt','detail':'QA','evidence':'QA'}},configuration={'HeartbeatInterval':{'value':'60','readonly':True}},unknown_keys=[])
    original_probe=main.probe_capabilities;main.probe_capabilities=fake_probe
    try:
        cap_route=client.post('/api/remote-control/CAP-API/capabilities')
        assert cap_route.status_code==200 and cap_route.json()['capability_snapshot']['capabilities']['smart_charging']['state']=='supported'
        cap_state=client.get('/api/remote-control/CAP-API/state')
        assert cap_state.status_code==200 and cap_state.json()['capability_snapshot']['profiles']==['Core','SmartCharging']
        db.record_remote_capability_result('CAP-API','unlock',outcome='unsupported',status='NotSupported',detail='QA')
        learned_state=client.get('/api/remote-control/CAP-API/state')
        assert learned_state.status_code==200 and learned_state.json()['remote_capabilities']['unlock']['state']=='unsupported'
        reset_learned=client.delete('/api/remote-control/CAP-API/capabilities/learned')
        assert reset_learned.status_code==200 and reset_learned.json()['remote_capabilities']['unlock']['state']=='untested'

        config_read=client.post('/api/remote-control/CAP-API/configuration/read',json={'keys':['MeterValueSampleInterval']})
        assert config_read.status_code==200 and config_read.json()['configuration']['MeterValueSampleInterval']['value']=='60'
        readonly_change=client.post('/api/remote-control/CAP-API/configuration/change',json={'key':'HeartbeatInterval','value':'30'})
        assert readonly_change.status_code==409
        config_change=client.post('/api/remote-control/CAP-API/configuration/change',json={'key':'MeterValueSampleInterval','value':'30'})
        assert config_change.status_code==200 and config_change.json()['accepted']

        trigger=client.post('/api/remote-control/CAP-API/trigger',json={'requested_message':'Heartbeat','connector_id':0})
        assert trigger.status_code==200 and trigger.json()['accepted']
        invalid_trigger=client.post('/api/remote-control/CAP-API/trigger',json={'requested_message':'TotallyUnknown'})
        assert invalid_trigger.status_code==400

        diagnostics=client.post('/api/remote-control/CAP-API/diagnostics',json={'location':'https://diag.example.org/upload','retries':1,'retry_interval':60})
        assert diagnostics.status_code==200 and diagnostics.json()['accepted']
        bad_diag=client.post('/api/remote-control/CAP-API/diagnostics',json={'location':'file:///tmp/diag'})
        assert bad_diag.status_code==400

        future=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        firmware=client.post('/api/remote-control/CAP-API/firmware',json={'location':'https://firmware.example.org/fw.bin','retrieve_date':future,'retries':1,'retry_interval':60})
        assert firmware.status_code==200 and firmware.json()['accepted']
        active_fw_tx=db.start_transaction('CAP-API',id_tag='QA-SERVICE-RFID-FW',connector_id=1)
        firmware_blocked=client.post('/api/remote-control/CAP-API/firmware',json={'location':'https://firmware.example.org/fw.bin','retrieve_date':future})
        assert firmware_blocked.status_code==409
        db.stop_transaction(active_fw_tx,status='Completed')
        bad_firmware=client.post('/api/remote-control/CAP-API/firmware',json={'location':'file:///tmp/fw.bin','retrieve_date':future})
        assert bad_firmware.status_code==400

        service_user=db.create_user('Service Reservation QA',role='Fahrer',department='Test')
        db.create_rfid_card('QA-SERVICE-RFID',label='Service Reserve',user_id=service_user)
        expiry=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        reserve=client.post('/api/remote-control/CAP-API/reservation',json={'connector_id':1,'id_tag':'QA-SERVICE-RFID','expiry_date':expiry})
        assert reserve.status_code==200 and reserve.json()['accepted'] and reserve.json()['reservation_id']>0
        reservation_id=reserve.json()['reservation_id']
        state_with_service=client.get('/api/remote-control/CAP-API/state').json()
        assert state_with_service['service_operations'] and any(int(x['reservation_id'])==reservation_id for x in state_with_service['reservations'])
        cancel=client.post('/api/remote-control/CAP-API/reservation/cancel',json={'reservation_id':reservation_id})
        assert cancel.status_code==200 and cancel.json()['accepted']
        assert next(x for x in db.ocpp_reservations_for_charge_point('CAP-API') if int(x['reservation_id'])==reservation_id)['status']=='Cancelled'
        assert {'change_configuration','trigger_message','get_diagnostics','update_firmware','reserve','cancel_reservation'} <= set(db.remote_capability_profile('CAP-API'))
    finally:
        main.probe_capabilities=original_probe
        db.upsert_charge_point('CAP-API',status='Offline',onboarded=1,ignored=1)
    update_settings=client.get('/api/updates/settings'); assert update_settings.status_code==200 and update_settings.json()['github_token_configured']
    enabled=client.put('/api/updates/settings',json={'check_enabled':True,'portainer_tls_verify':True}); assert enabled.status_code==200
    original_latest=updates._github_latest_release
    updates._github_latest_release=lambda token:{'tag_name':'v0.9.7.75','name':'V0.9.7.75 QA','html_url':'https://example.invalid/release','published_at':'2026-10-04T15:00:00Z','body':'QA release notes'}
    try:
        update_status=client.get('/api/updates/status?force=1'); assert update_status.status_code==200 and update_status.json()['update_available'] and update_status.json()['latest_version']=='0.9.7.75'
        # Fleet API remains public-path compatible but is independently protected by Bearer auth.
        os.environ.pop('FLEET_INTEGRATION_TOKEN',None)
        fleet_disabled=client.get('/api/integrations/fleet/v1/health')
        assert fleet_disabled.status_code==503
        os.environ['FLEET_INTEGRATION_TOKEN']='qa-fleet-token-abcdefghijklmnopqrstuvwxyz'
        fleet_wrong=client.get('/api/integrations/fleet/v1/health',headers={'Authorization':'Bearer wrong-token'})
        assert fleet_wrong.status_code==401 and fleet_wrong.headers.get('www-authenticate')=='Bearer'
        fleet_ok=client.get('/api/integrations/fleet/v1/health',headers={'Authorization':'Bearer qa-fleet-token-abcdefghijklmnopqrstuvwxyz'})
        assert fleet_ok.status_code==200 and fleet_ok.json()['mode']=='read-only'
        assert fleet_ok.headers.get('cache-control')=='no-store' and fleet_ok.headers.get('x-content-type-options')=='nosniff'
        fleet_sessions=client.get('/api/integrations/fleet/v1/sessions?limit=5',headers={'Authorization':'Bearer qa-fleet-token-abcdefghijklmnopqrstuvwxyz'})
        assert fleet_sessions.status_code==200 and fleet_sessions.headers.get('cache-control')=='no-store'
        os.environ.pop('FLEET_INTEGRATION_TOKEN',None)

        system_update=client.get('/api/system-status'); assert system_update.status_code==200
        system_update_data=system_update.json()
        assert system_update_data['overall']['update_available'] and system_update_data['overall']['update_version']=='0.9.7.75'
        update_service=next(x for x in system_update_data['services'] if x['key']=='update')
        assert update_service['available'] and update_service['latest_version']=='0.9.7.75'
    finally:
        updates._github_latest_release=original_latest
        updates.save_settings({'check_enabled':False,'portainer_tls_verify':True})
        db.set_setting('update_last_seen_version',main.APP_VERSION)
        db.set_setting('update_last_error','')

    report_pdf=client.get('/api/reports/export.pdf?period=current_month')
    assert report_pdf.status_code==200 and report_pdf.headers['content-type'].startswith('application/pdf') and report_pdf.content.startswith(b'%PDF') and len(report_pdf.content)>1000

    # Account 2FA setup requires the current password, exposes a local QR and returns recovery codes once.
    denied=client.post('/api/account/2fa/setup',json={'current_password':'wrong-password'})
    assert denied.status_code==400
    setup=client.post('/api/account/2fa/setup',json={'current_password':admin_password})
    assert setup.status_code==200
    active_secret=setup.json()['secret']
    qr=client.get('/api/account/2fa/setup/qr')
    assert qr.status_code==200 and qr.headers['content-type'].startswith('image/png') and len(qr.content)>100
    confirm=client.post('/api/account/2fa/setup/confirm',json={'code':totp.current_code(active_secret)})
    assert confirm.status_code==200
    saved_recovery=confirm.json()['recovery_codes']
    assert len(saved_recovery)==8
    state=client.get('/api/account/2fa/status').json()['two_factor']
    assert int(state['totp_enabled'])==1 and state['recovery_codes_remaining']==8
    listed=client.get('/api/system-users').json()['users']
    assert any(x['id']==admin_id and int(x['totp_enabled'])==1 for x in listed)

    # Test mail remains available even while regular sending is disabled.
    original=mailer.send_template
    mailer.send_template=lambda *a,**k:{'ok':True,'recipients':['qa@example.org'],'template':a[0],'subject':'QA'}
    try:
        mailer.save_settings({'enabled':False,'host':'smtp.example.org','port':587,'security':'starttls','username':'','from_email':'laden@example.org','from_name':'Laden','admin_recipients':'','public_base_url':'','event_rfid_requests':True,'event_pin_reset_admin':True,'event_backup_failures':True,'event_security_warnings':False,'event_access_requests':True})
        test=client.post('/api/settings/mail/test',json={'recipient':'qa@example.org','template':'system'})
        assert test.status_code==200 and db.get_setting('mail_last_test_at','')
    finally:
        mailer.send_template=original

# Password-only login must now stop at the second factor.
with TestClient(main.app) as client:
    login=client.post('/login',data={'username':'admin28','password':admin_password,'next':'/'},follow_redirects=False)
    assert login.status_code==303 and login.headers['location']=='/login/2fa'
    assert main.TWO_FACTOR_COOKIE in client.cookies
    assert main.SESSION_COOKIE not in client.cookies
    page=client.get('/login/2fa')
    assert page.status_code==200 and 'Zweiter Faktor' in page.text
    factor=client.post('/login/2fa',data={'code':totp.current_code(active_secret)},follow_redirects=False)
    assert factor.status_code==303 and factor.headers['location']=='/'
    assert main.SESSION_COOKIE in client.cookies
    current_counter=totp.counter_for_time()
    assert not db.accept_system_totp_counter(admin_id,current_counter)

# Recovery codes are one-time and can complete a fresh 2FA challenge.
with TestClient(main.app) as client:
    login=client.post('/login',data={'username':'admin28','password':admin_password,'next':'/'},follow_redirects=False)
    assert login.status_code==303 and login.headers['location']=='/login/2fa'
    factor=client.post('/login/2fa',data={'code':saved_recovery[0]},follow_redirects=False)
    assert factor.status_code==303
    state=client.get('/api/account/2fa/status').json()['two_factor']
    assert state['recovery_codes_remaining']==7

    # Admin recovery: reset another account's 2FA and revoke its sessions.
    viewer_id=db.create_system_user('viewer28','Viewer QA',main._password_hash('mnopqrstuvwx'),'viewer',True)
    viewer_secret=totp.generate_secret()
    db.enable_system_user_totp(viewer_id,viewer_secret)
    db.replace_system_recovery_codes(viewer_id,[totp.recovery_code_hash(x) for x in totp.generate_recovery_codes(2)])
    reset=client.delete('/api/system-users/'+str(viewer_id)+'/2fa')
    assert reset.status_code==200 and int(reset.json()['user']['totp_enabled'])==0
    own_reset=client.delete('/api/system-users/'+str(admin_id)+'/2fa')
    assert own_reset.status_code==400

# Public registration keeps its precise disabled-mail message.
with TestClient(main.app) as public:
    page=public.post('/public/access-request/start',data={'email':'person@example.org'})
    assert page.status_code==503 and 'E-Mail-Versand ist momentan deaktiviert' in page.text

# Unified operational state: startup grace, outage and partial operation.
saved_started_at=main.APP_STARTED_AT
main.APP_STARTED_AT=datetime.now(timezone.utc)-timedelta(seconds=180)
op=main._operational_state(4,0,0,4)
assert op['label']=='Störung' and op['level']=='critical'
op=main._operational_state(4,2,0,2)
assert op['label']=='Teilbetrieb' and op['level']=='warning'
op=main._operational_state(4,4,0,0)
assert op['label']=='Betriebsbereit' and op['level']=='ok'
main.APP_STARTED_AT=datetime.now(timezone.utc)
op=main._operational_state(4,0,0,4)
assert op['label']=='Startphase' and op['startup']
main.APP_STARTED_AT=saved_started_at

# Offline active charge point escalates the operational aggregate to outage outside startup grace.
main.APP_STARTED_AT=datetime.now(timezone.utc)-timedelta(seconds=180)
db.upsert_charge_point('QA-CP',status='Offline',onboarded=1,ignored=0)
mailer.save_settings({'enabled':True,'host':'smtp.example.org','port':587,'security':'starttls','username':'','from_email':'laden@example.org','from_name':'Laden','admin_recipients':'','public_base_url':'','event_rfid_requests':True,'event_pin_reset_admin':True,'event_backup_failures':True,'event_security_warnings':False,'event_access_requests':True})
status=main._system_status_payload({'role':'admin'})
assert status['overall']['label']=='Störung'
db.upsert_charge_point('QA-CP',status='Available')
assert main._system_status_payload({'role':'admin'})['overall']['label']=='System OK'

# Backup/SFTP behavior from V0.9.7.26 remains intact.
backup.save_settings({'schedule_enabled':True,'frequency':'daily','time':'03:00','weekday':0,'retention_days':30,'internal_enabled':True,'external_enabled':False,'external_protocol':'ftps','external_host':'','external_port':21,'external_username':'','external_path':'/','external_tls_verify':True})
bs=main._latest_backup_status()
assert bs['level']=='warn' and bs['label']=='Noch kein Backup'
backup.save_settings({'schedule_enabled':True,'frequency':'daily','time':'03:00','weekday':0,'retention_days':30,'internal_enabled':True,'external_enabled':True,'external_protocol':'sftp','external_host':'backup.example.org','external_port':22,'external_username':'backup','external_path':'/ocpp','external_tls_verify':True,'external_password':'qa-secret'})
bcfg=backup.settings()
assert bcfg['external_protocol']=='sftp' and bcfg['external_port']==22 and bcfg['external_password_configured']
bs=main._latest_backup_status()
assert 'Intern + SFTP' in bs['detail']
now=datetime.now(timezone.utc).isoformat()
db.set_setting('backup_last_success_at',now); db.set_setting('backup_external_last_success_at',now)
db.set_setting('backup_external_last_error_at',''); db.set_setting('backup_external_last_error','')
bs=main._latest_backup_status()
assert bs['level']=='ok' and bs['label']=='Aktuell' and bs['external_protocol']=='SFTP'

# PWA stays conservative about dynamic/authenticated data.
login_src=(root/'app/templates/login.html').read_text(encoding='utf-8')
base_src=(root/'app/templates/base.html').read_text(encoding='utf-8')
assert 'window.formatEnergyKwh=' in base_src and "MWh" in base_src
for token in ['systemUpdateBadge','systemStatusUpdateLink','has-update','update_version','↑ V','has-app-background','has-header-background','--app-bg','--header-bg','powered by {{ branding.product_name }}']:
    assert token in base_src
assert 'powered by {{ branding.product_name }}' in login_src
assert '{% if branding.logo_light_url or branding.logo_dark_url %}<span class="brand-visual"' in base_src
assert '{% else %}<span><b>{{ branding.display_name }}</b><small>' in base_src
assert '{% else %}<div><strong>{{ branding.display_name }}</strong><small>' in base_src
assert 'brand{% if branding.logo_light_url or branding.logo_dark_url %} brand-logo-only{% endif %}' in base_src
assert '.brand.brand-logo-only{justify-content:center' in style_src
assert '.sidebar .brand.brand-logo-only{justify-content:center' in style_src
pwa=(root/'app/static/pwa-install.js').read_text(encoding='utf-8')
sw=(root/'app/static/service-worker.js').read_text(encoding='utf-8')
assert 'data-pwa-install' in login_src and 'data-pwa-install' in base_src
security_tpl=(root/'app/templates/security.html').read_text(encoding='utf-8')
assert 'securityEventPager' in security_tpl and 'securityEventPrev' in security_tpl and 'securityEventNext' in security_tpl
assert "event_page=" in security_tpl
assert 'beforeinstallprompt' in pwa and 'appinstalled' in pwa
for token in ['PushManager','Notification.requestPermission','/api/notifications/push/subscribe','/api/notifications/push/preferences','/api/notifications/push/test','if(sub&&!known)','await sub.unsubscribe()']:
    assert token in pwa
for token in ["ocpp-pwa-v74","self.addEventListener('push'","self.addEventListener('notificationclick'","url.pathname.startsWith('/api/')","req.mode==='navigate'"]:
    assert token in sw
for token in ['pushNotificationPanel','pushNotificationToggle','pushNotificationSeverity','pushNotificationTest']:
    assert token in base_src
info_tpl=(root/'app/templates/_info_modal.html').read_text(encoding='utf-8')
style_src=(root/'app/static/style.css').read_text(encoding='utf-8')
assert 'about-hero{% if not branding.logo_light_url and not branding.logo_dark_url %} no-logo{% endif %}' in info_tpl
assert '.about-version{align-self:start;justify-self:end;width:max-content' in style_src and '.about-hero.no-logo{grid-template-columns:minmax(0,1fr) auto}' in style_src
updates_tpl=(root/'app/templates/updates.html').read_text(encoding='utf-8')
assert 'installUpdate' in updates_tpl and '/api/updates/install' in updates_tpl and 'Pre-Update-Backup' in updates_tpl
monitor_tpl=(root/'app/templates/ocpp_monitor.html').read_text(encoding='utf-8')
assert 'diagnosticSummary' in monitor_tpl and 'OCPP-Verbindungen &amp; Diagnose' in monitor_tpl and 'dg.score' in monitor_tpl
charge_point_tpl=(root/'app/templates/charge_point.html').read_text(encoding='utf-8')
assert 'capabilityCheck' in charge_point_tpl and '/capabilities' in charge_point_tpl and 'OCPP-Fähigkeiten' in charge_point_tpl
assert 'learnedCapabilityGrid' in charge_point_tpl and 'resetLearnedCapabilities' in charge_point_tpl and '/capabilities/learned' in charge_point_tpl
assert 'OCPP: Verfügbar · physischer Fahrzeugstatus unbekannt' in charge_point_tpl
assert "Finishing:'Beendet'" in charge_point_tpl
assert 'Ladevorgang beendet · Anschluss noch belegt' in charge_point_tpl
assert 'Max. Leistung je Connector' in charge_point_tpl
assert 'formatEnergyKwh(x.energy_kwh,3,3)' in charge_point_tpl
assert 'formatEnergyKwh(meter,3,3)' in charge_point_tpl
assert "OCPP: Beendet" in charge_point_tpl
for p in ['charge_points.html','dashboard.html','transaction_detail.html','liveview.html']:
    tpl=(root/'app/templates'/p).read_text(encoding='utf-8')
    assert "Finishing:'Beendet'" in tpl and "Finishing:'Abschluss'" not in tpl
assert 'OCPP Available bedeutet nur: keine aktive OCPP-Session' in charge_point_tpl
assert 'Kein Fahrzeug angeschlossen' not in charge_point_tpl
transaction_detail_tpl=(root/'app/templates/transaction_detail.html').read_text(encoding='utf-8')
assert 'EmergencyStop · von Ladepunkt gemeldet' in transaction_detail_tpl
assert 'data-help-title="Meterstart"' in transaction_detail_tpl
assert 'data-help-title="Meterstop"' in transaction_detail_tpl
assert 'formatEnergyKwh(t.meter_start_kwh,3,3)' in transaction_detail_tpl
assert 'formatEnergyKwh(m.energy_kwh,3,3)' in transaction_detail_tpl
assert 'VoltCore hat den Not-Aus-Grund nicht selbst erzeugt' in transaction_detail_tpl
assert 'post_session_occupancy' in transaction_detail_tpl and 'Nachbelegung nach Sessionende' in transaction_detail_tpl
assert 'optional_status_fields = []' in ocpp_server_src and '"vendor_error_code"' in ocpp_server_src and 'physically disconnected' in ocpp_server_src
for token in ['Service & Wartung','Get / ChangeConfiguration','TriggerMessage','GetDiagnostics','UpdateFirmware','ReserveNow','serviceHistory','serviceReservations','/configuration/read','/configuration/change','/diagnostics','/firmware','/reservation']:
    assert token in charge_point_tpl
liveview_tpl=(root/'app/templates/liveview.html').read_text(encoding='utf-8')
assert "const formatEnergyKwh=" in liveview_tpl
assert "formatEnergyKwh(all.energy_kwh,1,3)" in liveview_tpl
for token in ['simple','standard','pro','people','dark','light','importantStrip','designSelect','LiveView / Kiosk 2.0','peopleCard','people-level','simple-page-grid','standard-page-grid','pro-page-grid','people-grid','dark-page-grid','light-page-grid','standard-main','standard-connector-id','pro-main','pro-health','pro-operating','pro-dashboard','function paged','progress_pct']:
    assert token in liveview_tpl
assert "cp.diagnostic?.level==='critical')return 'STÖRUNG'" not in liveview_tpl
charge_points_tpl=(root/'app/templates/charge_points.html').read_text(encoding='utf-8')
assert '<span>Max. je Connector</span>' in charge_points_tpl
assert 'Max. Leistung je Connector (kW)' in charge_points_tpl
assert 'Max. Leistung je Connector muss zwischen 0 und 1000 kW liegen' in charge_points_tpl
for token in ['OCPP-Erkennung entfernen','Technische OCPP-Erkennung entfernen','data-action="delete-device"','Historische Ladevorgänge, Messwerte, Statistiken und OCPP-Ereignisse bleiben vollständig erhalten']:
    assert token in charge_points_tpl
reports_tpl=(root/'app/templates/reports.html').read_text(encoding='utf-8')
assert 'formatEnergyKwh(s.energy_kwh,2,3)' in reports_tpl
for token in ['data-tab="sessions"','Einzelne Ladevorgänge','report-user-details','fmtDateTime','/transactions/']:
    assert token in reports_tpl
main_src=(root/'app/main.py').read_text(encoding='utf-8')
assert 'FastAPI(title="VoltCore"' in main_src
assert 'or "OCPP Backend"' not in main_src
for token in ['remote_configuration_read','remote_configuration_change','remote_trigger_message','remote_get_diagnostics','remote_update_firmware','remote_reserve_now','remote_cancel_reservation']:
    assert token in main_src
totp_src=(root/'app/totp.py').read_text(encoding='utf-8')
assert 'issuer or "VoltCore"' in totp_src and 'issuer or "OCPP Backend"' not in totp_src
for token in ['def _pdf_session_table','Einzelladungsnachweis','def _pdf_report_page','Berichtskontext','Zusammenfassung','onFirstPage=page_cb','_pdf_session_table(data["transactions"]']:
    assert token in main_src

engagement_tpl=(root/'app/templates/engagement.html').read_text(encoding='utf-8')
for token in ['XP &amp; Level','xpOverview','achievementLeaderboard','leaderboard_metrics','renderLeaderboardMetrics','rankableAchievementMetrics','total_progress_pct','next_level_total_xp','prompt_unplug_sessions','Stecker-Sprinter']:
    assert token in engagement_tpl
portal_tpl=(root/'app/templates/public_budgets.html').read_text(encoding='utf-8')
for token in ['ranking_metric','ranking_period','p.ranking.metrics','Dein Platz','board.metric==\'xp\'','Mein Ladeprofil','Deine Lade-DNA','Persönliche Rekorde','Monatsvergleich','Zuletzt freigeschaltet','total_progress_pct','class="xp-progress"','id="ranglisten"','action="#ranglisten"','portalGamificationReveal','Geheimes Achievement entdeckt!','/gamification-reveals/ack']:
    assert token in portal_tpl
style_src=(root/'app/static/style.css').read_text(encoding='utf-8')
assert ':root{--brand:#2563eb' in style_src
for token in ['background:var(--brand)','color:var(--brand)','accent-color:var(--brand)','var(--brand-soft)','var(--brand-border)']:
    assert token in style_src
assert "d.voucher_prefix||'VOLT'" in (root/'app/templates/settings.html').read_text(encoding='utf-8')
assert "d.primary_color||'#2563eb'" in (root/'app/templates/settings.html').read_text(encoding='utf-8')
settings_tpl=(root/'app/templates/settings.html').read_text(encoding='utf-8')
assert 'liveviewSettingsForm' in settings_tpl and 'liveview-preset-grid' in settings_tpl and '/api/settings/liveview' in settings_tpl and 'data-preset-card="people"' in settings_tpl and 'Wallboard · 6 Designs' in settings_tpl
for token in ['app_background','header_background','previewAppBackground','previewHeaderBackground','VOLT-AB12CD34']:
    assert token in settings_tpl
assert 'DRK-AB12CD34' not in settings_tpl
assert {'app_background_url','header_background_url'} <= set(db.branding_settings())
users_tpl=(root/'app/templates/users.html').read_text(encoding='utf-8')
assert 'formatEnergyKwh(p.energy_kwh||0,1,3)' in users_tpl
for token in ['deletePermanent','/delete-check','/permanent','deletePurge','/purge','Benutzer + komplette Historie löschen','Admin-Sonderlöschung','Nur deaktivieren','chargeAccessMode','allowed_charge_point_ids','Nur ausgewählte Ladepunkte','Nirgends freigegeben']:
    assert token in users_tpl
assert 'userImageInput' in users_tpl and 'user-photo-large' in users_tpl and '/api/users/'+"'"+'+uid+'+"'"+'/image' in users_tpl
container_workflow=(root/'.github/workflows/container.yml').read_text(encoding='utf-8')
assert 'workflow_run:' in container_workflow and 'workflows: ["CI"]' in container_workflow and "github.event.workflow_run.conclusion == 'success'" in container_workflow
assert "github.event.workflow_run.head_sha || 'main'" in container_workflow
release_workflow=(root/'.github/workflows/release.yml').read_text(encoding='utf-8')
assert 'workflow_run:' in release_workflow and 'workflows: ["Container"]' in release_workflow and 'gh release create' in release_workflow
assert "^# VoltCore · V" in release_workflow and "^# OCPP Backend · V" not in release_workflow
backup_src=(root/'app/backup.py').read_text(encoding='utf-8')
mailer_src=(root/'app/mailer.py').read_text(encoding='utf-8')
assert 'db.branding_settings().get("product_name") or "VoltCore"' in backup_src
assert 'branding.get("product_name") or "VoltCore"' in mailer_src

# Templates compile and rendered inline scripts parse.
env=Environment(loader=FileSystemLoader(root/'app/templates'))
for p in sorted((root/'app/templates').glob('*.html')):
    env.get_template(p.name)
branding=db.branding_settings()
auth={'id':admin_id,'role':'admin','username':'admin28','display_name':'Admin QA'}
class _TemplateRequest:
    query_params={}
template_request=_TemplateRequest()
contexts=[
    ('base.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'dashboard','access_request_open_count':0}),
    ('login.html',{'app_version':main.APP_VERSION,'branding':branding,'registration':db.registration_settings(),'login_reason':None,'error':None,'username':'','next_path':'/'}),
    ('login_2fa.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':None,'registration':db.registration_settings(),'display_name':'Admin QA','error':None}),
    ('account_security.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'','access_request_open_count':0,'two_factor':db.system_user_2fa_state(admin_id)}),
    ('backups.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'backups','access_request_open_count':0}),
    ('updates.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'updates','access_request_open_count':0,'update_repository':updates.REPOSITORY}),
    ('settings.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'settings','access_request_open_count':0}),
    ('users.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'users','access_request_open_count':0}),
    ('access_requests.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'access-requests','access_request_open_count':0,'counts':{'new':0,'review':0,'approved':0,'rejected':0}}),
    ('tariffs.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'tariffs','access_request_open_count':0}),
    ('cost_centers.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'cost-centers','access_request_open_count':0}),
    ('engagement.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'engagement','access_request_open_count':0}),
    ('liveview.html',{'app_version':main.APP_VERSION,'branding':branding,'auth_user':auth,'page':'liveview','access_request_open_count':0,'liveview_settings':db.liveview_settings(),'liveview_preview':'people'}),
    ('public_budgets.html',{'request':template_request,'app_version':main.APP_VERSION,'branding':branding,'portal_user':reveal_dash['user'],'portal_data':reveal_dash,'admin_preview':False,'enrollment_points':[],'enrollment_state':None,'active_rfid_count':0,'self_service_rfid_limit':2,'page':'public'}),
]
for name,ctx in contexts:
    rendered=env.get_template(name).render(**ctx)
    for i,script in enumerate(re.findall(r'<script>(.*?)</script>',rendered,re.S)):
        f=Path(tempfile.gettempdir())/('ocpp09728_'+name.replace('.','_')+'_'+str(i)+'.js')
        f.write_text(script,encoding='utf-8')
        subprocess.run(['node','--check',str(f)],check=True,capture_output=True,text=True)
subprocess.run(['node','--check',str(root/'app/static/pwa-install.js')],check=True,capture_output=True,text=True)

# Product credit is visible only when installation/display branding differs from the product name.
drk_brand={**branding,'display_name':'DRK Ladeinfrastruktur','product_name':'VoltCore','product_subtitle':'OCPP Backend'}
base_drk=env.get_template('base.html').render(app_version=main.APP_VERSION,branding=drk_brand,auth_user=auth,page='dashboard',access_request_open_count=0)
login_drk=env.get_template('login.html').render(app_version=main.APP_VERSION,branding=drk_brand,registration=db.registration_settings(),login_reason=None,error=None,username='',next_path='/')
assert base_drk.count('powered by VoltCore')>=2
assert 'powered by VoltCore' in login_drk
neutral_brand={**branding,'display_name':'VoltCore','product_name':'VoltCore','product_subtitle':'OCPP Charging Management'}
base_neutral=env.get_template('base.html').render(app_version=main.APP_VERSION,branding=neutral_brand,auth_user=auth,page='dashboard',access_request_open_count=0)
login_neutral=env.get_template('login.html').render(app_version=main.APP_VERSION,branding=neutral_brand,registration=db.registration_settings(),login_reason=None,error=None,username='',next_path='/')
assert 'powered by VoltCore' not in base_neutral and 'OCPP Charging Management' in base_neutral
assert 'powered by VoltCore' not in login_neutral and 'OCPP Charging Management' in login_neutral


# Real backup/restore roundtrip: snapshot -> mutation -> restore -> schema remains current.
db.set_setting('qa_restore_roundtrip','before')
restore_probe=backup.create_backup(label='qa-roundtrip',keep_local=True)
db.set_setting('qa_restore_roundtrip','after')
restore_result=backup.restore_backup(backup.backup_path(restore_probe['filename']))
assert db.get_setting('qa_restore_roundtrip')=='before'
assert restore_result.get('ok') and restore_result.get('safety_backup',{}).get('filename')
assert 'offline_auth_status' in {r[1] for r in sqlite3.connect(db.DB_PATH).execute("PRAGMA table_info(charge_point_local_list_state)").fetchall()}

print('V0.9.7.74 restart-safe post-session occupancy QA: PASS')
