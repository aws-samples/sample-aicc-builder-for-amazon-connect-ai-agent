'use strict';
/**
 * Deploy step library. Static, tested code — the LLM never writes deploy
 * logic, it only selects these steps via the manifest (decision D2).
 *
 * Each step: { plan(ctx, params) -> string[], run(ctx, params) -> void }.
 * ``plan`` must be side-effect free (used by --dry-run).
 *
 * ctx = {
 *   bundleDir, project, region, log(line),
 *   client, sdk,           // ACXD SDK (fake-injectable)
 *   state,                 // lib/state.js object (mutated by steps)
 *   exec(cmd, args) -> string,   // external CLI (aws) — fake-injectable
 *   sleep(ms), env,        // injectables
 * }
 */

const fs = require('fs');
const path = require('path');

const { resolveFiles } = require('./manifest');
const { sendWithRetry, upsert, poll } = require('./client');
const { recordResource, resolvePlaceholders } = require('./state');

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf-8'));
}

function listFiles(ctx, params) {
  const files = resolveFiles(ctx.bundleDir, params.files || []);
  for (const f of files) {
    if (!fs.existsSync(f)) {
      throw new Error(`asset file not found: ${f}`);
    }
  }
  return files;
}

function send(ctx, CommandName, input) {
  const Command = ctx.sdk[CommandName];
  if (!Command) throw new Error(`SDK command not found: ${CommandName}`);
  return sendWithRetry(ctx.client, new Command(input), { sleep: ctx.sleep });
}

/** List every page of a paginated List* command. */
async function listAll(ctx, CommandName, input = {}) {
  const items = [];
  let nextToken;
  do {
    const resp = await send(ctx, CommandName, { ...input, nextToken });
    items.push(...(resp.items || []));
    nextToken = resp.nextToken;
  } while (nextToken);
  return items;
}

/** Resolve bundle placeholders, preferring a WEBHOOK_URL supplied by deploy.sh. */
function resolveAssetPlaceholders(doc, ctx) {
  const webhookUrl = ctx.state.webhookUrl || (ctx.env && ctx.env.WEBHOOK_URL);
  const state = webhookUrl === ctx.state.webhookUrl
    ? ctx.state
    : { ...ctx.state, webhookUrl };
  return resolvePlaceholders(doc, state);
}

// ---------------------------------------------------------------------------
// Backend (CloudFormation) steps — reuse the Classic aws-cli approach (D8)
// ---------------------------------------------------------------------------

const deployCfnBackend = {
  plan(ctx, params) {
    const stack = params.stackName || `${ctx.project}-acxd-backend`;
    const lines = [`deploy CloudFormation stack '${stack}' from ${params.templatePath}`];
    for (const dir of params.lambdaDirs || []) {
      lines.push(`update Lambda code from ${dir}`);
    }
    return lines;
  },
  async run(ctx, params) {
    const stack = params.stackName || `${ctx.project}-acxd-backend`;
    if (ctx.env && ctx.env.AICC_CFN_ALREADY_DEPLOYED === '1') {
      if (!ctx.env.WEBHOOK_URL) {
        throw new Error(
          'AICC_CFN_ALREADY_DEPLOYED=1 requires WEBHOOK_URL from the shared CloudFormation output');
      }
      ctx.state.cfnStackName = stack;
      ctx.log(`  = using shared CloudFormation backend '${stack}'`);
      return;
    }
    const template = path.join(ctx.bundleDir, params.templatePath);
    if (!fs.existsSync(template)) {
      throw new Error(`CloudFormation template not found: ${template}`);
    }
    ctx.exec('aws', [
      'cloudformation', 'deploy',
      '--template-file', template,
      '--stack-name', stack,
      '--capabilities', 'CAPABILITY_NAMED_IAM',
      '--no-fail-on-empty-changeset',
      '--region', ctx.region,
    ]);
    ctx.state.cfnStackName = stack;
    recordResource(ctx.state, 'cfn-stack', stack);
    for (const dir of params.lambdaDirs || []) {
      const absDir = path.join(ctx.bundleDir, dir);
      if (!fs.existsSync(absDir)) {
        // The CloudFormation template ships a placeholder handler that returns
        // HTTP 501; without the real code the deploy "succeeds" and every tool
        // call fails. Fail here instead.
        throw new Error(
          `Lambda source directory not found: ${absDir}. The bundle must ship ` +
          'real handler code — the CloudFormation placeholder answers every ' +
          'tool call with HTTP 501 "Upload Lambda code from lambda/ folder".');
      }
      const fnName = resolveLambdaName(ctx, stack, path.basename(dir), params.environment || 'dev');
      const zipPath = path.join(ctx.bundleDir, `${path.basename(dir)}.zip`);
      ctx.exec('sh', ['-c',
        `cd ${JSON.stringify(absDir)} && zip -qr ${JSON.stringify(zipPath)} .`]);
      ctx.exec('aws', [
        'lambda', 'update-function-code',
        '--function-name', fnName,
        '--zip-file', `fileb://${zipPath}`,
        '--region', ctx.region,
      ]);
      fs.rmSync(zipPath, { force: true });
      ctx.log(`  ~ updated Lambda code: ${fnName}`);
    }
  },
};

