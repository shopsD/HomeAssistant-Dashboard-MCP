"""Standalone regression checks; HA lifecycle still needs an integration test."""
import asyncio
import importlib
import math
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('dm_test'); pkg.__path__ = [str(ROOT)]; sys.modules['dm_test'] = pkg
# Only stub HA's storage/session/dispatcher boundary; import real catalogue/evaluator.
for name in ['homeassistant', 'homeassistant.helpers', 'homeassistant.helpers.aiohttp_client',
             'homeassistant.helpers.dispatcher', 'homeassistant.helpers.storage']:
    sys.modules[name] = types.ModuleType(name)

class Store:
    data = None
    def __init__(self, *args): pass
    async def async_load(self): return self.data
    async def async_save(self, data): self.data = data
    async def async_remove(self): self.data = None

sys.modules['homeassistant.helpers.storage'].Store = Store
sys.modules['homeassistant.helpers.dispatcher'].async_dispatcher_send = lambda *args: None
sys.modules['homeassistant.helpers.aiohttp_client'].async_get_clientsession = lambda hass: hass.session
cat = importlib.import_module('dm_test.catalogue')
emb = importlib.import_module('dm_test.embeddings')
const = importlib.import_module('dm_test.const')


def row(rid, name, text='', access='allowed', entity=''):
    return dict.fromkeys(cat.FIELDS, '') | dict(id=rid, name=name, text=text, access=access,
                                             entity_id=entity, section='Welcome Pack')


class RankingTests(unittest.TestCase):
    def test_semantic_match_without_keyword_overlap(self):
        r = row('1', 'Sink', 'The kitchen basin has a mixer.')
        vectors = {k: [1., 0.] for k in cat.search_chunks(r)}
        self.assertEqual(cat.ranked_search([r], 'tap'), [])
        self.assertEqual(cat.hybrid_search([r], 'tap', [1., 0.], vectors, .5), [r])

    def test_restricted_and_changed_content_do_not_match_old_vectors(self):
        old = row('1', 'Office', 'Private equipment')
        vectors = {k: [1., 0.] for k in cat.search_chunks(old)}
        for r in [old | {'access': 'restricted'}, old | {'access': 'redacted'},
                  old | {'text': 'New content'}]:
            self.assertEqual(cat.hybrid_search([r], 'workplace', [1., 0.], vectors, .5), [])

    def test_exact_entity_precedes_semantic_match(self):
        a = row('1', 'Lamp', entity='light.desk')
        b = row('2', 'Related', text='light.desk')
        vectors = {k: [1., 0.] for k in cat.search_chunks(b)}
        self.assertEqual(cat.hybrid_search([a,b], 'light.desk', [1.,0.], vectors, .5)[0], a)

    def test_redaction_before_embedding_and_no_state_fields(self):
        r = types.SimpleNamespace(id='1', fields=row('1','WiFi',"{{ states('text.secret') }}"))
        docs, _ = emb.EmbeddingIndex.documents([r], {'blocked_entities':['text.secret']})
        self.assertNotIn('text.secret', str(docs))
        self.assertIn('[REDACTED_ENTITY]', str(docs))
        self.assertEqual(cat.search_chunks(r.fields), cat.search_chunks(r.fields | {'live': 'new', 'condition': 'changed'}))

    def test_chunk_size_and_dedup(self):
        r = row('1', 'Long', ''.join(str(i) for i in range(5000)))
        self.assertTrue(all(len(s)<=2000 for s in cat.search_chunks(r).values()))
        vectors = {k:[1.,0.] for k in cat.search_chunks(r)}
        self.assertEqual(len(cat.hybrid_search([r], 'semantic', [1.,0.], vectors, .5)), 1)

    def test_invalid_vectors(self):
        for value in [[], [0,0], [math.nan], [True], ['1'], [math.inf]]:
            with self.assertRaises(ValueError): emb.unit_vector(value)
        self.assertEqual(emb.unit_vector([3,4]), [.6,.8])


class IndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = [types.SimpleNamespace(id='1', fields=row('1','Sink','Mixer tap'))]
        self.manager = types.SimpleNamespace(options=const.DEFAULT_OPTIONS | {
            'semantic_search':True, 'embedding_base_url':'http://localai/v1', 'embedding_model':'embed'}, active=True)
        async def snapshot(): return {}, [], {}, self.records, {}
        self.manager.snapshot = snapshot
        self.index = emb.EmbeddingIndex(types.SimpleNamespace(), types.SimpleNamespace(entry_id='test'), self.manager)
        self.calls = []
        async def request(texts, settings):
            self.calls.extend(texts)
            return [[1.,0.] for _ in texts]
        self.index.request = request

    async def test_build_reuse_delete_and_model_change(self):
        await self.index.update()
        self.assertEqual(self.index.status['state'], 'ready')
        self.assertEqual(self.index.status['indexed_records'], 1)
        count = len(self.calls)
        await self.index.update()
        self.assertEqual(len(self.calls), count)
        self.manager.options['embedding_model'] = 'new'
        self.index.invalidate()
        await self.index.update()
        self.assertGreater(len(self.calls), count)
        self.records = []
        await self.index.update()
        self.assertEqual(self.index.vectors, {})
        self.assertEqual(self.index.store.data['vectors'], {})

    async def test_restart_reuses_persisted_vectors(self):
        await self.index.update()
        saved = self.index.store.data
        fresh = emb.EmbeddingIndex(types.SimpleNamespace(), types.SimpleNamespace(entry_id='test'), self.manager)
        fresh.store.data = saved
        async def request(*args): self.fail('Unchanged documents must not be embedded again')
        fresh.request = request
        original = fresh.update
        async def update_once():
            await original()
            self.manager.active = False
            fresh.wake.set()
        fresh.update = update_once
        await fresh.run()
        self.assertEqual(fresh.status['state'], 'ready')

    async def test_failure_falls_back(self):
        await self.index.update()
        async def fail(*args): raise asyncio.TimeoutError()
        self.index.request = fail
        prepared = await self.index.query('Sink')
        self.assertIsNone(prepared)
        rows, backend = self.index.search([row('1','Sink')], 'Sink', prepared)
        self.assertEqual(backend, 'keyword')
        self.assertEqual(len(rows), 1)

    async def test_configuration_change_discards_inflight_batch(self):
        async def change(texts, settings):
            self.manager.options['blocked_entities'] = ['text.secret']
            self.index.invalidate()
            return [[1.,0.] for _ in texts]
        self.index.request = change
        await self.index.update()
        self.assertEqual(self.index.vectors, {})
        self.assertNotEqual(self.index.status['state'], 'ready')

    async def test_index_generation_invalidates_prepared_query(self):
        await self.index.update()
        prepared = await self.index.query('tap')
        self.index.invalidate()
        _, backend = self.index.search([row('1','Sink')], 'tap', prepared)
        self.assertEqual(backend, 'keyword')


if __name__ == '__main__': unittest.main()
