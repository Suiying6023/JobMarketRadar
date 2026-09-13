import assert from 'node:assert/strict';
import test from 'node:test';
import {reconcileConfig, draftPayload, pollStillCurrent} from '../src/job_radar/web/config-state.js';

const remote = (revision, batch = 20) => ({revision, config: {service: {batch_size: batch}}});

test('an unchanged poll preserves the object edited by mounted form controls', () => {
  const app = {};
  reconcileConfig(app, remote('r1'));
  const boundControl = app.draft.service;
  reconcileConfig(app, remote('r1'), true);
  boundControl.batch_size = 21;
  app.dirty = true;
  assert.equal(draftPayload(app).config.service.batch_size, 21);
});

test('remote edits cannot silently promote the revision of a dirty draft', () => {
  const app = {};
  reconcileConfig(app, remote('r1'));
  app.draft.service.batch_size = 21;
  app.dirty = true;
  reconcileConfig(app, remote('r2', 30), true);
  assert.equal(draftPayload(app).revision, 'r1');
  assert.equal(draftPayload(app).config.service.batch_size, 21);
  assert.equal(app.state.revision, 'r2');
  assert.equal(app.conflict, true);
});

test('a stale in-flight poll cannot replace a just-saved configuration', () => {
  const app = {};
  reconcileConfig(app, remote('r1'));
  const epoch = app.draftEpoch;
  reconcileConfig(app, remote('r2', 21));
  assert.equal(pollStillCurrent(app, epoch), false);
  assert.equal(app.draft.service.batch_size, 21);
});

test('a clean remote revision requests new form bindings', () => {
  const app = {};
  reconcileConfig(app, remote('r1'));
  const old = app.draft;
  assert.equal(reconcileConfig(app, remote('r2', 30), true), true);
  assert.notEqual(app.draft, old);
  assert.equal(draftPayload(app).revision, 'r2');
});