const wireWebhookUrls = {
  plan(ctx, params) {
    const key = (params && params.outputKey) || 'ApiEndpoint';
    return [`read CFN output '${key}' and record it as {WEBHOOK_URL}`];
  },
  async run(ctx, params) {
    const key = (params && params.outputKey) || 'ApiEndpoint';
    const supplied = ctx.env && ctx.env.WEBHOOK_URL;
    if (supplied) {
      ctx.state.webhookUrl = supplied.replace(/\/$/, '');
      ctx.log(`  = webhook base URL: ${ctx.state.webhookUrl} (WEBHOOK_URL)`);
      return;
    }
    const stack = ctx.state.cfnStackName;
    if (!stack) {
      throw new Error('wire-webhook-urls requires deploy-cfn-backend to run first');
    }
    const out = readCfnOutput(ctx, stack, key);
    if (!out || out === 'None') {
      throw new Error(`CFN stack '${stack}' has no output '${key}'`);
    }
    ctx.state.webhookUrl = out;
    ctx.log(`  = webhook base URL: ${out}`);
    readBackendApiKey(ctx, stack);
  },
};

// The template names functions `${ProjectName}-${Environment}-<op-with-hyphens>`
// while the bundle's lambda directories are `<op_with_underscores>`; deploy.sh
// resolves the real names from the stack, so must the runner-only path. Match
// the physical id of the stack's AWS::Lambda::Function resources on the
// normalised operation name; fall back to the historical convention.
function resolveLambdaName(ctx, stack, dirName, environment) {
  const norm = (v) => String(v || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const wanted = norm(dirName);
  try {
    const raw = ctx.exec('aws', [
      'cloudformation', 'describe-stack-resources',
      '--stack-name', stack,
      '--query', "StackResources[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId",
      '--output', 'text',
      '--region', ctx.region,
    ]).trim();
    const names = raw ? raw.split(/\s+/).filter(Boolean) : [];
    const hit = names.find((n) => norm(n).includes(wanted));
    if (hit) return hit;
    if (names.length) ctx.log(`  ! no stack Lambda matches '${dirName}' (have: ${names.join(', ')})`);
  } catch (err) {
    ctx.log(`  ! could not list stack Lambdas: ${err.message || err}`);
  }
  return `${ctx.project}-${dirName}-${environment}`;
}

function readCfnOutput(ctx, stack, key) {
  return ctx.exec('aws', [
    'cloudformation', 'describe-stacks',
    '--stack-name', stack,
    '--query', `Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue`,
    '--output', 'text',
    '--region', ctx.region,
  ]).trim();
}

// The generated API Gateway requires its key (ACXD target); the Data Requests
// send it from the BackendApiKey secret. When deploy.sh did not hand the value
// over as ACXD_SECRET_BACKENDAPIKEY (runner-only deploys), take it from the
// stack's ApiKeyValue output. Kept in memory only — never written to the state
// file or the log.
function readBackendApiKey(ctx, stack) {
  if (ctx.env && ctx.env.ACXD_SECRET_BACKENDAPIKEY) return;
  try {
    const value = readCfnOutput(ctx, stack, 'ApiKeyValue');
    if (value && value !== 'None' && value !== 'RETRIEVE_FAILED') {
      ctx.backendApiKey = value;
      ctx.log('  = BackendApiKey value taken from CFN output ApiKeyValue (not stored)');
    } else {
      ctx.log('  ! CFN output ApiKeyValue missing — the BackendApiKey secret will need ACXD_SECRET_BACKENDAPIKEY');
    }
  } catch (err) {
    ctx.log(`  ! could not read ApiKeyValue: ${err.message || err}`);
  }
}

// ---------------------------------------------------------------------------
// ACXD resource upserts
// ---------------------------------------------------------------------------

/** Build a simple Get/Create/Update upsert step for ID-addressable resources. */
function idUpsertStep({ label, idField, identifierField, getCmd, createCmd, updateCmd, deleteCmd, kind, transform }) {
  return {
    plan(ctx, params) {
      return listFiles(ctx, params).map((f) => `upsert ${label} from ${path.relative(ctx.bundleDir, f)}`);
    },
    async run(ctx, params) {
      for (const file of listFiles(ctx, params)) {
        let doc = readJson(file);
        if (transform) doc = transform(doc, ctx);
        const id = doc[idField];
        await upsert({
          label: `${label} ${id}`,
          log: ctx.log,
          get: () => send(ctx, getCmd, { [identifierField]: id }),
          create: () => send(ctx, createCmd, doc),
          update: () => {
            const { [idField]: _omit, ...rest } = doc;
            return send(ctx, updateCmd, { [identifierField]: id, ...rest });
          },
          // A create that fails validation part-way can leave a shell record:
          // Get succeeds but Update then fails with
          //   "The item is not found by key {...treeId: 'x-Omni'}".
          // Observed live on 2026-09-05. Heal it by recreating in place
          // instead of dead-ending an otherwise idempotent re-run.
          recreate: deleteCmd
            ? async () => {
                await send(ctx, deleteCmd, { [identifierField]: id });
                return send(ctx, createCmd, doc);
              }
            : undefined,
        });
        recordResource(ctx.state, kind, id);
      }
    },
  };
}

const upsertSlotTypes = idUpsertStep({
  label: 'slot type', kind: 'slot-type',
  idField: 'slotTypeId', identifierField: 'slotTypeIdentifier',
  getCmd: 'GetSlotTypeCommand', createCmd: 'CreateSlotTypeCommand',
  updateCmd: 'UpdateSlotTypeCommand',
  deleteCmd: 'DeleteSlotTypeCommand',
});

const upsertDataRequests = idUpsertStep({
  label: 'data request', kind: 'data-request',
  idField: 'dataRequestId', identifierField: 'dataRequestIdentifier',
  getCmd: 'GetDataRequestCommand', createCmd: 'CreateDataRequestCommand',
  updateCmd: 'UpdateDataRequestCommand',
  deleteCmd: 'DeleteDataRequestCommand',
  transform: (doc, ctx) => resolveAssetPlaceholders(doc, ctx),
});

// KnowledgeBaseNodeConfig keys the service accepts (SDK models_0.d.ts). The
// service also REQUIRES `name` although the type marks it optional — the second
// real deployment failed with "metadata.knowledgeBase.name is required".
const KB_NODE_KEYS = new Set(['name', 'knowledgeBaseId', 'prompt', 'question',
  'includeCitation', 'timeout', 'minConfidenceScore', 'brandId', 'filters']);

/** Make knowledge_base nodes deployable: name from the {KB:<name>} placeholder, known keys only. */
function normalizeFlowForService(rawDoc, doc) {
  const rawNodes = (rawDoc && rawDoc.nodes) || {};
  for (const [nodeId, node] of Object.entries(doc.nodes || {})) {
    if (!node || node.type !== 'knowledge_base' || !node.metadata) continue;
    const kb = node.metadata.knowledgeBase;
    if (!kb || typeof kb !== 'object') continue;
    if (!kb.name) {
      const rawKb = rawNodes[nodeId] && rawNodes[nodeId].metadata && rawNodes[nodeId].metadata.knowledgeBase;
      const m = rawKb && typeof rawKb.knowledgeBaseId === 'string' && rawKb.knowledgeBaseId.match(/^\{KB:([^}]+)\}$/);
      if (m) kb.name = m[1];
    }
    for (const key of Object.keys(kb)) if (!KB_NODE_KEYS.has(key)) delete kb[key];
  }
  return doc;
}

