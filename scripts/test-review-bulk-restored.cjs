// Exercise the restored HTML's actual initialization and JSON-export handler.
// No browser, network or production state is needed.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const expected = JSON.parse(fs.readFileSync(process.argv[3], 'utf8').replace(/^\uFEFF/, ''));
  const data = html.match(/<script\b[^>]*\bid="data"[^>]*>([\s\S]*?)<\/script>/)[1];
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
  const script = scripts.at(-1)[1];
  new vm.Script(script); // Parse the entire UI script as well.
  const prefix = script.slice(0, script.indexOf('// 묶음 카드'));
  const exportStart = script.indexOf("document.getElementById('export').addEventListener");
  const exportEnd = script.indexOf("document.getElementById('importBtn')", exportStart);
  assert(exportStart >= 0 && exportEnd > exportStart);
  for (const storageUnavailable of [false, true]) {
    let handler, downloaded;
    const context = vm.createContext({
      document: {
        getElementById(id) {
          if (id === 'data') return {textContent: data};
          assert.equal(id, 'export');
          return {addEventListener(event, callback) {assert.equal(event, 'click'); handler = callback;}};
        },
        createElement() { return {click() {}}; },
      },
      localStorage: {
        getItem() {if (storageUnavailable) throw Error('unavailable'); return null;},
        setItem() {if (storageUnavailable) throw Error('unavailable');},
        removeItem() {},
      },
      Blob, URL: {createObjectURL(blob) {downloaded = blob; return 'blob:synthetic';}, revokeObjectURL() {}},
      setTimeout() {},
    });
    vm.runInContext(prefix + '\n' + script.slice(exportStart, exportEnd), context);
    handler();
    const actual = JSON.parse(await downloaded.text());
    assert.deepEqual(actual.decisions, expected.decisions);
    assert.deepEqual(actual.document_notes, expected.document_notes);
    assert.deepEqual(actual.bulk_groups, expected.bulk_groups);
    vm.runInContext('S={groups:{},items:{},doc:{}};', context);
    handler();
    const cleared = JSON.parse(await downloaded.text());
    assert(cleared.decisions.every(r => !r.decision && !r.applied_via));
  }
  console.log(`PASS: ${expected.decisions.length} decisions and notes round-trip with and without browser storage; cleared rows retain no stale attribution.`);
}
main().catch(error => {console.error(error); process.exitCode = 1;});
