"""One-attempt cache for optional GoAny reads; never retry unknown external charges."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from . import spending


class RequestCache:
    def __init__(self, root: Path, *, allow_new: bool):
        self.root, self.allow_new = root, allow_new
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        from .goanyapi import GoAnyError
        lock = self.root / 'requests.lock'
        try:
            with lock.open('x', encoding='utf-8') as stream:
                stream.write('exclusive optional-provider request')
        except FileExistsError:
            raise GoAnyError('Optional provider cache active or interrupted; no request sent') from None
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def request(self, base, path, params, send):
        import json
        from .goanyapi import GoAnyError
        request = {'endpoint': spending._digest('external-endpoint', {'base': base}), 'path': path, 'params': params}
        key = spending._digest('external-request', request)
        record_path = self.root / (key + '.json')
        with self.locked():
            if record_path.exists():
                saved = json.loads(record_path.read_text(encoding='utf-8'))
                if saved.get('request') != request or saved.get('status') != 'complete':
                    raise GoAnyError('Previous optional-provider outcome unavailable; retry disabled')
                return saved['data'], saved['reported_cost_micros']
            if not self.allow_new:
                raise GoAnyError('Recovery only reuses existing optional-provider responses; new calls disabled')
            for file in self.root.glob('*.json'):
                if json.loads(file.read_text(encoding='utf-8')).get('status') != 'complete':
                    raise GoAnyError('An optional-provider charge is uncertain; further calls stopped')
            saved = {'request': request, 'status': 'pending', 'attempts': 1}
            spending.save(record_path, saved)
            try:
                data, cost = send(path, **params)
                if not isinstance(data, dict) or type(cost) is not int or cost < 0:
                    raise GoAnyError('Malformed optional-provider response')
            except Exception as exc:
                saved.update(status='failed', error_type=type(exc).__name__, charge_known=False)
                spending.save(record_path, saved)
                raise
            saved.update(status='complete', data=data, reported_cost_micros=cost,
                         charge_known=any(type(data.get(k)) is int and data[k] >= 0 and
                                          cost == data[k] * 1000 for k in ('costCredits', 'creditsUsed')),
                         billing_note='Provider-reported fee only; this is not AIsa billing or proof of all external charges')
            spending.save(record_path, saved)
            return data, cost

    def billing_summary(self):
        import json
        records = [json.loads(p.read_text(encoding='utf-8')) for p in self.root.glob('*.json')]
        return {'provider': 'GoAnyAPI', 'excluded_from_aisa': True,
                'scope': 'cumulative saved responses, not new charges of this recovery',
                'reported_usd': sum(r.get('reported_cost_micros', 0) for r in records if r.get('charge_known')) / 1e6,
                'unknown_charge_requests': sum(not r.get('charge_known', False) for r in records),
                'note': 'Missing provider fees remain unknown; successful cached requests are not resent.'}
