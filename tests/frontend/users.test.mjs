import test from 'node:test';
import assert from 'node:assert/strict';
import { UsersController } from '../../src/outage_explorer/frontend/static/outage_explorer/users.js';

test('closing generated credentials erases password values before acknowledging self-reset', () => {
  const operations = [];
  const input = { value: 'temporary-one-time-secret' };
  const owner = Object.assign(Object.create(UsersController.prototype), {
    awaitingSelfPasswordReset: true,
    credentialOpen: true,
    dialogGeneration: 3,
    dialog: {
      open: true,
      querySelectorAll: () => [input],
      replaceChildren: () => {
        assert.equal(input.value, '');
        operations.push('clear');
      },
      removeAttribute() {},
      close() {
        this.open = false;
        operations.push('close');
      },
    },
    context: {
      onSelfPasswordReset: () => {
        assert.equal(input.value, '');
        assert.equal(owner.credentialOpen, false);
        assert.equal(owner.awaitingSelfPasswordReset, false);
        operations.push('reauthenticate');
      },
    },
  });
  UsersController.prototype.closeDialog.call(owner);
  assert.deepEqual(operations, ['clear', 'close', 'reauthenticate']);
  assert.equal(owner.dialogGeneration, 4);
});

test('transition from password-reset confirmation retains self-reset latch until credentials acknowledged', () => {
  let notified = false;
  const owner = Object.assign(Object.create(UsersController.prototype), {
    awaitingSelfPasswordReset: true,
    dialogGeneration: 1,
    context: {
      onSelfPasswordReset: () => {
        notified = true;
      },
    },
  });
  UsersController.prototype.closeDialog.call(owner, { acknowledge: false });
  assert.equal(owner.awaitingSelfPasswordReset, true);
  assert.equal(notified, false);
  UsersController.prototype.closeDialog.call(owner);
  assert.equal(notified, true);
  assert.equal(owner.awaitingSelfPasswordReset, false);
});

test('late user-management responses require the active Admin view, request generation and live session', () => {
  const owner = {
    disposed: false,
    generation: 4,
    context: { alive: () => true, isView: (name) => name === 'users' },
  };
  assert.equal(UsersController.prototype.current.call(owner, 4), true);
  assert.equal(UsersController.prototype.current.call(owner, 3), false);
  owner.context.isView = () => false;
  assert.equal(UsersController.prototype.current.call(owner, 4), false);
  owner.context.isView = () => true;
  owner.context.alive = () => false;
  assert.equal(UsersController.prototype.current.call(owner, 4), false);
  owner.context.alive = () => true;
  owner.disposed = true;
  assert.equal(UsersController.prototype.current.call(owner, 4), false);
});

const generated = {
  user: { username: 'casey' },
  generated_password: 'temporary-one-time-secret-123456',
  reauthenticate: false,
};

function credentialOwner({ self = false } = {}) {
  const calls = [],
    renders = [],
    notices = [];
  const owner = Object.assign(Object.create(UsersController.prototype), {
    disposed: false,
    generation: 1,
    dialogGeneration: 1,
    foreground: false,
    view: 'users',
    awaitingSelfPasswordReset: self,
    context: {
      scope: { username: 'admin', persona: 'admin', session_scope: 'original-session' },
      alive: () => true,
      isView: (name) => name === owner.view,
      notice: (message) => notices.push(message),
      client: {
        request: async (path) => {
          calls.push(path);
          return {
            user: { username: 'admin', persona: 'admin' },
            session_scope: 'original-session',
          };
        },
      },
      onSelfPasswordReset: () => {
        owner.acknowledged = true;
      },
    },
    isForeground: () => owner.foreground,
    renderCredentials: (packet) => {
      owner.credentialOpen = true;
      owner.awaitingSelfPasswordReset = packet.reauthenticate;
      renders.push(packet);
    },
  });
  return { owner, calls, renders, notices };
}

test('a delayed credential reply after window blur remains outside the DOM until foreground session verification', async () => {
  const { owner, calls, renders } = credentialOwner();
  owner.showCredentials(generated);
  const pending = owner.pendingCredentials;
  assert.equal(renders.length, 0);
  assert.equal(calls.length, 0);
  assert.equal(pending.generated_password, generated.generated_password);
  assert.equal(owner.credentialOpen, undefined);
  owner.foreground = true;
  await owner.resumeCredentials();
  assert.deepEqual(calls, ['/auth/session']);
  assert.equal(renders.length, 1);
  assert.equal(renders[0].generated_password, generated.generated_password);
  assert.equal(owner.pendingCredentials, null);
  assert.equal(pending.generated_password, '');
  assert.equal(owner.pendingCredentialTimer, null);
  owner.clearCredentials();
});

