"""Plan-bound recovery tests never use live credentials or external providers."""
import copy
import json
from datetime import date

import pytest

from radar import aisa, daily_recovery as recovery, spending

DAY = date(2026, 10, 16)


@pytest.fixture
def saved(tmp_path, cfg):
    cfg = copy.deepcopy(cfg)
    cfg['demands']['enabled'] = True
    cfg['research']['enabled'] = False
    state = tmp_path / 'state'
    root = state / 'web-opportunities' / DAY.isoformat()
    root.mkdir(parents=True)
    spending.save(state / 'demand-ledger.json', {'version': 2, 'sources': {}, 'tasks': {}})
    row = {'id': 'e1', 'kind': 'post', 'text': 'I need to reconcile invoices manually.',
           'url': 'https://example.com/request', 'author': 'user:a'}
    result = {'date': DAY.isoformat(), 'workflow_version': 2, 'status': 'incomplete',
              'errors': ['source failed'], 'collection_errors': ['source failed'], 'selected': [],
              'evidence': [row], 'opportunities': [], 'billing': {'charged_usd': .001, 'after': {'balance_usd': 1}}}
    spending.save(root / 'result.json', result)
    recovery.save_plan(root, cfg, DAY, [], [], {'leads': []})
    return cfg, state, root, result


def test_resume_reuses_success_and_preserves_prior_result_and_scope(saved, monkeypatch, tmp_path):
    cfg, state, root, result = saved
    body = {'calls': [{'call_id': 'r', 'tool': 'get_reddit_search', 'arguments': {'query': 'invoice'}}]}
    response = {'results': [{'call_id': 'r', 'successful': True, 'customer_cost_micros_usd': 1000, 'data': {'posts': []}}]}
    monkeypatch.setattr(spending, 'quote', lambda _: .001)
    journal = spending.Journal(root / 'api', 1, {}, 1)
    journal.request(aisa.ROUTER, body, 1, lambda *a: response)
    old_journal = (root / 'api/requests.json').read_bytes()
    original_result = (root / 'result.json').read_bytes()
    monkeypatch.setattr(aisa, '_send', lambda *a: pytest.fail('Successful request must never be rebilled'))
    monkeypatch.setattr(spending, 'account', lambda: {'balance_usd': 1})
    monkeypatch.setattr(spending, 'model_prices', lambda _: {'input': .1, 'output': .2})

    def collect(*args):
        assert args[0] == [] and args[1] == DAY and args[3] == []
        assert aisa._REQUEST_SESSION.get().request(aisa.ROUTER, body, 1, aisa._send) == response
        return [result['evidence'][0]], [], {'queries': [], 'attempted_followups': []}

    monkeypatch.setattr(recovery.demand_sources, 'collect', collect)
    monkeypatch.setattr(recovery.demands, 'process', lambda *a: {'workflow_version': 2, 'analysis_errors': [],
        'tasks': [], 'dispositions': [], 'coverage': {}, 'opportunities': []})
    assert recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY) == 2
    final = recovery.read(root / 'result.json')
    assert final['status'] == 'awaiting_audit' and not final['errors']
    assert final['evidence'] == result['evidence']
    assert (root / 'recoveries/0001/result.json').read_bytes() == original_result
    assert (root / 'api/requests.json').read_bytes() == old_journal
    assert final['billing']['charged_usd'] == .001
    assert final['recovery_history'][0]['billing']['charged_usd'] == 0
    assert not (root / 'run.lock').exists()


def test_changed_scope_and_unknown_charge_block_before_account(saved, monkeypatch, tmp_path):
    cfg, state, root, _ = saved
    monkeypatch.setattr(spending, 'account', lambda: pytest.fail('Must block before controls or data'))
    changed = copy.deepcopy(cfg)
    changed['research']['videos_per_task'] += 1
    with pytest.raises(ValueError, match='scope or strategy'):
        recovery.resume_daily(changed, state, tmp_path / 'out.md', DAY)
    spending.save(root / 'api/requests.json', [{'status': 'pending', 'global_stop': True}])
    with pytest.raises(aisa.AisaError, match='billing outcome'):
        recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY)
    assert not (root / 'recoveries').exists()


