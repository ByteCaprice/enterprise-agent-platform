"""Exercise the actual dependency-free panel's paging and stale-response guards."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_observation_panel_paging_failures_and_connection_changes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Console behavior check requires Node; no packages are installed automatically")
    source = Path(__file__).resolve().parents[1] / "interfaces/http/console/app.js"
    code = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function observationPanel(');
const end = source.indexOf('\nconst definitions =', start);
assert.ok(start >= 0 && end > start);
const responses = [], paths = [];
const context = {
  el: (tag, attrs, ...children) => ({tag, ...attrs, children,
    replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); }}),
  button: (label, click) => ({label, click}),
  note: (text) => ({textContent: text}),
  panel: (title, description, ...children) => ({title, description, children}),
  encode: encodeURIComponent, time: (value) => String(value), message: (error) => error.message,
  api: async (path) => { paths.push(path); return await responses.shift()(); },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
let current = true;
function fixture() {
  const view = context.observationPanel('/v2/tasks/task/deliveries', 'Delivery', 'Metadata', () => current, (row) => row);
  return {refresh: view.children[0].children[0], more: view.children[0].children[1],
    summary: view.children[1], error: view.children[2], rows: view.children[3]};
}
(async () => {
  const view = fixture();
  assert.equal(view.more.hidden, true);
  assert.equal(paths.length, 0); // No automatic I/O just by selecting a Task.
  responses.push(() => ({items: [{id: 'first'}], next_cursor: 'position:1', read_time: 'now'}));
  await view.refresh.click();
  assert.equal(view.rows.children[0].id, 'first');
  assert.equal(view.more.hidden, false);
  responses.push(() => { throw new Error('Permission revoked'); });
  await view.more.click();
  assert.equal(view.rows.children[0].id, 'first');
  assert.equal(view.error.textContent, 'Permission revoked');
  assert.equal(view.more.disabled, false);
  responses.push(() => ({items: [{id: 'second'}], next_cursor: null, read_time: 'later'}));
  await view.more.click();
  assert.equal(paths.at(-1), '/v2/tasks/task/deliveries?limit=50&cursor=position%3A1');
  assert.equal(view.rows.children.length, 1); // Bounded page replacement, not unbounded accumulation.
  assert.equal(view.rows.children[0].id, 'second');
  assert.equal(view.more.hidden, true);
  responses.push(() => ({items: [], next_cursor: null, read_time: 'later'}));
  await view.refresh.click();
  assert.equal(paths.at(-1), '/v2/tasks/task/deliveries?limit=50');
  assert.ok(view.rows.children[0].textContent);
  let finish;
  responses.push(() => new Promise((resolve) => { finish = resolve; }));
  const pending = view.refresh.click();
  assert.equal(view.refresh.disabled, true);
  const count = paths.length;
  await view.refresh.click();
  assert.equal(paths.length, count); // Double click cannot race cursors.
  current = false;
  finish({items: [{id: 'stale-private-record'}], next_cursor: null, read_time: 'later'});
  await pending;
  assert.ok(view.rows.children[0].textContent); // Old Task/connection response was discarded.
  await view.refresh.click();
  assert.equal(paths.length, count);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", code, str(source)], check=True, capture_output=True, text=True, timeout=10)
