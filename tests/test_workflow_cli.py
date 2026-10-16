import json

from radar import __main__ as cli, opportunities, task_research, opportunity_validation


def test_local_research_cli_does_not_require_paid_config(tmp_path, monkeypatch):
    received = []
    monkeypatch.setattr(task_research, 'command', lambda *a, **k: received.append((a, k)) or 0)
    assert cli.main(['task-research', '--state', str(tmp_path), '--config', str(tmp_path/'missing.json'),
                     '--research-action', 'plan', '--date', '2026-10-16', '--task-id', 'd-one', '--plan', str(tmp_path/'plan.json')]) == 0
    assert received[0][1]['task_ids'] == ['d-one']
    assert received[0][0][1] == tmp_path/'demand-ledger.json'


def test_validation_resume_cli_passes_explicit_rephrase(tmp_path, monkeypatch):
    cfg = tmp_path/'config.json'
    cfg.write_text('{}', encoding='utf-8')
    received = []
    monkeypatch.setattr(opportunity_validation, 'command', lambda *a, **k: received.append((a, k)) or 0)
    assert cli.main(['validate-opportunities', '--config', str(cfg), '--result', str(tmp_path/'result.json'),
                     '--trends-action', 'resume', '--item', 'v1', '--keyword', 'editable PDF outline']) == 0
    assert received[0][0][0] == 'resume'
    assert received[0][1] == {'item_id': 'v1', 'keyword': 'editable PDF outline'}


def test_status_is_local_and_reports_missing_day(tmp_path, capsys):
    assert cli.main(['workflow-status', '--state', str(tmp_path), '--date', '2026-10-16']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'not_started' and output['active_or_interrupted_lock'] is False


def test_continuing_validation_is_not_reported_as_a_new_opportunity():
    text = opportunities.render({'date': '2026-10-16', 'status': 'complete', 'opportunities': [
        {'title': 'Existing qualified task', 'is_new': False, 'validation_continuation': True, 'evidence': []}]})
    assert '继续验证 1 个' in text and '今天新增' not in text