def test_analysis_only_failure_goes_to_local_audit_without_paid_retry(saved, monkeypatch, tmp_path):
    cfg, state, root, result = saved
    result.update(collection_errors=[], errors=['task review pending'])
    spending.save(root / 'result.json', result)
    monkeypatch.setattr(spending, 'account', lambda: pytest.fail('Local audit needs no paid rerun'))
    assert recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY) == 2
    assert recovery.read(root / 'result.json')['errors'] == ['task review pending']


def test_attempt_is_not_confirmed_evidence_and_duplicate_resume_is_idempotent(tmp_path):
    ledger = tmp_path / 'demand-ledger.json'
    spending.save(ledger, {'version': 2, 'sources': {}, 'tasks': {'t': {'id': 't'}}})
    collection = {'attempted_followups': ['t'], 'evidence_followups': [],
                  'queries': [{'source': 'reddit', 'query': 'invoice reconciliation', 'task_id': 't', 'status': 'complete', 'returned': 0}]}
    recovery.record_attempts(ledger, collection, DAY)
    recovery.record_attempts(ledger, collection, DAY)
    task = recovery.read(ledger)['tasks']['t']
    assert task['search_attempts'] == 1 and len(task['search_history']) == 1
    assert task['last_search'] == DAY.isoformat() and 'last_evidence' not in task
    assert task['search_history'][0]['outcome'] == 'no_confirmed_evidence'


def test_recovery_child_queries_and_multiple_sources_are_one_round(tmp_path):
    ledger = tmp_path / 'demand-ledger.json'
    spending.save(ledger, {'version': 2, 'sources': {}, 'tasks': {'t': {'id': 't'}}})
    first = {'attempted_followups': ['t'], 'queries': [{'source': 'reddit', 'query': 'invoice', 'task_id': 't', 'status': 'complete', 'returned': 0}]}
    recovery.record_attempts(ledger, first, DAY, 'frozen-plan')
    completed = {'attempted_followups': ['t'], 'evidence_followups': ['t'], 'queries': first['queries'] + [
        {'source': 'youtube_comments', 'query': 'video', 'task_id': 't', 'status': 'complete', 'returned': 10}]}
    recovery.record_attempts(ledger, completed, DAY, 'frozen-plan')
    task = recovery.read(ledger)['tasks']['t']
    assert task['search_attempts'] == 1 and len(task['search_history'][0]['queries']) == 2
    assert len(task['search_history'][0]['query_events']) == 2


@pytest.mark.parametrize('lock_name', ['run.lock', 'trends-edit.lock', 'api/requests.lock'])
def test_complete_cached_recovery_respects_active_locks(saved, monkeypatch, tmp_path, lock_name):
    cfg, state, root, result = saved
    result.update(status='complete', errors=[])
    spending.save(root / 'result.json', result)
    before = (root / 'result.json').read_bytes()
    lock = root / lock_name
    lock.parent.mkdir(exist_ok=True)
    lock.write_text('active')
    monkeypatch.setattr(spending, 'account', lambda: pytest.fail('No account lookup'))
    with pytest.raises(aisa.AisaError, match='locks'):
        recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY)
    assert (root / 'result.json').read_bytes() == before


def test_new_optional_key_cannot_expand_resume_scope(saved, monkeypatch, tmp_path):
    cfg, state, root, _ = saved
    monkeypatch.setenv('GOANYAPI_API_KEY', 'synthetic-test-value')
    monkeypatch.setattr(spending, 'account', lambda: pytest.fail('No account lookup'))
    with pytest.raises(ValueError, match='provider availability'):
        recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY)