const upsertFlows = idUpsertStep({
  label: 'flow', kind: 'flow',
  idField: 'flowId', identifierField: 'flowIdentifier',
  getCmd: 'GetFlowCommand', createCmd: 'CreateFlowCommand',
  updateCmd: 'UpdateFlowCommand',
  deleteCmd: 'DeleteFlowCommand',
  transform: (doc, ctx) => normalizeFlowForService(doc, resolveAssetPlaceholders(doc, ctx)),
});

/** Name-addressable resources (no get-by-name): list + match. */
const upsertSecrets = {
  plan(ctx, params) {
    return listFiles(ctx, params).map((f) => `upsert secret from ${path.relative(ctx.bundleDir, f)} (value from env)`);
  },
  async run(ctx, params) {
    const existing = await listAll(ctx, 'ListSecretsCommand');
    for (const file of listFiles(ctx, params)) {
      const doc = readJson(file);
      // Secret values are NEVER stored in the bundle: read from env.
      const envVar = doc.valueEnv || `ACXD_SECRET_${String(doc.name || '').toUpperCase()}`;
      const value = ctx.env[envVar] || (doc.name === 'BackendApiKey' ? ctx.backendApiKey : undefined);
      if (!value) {
        ctx.log(`  ! skipping secret '${doc.name}': env ${envVar} not set`);
        continue;
      }
      const match = existing.find((s) => s.name === doc.name);
      if (match) {
        await send(ctx, 'UpdateSecretCommand', { secretIdentifier: match.secretId || doc.name, value });
        ctx.log(`  ~ updated secret ${doc.name}`);
      } else {
        await send(ctx, 'CreateSecretCommand', { name: doc.name, value, description: doc.description });
        ctx.log(`  + created secret ${doc.name}`);
      }
      recordResource(ctx.state, 'secret', doc.name);
    }
  },
};

