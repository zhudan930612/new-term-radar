import copy

import pytest

from radar import google_trends, opportunities, opportunity_validation as validation


def sample():
    return {"date": "2026-10-15", "status": "complete", "workflow_version": 2,
            "opportunities": [{"title": "Invoice job", "keywords": ["split invoices"],
                "evidence": [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]}]}


def observations(tmp_path, result):
    item = result["opportunity_validation"]["items"][0]
    directory = tmp_path / "validation-evidence"
    directory.mkdir()
    def saved(url, name):
        text = url + "\nAn actual documented user request"
        (directory / name).write_text(text, encoding="utf-8")
        return {"url": url, "evidence_file": "validation-evidence/" + name, "quote": "An actual documented user request"}
    return {"date": result["date"], "items": [{"id": item["id"], "status": "observed",
             "observed_at": "2026-10-15T12:00:00+08:00", "note": "Original context and intent inspected",
             "sources": [saved(url, f"source-{i}.txt") for i, url in enumerate(item["source_urls"])],
             "search": saved(item["search_url"], "search.txt"), "alternatives": [],
             "same_task": True, "independent_requests": True, "intent_match": True}]}


def test_trends_waits_for_context_and_intent_then_uses_only_validated_phrase(tmp_path):
    result = sample()
    validation.prepare(result)
    google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
    assert result["google_trends"]["status"] == "waiting_validation"
    assert result["google_trends"]["items"] == []
    validation.import_observations(result, observations(tmp_path, result), tmp_path)
    result.pop("google_trends")
    google_trends.prepare(result, {"google_trends": {"enabled": True, "geo": "US"}})
    assert len(result["google_trends"]["items"]) == 2
    assert all(i["keyword"] == "split invoices" for i in result["google_trends"]["items"])


@pytest.mark.parametrize("failure", ["fake_quote", "wrong_url", "wrong_day", "missing_source"])
def test_import_requires_saved_provenance(tmp_path, failure):
    result = sample()
    validation.prepare(result)
    payload = observations(tmp_path, result)
    row = payload["items"][0]
    if failure == "fake_quote": row["search"]["quote"] = "invented statement absent from saved evidence"
    if failure == "wrong_url": row["search"]["url"] = "https://example.com/unplanned"
    if failure == "wrong_day": payload["date"] = "2026-10-14"
    if failure == "missing_source": row["sources"] = row["sources"][:1]
    with pytest.raises(ValueError): validation.import_observations(result, payload, tmp_path)


def test_context_rejection_withdraws_but_bad_keyword_only_defers_trends(tmp_path):
    result = sample()
    validation.prepare(result)
    payload = observations(tmp_path, result)
    wrong_keyword = copy.deepcopy(result)
    payload["items"][0]["intent_match"] = False
    validation.import_observations(wrong_keyword, payload, tmp_path)
    assert wrong_keyword["opportunities"][0]["search_validation"]["status"] == "needs_rephrasing"
    assert wrong_keyword["opportunities"][0].get("review_status") != "withdrawn"
    payload["items"][0]["same_task"] = False
    validation.import_observations(result, payload, tmp_path)
    assert result["opportunities"][0]["review_status"] == "withdrawn"
    assert "没有新增" in opportunities.render(result)
    google_trends.prepare(result, {"google_trends": {"enabled": True}})
    assert not result["google_trends"]["items"]
