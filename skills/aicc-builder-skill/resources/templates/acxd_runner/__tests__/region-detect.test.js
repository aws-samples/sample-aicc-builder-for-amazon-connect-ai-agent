// The ACXD API is regional and follows the workspace. A live deploy against a
// Seoul-created workspace died at its first ACXD step with Unauthorized
// because the runner assumed us-west-2. createClient must probe candidates.
const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

test('createClient probes candidate regions and picks the one that answers', async () => {
  const clientPath = path.resolve(__dirname, '..', 'lib', 'client.js');
  const attempts = [];
  // Inject a fake SDK: Unauthorized everywhere except ap-northeast-2.
  const Module = require('node:module');
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === 'amazon-connect-acxd-sdk') {
      return {
        AgenticCXDesignerClient: class {
          constructor({ region }) { this.region = region; }
          async send() {
            attempts.push(this.region);
            if (this.region !== 'ap-northeast-2') {
              const e = new Error('unauthorized'); e.name = 'UnauthorizedException'; throw e;
            }
            return { flows: [] };
          }
        },
        ListFlowsCommand: class { constructor(input) { this.input = input; } },
      };
    }
    return origLoad.apply(this, arguments);
  };
  try {
    delete require.cache[clientPath];
    const { createClient } = require(clientPath);
    process.env.ACXD_API_KEY = 'acxd_live_test';
    process.env.ACXD_WORKSPACE_ID = 'ws-test';
    process.env.AWS_DEFAULT_REGION = 'ap-northeast-2';
    delete process.env.ACXD_REGION;
    const { region } = await createClient();
    assert.strictEqual(region, 'ap-northeast-2');
    assert.ok(attempts.includes('ap-northeast-2'));
  } finally {
    Module._load = origLoad;
    delete require.cache[clientPath];
    delete process.env.ACXD_API_KEY;
    delete process.env.ACXD_WORKSPACE_ID;
  }
});