const upsertContextVariables = {
  plan(ctx, params) {
    return listFiles(ctx, params).map((f) => `upsert context variables from ${path.relative(ctx.bundleDir, f)}`);
  },
  async run(ctx, params) {
    const existing = await listAll(ctx, 'ListContextVariablesCommand');
    for (const file of listFiles(ctx, params)) {
      const docs = [].concat(readJson(file));
      for (const doc of docs) {
        const match = existing.find((v) => v.name === doc.name);
        if (match) {
          // UpdateContextVariableRequest keys the variable by
          // contextVariableIdentifier (its name); `name` is not an input.
          // Live: the second run of a bundle failed here with "No value
          // provided for input HTTP label: contextVariableIdentifier".
          const { name, type, ...rest } = doc;
          await send(ctx, 'UpdateContextVariableCommand', { contextVariableIdentifier: name, ...rest });
          ctx.log(`  ~ updated context variable ${doc.name}`);
        } else {
          await send(ctx, 'CreateContextVariableCommand', doc);
          ctx.log(`  + created context variable ${doc.name}`);
        }
        recordResource(ctx.state, 'context-variable', doc.name);
      }
    }
  },
};

const upsertKnowledgeBases = {
  plan(ctx, params) {
    const lines = [];
    for (const f of listFiles(ctx, params)) {
      const doc = readJson(f);
      lines.push(`upsert knowledge base '${doc.name}' (${(doc.articles || []).length} articles) + publish`);
    }
    return lines;
  },
  async run(ctx, params) {
    for (const file of listFiles(ctx, params)) {
      const doc = readJson(file);
      const { articles = [], ...kbPayload } = doc;
      const existing = await listAll(ctx, 'ListKnowledgeBasesCommand');
      const match = existing.find((k) => k.name === kbPayload.name);
      let kbId;
      if (match) {
        kbId = match.knowledgeBaseId;
        await send(ctx, 'UpdateKnowledgeBaseCommand', { knowledgeBaseId: kbId, ...kbPayload });
        ctx.log(`  ~ updated knowledge base ${kbPayload.name}`);
      } else {
        const created = await send(ctx, 'CreateKnowledgeBaseCommand', kbPayload);
        kbId = created.knowledgeBaseId;
        ctx.log(`  + created knowledge base ${kbPayload.name}`);
      }
      ctx.state.knowledgeBases[kbPayload.name] = { knowledgeBaseId: kbId };
      recordResource(ctx.state, 'knowledge-base', kbId, { name: kbPayload.name });

      const existingArticles = await listAll(ctx, 'ListKnowledgeBaseArticlesCommand', { knowledgeBaseId: kbId });
      for (const article of articles) {
        const found = existingArticles.find(
          (a) => a.question && article.question && a.question.text === article.question.text);
        if (found) {
          await send(ctx, 'UpdateKnowledgeBaseArticleCommand',
            { knowledgeBaseId: kbId, articleId: found.articleId, ...article });
        } else {
          await send(ctx, 'CreateKnowledgeBaseArticleCommand', { knowledgeBaseId: kbId, ...article });
        }
      }
      ctx.log(`  = ${articles.length} article(s) synced`);

      const publication = await send(ctx, 'PublishKnowledgeBaseCommand',
        { knowledgeBaseId: kbId, description: 'AICC Builder deploy' });
      const status = await poll(async () => {
        const p = await send(ctx, 'GetKnowledgeBasePublicationCommand',
          { knowledgeBaseId: kbId, deploymentId: publication.deploymentId });
        if (p.status === 'published') return p;
        if (p.status === 'failed') throw new Error(`knowledge base publish failed for '${kbPayload.name}'`);
        return null;
      }, { sleep: ctx.sleep });
      ctx.log(`  = published (deployment ${status.deploymentId})`);
    }
  },
};

