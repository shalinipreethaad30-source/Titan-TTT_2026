// Run: node scripts/test_iqf_f2_scan.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const base = fs.readFileSync(path.join(root, 'static/templates/base.html'), 'utf8');
const iqf = fs.readFileSync(path.join(root, 'static/templates/IQF/Iqf_PickTable.html'), 'utf8');
const marker = iqf.indexOf('<!-- ✅ GLOBAL SCAN INTEGRATION:');
const handler = iqf.slice(iqf.indexOf('>', iqf.indexOf('<script', marker)) + 1,
  iqf.indexOf('</script>', marker));
const dom = new JSDOM(`<!doctype html><table id="order-listing"><tbody>
  <tr data-stock-lot-id="LOT-32" data-batch-id="SHARED"><td><button class="tray-scan-btn">Audit</button>NB-A00032</td></tr>
  <tr class="gkb-row-focus" data-stock-lot-id="LOT-31" data-batch-id="SHARED"><td><button class="tray-scan-btn">Audit</button>NB-A00031</td></tr>
  </tbody></table>`, { runScripts: 'outside-only', url: 'http://localhost/iqf/iqf_picktable/' });
const win = dom.window;
win.console.log = () => {};
win.setTimeout = fn => fn();
win.eval(handler);
win.document.dispatchEvent(new win.Event('DOMContentLoaded'));
const rows = [...win.document.querySelectorAll('tbody tr')];
const opened = [];
rows.forEach(row => row.querySelector('button').addEventListener('click', () => opened.push(row.dataset.stockLotId)));
function scan(lot, module = 'IQF') {
  win.document.dispatchEvent(new win.CustomEvent('globalScan:success', {
    detail: { tray_id: lot === 'LOT-32' ? 'NB-A00032' : 'NB-A00031', lot_id: lot, module },
  }));
}
scan('LOT-31');
assert.deepEqual(opened.splice(0), ['LOT-31'], 'scanned second row must not open first row');
scan('LOT-32');
assert.deepEqual(opened.splice(0), ['LOT-32']);
scan('MISSING');
scan('');
scan('LOT-31', 'Brass QC');
assert.deepEqual(opened, [], 'missing lot and other modules must not open IQF');
rows[1].querySelector('button').disabled = true;
scan('LOT-31');
assert.deepEqual(opened, [], 'existing verification guard must remain intact');
rows[1].querySelector('button').disabled = false;
rows[1].classList.add('row-inactive');
scan('LOT-31');
assert.deepEqual(opened, [], 'held rows must remain blocked');

function baseFunction(name, next) {
  return base.slice(base.indexOf('  function ' + name + '('), base.indexOf('  function ' + next + '('));
}
win.eval(`function normalizeIdentifier(v) { return String(v || '').trim().toUpperCase(); }
  ${baseFunction('collectRowTokens', 'rowMatchesContext')}
  ${baseFunction('rowMatchesContext', 'findRowInRoot')}`);
const context = {
  identifiers: ['LOT-31', 'SHARED'], rowIdentifiers: ['LOT-31', 'SHARED'],
  responseData: { module: 'IQF', lot_id: 'LOT-31' },
};
assert.equal(win.rowMatchesContext(rows[0], context), false, 'shared batch cannot match sibling lot');
assert.equal(win.rowMatchesContext(rows[1], context), true);
context.responseData.module = 'Brass QC';
assert.equal(win.rowMatchesContext(rows[0], context), true, 'other-module matching stays unchanged');

win.eval(`var scanInput = {}, scanResult = {};
  function clearScanHighlights() {} function restorePrioritizedRow() {}
  function setInfo() {} function setScanMessage(v) { window.lastMessage = v; }
  function focusScanInput() {} function dispatchGlobalScanEvent() {}
  function logScan() {} function getCurrentTableId() {}
  ${baseFunction('handleTrayNotFound', 'handleTrayRestricted')}`);
win.handleTrayNotFound('NB-A99999');
assert.equal(win.lastMessage, 'Tray ID does not exist');
dom.reconfigure({ url: 'http://localhost/brass_qc/brass_picktable/' });
win.handleTrayNotFound('NB-A99999');
assert.equal(win.lastMessage, 'Not Exists');
dom.window.close();
console.log('PASS: IQF exact-row modal, sibling batch, missing/disabled/held rows, and other-module behavior');