test('leaving the Admin view removes pending password references and cancels later foreground delivery', async () => {
  const { owner, calls, renders } = credentialOwner();
  owner.showCredentials(generated);
  const pending = owner.pendingCredentials;
  owner.view = 'overview';
  owner.clearCredentials();
  owner.foreground = true;
  await owner.resumeCredentials();
  assert.equal(owner.pendingCredentials, null);
  assert.equal(pending.generated_password, '');
  assert.equal(owner.pendingCredentialTimer, null);
  assert.equal(calls.length, 0);
  assert.equal(renders.length, 0);
});

test('self-reset background delivery preserves its sign-in latch and avoids authenticated requests until acknowledgment', async () => {
  const { owner, calls, renders } = credentialOwner({ self: true });
  owner.showCredentials({ ...generated, reauthenticate: true });
  assert.equal(owner.awaitingSelfPasswordReset, true);
  assert.equal(renders.length, 0);
  owner.foreground = true;
  await owner.resumeCredentials();
  assert.equal(calls.length, 0);
  assert.equal(renders.length, 1);
  assert.equal(owner.awaitingSelfPasswordReset, true);
  assert.equal(owner.acknowledged, undefined);
  owner.clearCredentials();
  assert.equal(owner.awaitingSelfPasswordReset, false);
  assert.equal(owner.acknowledged, true);
});

test('an expired pending self-reset is erased and requires sign-in instead of revealing its password', async () => {
  const { owner, calls, renders } = credentialOwner({ self: true });
  owner.showCredentials({ ...generated, reauthenticate: true });
  const pending = owner.pendingCredentials;
  pending.expiresAt = Date.now() - 1;
  owner.foreground = true;
  await owner.resumeCredentials();
  assert.equal(pending.generated_password, '');
  assert.equal(owner.pendingCredentials, null);
  assert.equal(owner.pendingCredentialTimer, null);
  assert.equal(owner.awaitingSelfPasswordReset, false);
  assert.equal(owner.acknowledged, true);
  assert.equal(calls.length, 0);
  assert.equal(renders.length, 0);
});

test('a changed Admin session cannot reveal a retained credential reply', async () => {
  const { owner, renders } = credentialOwner();
  let checked = false;
  owner.context.client.request = async () => ({
    user: { username: 'admin', persona: 'viewer' },
    session_scope: 'replacement-session',
  });
  owner.context.onSessionCheckRequired = () => {
    checked = true;
  };
  owner.showCredentials(generated);
  const pending = owner.pendingCredentials;
  owner.foreground = true;
  await owner.resumeCredentials();
  assert.equal(pending.generated_password, '');
  assert.equal(owner.pendingCredentials, null);
  assert.equal(renders.length, 0);
  assert.equal(checked, true);
});

test('losing focus during pending session verification cannot insert credentials into the background DOM', async () => {
  const { owner, renders } = credentialOwner();
  let resolve;
  owner.context.client.request = () =>
    new Promise((done) => {
      resolve = done;
    });
  owner.showCredentials(generated);
  owner.foreground = true;
  const resumed = owner.resumeCredentials();
  await Promise.resolve();
  owner.foreground = false;
  resolve({ user: { username: 'admin', persona: 'admin' }, session_scope: 'original-session' });
  await resumed;
  assert.equal(renders.length, 0);
  assert.equal(owner.pendingCredentials.generated_password, generated.generated_password);
  owner.clearCredentials();
});

test('retiring the Admin view while session verification is in flight erases pending credentials before its result', async () => {
  const { owner, renders } = credentialOwner();
  let resolve;
  owner.context.client.request = () =>
    new Promise((done) => {
      resolve = done;
    });
  owner.showCredentials(generated);
  const pending = owner.pendingCredentials;
  owner.foreground = true;
  const resumed = owner.resumeCredentials();
  await Promise.resolve();
  owner.view = 'overview';
  owner.clearCredentials();
  resolve({ user: { username: 'admin', persona: 'admin' }, session_scope: 'original-session' });
  await resumed;
  assert.equal(pending.generated_password, '');
  assert.equal(owner.pendingCredentials, null);
  assert.equal(owner.pendingCredentialTimer, null);
  assert.equal(renders.length, 0);
});