const upsertGuardrails = {
  plan(ctx, params) {
    return listFiles(ctx, params).map((f) => `upsert guardrail from ${path.relative(ctx.bundleDir, f)} (+ smoke tests if present)`);
  },
  async run(ctx, params) {
    for (const file of listFiles(ctx, params)) {
      const doc = readJson(file);
      const { smokeTests = [], ...payload } = doc;
      const existing = await listAll(ctx, 'ListGuardrailsCommand');
      const match = existing.find((g) => g.name === payload.name);
      let guardrailId;
      if (match) {
        guardrailId = match.guardrailId;
        await send(ctx, 'UpdateGuardrailCommand', { guardrailIdentifier: guardrailId, ...payload });
        ctx.log(`  ~ updated guardrail ${payload.name}`);
      } else {
        const created = await send(ctx, 'CreateGuardrailCommand', payload);
        guardrailId = created.guardrailId;
        ctx.log(`  + created guardrail ${payload.name}`);
      }
      ctx.state.guardrails[payload.name] = { guardrailId };
      recordResource(ctx.state, 'guardrail', guardrailId, { name: payload.name });

      for (const test of smokeTests) {
        const result = await send(ctx, 'TestGuardrailCommand',
          { guardrailIdentifier: guardrailId, input: test.input });
        const triggered = (result.violations || []).length > 0;
        const ok = test.expectTriggered === undefined || test.expectTriggered === triggered;
        ctx.log(`  ${ok ? '✓' : '✗'} smoke: ${JSON.stringify(test.input)} → ${triggered ? 'triggered' : 'passed through'}`);
        if (!ok) {
          throw new Error(`guardrail smoke test failed for '${payload.name}': ` +
            `input ${JSON.stringify(test.input)} expected triggered=${test.expectTriggered}, got ${triggered}`);
        }
      }
    }
  },
};

// ---------------------------------------------------------------------------
// Application compose / build / deploy
// ---------------------------------------------------------------------------

const composeApplication = {
  plan(ctx, params) {
    return [`compose application from ${params.file} (resolve {KB:*}/{GUARDRAIL:*} placeholders, attach flows)`];
  },
  async run(ctx, params) {
    const doc = resolveAssetPlaceholders(readJson(path.join(ctx.bundleDir, params.file)), ctx);
    const existing = await listAll(ctx, 'ListApplicationsCommand');
    const match = existing.find((a) => a.name === doc.name);
    let appId;
    if (match) {
      appId = match.applicationId;
      await send(ctx, 'UpdateApplicationCommand', { applicationIdentifier: appId, ...doc });
      ctx.log(`  ~ updated application ${doc.name}`);
    } else {
      const created = await send(ctx, 'CreateApplicationCommand', doc);
      appId = created.applicationId;
      ctx.log(`  + created application ${doc.name}`);
    }
    ctx.state.applicationId = appId;
    ctx.state.applicationName = doc.name;
    // Remembered for deploy-application: a deployment requires at least one
    // language code (live: UpdateApplicationDeployment refused without them).
    ctx.state.applicationLanguageCodes = applicationLanguageCodes(doc);
    recordResource(ctx.state, 'application', appId, { name: doc.name });
  },
};

