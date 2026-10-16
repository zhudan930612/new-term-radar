from radar import competition


def test_serp_description_survives_real_parser_for_tutorial_triage(monkeypatch):
    payload = {'serp': {'tasks': [{'status_code': 20000, 'result': [{'items': [
        {'type': 'organic', 'domain': 'youtube.com', 'url': 'https://youtu.be/abcdefghijk',
         'title': 'Spreadsheet formulas', 'description': 'Extend protected formulas when adding rows.', 'rank_group': 1}]}]}]}}
    monkeypatch.setattr(competition.aisa, 'call', lambda _: (payload, 2000))
    page, fee = competition.page_one('protected spreadsheet formulas', 2840)
    assert page['results'][0]['description'] == 'Extend protected formulas when adding rows.'
    assert fee == 2000
