/** Configuration reconciliation keeps form references and optimistic revisions stable. */
const clone = value => JSON.parse(JSON.stringify(value));

export function reconcileConfig(app, next, preserveDraft = false) {
  const sameRevision = app.state?.revision === next.revision;
  app.state = next;
  if (preserveDraft && app.draft && (app.dirty || sameRevision)) {
    if (app.dirty && app.draftRevision !== next.revision) {
      app.staleRemote = true;
      app.conflict = true;
    }
    return false;
  }
  app.draft = clone(next.config);
  app.draftRevision = next.revision;
  app.draftEpoch = (app.draftEpoch || 0) + 1;
  app.dirty = false;
  app.conflict = false;
  app.staleRemote = false;
  app.clearSecrets = new Set();
  app.saveProblems = [];
  app.saveWarnings = [];
  return true;
}

export function draftPayload(app) {
  const config = clone(app.draft);
  if (app.budgetModeSupported === false) delete config.budget_mode;
  const result = {revision: app.draftRevision, config};
  const cleared = [...(app.clearSecrets || [])];
  if (cleared.length) result.clear_secrets = cleared;
  return result;
}

export function pollStillCurrent(app, epoch) {
  return !app.saving && app.draftEpoch === epoch;
}