/** Language codes an application document declares, in every shape the builder emits. */
function applicationLanguageCodes(doc) {
  const s = (doc && doc.settings) || {};
  const list = [
    ...(Array.isArray(s.languageCodes) ? s.languageCodes : []),
    ...(Array.isArray(s.languageSettings) ? s.languageSettings.map((x) => x && x.languageCode) : []),
    s.languageCode,
    ...(Array.isArray(doc && doc.languageCodes) ? doc.languageCodes : []),
    doc && doc.mainLanguageCode,
  ].filter(Boolean);
  return [...new Set(list)];
}

const buildApplication = {
  plan(ctx, params) {
    return [`create application build (poll until BUILT; FAILED aborts with details)`];
  },
  async run(ctx, params = {}) {
    const appId = ctx.state.applicationId;
    if (!appId) throw new Error('build-application requires compose-application to run first');
    const build = await send(ctx, 'CreateApplicationBuildCommand', {
      applicationIdentifier: appId,
      version: params.version,
      description: params.description || 'AICC Builder deploy',
    });
    ctx.log(`  = build ${build.buildId} started`);
    const result = await poll(async () => {
      const b = await send(ctx, 'GetApplicationBuildCommand',
        { applicationIdentifier: appId, buildIdentifier: build.buildId });
      if (b.status === 'BUILT') return b;
      if (b.status === 'FAILED') {
        // Server-side validation is the second half of the double safety
        // net — surface everything the API returns.
        throw new Error('application build FAILED (server-side validation):\n' +
          JSON.stringify(b, null, 2));
      }
      return null;
    }, { sleep: ctx.sleep });
    ctx.state.buildId = result.buildId;
    recordResource(ctx.state, 'build', result.buildId);
    ctx.log(`  = build ${result.buildId} BUILT`);
  },
};

