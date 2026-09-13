#!/usr/bin/env node
'use strict';
/**
 * AICC Builder — ACXD deploy runner.
 *
 * Usage:
 *   node runner.js deploy   [--dry-run] [--manifest deploy-manifest.json]
 *   node runner.js status
 *   node runner.js cleanup  [--yes]
 *
 * Environment:
 *   ACXD_API_KEY        (required for deploy/cleanup; never stored/logged)
 *   ACXD_WORKSPACE_ID   (required for deploy/cleanup)
 *   ACXD_REGION         (ACXD API region; default us-west-2)
 *   AWS_DEFAULT_REGION  (default: ap-northeast-2)
 *   CONNECT_INSTANCE_ID (optional; enables contact flow import)
 *   PROJECT_NAME        (optional; deploy.sh exports it — the CloudFormation
 *                        stack becomes ${PROJECT_NAME}-stack, so a runner-only
 *                        deploy reuses deploy.sh's backend instead of creating
 *                        a second one)
 *   AICC_STACK_NAME     (optional; exact stack name, wins over PROJECT_NAME)
 *
 * This file is static and tested — regenerating a bundle never changes it.
 * The per-project behavior lives entirely in deploy-manifest.json.
 */

const path = require('path');
const readline = require('readline');
const { execFileSync } = require('child_process');

const { loadManifest, RESERVED_STEP_TYPES } = require('./lib/manifest');
const { createClient, maskSecret } = require('./lib/client');
const { loadState, saveState } = require('./lib/state');
const { STEPS, send } = require('./lib/steps');

function parseArgs(argv) {
  const args = { command: argv[0] || 'deploy', dryRun: false, yes: false,
                 manifest: 'deploy-manifest.json' };
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === '--dry-run') args.dryRun = true;
    else if (argv[i] === '--yes') args.yes = true;
    else if (argv[i] === '--manifest') args.manifest = argv[++i];
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  return args;
}

function log(line) {
  console.log(maskSecret(line));
}

function makeCtx(manifest, bundleDir, { client, sdk, acxdRegion } = {}) {
  return {
    bundleDir,
    // deploy.sh derives every resource name from PROJECT_NAME and exports it
    // (with AICC_STACK_NAME) before calling the runner, so the two entry points
    // name the same stack and the same contact flow. Without this a runner-only
    // deploy silently used the manifest's default project and stood up a second
    // backend (live 2026-09-13).
    project: process.env.PROJECT_NAME || manifest.project,
    // AWS region for CloudFormation / Connect calls. Live: with no manifest
    // region and a Seoul default profile, the Connect import ran against
    // ap-northeast-2 while the instance (and the ACXD workspace) lived in
    // us-east-1 — fall back to the ACXD region, which follows the instance.
    region: manifest.region || process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION
      || acxdRegion || 'us-east-1',
    log,
    client,
    sdk,
    state: loadState(bundleDir),
    exec: (cmd, cmdArgs) => execFileSync(cmd, cmdArgs, { encoding: 'utf-8' }),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    env: process.env,
  };
}

async function cmdDeploy(args, bundleDir) {
  const manifest = loadManifest(path.join(bundleDir, args.manifest));
  const project = process.env.PROJECT_NAME || manifest.project;
  log(`AICC Builder ACXD deploy — project '${project}', ${manifest.steps.length} step(s)`);
  if (project !== manifest.project) {
    log(`  (PROJECT_NAME=${project} overrides the manifest's '${manifest.project}')`);
  } else if (!process.env.PROJECT_NAME && !process.env.AICC_STACK_NAME) {
    // deploy.sh always exports these; a bare runner invocation does not, and
    // then it deploys its own '<manifest project>-stack'.
    log(`  Runner-only deploy: CloudFormation stack '${manifest.project}-stack'. To share ` +
        "deploy.sh's backend, export PROJECT_NAME (or AICC_STACK_NAME) first.");
  }

  if (args.dryRun) {
    log('DRY RUN — no changes will be made.\n');
    const ctx = makeCtx(manifest, bundleDir);
    manifest.steps.forEach((step, i) => {
      log(`Step ${i + 1}/${manifest.steps.length}: ${step.type}` +
          (step.description ? ` — ${step.description}` : ''));
      for (const line of STEPS[step.type].plan(ctx, step.params || {})) {
        log(`    ${line}`);
      }
    });
    log('\nDry run complete. Run without --dry-run to deploy.');
    return 0;
  }

  const { client, sdk, region: acxdRegion } = await createClient();
  log(`ACXD API region: ${acxdRegion} · workspace: ${process.env.ACXD_WORKSPACE_ID}`);
  const ctx = makeCtx(manifest, bundleDir, { client, sdk, acxdRegion });
  // Keep only non-secret deploy state. Credentials and the workspace identifier
  // are prompted/exported at runtime and never written into the bundle.
  ctx.state.acxdRegion = acxdRegion;
  saveState(bundleDir, ctx.state);
  for (let i = 0; i < manifest.steps.length; i++) {
    const step = manifest.steps[i];
    log(`\n[${i + 1}/${manifest.steps.length}] ${step.type}` +
        (step.description ? ` — ${step.description}` : ''));
    try {
      await STEPS[step.type].run(ctx, step.params || {});
      saveState(bundleDir, ctx.state);
    } catch (err) {
      saveState(bundleDir, ctx.state);
      log(`\nFAILED at step ${i + 1} (${step.type}): ${err.message}`);
      log('State was saved — fix the issue and re-run ./deploy.sh (steps are idempotent).');
      return 1;
    }
  }
  log('\n✅ Deploy complete.');
  if (ctx.state.applicationId) {
    log(`   Application: ${ctx.state.applicationName} (${ctx.state.applicationId})`);
  }
  if (ctx.state.aliasRotated) {
    log('   ⚠️  The deployment was REPLACED, so its deployment key (the Agentic CX block\'s');
    log('      Alias) changed. Re-select the alias in the block and publish, or run');
    log('      ./deploy.sh --rebind-alias <deploymentKey> — until then Connect serves the');
    log('      previous build. See WIRING-GUIDE.md.');
    return 0;
  }
  log('   Next: wire the Agentic CX block in your Connect contact flow (see WIRING-GUIDE.md).');
  return 0;
}

