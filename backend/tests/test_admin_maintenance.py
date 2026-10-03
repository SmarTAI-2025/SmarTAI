"""Protected button uses disposable resources in every environment label."""
from dataclasses import replace
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.auth import create_token
from backend.config import settings
from backend.services import admin_reset as reset
from backend.tests.test_admin_reset import scope, rows
from backend.api import admin_maintenance as api

@pytest.fixture
def maintenance(scope, monkeypatch):
    monkeypatch.setattr(settings,"database_url",str(scope.engine.url))
    monkeypatch.setattr(api,"configured_scope",lambda: scope)
    api._future=None
    app=FastAPI();app.state.private_admin=True;app.state.maintenance_only=True
    app.include_router(api.router,prefix="/api")
    client=TestClient(app)
    client.headers.update({"Authorization": "Bearer "+create_token("test-owner","admin",auth_version=0),"Origin":"http://testserver"})
    yield client,scope
    if api._future: api._future.result(timeout=20)
    api._future=None

def payload(client):
    p=client.get('/api/admin/maintenance/preview').json()
    assert p['available'] and p['execution_available']
    return {"fingerprint":p["fingerprint"],"confirmation":p["confirmation"],"services_stopped":True,"maintenance_password":"isolated-maintenance-password"}

def completed(client):
    api._future.result(timeout=20)
    return client.get('/api/admin/maintenance/status').json()

@pytest.mark.parametrize('environment',['test','production'])
def test_same_protected_flow_and_post_wipe_cookie_status(maintenance,monkeypatch,environment):
    client,scope=maintenance
    scope=replace(scope,runtime_environment=environment)
    monkeypatch.setattr(api,'configured_scope',lambda:scope)
    body=payload(client)
    result=client.post('/api/admin/maintenance/execute',json=body)
    assert result.status_code==202,result.text
    cookie=result.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert completed(client)['status']=='completed'
    assert rows(scope)==0
    assert client.get('/api/admin/maintenance/preview').status_code==401
    # Completed receipt replay must not delete newly bootstrapped identities.
    from sqlalchemy import text
    with scope.engine.begin() as connection:
        connection.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,auth_version,is_read_only,created_at,updated_at) VALUES('new','reusable-name','reusable@example.invalid','fake','teacher',true,0,false,1,1)"))
    assert client.post('/api/admin/maintenance/execute',json=body).status_code==202
    assert completed(client)['status']=='completed' and rows(scope)==1
    receipts=list(scope.maintenance_dir.glob('receipt-*.json'))
    assert len(receipts)==1 and 'reusable' not in receipts[0].read_text()

def test_wrong_password_cancel_permission_and_origin_delete_nothing(maintenance):
    client,scope=maintenance;body=payload(client)
    body['maintenance_password']='wrong'
    assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_maintenance_password_invalid'
    body['maintenance_password']='isolated-maintenance-password';body['confirmation']='cancel'
    assert client.post('/api/admin/maintenance/execute',json=body).status_code==409
    body=payload(client)
    assert client.post('/api/admin/maintenance/execute',json=body,headers={'Origin':'https://evil.invalid'}).status_code==403
    from sqlalchemy import text
    with scope.engine.begin() as connection:connection.execute(text("UPDATE users SET role='teacher'"))
    assert client.post('/api/admin/maintenance/execute',json=body).status_code==403
    assert rows(scope)==1
    assert not list(scope.maintenance_dir.glob('receipt-*.json'))

def test_running_services_and_regular_private_app_block_execution(maintenance):
    client,scope=maintenance;body=payload(client)
    client.app.state.maintenance_only=False
    assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_offline_service_required'
    client.app.state.maintenance_only=True
    with reset.maintenance_service_guard(scope):
        assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_services_or_executor_running'
    assert rows(scope)==1

def test_stale_preview_and_failed_storage_resume_same_plan(maintenance,monkeypatch):
    client,scope=maintenance;body=payload(client)
    (scope.local_roots[0]/'changed.txt').write_text('synthetic')
    assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_preview_stale'
    body=payload(client)
    original=reset._purge_database
    monkeypatch.setattr(reset,'_purge_database',lambda _: (_ for _ in ()).throw(reset.ResetError('synthetic_failure')))
    assert client.post('/api/admin/maintenance/execute',json=body).status_code==202
    status=completed(client)
    assert status['status']=='failed' and status['error_code']=='synthetic_failure'
    assert rows(scope)==1
    with pytest.raises(reset.ResetError):
        with reset.maintenance_service_guard(scope):pass
    monkeypatch.setattr(reset,'_purge_database',original)
    assert client.post('/api/admin/maintenance/execute',json=body).status_code==202
    assert completed(client)['status']=='completed' and rows(scope)==0

def test_missing_independent_hash_and_bruteforce_fail_closed(maintenance,monkeypatch):
    client,scope=maintenance;body=payload(client)
    monkeypatch.setattr(api,'configured_scope',lambda:replace(scope,password_hash=''))
    assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_maintenance_password_not_configured'
    monkeypatch.setattr(api,'configured_scope',lambda:scope)
    for _ in range(5):
        assert client.post('/api/admin/maintenance/execute',json={**body,'maintenance_password':'wrong'}).json()['detail']['code']=='reset_maintenance_password_invalid'
    assert client.post('/api/admin/maintenance/execute',json=body).json()['detail']['code']=='reset_maintenance_password_rate_limited'
    assert rows(scope)==1