const deployApplication = {
  plan(ctx, params = {}) {
    return [`deploy build to environment '${params.environment || 'development'}' (poll until deployed)`];
  },
  async run(ctx, params = {}) {
    const appId = ctx.state.applicationId;
    const buildId = ctx.state.buildId;
    if (!appId || !buildId) {
      throw new Error('deploy-application requires compose-application and build-application first');
    }
    const environment = params.environment || 'development';

    // A deployment requires at least one language code. The manifest may pin
    // them; otherwise use the application's own languages (recorded at compose
    // time, or read back for a state file written before that was recorded).
    let languageCodes = Array.isArray(params.languageCodes) && params.languageCodes.length
      ? params.languageCodes
      : (ctx.state.applicationLanguageCodes || []);
    if (!languageCodes.length) {
      const app = await send(ctx, 'GetApplicationCommand', { applicationIdentifier: appId });
      languageCodes = applicationLanguageCodes(app);
    }
    if (!languageCodes.length) {
      ctx.log('  ! application declares no language code; deploying without languageCodes');
    }
    const langs = languageCodes.length ? { languageCodes } : {};

    // An environment holds ONE deployment record (a second CreateApplicationDeployment
    // for the same environment is refused: LimitExceededException). Promoting a new
    // build is an UPDATE of that record; live (2026-09-10) the service answered
    // UpdateApplicationDeployment with InternalServerException "Failed to update
    // deployment." for every payload shape, so the fallback is delete + create —
    // a brief gap on the development environment, not a failed deploy.
    const existing = await listAll(ctx, 'ListApplicationDeploymentsCommand',
      { applicationIdentifier: appId });
    const current = (existing || []).find((d) => d.environment === environment);

    let deploymentId;
    if (current) {
      try {
        await send(ctx, 'UpdateApplicationDeploymentCommand', {
          applicationIdentifier: appId,
          deploymentIdentifier: current.deploymentId,
          buildIdentifier: buildId,
          environment,
          ...langs,
          description: 'AICC Builder deploy',
        });
        deploymentId = current.deploymentId;
        ctx.log(`  ~ promoted build on existing '${environment}' deployment`);
      } catch (err) {
        ctx.log(`  ! update of '${environment}' deployment refused (${err.name}: ${err.message}); replacing it`);
        await send(ctx, 'DeleteApplicationDeploymentCommand',
          { applicationIdentifier: appId, deploymentIdentifier: current.deploymentId });
        const created = await send(ctx, 'CreateApplicationDeploymentCommand', {
          applicationIdentifier: appId,
          buildIdentifier: buildId,
          environment,
          ...langs,
          description: 'AICC Builder deploy',
        });
        deploymentId = created.deploymentId;
        ctx.log(`  + replaced '${environment}' deployment`);
      }
    } else {
      const created = await send(ctx, 'CreateApplicationDeploymentCommand', {
        applicationIdentifier: appId,
        buildIdentifier: buildId,
        environment,
        ...langs,
        description: 'AICC Builder deploy',
      });
      deploymentId = created.deploymentId;
      ctx.log(`  + created '${environment}' deployment`);
    }

    const result = await poll(async () => {
      const d = await send(ctx, 'GetApplicationDeploymentCommand',
        { applicationIdentifier: appId, deploymentIdentifier: deploymentId });
      const status = String(d.deploymentStatus || d.status || '').toLowerCase();
      // Treat 'failed' as terminal: otherwise a real failure is reported as a
      // timeout and the actual reason is lost.
      if (status === 'failed') {
        throw new Error(
          `deployment ${deploymentId} FAILED` +
          (d.statusReason ? `: ${d.statusReason}` : '') +
          '. A deployment will accept a failed build and then sit scheduled ' +
          'indefinitely — confirm the build reached BUILT first.');
      }
      return status === 'deployed' ? d : null;
    }, { sleep: ctx.sleep });

    ctx.state.deploymentId = deploymentId;
    recordResource(ctx.state, 'deployment', deploymentId, { environment });
    ctx.log(`  = deployment ${deploymentId} live (${environment})`);
  },
};

// ---------------------------------------------------------------------------
// Connect contact flow import (Classic-style aws cli; D7/D11)
// ---------------------------------------------------------------------------

function contactFlowName(doc, _file, project) {
  return doc.name || doc.flowName || (doc.Metadata && doc.Metadata.name)
    || `${project}-flow`;
}

function existingContactFlowId(output, name) {
  try {
    const listed = JSON.parse(output || '{}').ContactFlowSummaryList || [];
    const match = listed.find((flow) => flow.Name === name);
    return match && match.Id;
  } catch (_) {
    return undefined;
  }
}

const importContactFlows = {
  plan(ctx, params) {
    return listFiles(ctx, params).map((f) =>
      `import or update contact flow ${path.relative(ctx.bundleDir, f)} into Connect ` +
      '(requires CONNECT_INSTANCE_ID; Agentic CX block wired manually per WIRING-GUIDE.md)');
  },
  async run(ctx, params) {
    const instanceId = ctx.env.CONNECT_INSTANCE_ID;
    if (!instanceId) {
      ctx.log('  ! CONNECT_INSTANCE_ID not set — skipping contact flow import.');
      ctx.log('    Import manually and wire the Agentic CX block per WIRING-GUIDE.md.');
      return;
    }
    for (const file of listFiles(ctx, params)) {
      const doc = readJson(file);
      const name = contactFlowName(doc, file, ctx.project);
      const content = bindAgenticCx(doc.content || doc, ctx);
      try {
        const existing = ctx.exec('aws', [
          'connect', 'list-contact-flows',
          '--instance-id', instanceId,
          '--region', ctx.region,
          '--output', 'json',
        ]);
        const contactFlowId = existingContactFlowId(existing, name);
        if (contactFlowId) {
          ctx.exec('aws', [
            'connect', 'update-contact-flow-content',
            '--instance-id', instanceId,
            '--contact-flow-id', contactFlowId,
            '--content', JSON.stringify(content),
            '--region', ctx.region,
          ]);
          ctx.log(`  ~ updated existing contact flow ${name}`);
          recordResource(ctx.state, 'contact-flow', contactFlowId, { name });
          continue;
        }

        const created = ctx.exec('aws', [
          'connect', 'create-contact-flow',
          '--instance-id', instanceId,
          '--name', name,
          '--type', doc.type || 'CONTACT_FLOW',
          '--status', 'PUBLISHED',
          '--content', JSON.stringify(content),
          '--region', ctx.region,
          '--output', 'json',
          '--cli-error-format', 'json',
        ]);
        let createdId;
        try { createdId = JSON.parse(created || '{}').ContactFlowId; } catch (_) { /* best effort */ }
        ctx.log(`  + imported contact flow ${name}`);
        recordResource(ctx.state, 'contact-flow', createdId || name, { name });
      } catch (e) {
        ctx.log(`  ! contact flow ${name} import failed (${e.message.split('\n')[0]}).`);
        // The CLI's default error text hides the linter output ("problems:
        // <complex value>"); with --cli-error-format json the problems are in
        // the message body — surface them, they are the whole diagnosis.
        for (const problem of contactFlowProblems(e)) ctx.log(`    - ${problem}`);
        ctx.log('    Import it manually and wire the Agentic CX block per WIRING-GUIDE.md.');
      }
    }
  },
};

