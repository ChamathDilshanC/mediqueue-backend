'use strict';
const $ = (selector) => document.querySelector(selector);
let contract, operations = [], language = 'curl';
const origin = window.location.origin;
const registration = {email: 'you@example.com', password: 'a-strong-password', display_name: 'Your name'};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.remove('hidden');
  window.setTimeout(() => node.classList.add('hidden'), 2400);
}
async function copy(value) {
  try { await navigator.clipboard.writeText(value); toast('Copied to clipboard'); }
  catch { toast('Copy unavailable. Select the example text to copy it.'); }
}
function registrationExample() {
  if (language === 'javascript') return `const response = await fetch('${origin}/v1/auth/register', {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(${JSON.stringify(registration, null, 2).replaceAll('\n', '\n  ')})\n});\nconst result = await response.json();\nif (!response.ok) throw new Error(JSON.stringify(result));`;
  if (language === 'python') return `import httpx\n\nresponse = httpx.post(\n    '${origin}/v1/auth/register',\n    json=${JSON.stringify(registration, null, 4).replaceAll('\n', '\n    ')}\n)\nresponse.raise_for_status()\nprint(response.json())`;
  return `curl -X POST '${origin}/v1/auth/register' \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(registration, null, 2)}'`;
}
function resolve(schema = {}) {
  if (schema.$ref) return contract.components?.schemas?.[schema.$ref.split('/').pop()] || {};
  return schema;
}
function example(schema, name = '', depth = 0) {
  schema = resolve(schema);
  if (depth > 4) return {};
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined && schema.default !== null) return schema.default;
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf) return example(schema.anyOf.find(s => s.type !== 'null') || {}, name, depth + 1);
  if (schema.type === 'object' || schema.properties) return Object.fromEntries(Object.entries(schema.properties || {}).map(([key, value]) => [key, example(value, key, depth + 1)]));
  if (schema.type === 'array') return [example(schema.items, name, depth + 1)];
  if (schema.type === 'integer' || schema.type === 'number') return schema.minimum || 1;
  if (schema.type === 'boolean') return true;
  if (schema.format === 'uuid') return '00000000-0000-0000-0000-000000000001';
  if (schema.format === 'date-time') return name === 'ends_at' ? '2030-01-15T12:00:00+05:30' : '2030-01-15T09:00:00+05:30';
  if (name === 'email') return 'you@example.com';
  if (name === 'password') return 'a-strong-password';
  if (name === 'timezone') return 'Asia/Colombo';
  if (name === 'name') return 'Central Hospital';
  if (name === 'patient_ref' || name === 'external_ref') return 'PAT-001';
  return `<${name || 'value'}>`;
}
function schemaType(schema) {
  schema = resolve(schema);
  if (schema.anyOf) return schema.anyOf.map(schemaType).join(' | ');
  return schema.enum ? schema.enum.join(' · ') : schema.format || schema.type || 'object';
}
function requestExample(op) {
  const path = op.path.replace(/\{([^}]+)\}/g, (_, key) => key === 'action' ? 'start' : '00000000-0000-0000-0000-000000000001');
  let result = `curl -X ${op.method} '${origin}${path}'`;
  if (op.security?.length) result += ` \\\n  -H 'Authorization: Bearer <access_token>'`;
  for (const parameter of op.parameters || []) {
    if (parameter.in === 'header' && (parameter.required || ['x-tenant-id', 'x-branch-id'].includes(parameter.name.toLowerCase()))) {
      result += ` \\\n  -H '${parameter.name}: ${parameter.name.toLowerCase() === 'idempotency-key' ? 'unique-request-001' : '<' + parameter.name.toLowerCase() + '>'}'`;
    }
  }
  const schema = op.requestBody?.content?.['application/json']?.schema;
  if (schema) result += ` \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(example(schema), null, 2)}'`;
  return result;
}
function codePanel(value, title = 'EXAMPLE REQUEST · cURL') {
  const panel = element('div', 'code-panel');
  const toolbar = element('div', 'code-toolbar');
  toolbar.append(element('span', 'code-title', title));
  const button = element('button', 'copy', 'Copy ⧉');
  button.type = 'button'; button.addEventListener('click', () => copy(value));
  toolbar.append(button);
  const pre = element('pre'); pre.append(element('code', '', value));
  panel.append(toolbar, pre); return panel;
}
function fieldsTable(fields) {
  const table = element('table', 'schema-table');
  const head = element('thead'), header = element('tr');
  for (const label of ['Field', 'Type', 'Location']) header.append(element('th', '', label));
  head.append(header); table.append(head);
  const body = element('tbody');
  for (const field of fields) {
    const tr = element('tr'), name = element('td');
    name.append(element('code', '', field.name));
    if (field.required) name.append(element('span', 'required', 'required'));
    tr.append(name, element('td', '', schemaType(field.schema)), element('td', '', field.in));
    body.append(tr);
  }
  table.append(body); return table;
}
function endpoint(op) {
  const details = element('details', 'endpoint');
  const summary = element('summary');
  summary.append(element('span', `method ${op.method.toLowerCase()}`, op.method), element('code', 'endpoint-path', op.path), element('span', 'auth-pill', op.security?.length ? 'AUTH' : 'PUBLIC'), element('span', 'endpoint-arrow', '›'));
  details.append(summary);
  details.addEventListener('toggle', () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = 'true';
    const body = element('div', 'endpoint-body');
    body.append(element('h4', '', op.summary || op.operationId));
    if (op.description) body.append(element('p', '', op.description));
    const requestSchema = resolve(op.requestBody?.content?.['application/json']?.schema);
    const fields = [...(op.parameters || []).filter(p => !p.name.startsWith('x-dev-'))];
    for (const [name, schema] of Object.entries(requestSchema.properties || {})) fields.push({name, schema, in:'body', required:requestSchema.required?.includes(name)});
    if (fields.length) body.append(fieldsTable(fields));
    body.append(codePanel(requestExample(op)));
    const success = Object.entries(op.responses || {}).find(([code, response]) => code.startsWith('2') && response.content?.['application/json']?.schema);
    if (success) {
      const responseSchema = success[1].content['application/json'].schema;
      body.append(codePanel(JSON.stringify(example(responseSchema), null, 2), `RESPONSE SHAPE · ${success[0]} · EXAMPLE VALUES`));
    }
    body.append(element('p', '', `Responses: ${Object.entries(op.responses || {}).map(([code, value]) => `${code} ${value.description}`).join(' · ')}`));
    const link = element('a', 'endpoint-link', 'Try in API playground ↗');
    link.href = `/swagger#/${encodeURIComponent(op.tags?.[0] || 'default')}/${encodeURIComponent(op.operationId)}`;
    body.append(link); details.append(body);
  });
  return details;
}
const slug = value => value.toLowerCase().replace(/[^a-z0-9]+/g, '-');
function render() {
  const query = $('#search').value.trim().toLowerCase();
  const matching = operations.filter(op => `${op.method} ${op.path} ${op.summary} ${op.tags?.join(' ')}`.toLowerCase().includes(query));
  const groups = Map.groupBy(matching, op => op.tags?.[0] || 'Other');
  const container = $('#endpoints'); container.replaceChildren();
  for (const [name, items] of groups) {
    const section = element('section', 'resource-section'); section.id = `resource-${slug(name)}`;
    const title = element('div', 'resource-title'); title.append(element('h3', '', name), element('span', '', `${items.length} endpoints`));
    section.append(title, ...items.map(endpoint)); container.append(section);
  }
  $('#results-count').textContent = `${matching.length} of ${operations.length} endpoints`;
  $('#empty').classList.toggle('hidden', matching.length !== 0);
  for (const link of document.querySelectorAll('#resource-nav a')) link.classList.toggle('hidden', !groups.has(link.dataset.group));
}
async function load() {
  $('#load-error').classList.add('hidden');
  try {
    const response = await fetch('/openapi.json');
    if (!response.ok) throw new Error('Contract unavailable');
    contract = await response.json();
    operations = Object.entries(contract.paths).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ['get', 'post', 'put', 'patch', 'delete'].includes(method)).map(([method, operation]) => ({...operation, method: method.toUpperCase(), path})));
    $('#version').textContent = `v${contract.info.version}`;
    $('#endpoint-count').textContent = operations.length;
    const nav = $('#resource-nav'); nav.replaceChildren();
    for (const [name, items] of Map.groupBy(operations, op => op.tags?.[0] || 'Other')) {
      const link = element('a', 'nav-link', name); link.href = `#resource-${slug(name)}`; link.dataset.group = name;
      link.append(element('span', '', items.length)); nav.append(link);
    }
    render();
  } catch {
    $('#results-count').textContent = 'Reference unavailable';
    $('#load-error').classList.remove('hidden');
  }
}
document.querySelectorAll('[data-language]').forEach(button => button.addEventListener('click', () => {
  language = button.dataset.language;
  document.querySelectorAll('[data-language]').forEach(tab => {tab.classList.toggle('selected', tab === button);tab.setAttribute('aria-selected', String(tab === button));});
  $('#register-example').textContent = registrationExample();
}));
$('#copy-example').addEventListener('click', () => copy(registrationExample()));
$('#search').addEventListener('input', () => {render();if ($('#search').value) $('#reference').scrollIntoView({behavior:'auto'});});
$('#retry').addEventListener('click', load);
$('#menu').addEventListener('click', () => {const open = $('#sidebar').classList.toggle('open');$('#menu').setAttribute('aria-expanded', String(open));});
$('#sidebar').addEventListener('click', event => {if (event.target.closest('a')) {$('#sidebar').classList.remove('open');$('#menu').setAttribute('aria-expanded', 'false');}});
document.addEventListener('keydown', event => {if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {event.preventDefault();$('#sidebar').classList.add('open');$('#search').focus();}if (event.key === 'Escape') {$('#sidebar').classList.remove('open');$('#menu').setAttribute('aria-expanded', 'false');$('#search').blur();}});
const observer = new IntersectionObserver(entries => {for (const entry of entries) if (entry.isIntersecting) document.querySelectorAll('.toc nav a').forEach(link => link.classList.toggle('current', link.hash === `#${entry.target.id}`));}, {rootMargin:'-100px 0px -60% 0px'});
['overview', 'quickstart', 'authentication', 'reference'].forEach(id => observer.observe(document.getElementById(id)));
$('#register-example').textContent = registrationExample();
load();