function cmdStatus(bundleDir) {
  const state = loadState(bundleDir);
  if (!state.resources.length) {
    log('No deployment state found. Nothing has been deployed from this bundle.');
    return 0;
  }
  log('Deployed resources (from .deploy-state.json):');
  for (const r of state.resources) {
    log(`  - ${r.kind}: ${r.id}${r.name ? ` (${r.name})` : ''}`);
  }
  if (state.webhookUrl) log(`  webhook base URL: ${state.webhookUrl}`);
  if (state.cfnStackName) log(`  CloudFormation stack: ${state.cfnStackName}`);
  if (state.buildId) log(`  latest build: ${state.buildId}`);
  if (state.deploymentId) log(`  latest deployment: ${state.deploymentId}`);
  if (state.aliasRotated) {
    const r = state.aliasRotation || {};
    log('  ⚠️  alias rotated: the last deploy REPLACED the ' +
        `'${r.environment || 'development'}' deployment ` +
        `(${r.previousDeploymentId || '?'} -> ${r.deploymentId || state.deploymentId})`);
    log('      The Agentic CX block still holds the previous deployment key, so Connect');
    log('      serves the PREVIOUS build. Re-select the alias in the block and publish, or');
    log('      run ./deploy.sh --rebind-alias <deploymentKey>.');
  }
  return 0;
}

/** Reverse-order teardown of everything recorded in state. */
async function cmdCleanup(args, bundleDir) {
  const state = loadState(bundleDir);
  if (!state.resources.length) {
    log('Nothing to clean up.');
    return 0;
  }
  if (!args.yes) {
    const confirmed = await confirm(
      `This will DELETE ${state.resources.length} resource(s) listed by 'status'. Type 'delete' to proceed: `);
    if (!confirmed) {
      log('Aborted.');
      return 1;
    }
  }
  const { client, sdk } = await createClient();
  const ctx = makeCtx({ project: 'cleanup', steps: [] }, bundleDir, { client, sdk });
  ctx.state = state;

  const deleters = {
    'deployment': (r) => send(ctx, 'DeleteApplicationDeploymentCommand',
      { applicationIdentifier: state.applicationId, deploymentIdentifier: r.id }),
    'application': (r) => send(ctx, 'DeleteApplicationCommand', { applicationIdentifier: r.id }),
    'guardrail': (r) => send(ctx, 'DeleteGuardrailCommand', { guardrailIdentifier: r.id }),
    'knowledge-base': (r) => send(ctx, 'DeleteKnowledgeBaseCommand', { knowledgeBaseId: r.id }),
    'flow': (r) => send(ctx, 'DeleteFlowCommand', { flowIdentifier: r.id }),
    'data-request': (r) => send(ctx, 'DeleteDataRequestCommand', { dataRequestIdentifier: r.id }),
    'slot-type': (r) => send(ctx, 'DeleteSlotTypeCommand', { slotTypeIdentifier: r.id }),
    'context-variable': (r) => send(ctx, 'DeleteContextVariableCommand', { name: r.id }),
    'secret': (r) => send(ctx, 'DeleteSecretCommand', { secretIdentifier: r.id }),
    'cfn-stack': (r) => ctx.exec('aws', ['cloudformation', 'delete-stack',
      '--stack-name', r.id, '--region', ctx.region]),
    'build': () => {},          // builds are immutable; deleted with the app
    'contact-flow': (r) => {
      const instanceId = process.env.CONNECT_INSTANCE_ID;
      if (!instanceId || !r.id) return;
      return ctx.exec('aws', ['connect', 'delete-contact-flow',
        '--instance-id', instanceId, '--contact-flow-id', r.id,
        '--region', ctx.region]);
    },
  };

  for (const resource of [...state.resources].reverse()) {
    const del = deleters[resource.kind];
    if (!del) continue;
    try {
      await del(resource);
      log(`  - deleted ${resource.kind} ${resource.id}`);
    } catch (err) {
      log(`  ! could not delete ${resource.kind} ${resource.id}: ${err.message}`);
    }
  }
  saveState(bundleDir, { knowledgeBases: {}, guardrails: {}, resources: [] });
  log('Cleanup finished.');
  return 0;
}

function confirm(prompt) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(prompt, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase() === 'delete');
    });
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const bundleDir = process.cwd();
  switch (args.command) {
    case 'deploy':
      return cmdDeploy(args, bundleDir);
    case 'status':
      return cmdStatus(bundleDir);
    case 'cleanup':
      return cmdCleanup(args, bundleDir);
    default:
      log(`unknown command: ${args.command} (expected deploy|status|cleanup)`);
      return 2;
  }
}

if (require.main === module) {
  main().then(
    (code) => process.exit(code),
    (err) => {
      console.error(maskSecret(err.stack || String(err)));
      process.exit(1);
    },
  );
}

module.exports = { parseArgs, makeCtx, cmdDeploy, cmdStatus, cmdCleanup };