/**
 * Fill the Agentic CX block (Type ConnectParticipantWithAgenticCX) with the ids
 * this deploy produced. The bundle carries {ACXD_WORKSPACE_ID} /
 * {ACXD_APPLICATION_ID} / {ACXD_ALIAS_ID}; workspace and application are known
 * here, the alias is an opaque ACXD id the SDK does not expose, so it comes from
 * ACXD_ALIAS_ID or stays a visible placeholder (Connect accepts the import; the
 * operator picks the alias in the block's dropdown).
 */
function bindAgenticCx(content, ctx) {
  const env = ctx.env || process.env;
  const values = {
    '{ACXD_WORKSPACE_ID}': env.ACXD_WORKSPACE_ID || ctx.state.workspaceId || '',
    '{ACXD_APPLICATION_ID}': ctx.state.applicationId || '',
    '{ACXD_ALIAS_ID}': env.ACXD_ALIAS_ID || 'SELECT_ALIAS_IN_CONSOLE',
  };
  let text = JSON.stringify(content);
  for (const [placeholder, value] of Object.entries(values)) {
    if (value) text = text.split(placeholder).join(value);
  }
  const bound = JSON.parse(text);
  const hasBlock = (bound.Actions || []).some((a) => a && a.Type === 'ConnectParticipantWithAgenticCX');
  if (hasBlock && !env.ACXD_ALIAS_ID) {
    ctx.log('  ! ACXD_ALIAS_ID not set — the Agentic CX block is imported with alias SELECT_ALIAS_IN_CONSOLE; pick the alias in the block (see WIRING-GUIDE.md).');
  }
  return bound;
}

/** Extract Connect's per-action problem messages from a failed aws-cli call. */
function contactFlowProblems(err) {
  const text = String((err && (err.stderr || err.message)) || '');
  const start = text.indexOf('{');
  if (start < 0) return [];
  try {
    const body = JSON.parse(text.slice(start, text.lastIndexOf('}') + 1));
    const problems = body.problems || body.Problems || [];
    return problems.map((p) => (typeof p === 'string' ? p : (p.message || JSON.stringify(p))));
  } catch (_) {
    return [];
  }
}

// ---------------------------------------------------------------------------

const STEPS = {
  'deploy-cfn-backend': deployCfnBackend,
  'wire-webhook-urls': wireWebhookUrls,
  'upsert-secrets': upsertSecrets,
  'upsert-slot-types': upsertSlotTypes,
  'upsert-context-variables': upsertContextVariables,
  'upsert-data-requests': upsertDataRequests,
  'upsert-flows': upsertFlows,
  'upsert-knowledge-bases': upsertKnowledgeBases,
  'upsert-guardrails': upsertGuardrails,
  'compose-application': composeApplication,
  'build-application': buildApplication,
  'deploy-application': deployApplication,
  'import-contact-flows': importContactFlows,
};

module.exports = { STEPS, listAll, send, normalizeFlowForService, applicationLanguageCodes, bindAgenticCx };