def test_disabled_workflow_resume_never_falls_back_to_legacy_paid_daily(tmp_path, cfg, monkeypatch):
    from argparse import Namespace
    from radar import __main__ as cli
    monkeypatch.setattr(cli.sources, 'x_new', lambda *a: pytest.fail('Legacy paid collector must not run'))
    with pytest.raises(ValueError, match='Resume requires'):
        cli.cmd_daily(Namespace(resume=True, skip=set()), cfg, tmp_path)


def test_optional_success_cache_reused_and_unknown_attempt_never_retried(tmp_path):
    from radar.external_cache import RequestCache
    from radar.goanyapi import GoAnyError
    cache = RequestCache(tmp_path / 'cache', allow_new=True)
    calls = []
    send = lambda *a, **k: calls.append(1) or ({'comments': [], 'costCredits': 2}, 2000)
    assert cache.request('https://example.invalid/', 'reviews', {'id': 'a'}, send)[1] == 2000
    resume = RequestCache(tmp_path / 'cache', allow_new=False)
    assert resume.request('https://example.invalid/', 'reviews', {'id': 'a'}, send)[1] == 2000
    with pytest.raises(GoAnyError, match='new calls disabled'):
        resume.request('https://example.invalid/', 'reviews', {'id': 'b'}, send)
    assert calls == [1]
    def fail(*a, **k):
        calls.append(1)
        raise TimeoutError('unknown charge')
    with pytest.raises(TimeoutError):
        cache.request('https://example.invalid/', 'reviews', {'id': 'b'}, fail)
    with pytest.raises(GoAnyError, match='retry disabled'):
        cache.request('https://example.invalid/', 'reviews', {'id': 'b'}, send)
    with pytest.raises(GoAnyError, match='uncertain'):
        cache.request('https://example.invalid/', 'reviews', {'id': 'c'}, send)
    assert calls == [1, 1]


def test_unknown_external_fee_is_not_reported_as_confirmed_free(tmp_path):
    from radar.external_cache import RequestCache
    cache = RequestCache(tmp_path, allow_new=True)
    cache.request('https://example.invalid/', 'reviews', {}, lambda *a, **k: ({'comments': []}, 0))
    summary = cache.billing_summary()
    assert summary['unknown_charge_requests'] == 1
    assert summary['excluded_from_aisa'] is True


def test_model_crash_keeps_new_sources_in_local_audit_scope(saved, monkeypatch, tmp_path):
    cfg, state, root, result = saved
    monkeypatch.setattr(spending, 'account', lambda: {'balance_usd': 1})
    monkeypatch.setattr(spending, 'model_prices', lambda _: {'input': .1, 'output': .2})
    added = {**result['evidence'][0], 'id': 'new', 'url': 'https://example.com/second',
             'text': 'We need to check invoice duplicates each month.', 'author': 'user:b'}
    monkeypatch.setattr(recovery.demand_sources, 'collect', lambda *a: ([added], [], {'queries': [], 'attempted_followups': []}))
    def crash(*a):
        raise ValueError('Synthetic preprocessing crash')
    monkeypatch.setattr(recovery.demands, 'process', crash)
    assert recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY) == 2
    saved_result = recovery.read(root / 'result.json')
    assert len(saved_result['evidence']) == len(saved_result['dispositions']) == 2
    assert saved_result['semantic_audit']['status'] == 'pending'
    assert saved_result['collection_errors'] == [] and saved_result['analysis_errors']
    assert {d['id'] for d in saved_result['dispositions']} == set(recovery.read(state / 'demand-ledger.json')['sources'])
    assert not (root / 'run.lock').exists()


def test_cached_resume_does_not_relabel_scheduled_execution_as_manual(saved, monkeypatch, tmp_path):
    cfg, state, root, result = saved
    result.update(status='complete', errors=[])
    spending.save(root / 'result.json', result)
    recovery.record_status(root, result, 'scheduled')
    receipt = (root / 'run-receipt.json').read_bytes()
    monkeypatch.setattr(spending, 'account', lambda: pytest.fail('No new execution'))
    assert recovery.resume_daily(cfg, state, tmp_path / 'out.md', DAY, 'manual') == 0
    assert (root / 'run-receipt.json').read_bytes() == receipt
