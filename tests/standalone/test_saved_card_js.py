# -*- coding: utf-8 -*-
"""#11 P2: la scelta della carta salvata non deve sbloccare il pagamento
quando il salvataggio in sessione fallisce.

Estrae lo script inline dal template e lo esegue in Node con un DOM/fetch
finti: errore di rete, HTTP negativo e JSON applicativo di errore devono
lasciare il click su "Paga" bloccato; una risposta positiva invece passa.
"""
import html
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ADDON_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = ADDON_ROOT / 'views' / 'payment_nexi_templates.xml'

# Harness Node: espone i globals che il template usa davvero (document,
# fetch) e simula i tre fallimenti del report.
HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

const els = {};
let clickCount = 0;
const listeners = {};

function makeEl(extra) {
  const e = Object.assign({
    dataset: {},
    classList: {contains: () => false},
    appendChild() {},
    textContent: '',
    className: '',
  }, extra || {});
  Object.defineProperty(e, 'id', {
    get() { return e._id; },
    set(v) { e._id = v; els[v] = e; },
  });
  return e;
}
const box = makeEl({dataset: {csrf: 'csrf-token'}});
box.appendChild = (child) => { box.children.push(child); };
box.children = [];
els['nexi-saved-cards'] = box;
const selected = makeEl({value: 'card-1'});
const button = makeEl({click: () => { clickCount += 1; }});
button.closest = (sel) => (sel.indexOf('button') === 0 ? button : null);
global.document = {
  readyState: 'complete',
  addEventListener: (type, fn) => { listeners[type] = fn; },
  getElementById: (id) => els[id] || null,
  querySelector: (sel) => (sel === '.nexi-saved-radio:checked' ? selected : null),
  createElement: () => makeEl(),
};

const okResponse = {ok: true, json: async () => ({result: {ok: true}})};
const httpError = {ok: false, json: async () => ({})};
const jsonError = {
  ok: true,
  json: async () => ({result: {error: 'Sessione scaduta. Ricarica la pagina.'}}),
};
function networkError() { return Promise.reject(new Error('offline')); }
function delayed(response, ms) {
  return () => new Promise((resolve) => setTimeout(() => resolve(response), ms));
}

const failure = {
  network: networkError,
  http: async () => httpError,
  json: async () => jsonError,
}[scenario.replace(/^update-/, '')] || null;

let fetchImpl = scenario.startsWith('update-')
  ? async () => okResponse
  : (failure || (async () => okResponse));
global.fetch = (...args) => fetchImpl(...args);

const changeTarget = {
  value: 'card-2',
  classList: {contains: (c) => c === 'nexi-saved-radio'},
  closest: () => null,
};
function clickEvent() {
  return {target: button, preventDefault() {}, stopImmediatePropagation() {}};
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  vm.runInThisContext(code, {filename: 'saved_card.js'});
  await sleep(40);   // salvataggio iniziale della scelta mostrata
  if (scenario.startsWith('update-')) {
    fetchImpl = scenario === 'update-ok' ? delayed(okResponse, 10) : failure;
    listeners.change({target: changeTarget});
    if (scenario === 'update-ok') {
      listeners.click(clickEvent());     // pending: deve aspettare e poi passare
      await sleep(120);
    } else {
      await sleep(40);                   // l'errore arriva prima del click
      listeners.click(clickEvent());
      await sleep(40);
    }
  } else {
    listeners.click(clickEvent());
    await sleep(40);
  }
  if (scenario === 'update-ok') {
    assert.strictEqual(clickCount, 1, 'una risposta valida deve sbloccare il pagamento');
  } else {
    assert.strictEqual(clickCount, 0, 'il pagamento non deve partire');
    assert.ok(els['nexi-saved-cards-error'], 'deve comparire un messaggio di errore');
    assert.ok(els['nexi-saved-cards-error'].textContent.length > 0);
  }
  console.log('PASS ' + scenario);
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
'''


def _saved_card_script():
    raw = TEMPLATE.read_text(encoding='utf-8')
    for match in re.finditer(r'<script>(.*?)</script>', raw, re.S):
        if 'nexi-saved-card' in match.group(1):
            return html.unescape(match.group(1))
    raise AssertionError('script della carta salvata non trovato nel template')


@unittest.skipUnless(shutil.which('node'), 'node non installato')
class SavedCardScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix='nexi_js_'))
        (cls.tmp / 'saved_card.js').write_text(
            _saved_card_script(), encoding='utf-8')
        (cls.tmp / 'harness.js').write_text(HARNESS, encoding='utf-8')

    def _run(self, scenario):
        proc = subprocess.run(
            ['node', str(self.tmp / 'harness.js'),
             str(self.tmp / 'saved_card.js'), scenario],
            capture_output=True, text=True, timeout=60,
            cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('PASS ' + scenario, proc.stdout)

    def test_initial_save_failures_block_payment(self):
        for scenario in ('network', 'http', 'json'):
            with self.subTest(scenario=scenario):
                self._run(scenario)

    def test_failed_update_blocks_payment(self):
        for scenario in ('update-network', 'update-http', 'update-json'):
            with self.subTest(scenario=scenario):
                self._run(scenario)

    def test_successful_update_lets_payment_through(self):
        self._run('update-ok')


if __name__ == '__main__':
    unittest.main()
